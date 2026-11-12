"""The ``curation.run`` job: one curation pass over one workspace
(``satchel.curation.run.run_curation``).

Queued for every workspace by the worker's pass (every ``SATCHEL_CURATE_INTERVAL_MIN``) and by
**Run curation** (``POST /v1/curate/run`` without ``wait``). At most one is queued or running per
workspace. A pass that finds another run holding the workspace's lock (the CLI) retries later.
Its result is the run's counts (``CurationReport.summary``), and it is audited as ``curate.run``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Engine

from satchel import audit
from satchel.curation.run import run_curation
from satchel.jobs import queue
from satchel.jobs.queue import Claimed
from satchel.jobs.runner import RetryLater, Runtime, register
from satchel.store.db import admin_session

KIND = "curation.run"
MAX_ATTEMPTS = 4


def queue_curation(engine: Engine, workspace_id: uuid.UUID, *, use_llm: bool = True,
                   actor: uuid.UUID | None = None) -> tuple[int, bool]:
    """Queue a pass. Returns ``(job id, queued)``; False when one is already unfinished (its id
    is returned)."""
    key = str(workspace_id)
    payload = {"use_llm": use_llm, "actor": str(actor) if actor else None}
    for _ in range(2):  # the unfinished one can finish between the two queries
        with admin_session(engine=engine) as s:
            jid = queue.enqueue(s, workspace_id, KIND, payload, dedupe_key=key,
                                max_attempts=MAX_ATTEMPTS)
            if jid is not None:
                return jid, True
            found = queue.unfinished(s, KIND, key)
        if found is not None:
            return found, False
    raise RuntimeError(f"could not queue curation of workspace {workspace_id}")


def _handle(rt: Runtime, job: Claimed) -> dict[str, Any]:
    p = job.payload
    use_llm = bool(p.get("use_llm", True))
    report = run_curation(job.workspace_id, engine=rt.engine, embedder=rt.embedder,
                          chat=rt.chat, settings=rt.settings, use_llm=use_llm)
    if report is None:
        raise RetryLater("another curation run of this workspace is in progress")
    out = report.summary()
    actor = uuid.UUID(p["actor"]) if p.get("actor") else None
    with admin_session(engine=rt.engine) as s:
        audit.record(s, job.workspace_id, actor, "curate.run",
                     {**{k: v for k, v in out.items() if k != "errors"}, "use_llm": use_llm,
                      "job_id": job.id})
    return out


register(KIND, _handle)
