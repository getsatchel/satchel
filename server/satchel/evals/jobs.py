"""The ``eval.run`` job: run the cases of an existing ``running`` eval_run.

Runs started from the API, by the publish gate or by the schedule are created with
:func:`satchel.evals.runner.create_run` and then queued with :func:`enqueue_runs`; the web
server's or the worker's consumers run them. A run whose process stopped is run again from the
start (its partial case results are deleted) once, then marked failed.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import Engine, delete

from satchel.evals.providers import resolve_provider
from satchel.evals.runner import EvalProvider, _now, execute_run
from satchel.evals.store import suite_of
from satchel.jobs import queue
from satchel.jobs.queue import Claimed
from satchel.jobs.runner import Runtime, register
from satchel.llm.chat import ChatProvider
from satchel.mcp.auth import Principal
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import EvalCaseResult, EvalRun, EvalSuite

KIND = "eval.run"
MAX_ATTEMPTS = 2  # one retry after a lost process: a run can cost real model tokens


def eval_provider(
    row: EvalSuite, override: str | None, settings: Settings, engine: Engine,
    injected: ChatProvider | None = None,
) -> EvalProvider | None:
    """The model a suite runs with: an injected tool-capable provider (tests), else the suite's
    model, the override or the workspace/deployment default."""
    if injected is not None and hasattr(injected, "complete_with_tools"):
        return cast(EvalProvider, injected)
    return resolve_provider(suite_of(row).model, override, settings,
                            workspace_id=row.workspace_id, engine=engine)


def enqueue_runs(engine: Engine, principal: Principal, run_ids: list[uuid.UUID],
                 override: str | None = None) -> None:
    """Queue ``eval.run`` jobs for runs created with ``create_run``, run as ``principal``."""
    with admin_session(engine=engine) as s:
        for rid in run_ids:
            queue.enqueue(
                s, principal.workspace_id, KIND,
                {"run_id": str(rid), "user_id": str(principal.user_id),
                 "role": principal.role, "model": override},
                dedupe_key=str(rid), max_attempts=MAX_ATTEMPTS,
            )


def _handle(rt: Runtime, job: Claimed) -> dict[str, Any] | None:
    p = job.payload
    rid = uuid.UUID(p["run_id"])
    with admin_session(engine=rt.engine) as s:
        run = s.get(EvalRun, rid)
        if run is None or run.status != "running":
            return None  # deleted, or finished by an attempt that lost its lease late
        row = s.get(EvalSuite, run.suite_id)
        if row is None:
            return None
        if job.attempts > 1:
            s.execute(delete(EvalCaseResult).where(EvalCaseResult.run_id == rid))
        s.expunge(row)
    principal = Principal(job.workspace_id, uuid.UUID(p["user_id"]), str(p["role"]))
    provider = eval_provider(row, p.get("model"), rt.settings, rt.engine, rt.chat)
    execute_run(rt.engine, rt.embedder, principal, rid, provider=provider)
    return {"run_id": str(rid)}


def _give_up(rt: Runtime, job: Claimed, error: str) -> None:
    with admin_session(engine=rt.engine) as s:
        run = s.get(EvalRun, uuid.UUID(job.payload["run_id"]))
        if run is not None and run.status == "running":
            run.status, run.finished_at = "failed", _now()
            run.summary = {**(run.summary or {}), "error": f"the run was interrupted: {error}"}


register(KIND, _handle, give_up=_give_up)
