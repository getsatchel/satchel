"""The job queue: rows in ``job`` (migration 0015), Postgres only.

- :func:`enqueue` adds a job; a ``dedupe_key`` keeps at most one unfinished job per key.
- :func:`claim` takes the next due job with ``FOR UPDATE SKIP LOCKED``, so any number of
  consumers (the worker, the web server) can share the queue. A claimed job holds a lease
  (``locked_until``) that :func:`heartbeat` renews while it runs. When the lease runs out (the
  process died), the job is claimed again, up to ``max_attempts``.
- :func:`finish` and :func:`fail` end an attempt; a failed attempt is retried with backoff.
- :func:`reap` gives up on jobs whose lease ran out on their last attempt.
- :func:`record` adds an already finished job, for work that ran in the caller's own process
  (a CLI or upload sync), so its history sits beside the queued runs.

Every function takes an admin session (``satchel_worker``): the queue is server-internal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from satchel.store.models import Job

LEASE_S = 120
BACKOFF_S = 30
MAX_BACKOFF_S = 3600
KEEP_DAYS = 14
_UNFINISHED = "status in ('queued','running') and dedupe_key is not null"


@dataclass(frozen=True)
class Claimed:
    id: int
    workspace_id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int

    @property
    def last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


def unfinished(s: Session, kind: str, dedupe_key: str) -> int | None:
    """The id of the queued or running job of ``kind`` with ``dedupe_key``, if any."""
    found = s.scalar(text(
        "select id from job where kind = :k and dedupe_key = :d "
        "and status in ('queued','running') order by id limit 1"), {"k": kind, "d": dedupe_key})
    return int(found) if found is not None else None


def enqueue(
    s: Session, workspace_id: uuid.UUID, kind: str, payload: dict[str, Any] | None = None, *,
    dedupe_key: str | None = None, max_attempts: int = 3, run_after: datetime | None = None,
) -> int | None:
    """Add a job. Returns its id, or None when an unfinished job with ``dedupe_key`` exists."""
    values: dict[str, Any] = {"workspace_id": workspace_id, "kind": kind,
                              "payload": payload or {}, "dedupe_key": dedupe_key,
                              "max_attempts": max(1, max_attempts)}
    if run_after is not None:
        values["run_after"] = run_after
    stmt = (insert(Job).values(**values)
            .on_conflict_do_nothing(index_elements=["kind", "dedupe_key"],
                                    index_where=text(_UNFINISHED))
            .returning(Job.id))
    return s.scalar(stmt)


_CLAIM = text("""
update job set status = 'running', attempts = attempts + 1, locked_by = :who,
       locked_until = now() + make_interval(secs => :lease), started_at = now()
where id = (
    select id from job
    where kind = any(:kinds) and attempts < max_attempts
      and ((status = 'queued' and run_after <= now())
           or (status = 'running' and locked_until < now()))
    order by run_after, id
    for update skip locked
    limit 1)
returning id, workspace_id, kind, payload, attempts, max_attempts
""")


def claim(s: Session, who: str, kinds: list[str], *, lease_s: int = LEASE_S) -> Claimed | None:
    """Claim the next due job of one of ``kinds``, or None."""
    row = s.execute(_CLAIM, {"who": who, "kinds": kinds, "lease": lease_s}).first()
    return Claimed(*row) if row else None


def heartbeat(s: Session, job_id: int, who: str, *, lease_s: int = LEASE_S) -> bool:
    """Renew the lease. False when the job is no longer ours (another consumer took it)."""
    n = s.execute(text(
        "update job set locked_until = now() + make_interval(secs => :lease) "
        "where id = :id and locked_by = :who and status = 'running'"),
        {"id": job_id, "who": who, "lease": lease_s}).rowcount  # type: ignore[attr-defined]
    return bool(n)


def finish(s: Session, job_id: int, who: str, result: dict[str, Any] | None = None) -> None:
    s.execute(
        update(Job)
        .where(Job.id == job_id, Job.locked_by == who, Job.status == "running")
        .values(status="done", finished_at=func.now(), locked_until=None, error=None,
                result=result))


def record(
    s: Session, workspace_id: uuid.UUID, kind: str, payload: dict[str, Any], *,
    started_at: datetime, result: dict[str, Any] | None = None, error: str | None = None,
) -> int:
    """Add a finished job (``done``, or ``failed`` with ``error``) that no consumer runs."""
    jid = s.scalar(insert(Job).values(
        workspace_id=workspace_id, kind=kind, payload=payload,
        status="failed" if error else "done", attempts=1, max_attempts=1,
        error=error[:2000] if error else None, result=result, run_after=started_at,
        started_at=started_at, finished_at=func.now()).returning(Job.id))
    assert jid is not None
    return int(jid)


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(MAX_BACKOFF_S, BACKOFF_S * 4 ** max(0, attempts - 1)))


def fail(s: Session, job: Claimed, who: str, error: str, *, final: bool = False) -> bool:
    """End a failed attempt: queue a retry, or fail the job on its last attempt (or when
    ``final``). Returns True when the job failed for good."""
    final = final or job.last_attempt
    s.execute(text(
        "update job set status = :st, error = :err, locked_by = null, locked_until = null, "
        "finished_at = case when :final then now() end, "
        "run_after = case when :final then run_after else now() + make_interval(secs => :wait) "
        "end where id = :id and locked_by = :who and status = 'running'"),
        {"st": "failed" if final else "queued", "err": error[:2000], "final": final,
         "wait": backoff(job.attempts).total_seconds(), "id": job.id, "who": who})
    return final


def reap(s: Session, kinds: list[str]) -> list[Claimed]:
    """Fail the jobs whose lease ran out on their last attempt; returns them so the caller can
    run each kind's give-up hook."""
    rows = s.execute(text("""
        update job set status = 'failed', finished_at = now(), locked_until = null,
               error = 'the process running it stopped (' || attempts || ' attempt(s))'
        where id in (
            select id from job
            where status = 'running' and locked_until < now() and attempts >= max_attempts
              and kind = any(:kinds)
            for update skip locked)
        returning id, workspace_id, kind, payload, attempts, max_attempts"""),
        {"kinds": kinds}).all()
    return [Claimed(*r) for r in rows]


def purge(s: Session, *, keep_days: int = KEEP_DAYS) -> int:
    """Delete finished jobs older than ``keep_days``."""
    n = s.execute(text(
        "delete from job where status in ('done','failed') "
        "and finished_at < now() - make_interval(days => :d)"),
        {"d": keep_days}).rowcount  # type: ignore[attr-defined]
    return int(n or 0)
