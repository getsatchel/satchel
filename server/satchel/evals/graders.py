"""Graders (design §10). Each returns a ``Grade`` (pass / fail / error / skipped + reasoning).

``numeric_value``, ``sql_equivalent`` and ``context_used`` are deterministic; the judges use an
LLM. ``context_used`` needs no agent run. ``numeric_value`` only falls back to the LLM to
extract the number when there is an ``extraction_guidance`` or no number can be found.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine

from satchel import limits
from satchel.data.connections import ConnectionError_, open_connector, pick_connection
from satchel.data.gateway import run_guarded
from satchel.evals.harness import AgentRun, SessionEvidence, trace_text
from satchel.evals.suite import Case, Rubric
from satchel.llm.chat import ChatError, ChatProvider
from satchel.llm.embeddings import EmbeddingProvider
from satchel.retrieval import build_context_pack
from satchel.settings import get_settings
from satchel.store.db import workspace_session
from satchel.store.models import Connection

log = logging.getLogger("satchel.evals")

Outcome = str  # "pass" | "fail" | "error" | "skipped"
DEFAULT_TOLERANCE_PCT = 0.5


@dataclass
class Grade:
    grader: str
    measure: str
    warn_only: bool
    outcome: Outcome
    reasoning: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class GradeEnv:
    """Everything a grader needs besides the rubric: data access, retrieval, an LLM judge."""

    engine: Engine
    embedder: EmbeddingProvider
    workspace_id: uuid.UUID
    user_id: uuid.UUID | None
    release_id: uuid.UUID
    judge: ChatProvider | None = None


def _grade(r: Rubric, outcome: Outcome, reasoning: str, **details: Any) -> Grade:
    return Grade(r.grader, str(r.measure), r.warn_only, outcome, reasoning, details)


# ----------------------------------------------------------------------------- SQL access


def run_expected_sql(
    env: GradeEnv, sql: str, connection: str | None, connection_id: uuid.UUID | None = None
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Execute ground-truth SQL through the data gateway. Raises ``ValueError`` on failure."""
    st = get_settings()
    with workspace_session(env.workspace_id, env.user_id, engine=env.engine) as s:
        try:
            if connection is None and connection_id is not None:
                conn: Connection | None = s.get(Connection, connection_id)
                if conn is None:
                    raise ConnectionError_("the connection used by the session no longer exists")
            else:
                conn = pick_connection(s, connection)
            connector = open_connector(conn)
        except ConnectionError_ as e:
            raise ValueError(str(e)) from None
        lim = limits.for_workspace(s, env.workspace_id, st)
    try:
        res = run_guarded(connector, sql, row_cap=lim.sql_row_cap, timeout_s=lim.sql_timeout_s)
    except Exception as e:
        raise ValueError(f"{type(e).__name__}: {e}") from None
    finally:
        connector.close()
    data = res.table.to_pydict()
    cols = res.table.column_names
    n = res.table.num_rows
    return cols, [tuple(data[c][i] for c in cols) for i in range(n)]


# ----------------------------------------------------------------------------- numeric_value

_NUM = re.compile(
    r"(?<![\w.])(?P<neg>[-−(])?\s*(?P<cur>[$€£]|A\$|AUD\s?)?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    r"\s*(?P<suf>[kKmMbB]\b|%|\s?(?:million|billion|thousand)\b)?"
)
_DATE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?:\d{1,2},?\s+)?(?:19|20)\d\d\b"
    r"|\b(?:19|20)\d\d-\d\d(?:-\d\d)?\b|\bQ[1-4]\s+(?:19|20)\d\d\b|\b(?:19|20)\d\d\b(?=\s*(?:q[1-4]|\)|$))",
    re.I,
)
_SUFFIX = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}


@dataclass
class _Cand:
    value: float
    bold: bool
    currency: bool
    pos: int


def extract_numbers(text: str) -> list[_Cand]:
    """Numbers in the answer, in order, ignoring dates and list markers."""
    cleaned = _DATE.sub(lambda m: " " * len(m.group(0)), text)
    cleaned = re.sub(r"(?m)^\s*(?:\d+[.)]|[-*•])\s+", lambda m: " " * len(m.group(0)), cleaned)
    out: list[_Cand] = []
    for m in _NUM.finditer(cleaned):
        raw = m.group("num").replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        suf = (m.group("suf") or "").strip().lower()
        if suf in _SUFFIX:
            v *= _SUFFIX[suf]
        if m.group("neg") in ("-", "−"):
            v = -v
        before = cleaned[max(0, m.start() - 2): m.start()]
        after = cleaned[m.end(): m.end() + 2]
        bold = "**" in before + after or "__" in before + after
        out.append(_Cand(v, bold, bool(m.group("cur")), m.start()))
    return out


def headline_number(text: str) -> float | None:
    """The headline figure: first bolded number, else first currency amount, else first."""
    cands = extract_numbers(text)
    if not cands:
        return None
    for pick in (lambda c: c.bold, lambda c: c.currency, lambda c: True):
        for c in cands:
            if pick(c):
                return c.value
    return None


def _llm_extract(env: GradeEnv, answer: str, guidance: str | None) -> float | None:
    if env.judge is None:
        return None
    schema = {"type": "object", "properties": {"value": {"type": ["number", "null"]}},
              "required": ["value"]}
    system = ("Extract one number from an analyst's answer. Return null if the answer gives no "
              "number for what is asked. Convert units as instructed. Return only the number.")
    try:
        out = env.judge.complete_json(
            system, [{"role": "user", "content": f"Instruction: {guidance or 'the headline figure'}"
                      f"\n\nAnswer:\n{answer}"}], schema, max_tokens=200)
    except ChatError:
        return None
    v = out.get("value")
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def within(actual: float, expected: float, pct: float | None, abs_: float | None) -> bool:
    if pct is None and abs_ is None:
        pct = DEFAULT_TOLERANCE_PCT
    ok = False
    if abs_ is not None:
        ok = ok or abs(actual - expected) <= abs_
    if pct is not None:
        denom = abs(expected)
        ok = ok or (abs(actual - expected) <= 1e-9 if denom == 0
                    else abs(actual - expected) / denom * 100 <= pct)
    return ok


def scalar(rows: list[tuple[Any, ...]]) -> float:
    if not rows or not rows[0]:
        raise ValueError("the expected SQL returned no rows")
    v = rows[0][0]
    if isinstance(v, bool) or not isinstance(v, int | float | Decimal):
        raise ValueError(f"the expected SQL's first value is not numeric: {v!r}")
    return float(v)


def grade_numeric(r: Rubric, run: AgentRun, env: GradeEnv) -> Grade:
    try:
        if r.expected_sql:
            _, rows = run_expected_sql(env, r.expected_sql, r.connection)
            expected = scalar(rows)
        else:
            expected = float(r.expected)  # type: ignore[arg-type]
    except ValueError as e:
        return _grade(r, "error", f"could not compute the expected value: {e}")
    method = "deterministic"
    actual: float | None
    if r.extraction_guidance:
        actual = _llm_extract(env, run.answer, r.extraction_guidance)
        method = "llm"
        if actual is None:
            actual, method = headline_number(run.answer), "deterministic"
    else:
        actual = headline_number(run.answer)
        if actual is None:
            actual, method = _llm_extract(env, run.answer, None), "llm-fallback"
    if actual is None:
        return _grade(r, "fail", "no number found in the final answer", expected=expected)
    ok = within(actual, expected, r.tolerance_pct, r.tolerance_abs)
    tol = ", ".join(x for x in (
        f"{r.tolerance_pct}%" if r.tolerance_pct is not None else "",
        f"abs {r.tolerance_abs}" if r.tolerance_abs is not None else "",
    ) if x) or f"{DEFAULT_TOLERANCE_PCT}% (default)"
    return _grade(
        r, "pass" if ok else "fail",
        f"answer {actual:g} vs expected {expected:g} (tolerance {tol}; extracted {method})",
        actual=actual, expected=expected,
    )


# ----------------------------------------------------------------------------- sql_equivalent


def _norm(v: Any) -> Any:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int | float | Decimal):
        return round(float(v), 4)
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v).strip()


def _norm_rows(rows: list[tuple[Any, ...]], ordered: bool) -> list[tuple[Any, ...]]:
    out = [tuple(_norm(v) for v in row) for row in rows]
    return out if ordered else sorted(out, key=lambda t: tuple(repr(x) for x in t))


def compare_results(
    a: list[tuple[Any, ...]], b: list[tuple[Any, ...]], *, ordered: bool
) -> str | None:
    """None when equal, else why not."""
    if len(a) != len(b):
        return f"row counts differ: {len(a)} vs {len(b)}"
    na, nb = _norm_rows(a, ordered), _norm_rows(b, ordered)
    if na != nb:
        for i, (x, y) in enumerate(zip(na, nb, strict=True)):
            if x != y:
                return f"first difference at row {i + 1}: {x} vs {y}"
    return None


def grade_sql_equivalent(
    r: Rubric, run: AgentRun, ev: SessionEvidence, env: GradeEnv
) -> Grade:
    assert r.expected_sql
    if not ev.final_sql:
        return _grade(r, "fail", "the agent ran no data query")
    try:
        _, want = run_expected_sql(env, r.expected_sql, r.connection)
        _, got = run_expected_sql(env, ev.final_sql, r.connection, ev.final_connection_id)
    except ValueError as e:
        return _grade(r, "error", f"could not execute: {e}", final_sql=ev.final_sql)
    why = compare_results(got, want, ordered=r.ordered)
    if why is None:
        return _grade(r, "pass", f"result sets match ({len(want)} rows)", final_sql=ev.final_sql)
    return _grade(r, "fail", f"result sets differ: {why}", final_sql=ev.final_sql)


# ----------------------------------------------------------------------------- context_used


def grade_context_used(r: Rubric, prompt: str, env: GradeEnv) -> Grade:
    with workspace_session(env.workspace_id, env.user_id, engine=env.engine) as s:
        pack = build_context_pack(
            s, prompt, embedder=env.embedder, release_id=env.release_id
        )
        entries = [(e.key, e.score) for e in pack.entries]
    ranked = sorted(entries, key=lambda t: -(t[1] if t[1] is not None else float("inf")))
    order = [k for k, _ in ranked]
    missing, too_low = [], []
    for key in r.items:
        if key not in order:
            missing.append(key)
        elif r.top_n is not None and order.index(key) + 1 > r.top_n:
            too_low.append(f"{key} (rank {order.index(key) + 1})")
    if not missing and not too_low:
        where = f" in the top {r.top_n}" if r.top_n else ""
        return _grade(r, "pass", f"all {len(r.items)} item(s) retrieved{where}", got=order[:10])
    bits = []
    if missing:
        bits.append("not retrieved: " + ", ".join(missing))
    if too_low:
        bits.append(f"outside the top {r.top_n}: " + ", ".join(too_low))
    return _grade(r, "fail", "; ".join(bits), got=order[:10])


# ----------------------------------------------------------------------------- judges

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"pass": {"type": "boolean"}, "reasoning": {"type": "string"}},
    "required": ["pass", "reasoning"],
}
_JUDGE_SYSTEM = (
    "You are a strict, fair grader of a data analyst agent. Judge only against the criteria "
    "given. Use the ground truth only if provided. Reply with pass (true/false) and short "
    "reasoning citing evidence."
)


def grade_judge(
    r: Rubric, case: Case, run: AgentRun, ev: SessionEvidence, env: GradeEnv
) -> Grade:
    if env.judge is None:
        return _grade(r, "skipped", "no LLM judge is configured")
    parts = [f"Question asked of the agent:\n{case.prompt}", f"Criteria:\n{r.criteria}"]
    if r.expected is not None:
        parts.append(f"Expected answer (ground truth):\n{r.expected}")
    if r.expected_sql:
        try:
            cols, rows = run_expected_sql(env, r.expected_sql, r.connection)
            parts.append(f"Ground-truth SQL: {r.expected_sql}\nGround-truth result "
                         f"({', '.join(cols)}): {rows[:20]}")
        except ValueError as e:
            parts.append(f"(ground-truth SQL failed: {e})")
    parts.append(f"Agent's final answer:\n{run.answer or '(none)'}")
    if r.grader == "judge_session":
        parts.append(f"Context items served to the agent: {', '.join(ev.served) or '(none)'}")
        parts.append(f"Tool trace:\n{trace_text(run)}")
    try:
        out = env.judge.complete_json(
            _JUDGE_SYSTEM, [{"role": "user", "content": "\n\n".join(parts)}], _JUDGE_SCHEMA,
            max_tokens=600,
        )
    except ChatError as e:
        return _grade(r, "error", f"judge call failed: {e}")
    ok = out.get("pass")
    if not isinstance(ok, bool):
        return _grade(r, "error", "judge returned no pass/fail verdict")
    return _grade(r, "pass" if ok else "fail", str(out.get("reasoning") or ""))


# ----------------------------------------------------------------------------- dispatch


def grade_rubric(
    r: Rubric, case: Case, run: AgentRun | None, ev: SessionEvidence | None, env: GradeEnv
) -> Grade:
    try:
        if r.grader == "context_used":
            return grade_context_used(r, case.prompt, env)
        if run is None or ev is None:
            return _grade(r, "skipped", "needs an agent run but no LLM is configured")
        if run.error and not run.answer:
            return _grade(r, "error", f"agent run failed: {run.error}")
        if r.grader == "numeric_value":
            return grade_numeric(r, run, env)
        if r.grader == "sql_equivalent":
            return grade_sql_equivalent(r, run, ev, env)
        return grade_judge(r, case, run, ev, env)
    except Exception as e:  # a grader bug must not lose the run
        log.exception("grader %s failed", r.grader)
        return _grade(r, "error", f"grader crashed: {type(e).__name__}: {e}")


# ----------------------------------------------------------------------------- aggregation


def attempt_outcome(grades: list[Grade]) -> Outcome:
    """One attempt: warn_only rubrics never affect it; any error beats a fail."""
    core = [g for g in grades if not g.warn_only]
    if not core:
        return "pass"
    if any(g.outcome == "error" for g in core):
        return "error"
    if any(g.outcome == "fail" for g in core):
        return "fail"
    if all(g.outcome == "skipped" for g in core):
        return "skipped"
    return "pass"


def combine_attempts(outcomes: list[Outcome]) -> Outcome:
    """Several attempts: any error -> error; at most one failure is tolerated (with 2+
    attempts); everything skipped -> skipped."""
    if not outcomes:
        return "skipped"
    if any(o == "error" for o in outcomes):
        return "error"
    ran = [o for o in outcomes if o != "skipped"]
    if not ran:
        return "skipped"
    fails = sum(o == "fail" for o in ran)
    tolerated = 1 if len(outcomes) >= 2 else 0
    return "fail" if fails > tolerated else "pass"
