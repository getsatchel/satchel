"""Scheduled eval runs (``satchel.evals.schedule``): due checks, who a run is attributed to, the
no-LLM skip, the per-suite lock, the comparison link and the API fields the Evals UI shows."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text

from satchel.evals.schedule import run_due_evals
from satchel.evals.suite import SuiteError, parse_suite_text
from satchel.store.db import admin_session
from satchel.store.models import AuditEvent, EvalRun
from satchel.worker.main import run_forever
from tests.helpers import build_jaffle_db
from tests.test_evals import SUITE_YAML, Env, fake_agent, make_env, put_suite, run_eval

RETRIEVAL = """
id: nightly
name: Nightly retrieval
schedule: { interval_hours: 24 }
cases:
  - id: revenue
    prompt: revenue last month
    rubrics:
      - { grader: context_used, items: [glossary/business-terms.yaml#revenue], top_n: 5 }
"""
AGENT = SUITE_YAML.replace("name: Mini suite", "name: Mini suite\nschedule:\n  interval_hours: 6")


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_jaffle_db(tmp_path_factory.mktemp("wh") / "jaffle.duckdb")


@pytest.fixture(autouse=True)
def _result_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SATCHEL_RESULT_DIR", str(tmp_path / "results"))


@pytest.fixture
def env(app_engine: Engine, tmp_path: Path, warehouse: Path) -> Env:
    return make_env(app_engine, tmp_path, warehouse, chat=None, prefix="sched")


def due(env: Env, now: datetime | None = None, provider: object = None) -> list[str]:
    out = run_due_evals(engine=env.engine, workspace_id=env.ws_id, now=now,
                        provider_for=lambda _row: provider)  # type: ignore[arg-type,return-value]
    return [f"{r.suite}:{r.outcome}" for r in out]


def runs(env: Env) -> list[EvalRun]:
    with admin_session(engine=env.engine) as s:
        rows = list(s.scalars(select(EvalRun).where(EvalRun.workspace_id == env.ws_id)
                              .order_by(EvalRun.started_at)))
        s.expunge_all()
        return rows


def test_schedule_is_validated() -> None:
    assert parse_suite_text(RETRIEVAL).schedule.interval_hours == 24  # type: ignore[union-attr]
    for bad in ("{ interval_hours: 0 }", "{ interval_hours: 169 }", "{ every: 2 }"):
        with pytest.raises(SuiteError, match="schedule"):
            parse_suite_text(RETRIEVAL.replace("{ interval_hours: 24 }", bad))


def test_scheduled_suite_runs_on_the_latest_release_and_waits_for_the_interval(env: Env) -> None:
    put_suite(env, RETRIEVAL)
    assert due(env) == ["nightly:completed"]  # the example's git suites have no schedule
    [first] = runs(env)
    assert first.status == "completed" and first.release_id == env.head()
    assert first.started_by is None and first.summary["trigger"] == "schedule"
    assert "previous_run_id" not in first.summary
    with admin_session(engine=env.engine) as s:
        ev = s.scalars(select(AuditEvent).where(AuditEvent.workspace_id == env.ws_id,
                                                AuditEvent.action == "eval.run.start")).one()
        assert ev.actor_user_id is None and ev.target["trigger"] == "schedule"

    assert due(env) == []  # not due again yet
    run_eval(env, "nightly", env.head())  # a manual run never moves the schedule
    later = first.started_at + timedelta(hours=24, minutes=1)
    assert due(env, now=first.started_at + timedelta(hours=23)) == []
    assert due(env, now=later) == ["nightly:completed"]
    third = runs(env)[-1]
    assert third.summary["previous_run_id"] == str(first.id)

    api = env.client.get("/v1/evals/suites", headers=env.h("explorer")).json()
    nightly = next(x for x in api if x["key"] == "nightly")
    assert nightly["schedule_hours"] == 24
    nxt = datetime.fromisoformat(nightly["next_run_at"])
    assert nxt == third.started_at + timedelta(hours=24)
    by_id = {r["id"]: r for r in env.client.get("/v1/evals/runs",
                                                headers=env.h("explorer")).json()}
    assert by_id[str(third.id)]["trigger"] == "schedule"
    assert by_id[str(third.id)]["previous_run_id"] == str(first.id)
    assert {r["trigger"] for r in by_id.values()} == {"schedule", "manual"}


def test_agent_suite_is_skipped_without_an_llm_and_runs_with_one(env: Env) -> None:
    put_suite(env, AGENT)
    assert due(env) == ["mini:no_llm"]
    assert runs(env) == []  # nothing recorded, so it is retried on the next tick
    assert due(env, provider=fake_agent()) == ["mini:completed"]
    [run] = runs(env)
    assert run.model is not None and run.summary["cases"]["pass"] == 3, run.summary


def test_runs_as_the_suite_author_or_the_oldest_runner(env: Env) -> None:
    from satchel.store.models import Session as SessionRow

    put_suite(env, AGENT)  # saved by the curator
    due(env, provider=fake_agent())
    with admin_session(engine=env.engine) as s:
        owners = set(s.scalars(select(SessionRow.user_id).where(
            SessionRow.workspace_id == env.ws_id)))
    assert owners == {env.users["curator"]}


def test_a_suite_another_worker_is_starting_is_skipped(env: Env) -> None:
    put_suite(env, RETRIEVAL)
    key = f"satchel-eval-schedule:{env.ws_id}:nightly"
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with env.engine.connect() as c:
            c.execute(text("select pg_advisory_lock(hashtextextended(:k, 0))"), {"k": key})
            held.set()
            release.wait(10)
            c.execute(text("select pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key})

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert held.wait(5)
        assert due(env) == ["nightly:not_due"] and runs(env) == []
    finally:
        release.set()
        t.join(5)
    assert due(env) == ["nightly:completed"]


def test_no_published_release(app_engine: Engine) -> None:
    from satchel.store.models import Workspace
    from tests.helpers import unique_slug

    slug = unique_slug("schednr")
    with admin_session(engine=app_engine) as s:
        ws = Workspace(slug=slug, name=slug)
        s.add(ws)
        s.flush()
        wid = ws.id
    from satchel.evals.store import save_suite

    with admin_session(engine=app_engine) as s:
        save_suite(s, wid, parse_suite_text(RETRIEVAL))
    out = run_due_evals(engine=app_engine, workspace_id=wid, provider_for=lambda _r: None)
    assert [r.outcome for r in out] == ["no_release"]


def test_worker_runs_eval_ticks_on_their_own_thread() -> None:
    calls: list[str] = []
    run_forever(once=True, pass_fn=lambda: calls.append("pass"),
                sources_fn=lambda: calls.append("sources"), evals_fn=lambda: calls.append("evals"))
    assert calls == ["pass", "sources", "evals"]

    stop, ticked = threading.Event(), threading.Event()
    t = threading.Thread(target=run_forever, kwargs={
        "stop": stop, "interval_min": 1440, "tick_min": 0, "eval_tick_min": 1,
        "pass_fn": lambda: None, "evals_fn": ticked.set})
    t.start()
    assert ticked.wait(5)
    stop.set()
    t.join(5)
    assert not t.is_alive()

    off: list[str] = []
    run_forever(once=True, pass_fn=lambda: off.append("pass"), eval_tick_min=0,
                evals_fn=lambda: off.append("evals"))
    assert off == ["pass"]
