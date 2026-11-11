"""Scheduled eval runs: a suite with ``schedule: {interval_hours: N}`` runs against the workspace's
latest published release every N hours, from ``satchel worker``.

- **Due.** A suite is due when its last scheduled run (of any version of the suite) started more
  than ``interval_hours`` ago, or it never ran on a schedule. Manual and gate runs do not count:
  the schedule is a steady time series, so a run someone started by hand never moves it.
- **Who it runs as.** Runs need a user (the reference agent's sessions belong to one). A scheduled
  run uses the user who saved the suite's latest version if they are still an analyst or above,
  else the workspace's oldest admin, curator or analyst. ``started_by`` stays null and
  ``summary.trigger`` is ``"schedule"``, so the UI shows the run as scheduled, not as theirs.
- **No LLM.** A suite that needs an agent is skipped (not run) when no model is configured, so a
  missing key does not fill the history with skipped runs. ``context_used``-only suites always run.
- **Comparison.** Each scheduled run records the previous completed scheduled run of the suite in
  ``summary.previous_run_id``; the run page links the comparison.
- **One at a time.** The due check and the insert of the ``running`` row happen under a
  transaction-level advisory lock per (workspace, suite), so replicas never start the same
  scheduled run twice. Cases then run outside the lock.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import Engine, Select, func, select, text
from sqlalchemy.orm import Session

from satchel import audit
from satchel.curation.releases import latest_published
from satchel.evals.providers import resolve_provider
from satchel.evals.runner import EvalProvider, create_run, execute_run
from satchel.evals.store import latest_suites, suite_of
from satchel.llm.embeddings import EmbeddingProvider, make_embedder
from satchel.mcp.auth import Principal
from satchel.settings import Settings, get_settings
from satchel.store.db import admin_session, get_engine
from satchel.store.models import AppUser, EvalRun, EvalSuite, Workspace

log = logging.getLogger("satchel.evals")

TRIGGER = "schedule"
RUN_ROLES = ("admin", "curator", "analyst")
Outcome = Literal["completed", "failed", "no_release", "no_user", "no_llm", "not_due", "error"]
ProviderFor = Callable[[EvalSuite], EvalProvider | None]


def interval_of(row: EvalSuite) -> int | None:
    sched = (row.spec or {}).get("schedule")
    if not isinstance(sched, dict):
        return None
    try:
        return int(sched["interval_hours"]) or None
    except (KeyError, TypeError, ValueError):
        return None


def _scheduled_runs(workspace_id: uuid.UUID, key: str) -> Select[EvalRun]:
    return (
        select(EvalRun)
        .join(EvalSuite, EvalSuite.id == EvalRun.suite_id)
        .where(EvalSuite.workspace_id == workspace_id, EvalSuite.key == key,
               EvalRun.summary["trigger"].astext == TRIGGER)
        .order_by(EvalRun.started_at.desc())
    )


def last_scheduled_at(s: Session, workspace_id: uuid.UUID, key: str) -> datetime | None:
    return s.scalar(
        select(func.max(EvalRun.started_at))
        .join(EvalSuite, EvalSuite.id == EvalRun.suite_id)
        .where(EvalSuite.workspace_id == workspace_id, EvalSuite.key == key,
               EvalRun.summary["trigger"].astext == TRIGGER))


def next_run_at(row: EvalSuite, last: datetime | None) -> datetime | None:
    """When the schedule next runs this suite (None: no schedule). In the past means due."""
    hours = interval_of(row)
    if not hours:
        return None
    return row.created_at if last is None else last + timedelta(hours=hours)


def is_due(row: EvalSuite, last: datetime | None, now: datetime) -> bool:
    nxt = next_run_at(row, last)
    return nxt is not None and (last is None or now >= nxt)


def _run_as(s: Session, row: EvalSuite) -> Principal | None:
    if row.created_by is not None:
        u = s.get(AppUser, row.created_by)
        if u is not None and u.workspace_id == row.workspace_id and u.role in RUN_ROLES:
            return Principal(row.workspace_id, u.id, u.role)
    u = s.scalars(
        select(AppUser).where(AppUser.workspace_id == row.workspace_id,
                              AppUser.role.in_(RUN_ROLES))
        .order_by(AppUser.created_at, AppUser.id).limit(1)
    ).first()
    return Principal(row.workspace_id, u.id, u.role) if u else None


@dataclass
class ScheduledResult:
    suite: str
    workspace: str
    outcome: Outcome
    run_id: uuid.UUID | None = None
    message: str = ""

    def line(self) -> str:
        what = {"completed": f"run {self.run_id}", "failed": f"run {self.run_id} FAILED"}.get(
            self.outcome, self.outcome.replace("_", " "))
        return f"eval {self.workspace}/{self.suite} (scheduled): {what}" + (
            f": {self.message}" if self.message else "")


def _default_provider_for(engine: Engine, st: Settings) -> ProviderFor:
    def pick(row: EvalSuite) -> EvalProvider | None:
        return resolve_provider(suite_of(row).model, None, st, workspace_id=row.workspace_id,
                                engine=engine)
    return pick


def _lock_key(workspace_id: uuid.UUID, key: str) -> str:
    return f"satchel-eval-schedule:{workspace_id}:{key}"


def _start(
    engine: Engine, row: EvalSuite, slug: str, provider_for: ProviderFor, now: datetime,
) -> tuple[ScheduledResult, Principal | None, EvalProvider | None]:
    """Under the lock: re-check that the suite is due, then insert the ``running`` row."""
    def res(outcome: Outcome, msg: str = "", run_id: uuid.UUID | None = None
            ) -> ScheduledResult:
        return ScheduledResult(row.key, slug, outcome, run_id, msg)

    with admin_session(engine=engine) as s:
        got = s.scalar(text("select pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                       {"k": _lock_key(row.workspace_id, row.key)})
        if not got or not is_due(row, last_scheduled_at(s, row.workspace_id, row.key), now):
            return res("not_due"), None, None
        rel = latest_published(s, row.workspace_id)
        if rel is None:
            return res("no_release", "the workspace has no published release"), None, None
        principal = _run_as(s, row)
        if principal is None:
            return res("no_user", "no admin, curator or analyst to run it as"), None, None
        suite = suite_of(row)
        provider = provider_for(row)
        if suite.needs_agent and provider is None:
            return res("no_llm", "the suite needs an LLM and none is configured"), None, None
        prev = s.scalars(_scheduled_runs(row.workspace_id, row.key)
                         .where(EvalRun.status == "completed").limit(1)).first()
        note: dict[str, str] = {"trigger": TRIGGER}
        if prev is not None:
            note["previous_run_id"] = str(prev.id)
        # Its own transaction: committed before this one releases the lock.
        run_id = create_run(engine, principal, row, rel.id, provider=provider, note=note,
                            record_starter=False)
        with admin_session(engine=engine) as s2:
            audit.record(s2, row.workspace_id, None, "eval.run.start",
                         {"run_id": str(run_id), "suite": row.key, "release_id": str(rel.id),
                          "trigger": TRIGGER})
        return res("completed", run_id=run_id), principal, provider


def run_due_evals(
    *, engine: Engine | None = None, embedder: EmbeddingProvider | None = None,
    settings: Settings | None = None, workspace_id: uuid.UUID | None = None,
    provider_for: ProviderFor | None = None, now: datetime | None = None,
) -> list[ScheduledResult]:
    """Run every scheduled suite that is due, one after another. Never raises for one suite."""
    eng = engine or get_engine()
    st = settings or get_settings()
    when = now or datetime.now(UTC)
    pick = provider_for or _default_provider_for(eng, st)
    due: list[tuple[EvalSuite, str]] = []
    with admin_session(engine=eng) as s:
        q = select(Workspace.id, Workspace.slug).order_by(Workspace.slug)
        if workspace_id is not None:
            q = q.where(Workspace.id == workspace_id)
        for wid, slug in s.execute(q).all():
            for row in latest_suites(s, wid):
                if interval_of(row) and is_due(row, last_scheduled_at(s, wid, row.key), when):
                    s.expunge(row)
                    due.append((row, slug))
    out: list[ScheduledResult] = []
    emb: EmbeddingProvider | None = embedder
    for row, slug in due:
        try:
            r, principal, provider = _start(eng, row, slug, pick, when)
            if principal is not None and r.run_id is not None:
                emb = emb or make_embedder(st)
                execute_run(eng, emb, principal, r.run_id, provider=provider)
                with admin_session(engine=eng) as s:
                    run = s.get(EvalRun, r.run_id)
                    if run is not None and run.status != "completed":
                        r.outcome, r.message = "failed", str((run.summary or {}).get("error", ""))
        except Exception as e:  # noqa: BLE001 - one broken suite never stops the others
            log.exception("scheduled eval of %s/%s failed", slug, row.key)
            r = ScheduledResult(row.key, slug, "error", message=f"{type(e).__name__}: {e}")
        if r.outcome != "not_due":
            (log.info if r.outcome == "completed" else log.warning)(r.line())
        out.append(r)
    return out
