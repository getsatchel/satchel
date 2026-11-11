"""Automatic source syncs: on a schedule (the worker) and on a push webhook (the API).

- **Schedule.** A source with ``sync_interval_hours`` is due when its ``last_synced_at`` is older
  than that (or it never synced). The worker checks every ``SATCHEL_SOURCE_TICK_MIN`` minutes
  (:func:`sync_due_sources`), on its own thread, so curation keeps its nightly cadence.
- **Webhook.** ``POST /v1/hooks/sources/{id}`` (``satchel.api.hooks``) verifies the delivery and
  calls :func:`run_sync` after responding.
- **Unchanged commit.** Before an automatic sync of a git source, the ref is resolved
  (``git ls-remote``). When it points at the commit of the last successful sync and the config
  and adapter version are unchanged (``input_key``), nothing is checked out or applied; the check
  is stored in ``last_report["checked"]`` and ``last_synced_at`` is set, because the source is
  known to be current as of then (so the schedule waits a full interval again, rather than
  re-running ``ls-remote`` on every tick). Manual and CLI syncs always run.
- **Retry after a failure.** A failed sync is retried sooner than the interval: 15 minutes after
  the first failure, then 30, 60, ... doubling with each consecutive failure
  (``last_report["failures"]``), never later than the interval itself. A success resets it.
- **One at a time.** Every sync path (REST, CLI, schedule, webhook) takes a session-level
  Postgres advisory lock keyed on the source id, so replicas never sync a source twice at once.
  The schedule re-checks that the source is still due after taking the lock.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Engine, select, text

from satchel import audit
from satchel.llm.embeddings import EmbeddingProvider, make_embedder
from satchel.settings import Settings, get_settings
from satchel.sources import git
from satchel.sources.apply import SourceSyncError
from satchel.sources.config import WEBHOOK_KEY
from satchel.sources.service import input_key, plain_config, record_failure, sync_in_session
from satchel.store.db import admin_session, get_engine
from satchel.store.models import ContextSource

log = logging.getLogger("satchel.sources")

Trigger = Literal["schedule", "webhook"]
Outcome = Literal["synced", "unchanged", "failed", "busy", "not_due", "gone"]


def _lock_key(source_id: uuid.UUID) -> str:
    return f"satchel-source-sync:{source_id}"


@contextmanager
def sync_lock(engine: Engine | None, source_id: uuid.UUID, *,
              wait_s: float = 0.0, poll_s: float = 2.0) -> Iterator[bool]:
    """Session-level advisory lock per source; yields False when another sync holds it (after
    polling for up to ``wait_s`` seconds). Each attempt borrows a pool connection and gives it
    back on failure, so waiters never pin connections; only the holder keeps one."""
    eng = engine or get_engine()
    deadline = time.monotonic() + wait_s
    while True:
        conn = eng.connect()
        try:
            got = bool(conn.execute(
                text("select pg_try_advisory_lock(hashtextextended(:k, 0))"),
                {"k": _lock_key(source_id)}).scalar())
            conn.commit()
        except BaseException:
            conn.close()
            raise
        if got:
            break
        conn.close()
        if time.monotonic() >= deadline:
            yield False
            return
        time.sleep(poll_s)
    try:
        yield True
    finally:
        try:
            conn.execute(text("select pg_advisory_unlock(hashtextextended(:k, 0))"),
                         {"k": _lock_key(source_id)})
            conn.commit()
        finally:
            conn.close()


def webhook_url(src: ContextSource, public_url: str) -> str | None:
    """Where the source's push webhook is delivered; None when no webhook is set up."""
    if not (src.config or {}).get(WEBHOOK_KEY):
        return None
    return f"{public_url.rstrip('/')}/v1/hooks/sources/{src.id}"


def interval_of(src: ContextSource) -> int | None:
    v = (src.config or {}).get("sync_interval_hours")
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


RETRY_BASE = timedelta(minutes=15)


def failures_of(src: ContextSource) -> int:
    if src.last_status != "error":
        return 0
    try:
        return max(1, int((src.last_report or {}).get("failures") or 1))
    except (TypeError, ValueError):
        return 1


def next_sync_at(src: ContextSource) -> datetime | None:
    """When the schedule next syncs this source (None: no schedule). In the past means due."""
    hours = interval_of(src)
    if not hours:
        return None
    if src.last_synced_at is None:
        return src.created_at
    wait = timedelta(hours=hours)
    n = failures_of(src)
    if n:
        wait = min(wait, RETRY_BASE * 2 ** min(n - 1, 16))
    return src.last_synced_at + wait


def is_due(src: ContextSource, now: datetime) -> bool:
    nxt = next_sync_at(src)
    return nxt is not None and (src.last_synced_at is None or now >= nxt)


def unchanged(src: ContextSource, commit: str) -> bool:
    """The last sync succeeded at this commit, from the stored locations, with this config."""
    rep = src.last_report or {}
    return (src.last_status == "ok" and rep.get("git_commit") == commit
            and rep.get("input_key") is not None and rep.get("input_key") == input_key(src))


@dataclass
class AutoResult:
    source_id: uuid.UUID
    outcome: Outcome
    source: str | None = None
    commit: str | None = None
    error: str | None = None
    counts: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        what = {"synced": ", ".join(f"{k} {v}" for k, v in self.counts.items()) or "synced",
                "unchanged": f"no new commit ({(self.commit or '')[:10]})",
                "failed": f"FAILED: {self.error}"}.get(self.outcome, self.outcome)
        return f"source {self.source or self.source_id}: {what}"


def run_sync(
    source_id: uuid.UUID, *, trigger: Trigger, engine: Engine | None = None,
    embedder: EmbeddingProvider | None = None, settings: Settings | None = None,
    detail: dict[str, Any] | None = None, only_if_due: bool = False,
    wait_s: float = 0.0, now: datetime | None = None,
) -> AutoResult:
    """One automatic sync under the source's lock. Never raises for a sync failure: it is
    recorded on the source (``last_status``/``last_error``) and returned."""
    st = settings or get_settings()
    with sync_lock(engine, source_id, wait_s=wait_s) as got:
        if not got:
            return AutoResult(source_id, "busy")
        return _run_locked(source_id, trigger, engine, embedder, st, detail or {}, only_if_due,
                           now)


def _run_locked(
    source_id: uuid.UUID, trigger: Trigger, engine: Engine | None,
    embedder: EmbeddingProvider | None, st: Settings, detail: dict[str, Any],
    only_if_due: bool, now: datetime | None,
) -> AutoResult:
    error: str
    with admin_session(engine=engine) as s:
        src = s.get(ContextSource, source_id)
        if src is None:
            return AutoResult(source_id, "gone")
        name, ws_id = src.name, src.workspace_id
        when = now or datetime.now(UTC)
        if only_if_due and not is_due(src, when):
            return AutoResult(source_id, "not_due", name)  # another replica just synced it
        commit: str | None = None
        try:
            spec = git.spec_from_config(plain_config(src, st))
            if spec is not None:
                commit = git.resolve(spec)
                if unchanged(src, commit):
                    src.last_synced_at = when
                    src.last_report = {**(src.last_report or {}), "checked": {
                        "at": when.isoformat(), "trigger": trigger, "commit": commit}}
                    return AutoResult(source_id, "unchanged", name, commit)
            with s.begin_nested():
                report = sync_in_session(s, src, embedder=embedder or make_embedder(st),
                                         settings=st, trigger=trigger)
        except (SourceSyncError, git.GitSourceError) as e:
            error = str(e)
        else:
            counts = report.counts()
            audit.record(s, ws_id, None, "source.sync",
                         {"source": name, "trigger": trigger, **counts,
                          **({"git_commit": report.git_commit} if report.git_commit else {}),
                          **detail})
            return AutoResult(source_id, "synced", name, report.git_commit, counts=counts)
    record_failure(engine, source_id, error)
    with admin_session(engine=engine) as s:
        audit.record(s, ws_id, None, "source.sync",
                     {"source": name, "trigger": trigger, "status": "error",
                      "error": error[:500], **detail})
    return AutoResult(source_id, "failed", name, commit, error)


def sync_due_sources(
    *, engine: Engine | None = None, embedder: EmbeddingProvider | None = None,
    settings: Settings | None = None, workspace_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> list[AutoResult]:
    """Sync every scheduled source that is due, one after another. A source another process is
    syncing is skipped (it is current once that finishes)."""
    when = now or datetime.now(UTC)
    with admin_session(engine=engine) as s:
        q = select(ContextSource).where(ContextSource.config.has_key("sync_interval_hours"))
        if workspace_id is not None:
            q = q.where(ContextSource.workspace_id == workspace_id)
        due = [src.id for src in s.scalars(q.order_by(ContextSource.last_synced_at.nulls_first()))
               if is_due(src, when)]
    out: list[AutoResult] = []
    for sid in due:
        try:
            r = run_sync(sid, trigger="schedule", engine=engine, embedder=embedder,
                         settings=settings, only_if_due=True, now=now)
        except Exception as e:  # noqa: BLE001 - one broken source never stops the others
            log.exception("scheduled sync of source %s failed", sid)
            r = AutoResult(sid, "failed", error=f"{type(e).__name__}: {e}")
        if r.outcome in ("synced", "unchanged", "failed"):
            (log.warning if r.outcome == "failed" else log.info)(r.line())
        out.append(r)
    return out
