"""Eval runner (design §10): run a suite against a release, aggregate, compare.

A suite made only of ``context_used`` rubrics runs without any LLM, so it doubles as a cheap
retrieval regression check in CI. Cases run with limited concurrency (4); a case's attempts
run one after another.
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import Engine

from satchel.evals.graders import (
    Grade,
    GradeEnv,
    attempt_outcome,
    combine_attempts,
    grade_rubric,
)
from satchel.evals.harness import (
    DEFAULT_MAX_STEPS,
    AgentRun,
    SessionEvidence,
    run_agent,
    session_evidence,
)
from satchel.evals.store import suite_of
from satchel.evals.suite import Case, Suite
from satchel.llm.chat import ChatProvider, ToolChatProvider
from satchel.llm.embeddings import EmbeddingProvider
from satchel.mcp.auth import Principal
from satchel.store.db import workspace_session
from satchel.store.models import EvalCaseResult, EvalRun, EvalSuite

log = logging.getLogger("satchel.evals")

CONCURRENCY = 4


class EvalProvider(ToolChatProvider, ChatProvider, Protocol):
    """Drives the agent loop and judges answers."""


def _now() -> datetime:
    return datetime.now(UTC)


def _case_and_rubric_outcomes(
    case: Case, attempts: list[list[Grade]]
) -> tuple[str, list[dict[str, Any]]]:
    outs = [attempt_outcome(g) for g in attempts]
    rubrics: list[dict[str, Any]] = []
    for i, r in enumerate(case.rubrics):
        per = [g[i].outcome for g in attempts]
        rubrics.append({
            "index": i, "grader": r.grader, "measure": r.measure, "warn_only": r.warn_only,
            "outcome": combine_attempts(per),
        })
    return combine_attempts(outs), rubrics


def measure_stats(case_summaries: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-measure counts and pass rate over non-warn rubrics; warn_only failures are counted
    separately and never move the pass rate."""
    out: dict[str, dict[str, Any]] = {}
    for cs in case_summaries.values():
        for r in cs["rubrics"]:
            m = out.setdefault(str(r["measure"]), {
                "passed": 0, "failed": 0, "errored": 0, "skipped": 0, "warnings": 0,
            })
            if r["warn_only"]:
                if r["outcome"] in ("fail", "error"):
                    m["warnings"] += 1
                continue
            key = {"pass": "passed", "fail": "failed", "error": "errored",
                   "skipped": "skipped"}[r["outcome"]]
            m[key] += 1
    for m in out.values():
        denom = m["passed"] + m["failed"] + m["errored"]
        m["pass_rate"] = (m["passed"] / denom) if denom else None
    return out


def summarize(
    case_summaries: dict[str, dict[str, Any]], *, steps: int, usage: dict[str, int],
    seconds: float, needs_agent: bool, llm: bool,
) -> dict[str, Any]:
    counts = {"pass": 0, "fail": 0, "error": 0, "skipped": 0}
    for cs in case_summaries.values():
        counts[cs["outcome"]] += 1
    return {
        "cases": {"total": len(case_summaries), **counts},
        "measures": measure_stats(case_summaries),
        "case_outcomes": case_summaries,
        "steps": steps, "tokens": usage, "seconds": round(seconds, 2),
        "needs_agent": needs_agent, "llm": llm,
    }


class _Ctx:
    def __init__(
        self, engine: Engine, embedder: EmbeddingProvider, principal: Principal,
        provider: EvalProvider | None, run_id: uuid.UUID, release_id: uuid.UUID,
        max_steps: int,
    ) -> None:
        self.engine, self.embedder, self.principal = engine, embedder, principal
        self.provider, self.run_id, self.release_id = provider, run_id, release_id
        self.max_steps = max_steps
        self.env = GradeEnv(
            engine, embedder, principal.workspace_id, principal.user_id, release_id, provider
        )


def _store_attempt(
    c: _Ctx, case: Case, attempt: int, grades: list[Grade], run: AgentRun | None
) -> None:
    outcome = attempt_outcome(grades)
    with workspace_session(c.principal.workspace_id, c.principal.user_id, engine=c.engine) as s:
        s.add(EvalCaseResult(
            workspace_id=c.principal.workspace_id, run_id=c.run_id, case_key=case.id,
            attempt=attempt, outcome=outcome, session_id=run.session_id if run else None,
            answer=run.answer if run else None,
            grades=[{
                "grader": g.grader, "measure": g.measure, "warn_only": g.warn_only,
                "outcome": g.outcome, "reasoning": g.reasoning, "details": g.details,
            } for g in grades],
            cost=run.cost() if run else {},
        ))


def _run_case(c: _Ctx, case: Case) -> tuple[Case, list[list[Grade]], list[AgentRun]]:
    attempts = case.attempts if case.needs_agent else 1
    all_grades: list[list[Grade]] = []
    runs: list[AgentRun] = []
    cached_ctx: dict[int, Grade] = {}
    for attempt in range(1, attempts + 1):
        run: AgentRun | None = None
        ev: SessionEvidence | None = None
        if case.needs_agent and c.provider is not None:
            try:
                run = run_agent(
                    c.engine, c.embedder, c.principal, c.provider, case.prompt,
                    release_id=c.release_id, eval_run_id=c.run_id, max_steps=c.max_steps,
                )
            except Exception as e:  # keep the run going; the case becomes an error
                log.exception("eval agent crashed on %s", case.id)
                run = AgentRun(error=f"agent crashed: {type(e).__name__}: {e}")
            runs.append(run)
            ev = session_evidence(c.engine, c.principal, run.session_id)
        grades: list[Grade] = []
        for i, r in enumerate(case.rubrics):
            if r.grader == "context_used" and i in cached_ctx:  # deterministic: grade once
                grades.append(cached_ctx[i])
                continue
            g = grade_rubric(r, case, run, ev, c.env)
            if r.grader == "context_used":
                cached_ctx[i] = g
            grades.append(g)
        _store_attempt(c, case, attempt, grades, run)
        all_grades.append(grades)
    return case, all_grades, runs


def create_run(
    engine: Engine,
    principal: Principal,
    suite_row: EvalSuite,
    release_id: uuid.UUID,
    *,
    provider: EvalProvider | None = None,
    note: dict[str, Any] | None = None,
    record_starter: bool = True,
) -> uuid.UUID:
    """Insert the ``running`` eval_run row (so a caller can return before the run finishes).
    ``record_starter=False`` leaves ``started_by`` null (scheduled runs)."""
    suite = suite_of(suite_row)
    with workspace_session(principal.workspace_id, principal.user_id, engine=engine) as s:
        run = EvalRun(
            workspace_id=principal.workspace_id, suite_id=suite_row.id, release_id=release_id,
            model=provider.model_id if (provider and suite.needs_agent) else None,
            started_by=principal.user_id if record_starter else None, summary=dict(note or {}),
        )
        s.add(run)
        s.flush()
        return run.id


def execute_run(
    engine: Engine,
    embedder: EmbeddingProvider,
    principal: Principal,
    run_id: uuid.UUID,
    *,
    provider: EvalProvider | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    concurrency: int = CONCURRENCY,
) -> None:
    """Run the cases of an existing ``running`` eval_run and store the summary."""
    with workspace_session(principal.workspace_id, principal.user_id, engine=engine) as s:
        run = s.get(EvalRun, run_id)
        assert run is not None
        suite_row = s.get(EvalSuite, run.suite_id)
        assert suite_row is not None
        release_id, prior = run.release_id, dict(run.summary or {})
        suite: Suite = suite_of(suite_row)
    llm_needed = suite.needs_agent
    c = _Ctx(engine, embedder, principal, provider, run_id, release_id, max_steps)
    t0 = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            done = list(pool.map(lambda case: _run_case(c, case), suite.cases))
        summaries: dict[str, dict[str, Any]] = {}
        steps = tin = tout = 0
        for case, grades, runs in done:
            outcome, rubrics = _case_and_rubric_outcomes(case, grades)
            summaries[case.id] = {"outcome": outcome, "rubrics": rubrics,
                                  "tags": case.tags, "attempts": len(grades)}
            for r in runs:
                steps += r.steps
                tin += r.usage.input_tokens
                tout += r.usage.output_tokens
        summary = summarize(
            summaries, steps=steps, usage={"input": tin, "output": tout},
            seconds=time.perf_counter() - t0, needs_agent=llm_needed,
            llm=provider is not None and llm_needed,
        )
        status = "completed"
    except Exception as e:
        log.exception("eval run %s failed", run_id)
        summary, status = {"error": f"{type(e).__name__}: {e}"}, "failed"
    with workspace_session(principal.workspace_id, principal.user_id, engine=engine) as s:
        row = s.get(EvalRun, run_id)
        assert row is not None
        row.status, row.summary, row.finished_at = status, {**prior, **summary}, _now()


def run_suite(
    engine: Engine,
    embedder: EmbeddingProvider,
    principal: Principal,
    suite_row: EvalSuite,
    release_id: uuid.UUID,
    *,
    provider: EvalProvider | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    concurrency: int = CONCURRENCY,
) -> uuid.UUID:
    """Run every case of ``suite_row`` against ``release_id``. Returns the run id."""
    run_id = create_run(engine, principal, suite_row, release_id, provider=provider)
    execute_run(engine, embedder, principal, run_id, provider=provider, max_steps=max_steps,
                concurrency=concurrency)
    return run_id


# ----------------------------------------------------------------------------- compare


def compare_summaries(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """``a`` is the baseline, ``b`` the candidate: per-measure pass rates and case movement."""
    ma, mb = a.get("measures", {}), b.get("measures", {})
    measures = []
    for name in sorted(set(ma) | set(mb)):
        ra = (ma.get(name) or {}).get("pass_rate")
        rb = (mb.get(name) or {}).get("pass_rate")
        delta = (rb - ra) if (ra is not None and rb is not None) else None
        measures.append({"measure": name, "a": ra, "b": rb, "delta": delta})
    ca, cb = a.get("case_outcomes", {}), b.get("case_outcomes", {})
    newly_failing, newly_passing = [], []
    for key in sorted(set(ca) & set(cb)):
        oa, ob = ca[key]["outcome"], cb[key]["outcome"]
        if oa == "pass" and ob in ("fail", "error"):
            newly_failing.append({"case": key, "a": oa, "b": ob})
        elif oa in ("fail", "error") and ob == "pass":
            newly_passing.append({"case": key, "a": oa, "b": ob})
    return {
        "measures": measures, "newly_failing": newly_failing, "newly_passing": newly_passing,
        "only_in_a": sorted(set(ca) - set(cb)), "only_in_b": sorted(set(cb) - set(ca)),
    }

