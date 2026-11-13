"""The ``source.sync`` job: sync one source (``satchel.sources.automation.run_sync``).

Queued by "Sync now" in the UI (``POST /v1/sources/{id}/sync`` without files), by push webhooks
and by the worker's schedule tick. At most one sync of a source waits in the queue, plus one
that runs: a push while a sync runs queues one more, so the newest commit is never missed. A
source another sync holds (a CI upload syncing in a request) is retried after the backoff. A
sync that fails is recorded on the source (``last_status``, ``last_error``) and fails the job at
once with the same error (no automatic retry: the schedule has its own backoff, and a person can
retry the job from the Jobs page).

Syncs that run in their caller's process (``satchel source sync`` and a sync with uploaded
files, such as a CI push) are not queued, but :func:`record_sync` adds each as a finished job
with the same result, so a source's sync history (and the Jobs page) lists every sync. Their
triggers are ``cli`` and ``upload``; they can't be retried, because their files are gone.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from satchel.jobs import queue
from satchel.jobs.queue import Claimed
from satchel.jobs.runner import Permanent, RetryLater, Runtime, register
from satchel.sources.apply import SyncReport
from satchel.sources.automation import Trigger, run_sync
from satchel.store.db import admin_session
from satchel.store.models import Job

log = logging.getLogger("satchel.sources")

KIND = "source.sync"
# Triggers of syncs that ran outside the queue (recorded afterwards); they can't be retried.
RECORDED = {"cli", "upload"}
MAX_ATTEMPTS = 6  # busy retries: 30 s, 2 min, 8 min, 32 min, 1 h


def enqueue_sync(
    s: Session, workspace_id: uuid.UUID, source_id: uuid.UUID, *, trigger: Trigger,
    detail: dict[str, Any] | None = None, actor: uuid.UUID | None = None,
    only_if_due: bool = False, now: datetime | None = None,
) -> tuple[int, bool]:
    """Queue a sync of the source. Returns ``(job id, queued)``; ``queued`` is False when a
    sync of the source was already waiting (that job's id is returned)."""
    payload = {"source_id": str(source_id), "trigger": trigger, "detail": detail or {},
               "actor": str(actor) if actor else None, "only_if_due": only_if_due,
               # The time the scheduler found it due; the job re-checks against it.
               "now": now.isoformat() if now else None}
    first, second = str(source_id), f"{source_id}:next"
    for _ in range(2):  # twice: the unfinished job can finish between the two queries
        jid = queue.enqueue(s, workspace_id, KIND, payload, dedupe_key=first,
                            max_attempts=MAX_ATTEMPTS)
        if jid is not None:
            return jid, True
        status = s.scalar(select(Job.status).where(
            Job.kind == KIND, Job.dedupe_key == first, Job.status.in_(["queued", "running"])))
        if status == "queued":  # it will sync the newest commit when it runs
            found = queue.unfinished(s, KIND, first)
            if found is not None:
                return found, False
        if status == "running":  # queue one more after it, at most one
            jid = queue.enqueue(s, workspace_id, KIND, payload, dedupe_key=second,
                                max_attempts=MAX_ATTEMPTS)
            if jid is not None:
                return jid, True
            found = queue.unfinished(s, KIND, second)
            if found is not None:
                return found, False
    raise RuntimeError(f"could not queue a sync of source {source_id}")


def queue_sync(engine: Engine, workspace_id: uuid.UUID, source_id: uuid.UUID, *,
               trigger: Trigger, detail: dict[str, Any] | None = None,
               actor: uuid.UUID | None = None, only_if_due: bool = False,
               now: datetime | None = None) -> tuple[int, bool]:
    with admin_session(engine=engine) as s:
        return enqueue_sync(s, workspace_id, source_id, trigger=trigger, detail=detail,
                            actor=actor, only_if_due=only_if_due, now=now)


def record_sync(
    engine: Engine | None, workspace_id: uuid.UUID, source_id: uuid.UUID, *, trigger: str,
    started_at: datetime, report: SyncReport | None = None, error: str | None = None,
    actor: uuid.UUID | None = None, source: str | None = None,
) -> None:
    """Record a sync that ran outside the queue as a finished ``source.sync`` job. Never raises:
    the sync itself is already committed, and losing its history row must not fail it."""
    payload: dict[str, Any] = {"source_id": str(source_id), "trigger": trigger, "detail": {},
               "actor": str(actor) if actor else None}
    result = {"outcome": "failed" if error else "synced", "source": source,
              "commit": report.git_commit if report else None, "error": error,
              "counts": report.counts() if report else None,
              "report": report.to_json() if report else None}
    try:
        with admin_session(engine=engine) as s:
            queue.record(s, workspace_id, KIND, payload, started_at=started_at, result=result,
                         error=error)
    except Exception:  # noqa: BLE001
        log.exception("recording the %s sync of source %s failed", trigger, source_id)


def _handle(rt: Runtime, job: Claimed) -> dict[str, Any]:
    p = job.payload
    trigger: Trigger = p.get("trigger") or "schedule"
    res = run_sync(
        uuid.UUID(p["source_id"]), trigger=trigger, engine=rt.engine, embedder=rt.embedder,
        settings=rt.settings, detail=p.get("detail") or {},
        only_if_due=bool(p.get("only_if_due")),
        actor=uuid.UUID(p["actor"]) if p.get("actor") else None, force=trigger == "manual",
        now=datetime.fromisoformat(p["now"]) if p.get("now") else None,
    )
    if res.outcome == "busy":
        raise RetryLater("another sync of this source is running")
    if res.outcome == "failed":  # recorded on the source; the job shows it too (and can retry)
        raise Permanent(res.error or "sync failed")
    if res.outcome == "gone":
        raise Permanent("the source was deleted")
    return {"outcome": res.outcome, "source": res.source, "commit": res.commit,
            "error": res.error, "counts": res.counts, "report": res.report}


register(KIND, _handle)
