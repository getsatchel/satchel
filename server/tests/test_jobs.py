"""The job queue (``satchel.jobs``): dedupe, concurrent claims, retries with backoff, lost
processes (lease expiry), give-up hooks, workspace RLS, and ``eval.run`` jobs from the API and
after a crash."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text, update

from satchel.evals.jobs import KIND as EVAL_RUN
from satchel.evals.jobs import enqueue_runs
from satchel.evals.runner import create_run
from satchel.evals.store import latest_suite
from satchel.jobs import queue
from satchel.jobs.queue import Claimed
from satchel.jobs.runner import JobRunner, Runtime, register
from satchel.settings import Settings
from satchel.store.db import admin_session, workspace_session
from satchel.store.models import EvalCaseResult, EvalRun, Job, Workspace
from satchel.worker.main import run_forever
from tests.helpers import build_jaffle_db, unique_slug
from tests.test_evals import EMB, Env, make_env, run_eval

OK, FLAKY, BROKEN = "test.ok", "test.flaky", "test.broken"
seen: list[tuple[str, int]] = []
gave_up: list[tuple[int, str]] = []


def _ok(_rt: Runtime, job: Claimed) -> None:
    seen.append((OK, job.id))


def _flaky(_rt: Runtime, job: Claimed) -> None:
    if job.attempts < 2:
        raise RuntimeError("first attempt fails")
    seen.append((FLAKY, job.id))


def _broken(_rt: Runtime, job: Claimed) -> None:
    raise ValueError("always fails")


def _record_give_up(_rt: Runtime, job: Claimed, error: str) -> None:
    gave_up.append((job.id, error))


register(OK, _ok)
register(FLAKY, _flaky)
register(BROKEN, _broken, give_up=_record_give_up)
TEST_KINDS = {OK, FLAKY, BROKEN}


@pytest.fixture(autouse=True)
def _no_leftover_jobs(app_engine: Engine) -> None:
    """Finish unfinished jobs earlier tests left, so each test sees only its own."""
    with admin_session(engine=app_engine) as s:
        s.execute(update(Job).where(Job.status.in_(["queued", "running"]))
                  .values(status="done", finished_at=datetime.now(UTC)))


@pytest.fixture
def ws(app_engine: Engine) -> uuid.UUID:
    with admin_session(engine=app_engine) as s:
        w = Workspace(slug=unique_slug("jobs"), name="jobs")
        s.add(w)
        s.flush()
        return w.id


def runner(engine: Engine, kinds: set[str] = TEST_KINDS) -> JobRunner:
    return JobRunner(Runtime(engine, Settings(), EMB), kinds=kinds, threads=0)


def job(engine: Engine, job_id: int) -> Job:
    with admin_session(engine=engine) as s:
        row = s.get(Job, job_id)
        assert row is not None
        s.expunge(row)
        return row


def enqueue(engine: Engine, ws: uuid.UUID, kind: str, **kw: object) -> int | None:
    with admin_session(engine=engine) as s:
        return queue.enqueue(s, ws, kind, {"n": 1}, **kw)  # type: ignore[arg-type]


def expire(engine: Engine, job_id: int) -> None:
    """Simulate a dead process: the running job's lease is in the past."""
    with admin_session(engine=engine) as s:
        s.execute(update(Job).where(Job.id == job_id)
                  .values(locked_until=datetime.now(UTC) - timedelta(seconds=1)))


def make_due(engine: Engine, job_id: int) -> None:
    with admin_session(engine=engine) as s:
        s.execute(update(Job).where(Job.id == job_id).values(run_after=datetime.now(UTC)))


def test_runs_a_job_and_dedupes_unfinished_ones(app_engine: Engine, ws: uuid.UUID) -> None:
    first = enqueue(app_engine, ws, OK, dedupe_key="k1")
    assert first is not None
    assert enqueue(app_engine, ws, OK, dedupe_key="k1") is None
    assert runner(app_engine).drain() >= 1
    done = job(app_engine, first)
    assert (done.status, done.attempts, done.locked_by) == ("done", 1, done.locked_by)
    assert done.finished_at is not None and (OK, first) in seen
    # Finished, so the same key can be queued again.
    assert enqueue(app_engine, ws, OK, dedupe_key="k1") is not None


def test_concurrent_claims_never_take_the_same_job(app_engine: Engine, ws: uuid.UUID) -> None:
    ids = {enqueue(app_engine, ws, OK) for _ in range(3)}
    with admin_session(engine=app_engine) as a, admin_session(engine=app_engine) as b:
        got_a = queue.claim(a, "a", [OK])
        got_b = queue.claim(b, "b", [OK])  # a's row is locked: b skips it
        assert got_a is not None and got_b is not None and got_a.id != got_b.id
    with admin_session(engine=app_engine) as s:
        owners = dict(s.execute(select(Job.id, Job.locked_by).where(Job.id.in_(ids))).all())
    assert sorted(o for o in owners.values() if o) == ["a", "b"]


def test_a_failed_attempt_is_retried_after_a_backoff(app_engine: Engine, ws: uuid.UUID) -> None:
    jid = enqueue(app_engine, ws, FLAKY)
    assert jid is not None
    r = runner(app_engine, {FLAKY})
    assert r.run_one()
    queued = job(app_engine, jid)
    assert (queued.status, queued.attempts) == ("queued", 1)
    assert "first attempt fails" in (queued.error or "")
    assert queued.run_after > datetime.now(UTC) + timedelta(seconds=20)  # backoff
    assert not r.run_one()  # not due yet
    make_due(app_engine, jid)
    assert r.run_one()
    assert job(app_engine, jid).status == "done" and (FLAKY, jid) in seen


def test_the_last_failed_attempt_fails_the_job_and_gives_up(
    app_engine: Engine, ws: uuid.UUID
) -> None:
    jid = enqueue(app_engine, ws, BROKEN, max_attempts=2)
    assert jid is not None
    r = runner(app_engine, {BROKEN})
    assert r.run_one()
    make_due(app_engine, jid)
    assert r.run_one()
    failed = job(app_engine, jid)
    assert (failed.status, failed.attempts) == ("failed", 2)
    assert "always fails" in (failed.error or "")
    assert [e for i, e in gave_up if i == jid] == ["ValueError: always fails"]


def test_a_job_whose_process_died_is_claimed_again(app_engine: Engine, ws: uuid.UUID) -> None:
    jid = enqueue(app_engine, ws, OK)
    assert jid is not None
    with admin_session(engine=app_engine) as s:
        dead = queue.claim(s, "dead-process", [OK])
    assert dead is not None and dead.id == jid
    assert not runner(app_engine, {OK}).run_one()  # its lease still holds
    expire(app_engine, jid)
    with admin_session(engine=app_engine) as s:
        assert not queue.heartbeat(s, jid, "someone-else")
    assert runner(app_engine, {OK}).run_one()
    again = job(app_engine, jid)
    assert (again.status, again.attempts) == ("done", 2)
    with admin_session(engine=app_engine) as s:  # the dead process can no longer touch it
        assert not queue.heartbeat(s, jid, "dead-process")


def test_a_job_lost_on_its_last_attempt_is_failed_by_the_reaper(
    app_engine: Engine, ws: uuid.UUID
) -> None:
    jid = enqueue(app_engine, ws, BROKEN, max_attempts=2)
    assert jid is not None
    for who in ("dead-1", "dead-2"):
        with admin_session(engine=app_engine) as s:
            got = queue.claim(s, who, [BROKEN])
        assert got is not None and got.id == jid
        expire(app_engine, jid)
    assert not runner(app_engine, {BROKEN}).run_one()  # reaps instead of claiming
    lost = job(app_engine, jid)
    assert lost.status == "failed" and "stopped (2 attempt(s))" in (lost.error or "")
    assert any(i == jid and "stopped" in e for i, e in gave_up)


def test_jobs_are_workspace_scoped_for_the_app_role(app_engine: Engine, ws: uuid.UUID) -> None:
    mine = enqueue(app_engine, ws, OK)
    with admin_session(engine=app_engine) as s:
        other = Workspace(slug=unique_slug("jobs"), name="other")
        s.add(other)
        s.flush()
        theirs = queue.enqueue(s, other.id, OK)
    with workspace_session(ws, None, engine=app_engine) as s:
        visible = set(s.scalars(select(Job.id).where(Job.id.in_([mine, theirs]))))
    assert visible == {mine}


def test_purge_drops_old_finished_jobs(app_engine: Engine, ws: uuid.UUID) -> None:
    old, fresh = enqueue(app_engine, ws, OK), enqueue(app_engine, ws, OK)
    with admin_session(engine=app_engine) as s:
        s.execute(update(Job).where(Job.id.in_([old, fresh])).values(
            status="done", finished_at=datetime.now(UTC)))
        s.execute(update(Job).where(Job.id == old).values(
            finished_at=datetime.now(UTC) - timedelta(days=30)))
        assert queue.purge(s) >= 1
        left = set(s.scalars(select(Job.id).where(Job.id.in_([old, fresh]))))
    assert left == {fresh}


def test_worker_once_runs_due_jobs(app_engine: Engine, ws: uuid.UUID) -> None:
    jid = enqueue(app_engine, ws, OK)
    assert jid is not None
    run_forever(once=True, pass_fn=lambda: None, jobs=runner(app_engine, {OK}))
    assert job(app_engine, jid).status == "done"


# ----------------------------------------------------------------------------- eval.run


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_jaffle_db(tmp_path_factory.mktemp("wh") / "jaffle.duckdb")


@pytest.fixture
def env(app_engine: Engine, tmp_path: Path, warehouse: Path,
        monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setenv("SATCHEL_RESULT_DIR", str(tmp_path / "results"))
    return make_env(app_engine, tmp_path, warehouse, chat=None, prefix="jobev")


def run_status(env: Env, run_id: uuid.UUID) -> EvalRun:
    with admin_session(engine=env.engine) as s:
        run = s.get(EvalRun, run_id)
        assert run is not None
        s.expunge(run)
        return run


def eval_job(env: Env, run_id: uuid.UUID) -> Job:
    with admin_session(engine=env.engine) as s:
        row = s.scalars(select(Job).where(Job.kind == EVAL_RUN,
                                          Job.dedupe_key == str(run_id))).one()
        s.expunge(row)
        return row


def test_an_api_run_is_queued_and_run_by_the_web_server(env: Env) -> None:
    out = run_eval(env, "retrieval", env.head(), wait=False)
    rid = uuid.UUID(out["id"])
    deadline = time.monotonic() + 60
    while run_status(env, rid).status == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
    assert run_status(env, rid).status == "completed"
    j = eval_job(env, rid)
    assert j.status == "done" and j.locked_by and j.locked_by.startswith("web:")
    assert j.payload["user_id"] == str(env.users["analyst"])


def _new_run(env: Env) -> uuid.UUID:
    with admin_session(engine=env.engine) as s:
        row = latest_suite(s, env.ws_id, "retrieval")
        assert row is not None
        s.expunge(row)
    p = env.principal("analyst")
    rid = create_run(env.engine, p, row, env.head())
    enqueue_runs(env.engine, p, [rid])
    return rid


def test_a_run_whose_process_died_runs_again_from_the_start(env: Env) -> None:
    rid = _new_run(env)
    with admin_session(engine=env.engine) as s:
        dead = queue.claim(s, "dead-web", [EVAL_RUN])
        assert dead is not None and dead.payload["run_id"] == str(rid)
        # The dead attempt had stored a case result.
        s.add(EvalCaseResult(workspace_id=env.ws_id, run_id=rid, case_key="stale", attempt=1,
                             outcome="error"))
    expire(env.engine, dead.id)
    JobRunner(Runtime(env.engine, Settings(), EMB), kinds={EVAL_RUN}, threads=0).drain()
    assert run_status(env, rid).status == "completed"
    with admin_session(engine=env.engine) as s:
        keys = set(s.scalars(select(EvalCaseResult.case_key).where(EvalCaseResult.run_id == rid)))
    assert keys and "stale" not in keys
    assert eval_job(env, rid).attempts == 2


def test_a_run_lost_twice_is_marked_failed(env: Env) -> None:
    rid = _new_run(env)
    for who in ("dead-1", "dead-2"):
        with admin_session(engine=env.engine) as s:
            got = queue.claim(s, who, [EVAL_RUN])
        assert got is not None
        expire(env.engine, got.id)
    JobRunner(Runtime(env.engine, Settings(), EMB), kinds={EVAL_RUN}, threads=0).drain()
    run = run_status(env, rid)
    assert run.status == "failed" and run.finished_at is not None
    assert "interrupted" in run.summary["error"]


def test_the_queue_table_has_rls(app_engine: Engine) -> None:
    with admin_session(engine=app_engine) as s:
        on = s.scalar(text("select relrowsecurity and relforcerowsecurity from pg_class "
                           "where relname = 'job'"))
    assert on is True
