"""M5 evals: suite format, graders, the reference harness against real tools, runs, compare,
the publish gate, eval-session exclusion, API roles and the importer."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, update

from satchel.api.app import create_app
from satchel.data.connections import add_connection
from satchel.data.crawl import crawl_connection
from satchel.evals import graders as g
from satchel.evals.gate import GateConfig, gate_config, regressions
from satchel.evals.harness import run_agent, tool_specs
from satchel.evals.runner import compare_summaries, measure_stats
from satchel.evals.suite import (
    MAX_CASES,
    SuiteError,
    load_suite_file,
    parse_suite,
    parse_suite_text,
)
from satchel.llm.chat import (
    AnthropicChat,
    ChatError,
    FakeChatProvider,
    ModelTurn,
    OpenAICompatChat,
    ToolSpec,
    ToolUse,
    Usage,
)
from satchel.llm.embeddings import HashEmbedder
from satchel.mcp.auth import Principal
from satchel.settings import Settings
from satchel.store.db import admin_session, workspace_session
from satchel.store.models import (
    AppUser,
    AuditEvent,
    ContextItem,
    EvalCaseResult,
    EvalRun,
    EvalSuite,
    Release,
    Signal,
    Workspace,
)
from satchel.store.models import Session as SessionRow
from satchel.store.scopes import signal_not_eval
from satchel.sync import import_context
from tests.helpers import EXAMPLE_CONTEXT, build_jaffle_db, copy_example, unique_slug

EMB = HashEmbedder()
ROLES = ["admin", "curator", "analyst", "explorer"]
REVENUE_KEY = "glossary/business-terms.yaml#revenue"

REV_SQL = (
    "select sum(amount) / 100.0 as revenue from analytics.orders "
    "where status not in ('returned','return_pending') and customer_id <> 0 "
    "and order_date >= date '2025-03-01' and order_date < date '2025-04-01'"
)
TOP_SQL = (
    "select c.customer_id, c.first_name, c.last_name, sum(o.amount) / 100.0 as revenue_aud "
    "from analytics.orders o join analytics.customers c on c.customer_id = o.customer_id "
    "where o.status not in ('returned','return_pending') and o.customer_id <> 0 "
    "group by 1, 2, 3 order by revenue_aud desc limit 10"
)

SUITE_YAML = f"""
id: mini
name: Mini suite
cases:
  - id: revenue
    prompt: What was revenue in March 2025?
    attempts: 2
    rubrics:
      - grader: numeric_value
        expected_sql: "{REV_SQL}"
        tolerance_pct: 0.5
        measure: correctness
      - grader: judge_session
        criteria: Excluded returned orders.
        measure: method
      - {{ grader: context_used, items: ["{REVENUE_KEY}"], top_n: 5, measure: retrieval,
          warn_only: true }}
  - id: top
    prompt: Who are our top 10 customers by revenue?
    rubrics:
      - grader: sql_equivalent
        expected_sql: "{TOP_SQL}"
        ordered: true
        measure: correctness
  - id: churn
    prompt: What was our customer churn rate last quarter?
    rubrics:
      - grader: judge_final_answer
        criteria: Says churn is not defined instead of inventing a number.
        measure: honesty
"""


# ----------------------------------------------------------------------------- scripted agent


class Script:
    """A scripted reference agent: per prompt, a list of tool calls then a final answer.
    Stateless (derived from the transcript) so concurrent cases can share it."""

    def __init__(self, plans: dict[str, tuple[list[tuple[str, dict[str, Any]]], str]]) -> None:
        self.plans = plans

    def __call__(self, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec]
                 ) -> ModelTurn:
        prompt = messages[0]["content"]
        steps, final = self.plans[prompt]
        n = sum(1 for m in messages if m["role"] == "assistant")
        if n < len(steps):
            name, args = steps[n]
            return ModelTurn(
                tool_uses=[ToolUse(f"t{n}", name, {"question": prompt, **args})],
                usage=Usage(100, 20),
            )
        return ModelTurn(text=final, usage=Usage(100, 20))


def rev_plan(final: str = "Revenue in March 2025 was **$1,716.15**.", sql: str = REV_SQL
             ) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    return ([
        ("start_analysis", {}),
        ("get_context_pack", {}),
        ("run_sql", {"sql": sql}),
        ("report_answer", {"answer_summary": "revenue", "confidence": "high"}),
    ], final)


PLANS = {
    "What was revenue in March 2025?": rev_plan(),
    "Who are our top 10 customers by revenue?": ([
        ("start_analysis", {}), ("run_sql", {"sql": TOP_SQL}),
        ("report_answer", {"answer_summary": "top", "confidence": "high"}),
    ], "Top customer is Ava Taylor with $1,361.70."),
    "What was our customer churn rate last quarter?": ([
        ("start_analysis", {}), ("get_context_pack", {}),
        ("flag_context_gap", {"what_was_missing": "churn definition", "why": "no glossary term"}),
        ("report_answer", {"answer_summary": "no churn", "confidence": "low",
                           "caveats": ["churn is not defined"]}),
    ], "Churn is not defined in this workspace, so I cannot give a number."),
}


def judge(system: str, messages: list[Any], schema: dict[str, Any]) -> dict[str, Any]:
    body = messages[0]["content"]
    if "Extract one number" in system:
        return {"value": 42.0}
    return {"pass": "FAILJUDGE" not in body and "$9,999" not in body, "reasoning": "scripted"}


def fake_agent(plans: dict[str, Any] | None = None) -> FakeChatProvider:
    return FakeChatProvider(replies=judge, turns=Script(plans or PLANS))  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- environment


@dataclass
class Env:
    client: TestClient
    engine: Engine
    slug: str
    ws_id: uuid.UUID
    users: dict[str, uuid.UUID]
    chat: FakeChatProvider | None

    def h(self, role: str) -> dict[str, str]:
        return {"X-Satchel-Workspace": self.slug, "X-Satchel-User": str(self.users[role])}

    def principal(self, role: str = "curator") -> Principal:
        return Principal(self.ws_id, self.users[role], role)

    def head(self) -> uuid.UUID:
        with admin_session(engine=self.engine) as s:
            return s.scalars(
                select(Release.id).where(Release.workspace_id == self.ws_id,
                                         Release.kind == "published")
                .order_by(Release.seq.desc())
            ).first()  # type: ignore[return-value]


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_jaffle_db(tmp_path_factory.mktemp("wh") / "jaffle.duckdb")


@pytest.fixture(autouse=True)
def _result_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SATCHEL_RESULT_DIR", str(tmp_path / "results"))


def make_env(
    app_engine: Engine, tmp_path: Path, warehouse: Path, *, gate: bool = False,
    chat: FakeChatProvider | None = None, prefix: str = "ev",
) -> Env:
    slug = unique_slug(prefix)
    r = import_context(copy_example(tmp_path, gate=gate), slug, embedder=EMB, engine=app_engine)
    add_connection(slug, "warehouse", "duckdb", {"path": str(warehouse)}, engine=app_engine)
    crawl_connection(slug, "warehouse", engine=app_engine)
    users = {role: uuid.uuid4() for role in ROLES}
    with admin_session(engine=app_engine) as s:
        for role, uid in users.items():
            s.add(AppUser(id=uid, workspace_id=r.workspace_id, role=role,
                          email=f"{role}@{slug}.test", display_name=role))
    client = TestClient(create_app(app_engine, EMB, settings=Settings(dev_auth=True), chat=chat))
    return Env(client, app_engine, slug, r.workspace_id, users, chat)


@pytest.fixture
def env(app_engine: Engine, tmp_path: Path, warehouse: Path) -> Env:
    return make_env(app_engine, tmp_path, warehouse, chat=fake_agent())


def put_suite(env: Env, yaml_text: str = SUITE_YAML) -> dict[str, Any]:
    r = env.client.post("/v1/evals/suites", json={"yaml": yaml_text}, headers=env.h("curator"))
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def run_eval(env: Env, suite_key: str, release_id: uuid.UUID, role: str = "analyst",
             **extra: Any) -> dict[str, Any]:
    r = env.client.post(
        "/v1/evals/runs",
        json={"suite_key": suite_key, "release_id": str(release_id), "wait": True, **extra},
        headers=env.h(role),
    )
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def k(key: str) -> str:
    return quote(key, safe="/")


# ----------------------------------------------------------------------------- suite format


def test_example_suites_parse() -> None:
    core = load_suite_file(EXAMPLE_CONTEXT / "evals" / "core.yaml")
    assert core.id == "core" and len(core.cases) >= 7 and core.needs_agent
    assert {"correctness", "retrieval", "honesty", "method"} <= set(core.measures())
    retrieval = load_suite_file(EXAMPLE_CONTEXT / "evals" / "retrieval.yaml")
    assert not retrieval.needs_agent  # context_used only: runs without an LLM


def _case(i: int, **over: Any) -> dict[str, Any]:
    return {"id": f"c{i}", "prompt": "q",
            "rubrics": [{"grader": "context_used", "items": ["a"]}], **over}


def test_suite_limits_and_validation() -> None:
    ok = parse_suite({"id": "s", "cases": [_case(i) for i in range(MAX_CASES)]})
    assert len(ok.cases) == MAX_CASES
    bad: list[dict[str, Any]] = [
        {"id": "s", "cases": [_case(i) for i in range(MAX_CASES + 1)]},
        {"id": "s", "cases": [_case(0, attempts=6)]},
        {"id": "s", "cases": [_case(0, attempts=0)]},
        {"id": "s", "cases": [_case(0, rubrics=[{"grader": "context_used", "items": ["a"]}] * 16)]},
        {"id": "s", "cases": [_case(0, rubrics=[])]},
        {"id": "s", "cases": [_case(0, rubrics=[{"grader": "nope"}])]},
        {"id": "s", "cases": [_case(0, rubrics=[{"grader": "numeric_value"}])]},
        {"id": "s", "cases": [_case(0, rubrics=[{"grader": "sql_equivalent"}])]},
        {"id": "s", "cases": [_case(0, rubrics=[{"grader": "context_used"}])]},
        {"id": "s", "cases": [_case(0, rubrics=[{"grader": "judge_session"}])]},
        {"id": "s", "cases": [_case(0), _case(0)]},
        {"id": "bad id!", "cases": [_case(0)]},
        {"id": "s", "cases": []},
    ]
    for raw in bad:
        with pytest.raises(SuiteError):
            parse_suite(raw)
    with pytest.raises(SuiteError, match="invalid YAML"):
        parse_suite_text("id: [unclosed")
    # 15 rubrics is the maximum allowed
    parse_suite({"id": "s", "cases": [_case(0, rubrics=[{"grader": "context_used",
                                                          "items": ["a"]}] * 15)]})


def test_default_measures() -> None:
    s = parse_suite(parse_suite_text(SUITE_YAML).model_dump())
    measures = {r.grader: r.measure for c in s.cases for r in c.rubrics}
    assert measures["context_used"] == "retrieval"
    bare = parse_suite({"id": "b", "cases": [_case(0, rubrics=[
        {"grader": "judge_session", "criteria": "x"}])]})
    assert bare.cases[0].rubrics[0].measure == "method"


# ----------------------------------------------------------------------------- graders (unit)


@pytest.mark.parametrize("text,want", [
    ("Revenue in March 2025 was **$1,716.15**.", 1716.15),
    ("In March 2025 we made $1,716.15 AUD (32 orders).", 1716.15),
    ("1. Looking at the data\n2. Revenue: 1716.15", 1716.15),
    ("About $1.7k", 1700.0),
    ("The refund rate was 26.9%", 26.9),
    ("Total was -12.5", -12.5),
    ("2025-03-31: revenue 55", 55.0),
])
def test_headline_number(text: str, want: float) -> None:
    assert g.headline_number(text) == pytest.approx(want)


def test_headline_number_none() -> None:
    assert g.headline_number("I could not find any figure.") is None


def test_within_tolerances() -> None:
    assert g.within(100.4, 100, 0.5, None)
    assert not g.within(101, 100, 0.5, None)
    assert g.within(101, 100, None, 1.0)
    assert g.within(101, 100, 0.1, 1.0)  # either tolerance is enough
    assert g.within(100.4, 100, None, None)  # default 0.5%
    assert g.within(0, 0, 0.5, None)
    assert not g.within(0.01, 0, 0.5, None)


def test_compare_results() -> None:
    a = [(1, "x", 1.00001), (2, "y", 2.0)]
    assert g.compare_results(a, [(2, "y", 2.0), (1, "x", 1.0)], ordered=False) is None
    assert g.compare_results(a, [(2, "y", 2.0), (1, "x", 1.0)], ordered=True) is not None
    assert g.compare_results(a, a[:1], ordered=False) == "row counts differ: 2 vs 1"
    assert g.compare_results(a, [(1, "x", 1.0), (2, "z", 2.0)], ordered=False) is not None


def _gr(outcome: str, warn: bool = False) -> g.Grade:
    return g.Grade("numeric_value", "correctness", warn, outcome, "")


def test_attempt_and_case_tolerance_rules() -> None:
    assert g.attempt_outcome([_gr("pass"), _gr("fail")]) == "fail"
    assert g.attempt_outcome([_gr("pass"), _gr("fail", warn=True)]) == "pass"  # warn_only
    assert g.attempt_outcome([_gr("fail"), _gr("error")]) == "error"
    assert g.attempt_outcome([_gr("error", warn=True)]) == "pass"
    assert g.attempt_outcome([_gr("skipped"), _gr("skipped")]) == "skipped"
    assert g.attempt_outcome([_gr("pass"), _gr("skipped")]) == "pass"
    # several attempts: one failure is tolerated, two are not, any error is an error
    assert g.combine_attempts(["pass", "fail", "pass"]) == "pass"
    assert g.combine_attempts(["fail", "fail", "pass"]) == "fail"
    assert g.combine_attempts(["pass", "fail"]) == "pass"
    assert g.combine_attempts(["fail"]) == "fail"  # a single attempt tolerates nothing
    assert g.combine_attempts(["pass", "error", "pass"]) == "error"
    assert g.combine_attempts(["skipped", "skipped"]) == "skipped"


def test_measure_stats_exclude_warn_only_and_skipped() -> None:
    def case(outcome: str, rubrics: list[tuple[str, str, bool]]) -> dict[str, Any]:
        return {"outcome": outcome, "rubrics": [
            {"index": i, "grader": "x", "measure": m, "warn_only": w, "outcome": o}
            for i, (m, o, w) in enumerate(rubrics)]}

    stats = measure_stats({
        "a": case("pass", [("correctness", "pass", False), ("retrieval", "fail", True)]),
        "b": case("fail", [("correctness", "fail", False)]),
        "c": case("skipped", [("correctness", "skipped", False)]),
    })
    assert stats["correctness"]["pass_rate"] == 0.5
    assert stats["correctness"]["skipped"] == 1
    assert stats["retrieval"]["pass_rate"] is None and stats["retrieval"]["warnings"] == 1


def test_compare_and_regressions() -> None:
    def summary(rate: float, outcomes: dict[str, str]) -> dict[str, Any]:
        return {"measures": {"correctness": {"pass_rate": rate}},
                "case_outcomes": {k_: {"outcome": o} for k_, o in outcomes.items()}}

    a = summary(1.0, {"x": "pass", "y": "fail", "z": "pass"})
    b = summary(0.5, {"x": "fail", "y": "pass", "z": "pass"})
    cmp = compare_summaries(a, b)
    assert cmp["measures"] == [{"measure": "correctness", "a": 1.0, "b": 0.5, "delta": -0.5}]
    assert [c["case"] for c in cmp["newly_failing"]] == ["x"]
    assert [c["case"] for c in cmp["newly_passing"]] == ["y"]
    assert len(regressions(cmp, 0)) == 1
    assert regressions(cmp, 50) == []  # a 50-point drop is within a 50-point threshold
    assert regressions(compare_summaries(b, a), 0) == []


def test_gate_config() -> None:
    assert gate_config(None) == GateConfig()
    assert gate_config({"publish": {"require_evals": ["a", "b"], "eval_threshold": 5}}) == (
        GateConfig(["a", "b"], 5.0))
    assert gate_config({"publish": "junk"}) == GateConfig()


# ----------------------------------------------------------------------------- providers


def test_anthropic_and_openai_tool_loops_over_http() -> None:
    seen: list[dict[str, Any]] = []

    def anthropic(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={
            "stop_reason": "tool_use", "usage": {"input_tokens": 11, "output_tokens": 7},
            "content": [{"type": "text", "text": "thinking"},
                        {"type": "tool_use", "id": "tu1", "name": "start_analysis",
                         "input": {"question": "q"}}]})

    a = AnthropicChat("k", "m", client=httpx.Client(transport=httpx.MockTransport(anthropic)))
    tools = [ToolSpec(name="start_analysis", description="d", input_schema={"type": "object"})]
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "text": "", "tool_uses": [{"id": "x", "name": "t", "input": {}}]},
        {"role": "tool", "results": [{"id": "x", "name": "t", "content": "ok", "is_error": False}]},
    ]
    turn = a.complete_with_tools("sys", msgs, tools)
    assert turn.tool_uses[0].name == "start_analysis" and turn.text == "thinking"
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (11, 7)
    sent = seen[0]
    assert sent["tools"][0]["name"] == "start_analysis" and sent["system"] == "sys"
    assert sent["messages"][2]["content"][0]["type"] == "tool_result"
    assert a.usage.input_tokens == 11

    def openai(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            "choices": [{"finish_reason": "tool_calls", "message": {
                "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "run_sql", "arguments": json.dumps({"sql": "select 1"})}}]}}]})

    o = OpenAICompatChat("m", "http://x/v1", client=httpx.Client(
        transport=httpx.MockTransport(openai)))
    turn = o.complete_with_tools("sys", msgs, tools)
    assert turn.tool_uses[0].input == {"sql": "select 1"} and turn.usage.output_tokens == 3
    assert seen[1]["messages"][0]["role"] == "system"
    assert seen[1]["messages"][-1] == {"role": "tool", "tool_call_id": "x", "content": "ok"}
    assert seen[1]["tools"][0]["function"]["name"] == "start_analysis"


def test_fake_provider_tool_turns() -> None:
    f = FakeChatProvider(turns=[ModelTurn(text="a"), ModelTurn(text="b")])
    assert [f.complete_with_tools("s", [], []).text for _ in range(3)] == ["a", "b", "b"]
    with pytest.raises(ChatError):
        FakeChatProvider().complete_with_tools("s", [], [])


# ----------------------------------------------------------------------------- harness


def test_harness_runs_real_tools_pinned_to_a_release(env: Env) -> None:
    run_id = uuid.uuid4()
    rel = env.head()
    out = run_agent(
        env.engine, EMB, env.principal("analyst"), fake_agent(), "What was revenue in March 2025?",
        release_id=rel, eval_run_id=run_id,
    )
    assert out.error is None and "1,716.15" in out.answer
    assert [t.tool for t in out.trace] == [
        "start_analysis", "get_context_pack", "run_sql", "report_answer"]
    assert not any(t.is_error for t in out.trace)
    assert out.usage.input_tokens == 500 and out.steps == 5
    names = {t["name"] for t in tool_specs(env.engine, env.principal("analyst"))}
    assert "propose_context_change" not in names and {"run_sql", "start_analysis"} <= names
    with workspace_session(env.ws_id, engine=env.engine) as s:
        sess = s.get(SessionRow, out.session_id)
        assert sess is not None
        assert sess.release_id == rel and sess.eval_run_id == run_id
        assert sess.client == "satchel-eval" and sess.ended_at is not None
        assert sess.status == "answered"  # report_answer ran: a normal session


def test_harness_step_cap_and_tool_errors(env: Env) -> None:
    loop = FakeChatProvider(turns=lambda s, m, t: ModelTurn(tool_uses=[
        ToolUse("x", "get_item", {"key": "nope", "question": "q"})]))
    out = run_agent(env.engine, EMB, env.principal("analyst"), loop, "hello",
                    release_id=env.head(), eval_run_id=uuid.uuid4(), max_steps=3)
    assert out.hit_step_limit and out.error and "step limit" in out.error
    assert out.steps == 3 and all(t.is_error for t in out.trace)


def test_harness_pins_tools_to_the_preview(env: Env) -> None:
    """get_item / search served by a pinned preview see the preview, not the published head."""
    env.client.post("/v1/items", json={
        "key": "guides/zanzibar.md", "type": "guide",
        "body": "# Zanzibar\n\nThe zanzibar surcharge applies to island orders.",
        "meta": {"name": "Zanzibar", "description": "d", "tags": []}}, headers=env.h("curator"))
    prev = env.client.post("/v1/previews", json={}, headers=env.h("curator")).json()
    plan = {"zq": ([("start_analysis", {}), ("get_item", {"key": "guides/zanzibar.md"})], "done")}
    on_preview = run_agent(env.engine, EMB, env.principal("analyst"), fake_agent(plan), "zq",
                           release_id=uuid.UUID(prev["id"]), eval_run_id=uuid.uuid4())
    on_head = run_agent(env.engine, EMB, env.principal("analyst"), fake_agent(plan), "zq",
                        release_id=env.head(), eval_run_id=uuid.uuid4())
    assert not on_preview.trace[1].is_error and "zanzibar surcharge" in on_preview.trace[1].result
    assert on_head.trace[1].is_error


# ----------------------------------------------------------------------------- runs via the API


def test_run_suite_grades_and_hides_eval_sessions(env: Env) -> None:
    put_suite(env)
    before = env.client.get("/v1/observability/summary", headers=env.h("curator")).json()
    head = env.head()
    run = run_eval(env, "mini", head)
    assert run["status"] == "completed" and run["llm"] is True
    assert run["cases_total"] == 3 and run["cases_pass"] == 3, run
    m = {x["measure"]: x for x in run["measures"]}
    assert m["correctness"]["pass_rate"] == 1.0 and m["honesty"]["pass_rate"] == 1.0
    assert run["tokens"]["input"] > 0 and run["model"] == "fake:scripted"

    d = env.client.get(f"/v1/evals/runs/{run['id']}", headers=env.h("explorer")).json()
    rev = next(c for c in d["cases"] if c["case_key"] == "revenue")
    assert len(rev["attempts"]) == 2 and rev["outcome"] == "pass"
    att = rev["attempts"][0]
    assert att["session_id"] and "1,716.15" in att["answer"]
    num = next(x for x in att["grades"] if x["grader"] == "numeric_value")
    assert num["outcome"] == "pass" and "1716.15" in num["reasoning"]
    assert att["cost"]["input_tokens"] > 0 and att["cost"]["tool_calls"] == 4

    # eval sessions: recorded, tagged, and excluded from observability / signals / curation
    with admin_session(engine=env.engine) as s:
        ids = list(s.scalars(select(SessionRow.id).where(SessionRow.eval_run_id == run["id"])))
        assert len(ids) == 4  # revenue x2 attempts, top, churn
    after = env.client.get("/v1/observability/summary", headers=env.h("curator")).json()
    assert after == before
    page = env.client.get("/v1/sessions", headers=env.h("admin")).json()
    assert not {x["id"] for x in page["sessions"]} & {str(i) for i in ids}
    incl = env.client.get("/v1/sessions?include_evals=true", headers=env.h("admin")).json()
    assert {str(i) for i in ids} <= {x["id"] for x in incl["sessions"]}
    with admin_session(engine=env.engine) as s:
        raw = s.scalar(select(func.count()).select_from(Signal).where(
            Signal.session_id.in_(ids)))
        visible = s.scalar(select(func.count()).select_from(Signal).where(
            Signal.session_id.in_(ids), signal_not_eval()))
    assert raw and raw > 0 and visible == 0
    # but the session is still inspectable directly from the run detail's link
    assert env.client.get(f"/v1/sessions/{att['session_id']}",
                          headers=env.h("admin")).status_code == 200


def test_failing_answers_and_attempt_tolerance(env: Env) -> None:
    put_suite(env)
    plans = dict(PLANS)
    plans["What was revenue in March 2025?"] = rev_plan("Revenue was **$9,999**.")
    bad = TestClient(create_app(env.engine, EMB, settings=Settings(dev_auth=True),
                                chat=fake_agent(plans)))
    r = bad.post("/v1/evals/runs", json={"suite_key": "mini", "release_id": str(env.head()),
                                         "wait": True}, headers=env.h("analyst"))
    run = r.json()
    assert run["cases_fail"] == 1 and run["cases_pass"] == 2
    d = bad.get(f"/v1/evals/runs/{run['id']}", headers=env.h("explorer")).json()
    rev = next(c for c in d["cases"] if c["case_key"] == "revenue")
    assert rev["outcome"] == "fail"
    grades = {x["grader"]: x for x in rev["attempts"][0]["grades"]}
    assert grades["numeric_value"]["outcome"] == "fail"
    assert grades["judge_session"]["outcome"] == "fail"  # the scripted judge saw $9,999
    # warn_only retrieval rubric passed independently of the agent
    assert grades["context_used"]["outcome"] == "pass"
    cmp = env.client.get(f"/v1/evals/compare?a={run['id']}&b={run['id']}",
                         headers=env.h("explorer"))
    assert cmp.status_code == 200 and cmp.json()["newly_failing"] == []


def test_compare_runs_shows_newly_failing_cases(env: Env) -> None:
    put_suite(env)
    good = run_eval(env, "mini", env.head())
    plans = dict(PLANS)
    plans["What was revenue in March 2025?"] = rev_plan("Revenue was **$9,999**.")
    bad_client = TestClient(create_app(env.engine, EMB, settings=Settings(dev_auth=True),
                                       chat=fake_agent(plans)))
    bad = bad_client.post("/v1/evals/runs", json={
        "suite_key": "mini", "release_id": str(env.head()), "wait": True},
        headers=env.h("analyst")).json()
    cmp = env.client.get(f"/v1/evals/compare?a={good['id']}&b={bad['id']}",
                         headers=env.h("explorer")).json()
    corr = next(x for x in cmp["measures"] if x["measure"] == "correctness")
    assert corr["a"] == 1.0 and corr["b"] == 0.5 and corr["delta"] < 0
    assert [c["case"] for c in cmp["newly_failing"]] == ["revenue"]
    assert cmp["a"]["id"] == good["id"]


def test_no_llm_runs_retrieval_suite_and_skips_agent_cases(
    app_engine: Engine, tmp_path: Path, warehouse: Path
) -> None:
    e = make_env(app_engine, tmp_path, warehouse, chat=None, prefix="nollm")
    r = e.client.get("/v1/evals/suites", headers=e.h("explorer")).json()
    by_key = {x["key"]: x for x in r}
    assert by_key["retrieval"]["needs_agent"] is False and by_key["core"]["needs_agent"] is True
    run = run_eval(e, "retrieval", e.head())
    assert run["status"] == "completed" and run["llm"] is False and run["model"] is None
    assert run["cases_total"] == 8 and run["cases_pass"] == 8, run
    assert run["tokens"] == {"input": 0, "output": 0}
    with admin_session(engine=e.engine) as s:
        assert s.scalar(select(func.count()).select_from(SessionRow).where(
            SessionRow.workspace_id == e.ws_id)) == 0  # no agent, no sessions
    # an agent suite without an LLM is skipped, not failed (context_used warn-only still runs)
    put_suite(e)
    skipped = run_eval(e, "mini", e.head())
    assert skipped["cases_skipped"] == 3 and skipped["cases_fail"] == 0


def test_run_against_preview_vs_published(env: Env) -> None:
    put_suite(env, """
id: ctx
cases:
  - id: rev
    prompt: What was revenue in March 2025?
    rubrics:
      - {grader: context_used, items: ["glossary/business-terms.yaml#revenue"], top_n: 5}
""")
    _git_to_ui(env, REVENUE_KEY)
    assert env.client.post(f"/v1/items/{k(REVENUE_KEY)}/deprecate",
                           headers=env.h("curator")).status_code == 200
    prev = env.client.post("/v1/previews", json={}, headers=env.h("curator")).json()
    pub = run_eval(env, "ctx", env.head())
    pre = run_eval(env, "ctx", uuid.UUID(prev["id"]))
    assert pub["cases_pass"] == 1 and pre["cases_fail"] == 1
    assert pre["release_kind"] == "preview" and pub["release_kind"] == "published"


def _git_to_ui(env: Env, key: str) -> None:
    """Make a git-imported item UI-editable (a workspace bootstrapped via `sync export`)."""
    with admin_session(engine=env.engine) as s:
        s.execute(update(ContextItem).where(ContextItem.workspace_id == env.ws_id,
                                            ContextItem.key == key).values(managed_by="ui"))


# ----------------------------------------------------------------------------- the publish gate


@pytest.fixture
def gated(app_engine: Engine, tmp_path: Path, warehouse: Path) -> Env:
    return make_env(app_engine, tmp_path, warehouse, gate=True, chat=None, prefix="gate")


def _preview_without_revenue(e: Env) -> dict[str, Any]:
    _git_to_ui(e, REVENUE_KEY)
    r = e.client.post(f"/v1/items/{k(REVENUE_KEY)}/deprecate", headers=e.h("curator"))
    assert r.status_code == 200, r.text
    p = e.client.post("/v1/previews", json={}, headers=e.h("curator"))
    assert p.status_code == 201, p.text
    out: dict[str, Any] = p.json()
    return out


def test_importer_registers_suites_and_gate_config(gated: Env) -> None:
    suites = {x["key"]: x for x in gated.client.get(
        "/v1/evals/suites", headers=gated.h("explorer")).json()}
    assert set(suites) == {"core", "retrieval"}
    assert suites["retrieval"]["managed_by"] == "git" and suites["retrieval"]["required_by_gate"]
    assert not suites["core"]["required_by_gate"]
    with admin_session(engine=gated.engine) as s:
        ws = s.get(Workspace, gated.ws_id)
        assert ws is not None and ws.settings["publish"]["require_evals"] == ["retrieval"]
    r = gated.client.post("/v1/evals/suites", json={"yaml": "id: retrieval\ncases:\n"
                          "  - {id: a, prompt: q, rubrics: [{grader: context_used, items: [x]}]}"},
                          headers=gated.h("curator"))
    assert r.status_code == 409 and "git" in r.text
    detail = gated.client.get("/v1/evals/suites/retrieval", headers=gated.h("explorer")).json()
    assert detail["versions"] == [1] and detail["spec"]["cases"][0]["id"] == "revenue"


def test_reimport_unchanged_does_not_bump_suite_version(
    gated: Env, tmp_path: Path
) -> None:
    import_context(EXAMPLE_CONTEXT, gated.slug, embedder=EMB, engine=gated.engine)
    with admin_session(engine=gated.engine) as s:
        vers = list(s.scalars(select(EvalSuite.version_seq).where(
            EvalSuite.workspace_id == gated.ws_id, EvalSuite.key == "core")))
    assert vers == [1]


def test_gate_blocks_regression_and_admin_override_is_audited(gated: Env) -> None:
    prev = _preview_without_revenue(gated)
    pid = prev["id"]
    # not run yet: blocked as pending
    gate = gated.client.get(f"/v1/previews/{pid}/evals", headers=gated.h("explorer")).json()
    assert gate["configured"] and gate["status"] == "pending"
    blocked = gated.client.post(f"/v1/previews/{pid}/publish", headers=gated.h("curator"))
    assert blocked.status_code == 409 and "no results" in blocked.json()["detail"]

    gate = gated.client.post(f"/v1/previews/{pid}/evals", json={"wait": True},
                             headers=gated.h("analyst")).json()
    assert gate["status"] == "fail", gate
    suite = gate["suites"][0]
    assert suite["suite_key"] == "retrieval" and suite["baseline_run_id"]
    reg = suite["regressions"][0]
    assert reg["measure"] == "retrieval" and reg["a"] == 1.0 and reg["b"] < 1.0
    assert [c["case"] for c in suite["newly_failing"]] == ["revenue"]

    blocked = gated.client.post(f"/v1/previews/{pid}/publish", headers=gated.h("curator"))
    assert blocked.status_code == 409
    assert "blocked publish" in blocked.json()["detail"] and "retrieval" in blocked.json()["detail"]
    # a curator cannot override; an admin can, with a reason, and it is audited
    body = {"override_reason": "revenue term is being rewritten"}
    denied = gated.client.post(f"/v1/previews/{pid}/publish", json=body, headers=gated.h("curator"))
    assert denied.status_code == 403
    ok = gated.client.post(f"/v1/previews/{pid}/publish", json=body, headers=gated.h("admin"))
    assert ok.status_code == 200 and ok.json()["kind"] == "published"
    with admin_session(engine=gated.engine) as s:
        ev = [e.target for e in s.scalars(select(AuditEvent).where(
            AuditEvent.workspace_id == gated.ws_id,
            AuditEvent.action == "release.publish_override"))]
    assert len(ev) == 1 and ev[0]["reason"] == body["override_reason"]
    assert ev[0]["gate"] == "fail"


def test_gate_passes_for_harmless_change_and_reuses_the_baseline(gated: Env) -> None:
    gated.client.post("/v1/items", json={
        "key": "guides/zanzibar.md", "type": "guide",
        "body": "# Zanzibar\n\nThe zanzibar surcharge applies to island orders.",
        "meta": {"name": "Zanzibar", "description": "d", "tags": []}}, headers=gated.h("curator"))
    p1 = gated.client.post("/v1/previews", json={}, headers=gated.h("curator")).json()
    gate = gated.client.post(f"/v1/previews/{p1['id']}/evals", json={"wait": True},
                             headers=gated.h("analyst")).json()
    assert gate["status"] == "pass", gate
    assert gate["suites"][0]["regressions"] == []
    # an unneeded override is rejected rather than silently audited
    junk = gated.client.post(f"/v1/previews/{p1['id']}/publish", json={"override_reason": "x"},
                             headers=gated.h("admin"))
    assert junk.status_code == 422
    ok = gated.client.post(f"/v1/previews/{p1['id']}/publish", headers=gated.h("curator"))
    assert ok.status_code == 200

    # a second preview on the *new* head needs a new baseline; two previews on one head share it
    gated.client.post("/v1/items", json={
        "key": "guides/a.md", "type": "guide", "body": "# A\n\nalpha",
        "meta": {"name": "A", "description": "d", "tags": []}}, headers=gated.h("curator"))
    pa = gated.client.post("/v1/previews", json={"keys": ["guides/a.md"]},
                           headers=gated.h("curator")).json()
    gated.client.post("/v1/items", json={
        "key": "guides/b.md", "type": "guide", "body": "# B\n\nbeta",
        "meta": {"name": "B", "description": "d", "tags": []}}, headers=gated.h("curator"))
    pb = gated.client.post("/v1/previews", json={"keys": ["guides/b.md"]},
                           headers=gated.h("curator")).json()
    for p in (pa, pb):
        r = gated.client.post(f"/v1/previews/{p['id']}/evals", json={"wait": True},
                              headers=gated.h("analyst")).json()
        assert r["status"] == "pass"
    with admin_session(engine=gated.engine) as s:
        base_runs = s.scalar(select(func.count()).select_from(EvalRun).where(
            EvalRun.release_id == pa["parent_release_id"]))
    assert base_runs == 1  # the second preview reused the first one's baseline run


def test_gate_goes_stale_when_the_suite_changes(gated: Env) -> None:
    gated.client.post("/v1/items", json={
        "key": "guides/c.md", "type": "guide", "body": "# C\n\ngamma",
        "meta": {"name": "C", "description": "d", "tags": []}}, headers=gated.h("curator"))
    p = gated.client.post("/v1/previews", json={}, headers=gated.h("curator")).json()
    assert gated.client.post(f"/v1/previews/{p['id']}/evals", json={"wait": True},
                             headers=gated.h("analyst")).json()["status"] == "pass"
    with admin_session(engine=gated.engine) as s:  # a newer suite version appears
        row = s.scalars(select(EvalSuite).where(EvalSuite.workspace_id == gated.ws_id,
                                                EvalSuite.key == "retrieval")).one()
        s.add(EvalSuite(workspace_id=gated.ws_id, key="retrieval", version_seq=2, name="r",
                        spec=row.spec, content_hash="changed", managed_by="git"))
    now = gated.client.get(f"/v1/previews/{p['id']}/evals", headers=gated.h("explorer")).json()
    assert now["status"] == "pending" and now["suites"][0]["status"] == "stale"
    assert gated.client.post(f"/v1/previews/{p['id']}/publish",
                             headers=gated.h("curator")).status_code == 409


def test_no_required_suites_means_no_gate(env: Env) -> None:
    env.client.post("/v1/items", json={
        "key": "guides/d.md", "type": "guide", "body": "# D\n\ndelta",
        "meta": {"name": "D", "description": "d", "tags": []}}, headers=env.h("curator"))
    p = env.client.post("/v1/previews", json={}, headers=env.h("curator")).json()
    gate = env.client.get(f"/v1/previews/{p['id']}/evals", headers=env.h("explorer")).json()
    assert gate == {"preview_id": p["id"], "configured": False, "status": "none",
                    "threshold": 0.0, "suites": []}
    assert env.client.post(f"/v1/previews/{p['id']}/evals", json={"wait": True},
                           headers=env.h("analyst")).json()["status"] == "none"
    assert env.client.post(f"/v1/previews/{p['id']}/publish",
                           headers=env.h("curator")).status_code == 200


# ----------------------------------------------------------------------------- roles


def test_roles(env: Env) -> None:
    rank = {"explorer": 0, "analyst": 1, "curator": 2, "admin": 3}
    rid, bogus = str(uuid.uuid4()), str(uuid.uuid4())
    put_suite(env)
    cases: list[tuple[str, str, dict[str, Any] | None, str]] = [
        ("GET", "/v1/evals/suites", None, "explorer"),
        ("GET", "/v1/evals/suites/mini", None, "explorer"),
        ("POST", "/v1/evals/suites", {"yaml": SUITE_YAML}, "curator"),
        ("POST", "/v1/evals/runs", {"suite_key": "mini", "release_id": bogus}, "analyst"),
        ("GET", "/v1/evals/runs", None, "explorer"),
        ("GET", f"/v1/evals/runs/{rid}", None, "explorer"),
        ("GET", f"/v1/evals/compare?a={rid}&b={bogus}", None, "explorer"),
        ("GET", f"/v1/previews/{rid}/evals", None, "explorer"),
        ("POST", f"/v1/previews/{rid}/evals", {}, "analyst"),
    ]
    for method, path, body, minimum in cases:
        assert env.client.request(method, path, json=body).status_code == 401
        for role in ROLES:
            r = env.client.request(method, path, json=body, headers=env.h(role))
            if rank[role] < rank[minimum]:
                assert r.status_code == 403, (method, path, role, r.text)
            else:
                assert r.status_code != 403, (method, path, role, r.text)


def test_invalid_suite_yaml_is_a_422(env: Env) -> None:
    r = env.client.post("/v1/evals/suites", json={"yaml": "id: x\ncases: []"},
                        headers=env.h("curator"))
    assert r.status_code == 422
    r = env.client.post("/v1/evals/runs", json={"suite_key": "nope", "release_id": str(env.head())},
                        headers=env.h("analyst"))
    assert r.status_code == 404


def test_eval_rows_are_workspace_isolated(env: Env, app_engine: Engine) -> None:
    put_suite(env)
    run = run_eval(env, "retrieval", env.head())
    other = uuid.uuid4()
    with admin_session(engine=app_engine) as s:
        s.add(Workspace(id=other, slug=unique_slug("iso"), name="iso"))
    with workspace_session(other, engine=app_engine) as s:
        assert s.scalar(select(func.count()).select_from(EvalRun)) == 0
        assert s.scalar(select(func.count()).select_from(EvalSuite)) == 0
        assert s.scalar(select(func.count()).select_from(EvalCaseResult)) == 0
    with workspace_session(env.ws_id, engine=app_engine) as s:
        assert s.get(EvalRun, uuid.UUID(run["id"])) is not None
