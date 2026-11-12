"""The job queue over REST (``satchel.jobs``), curator and above, scoped to the workspace by RLS:

- ``GET /v1/jobs``: recent jobs, newest first, filtered by ``status``, ``kind`` and ``subject_id``
  (the source, connection or eval run a job is about): the Jobs page, and a source's sync history.
- ``GET /v1/jobs/{id}``: one job and, once it is done, its result. The UI polls it after "Sync
  now", "Crawl" or "Run curation".
- ``POST /v1/jobs/{id}/retry``: queue a failed sync, crawl or curation job again.
"""

import contextlib
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from satchel.api.auth import Auth, Caller
from satchel.api.common import audit
from satchel.store.db import workspace_session
from satchel.store.models import Connection, ContextSource, EvalRun, EvalSuite, Job

# The payload key that names what each kind of job is about.
_SUBJECT_KEYS = ("source_id", "connection_id", "run_id")
JobStatus = Literal["queued", "running", "done", "failed"]
# Kinds a person can retry. A failed eval run is already marked failed: start a new run instead.
RETRYABLE = {"source.sync", "connection.crawl", "curation.run"}


class JobOut(BaseModel):
    id: int
    kind: str
    status: JobStatus
    attempts: int
    max_attempts: int
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    run_after: datetime | None = None
    subject: str | None = None  # what it is about: a source, connection or suite name
    trigger: str | None = None  # source syncs: manual, webhook or schedule
    retryable: bool = False


def _subjects(s: Session, jobs: list[Job]) -> dict[int, str]:
    """A readable name for each job's target, looked up in batches."""
    ids: dict[str, set[uuid.UUID]] = {"source": set(), "connection": set(), "run": set()}
    field = {"source.sync": ("source", "source_id"),
             "connection.crawl": ("connection", "connection_id"),
             "eval.run": ("run", "run_id")}
    for j in jobs:
        if j.kind in field:
            group, key = field[j.kind]
            with contextlib.suppress(ValueError):
                ids[group].add(uuid.UUID(str(j.payload.get(key))))
    names: dict[str, str] = {}
    if ids["source"]:
        names |= {str(i): n for i, n in s.execute(
            select(ContextSource.id, ContextSource.name)
            .where(ContextSource.id.in_(ids["source"])))}
    if ids["connection"]:
        names |= {str(i): n for i, n in s.execute(
            select(Connection.id, Connection.name).where(Connection.id.in_(ids["connection"])))}
    if ids["run"]:
        names |= {str(i): k for i, k in s.execute(
            select(EvalRun.id, EvalSuite.key).join(EvalSuite, EvalSuite.id == EvalRun.suite_id)
            .where(EvalRun.id.in_(ids["run"])))}
    out: dict[int, str] = {}
    for j in jobs:
        if j.kind == "curation.run":
            out[j.id] = "workspace"
        elif j.kind in field:
            name = names.get(str(j.payload.get(field[j.kind][1])))
            if name:
                out[j.id] = name
    return out


def job_out(j: Job, subject: str | None = None) -> JobOut:
    trigger = j.payload.get("trigger") if j.kind == "source.sync" else None
    return JobOut(id=j.id, kind=j.kind, status=j.status,
                  attempts=j.attempts, max_attempts=j.max_attempts, error=j.error,
                  result=j.result, created_at=j.created_at, started_at=j.started_at,
                  finished_at=j.finished_at,
                  run_after=j.run_after if j.status == "queued" else None,
                  subject=subject, trigger=str(trigger) if trigger else None,
                  retryable=j.status == "failed" and j.kind in RETRYABLE)


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["jobs"])
    Cur = Annotated[Caller, Depends(auth.require("curator"))]
    eng = auth.ctx.engine

    @r.get("/v1/jobs", response_model=list[JobOut])
    def list_jobs(
        who: Cur, status: JobStatus | None = None, kind: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before: Annotated[int | None, Query(description="ids below this (paging)")] = None,
        subject_id: Annotated[uuid.UUID | None, Query(
            description="only jobs about this source, connection or eval run")] = None,
    ) -> list[JobOut]:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            q = select(Job).order_by(Job.id.desc()).limit(limit)
            if status is not None:
                q = q.where(Job.status == status)
            if kind is not None:
                q = q.where(Job.kind == kind)
            if before is not None:
                q = q.where(Job.id < before)
            if subject_id is not None:
                sid = str(subject_id)
                q = q.where(or_(*(Job.payload[k].astext == sid for k in _SUBJECT_KEYS)))
            jobs = list(s.scalars(q))
            names = _subjects(s, jobs)
            return [job_out(j, names.get(j.id)) for j in jobs]

    @r.get("/v1/jobs/{job_id}", response_model=JobOut)
    def get_job(job_id: int, who: Cur) -> JobOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            j = s.get(Job, job_id)  # RLS: another workspace's job is not found
            if j is None:
                raise HTTPException(404, "no such job")
            return job_out(j, _subjects(s, [j]).get(j.id))

    @r.post("/v1/jobs/{job_id}/retry", response_model=JobOut)
    def retry_job(job_id: int, who: Cur) -> JobOut:
        try:
            with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
                j = s.get(Job, job_id)
                if j is None:
                    raise HTTPException(404, "no such job")
                if j.status != "failed":
                    raise HTTPException(409, f"only failed jobs can be retried; this one is "
                                        f"{j.status}")
                if j.kind not in RETRYABLE:
                    raise HTTPException(409, "a failed eval run cannot be retried; start a new "
                                        "run from Evals")
                s.execute(update(Job).where(Job.id == job_id).values(
                    status="queued", attempts=0, error=None, result=None, finished_at=None,
                    started_at=None, locked_by=None, locked_until=None, run_after=func.now()))
                audit(s, who, "job.retry", {"job_id": job_id, "kind": j.kind})
        except IntegrityError:
            raise HTTPException(409, "the same work is already queued or running") from None
        if auth.ctx.jobs is not None:
            auth.ctx.jobs.wake()
        return job_of(eng, who.workspace_id, job_id)

    return r


def job_of(eng: Any, workspace_id: uuid.UUID, job_id: int) -> JobOut:
    with workspace_session(workspace_id, None, engine=eng) as s:
        j = s.get(Job, job_id)
        assert j is not None
        return job_out(j, _subjects(s, [j]).get(j.id))
