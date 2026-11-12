"""``GET /v1/jobs/{id}``: the state of a queued job (``satchel.jobs``) and, once it is done, its
result. The UI polls it after "Sync now" or "Crawl"."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from satchel.api.auth import Auth, Caller
from satchel.store.db import workspace_session
from satchel.store.models import Job


class JobOut(BaseModel):
    id: int
    kind: str
    status: Literal["queued", "running", "done", "failed"]
    attempts: int
    max_attempts: int
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


def job_out(j: Job) -> JobOut:
    return JobOut(id=j.id, kind=j.kind, status=j.status,
                  attempts=j.attempts, max_attempts=j.max_attempts, error=j.error,
                  result=j.result, created_at=j.created_at, started_at=j.started_at,
                  finished_at=j.finished_at)


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["jobs"])
    Cur = Annotated[Caller, Depends(auth.require("curator"))]
    eng = auth.ctx.engine

    @r.get("/v1/jobs/{job_id}", response_model=JobOut)
    def get_job(job_id: int, who: Cur) -> JobOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            j = s.get(Job, job_id)  # RLS: another workspace's job is not found
            if j is None:
                raise HTTPException(404, "no such job")
            return job_out(j)

    return r


def job_of(eng: Any, workspace_id: uuid.UUID, job_id: int) -> JobOut:
    with workspace_session(workspace_id, None, engine=eng) as s:
        j = s.get(Job, job_id)
        assert j is not None
        return job_out(j)
