"""Evals API (design §10): suites, runs, comparison and the publish gate for previews.

Roles: explorers view; analysts and above run evals; curators and above manage suites.
Runs execute in a background thread unless ``wait`` is set; poll ``GET /v1/evals/runs/{id}``
(or ``GET /v1/previews/{id}/evals`` for the gate).
"""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any, cast

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from satchel.api.auth import Auth, Caller
from satchel.api.common import audit
from satchel.api.eval_schemas import (
    AttemptOut,
    CaseMove,
    CaseOut,
    CompareOut,
    GateOut,
    GateSuiteOut,
    GradeOut,
    MeasureDelta,
    MeasureStat,
    PreviewEvalsRequest,
    RunDetail,
    RunOut,
    RunRequest,
    SuiteDetail,
    SuiteIn,
    SuiteOut,
)
from satchel.evals.gate import evaluate_gate, load_gate_config, start_gate_runs
from satchel.evals.providers import resolve_provider
from satchel.evals.runner import EvalProvider, compare_summaries, create_run, execute_run
from satchel.evals.store import latest_suite, latest_suites, save_suite, suite_of
from satchel.evals.suite import SuiteError, parse_suite_text
from satchel.mcp.auth import Principal
from satchel.store.db import workspace_session
from satchel.store.models import EvalCaseResult, EvalRun, EvalSuite, Release

log = logging.getLogger("satchel.evals")
_BG = ThreadPoolExecutor(max_workers=2, thread_name_prefix="satchel-eval")


def _stat(name: str, m: dict[str, Any]) -> MeasureStat:
    return MeasureStat(
        measure=name, passed=m["passed"], failed=m["failed"], errored=m["errored"],
        skipped=m["skipped"], warnings=m["warnings"], pass_rate=m["pass_rate"],
    )


def run_out(s: Session, run: EvalRun) -> RunOut:
    suite = s.get(EvalSuite, run.suite_id)
    rel = s.get(Release, run.release_id)
    sm = run.summary or {}
    cases = sm.get("cases") or {}
    return RunOut(
        id=run.id, suite_id=run.suite_id, suite_key=suite.key if suite else "",
        suite_version=suite.version_seq if suite else 0, release_id=run.release_id,
        release_seq=rel.seq if rel else None, release_kind=rel.kind if rel else None,
        model=run.model, status=run.status, started_at=run.started_at,
        finished_at=run.finished_at, started_by=run.started_by,
        cases_total=cases.get("total", 0), cases_pass=cases.get("pass", 0),
        cases_fail=cases.get("fail", 0), cases_error=cases.get("error", 0),
        cases_skipped=cases.get("skipped", 0),
        measures=[_stat(n, m) for n, m in sorted((sm.get("measures") or {}).items())],
        tokens=sm.get("tokens") or {}, seconds=sm.get("seconds"), llm=bool(sm.get("llm")),
        error=sm.get("error"),
    )


def _deltas(rows: list[dict[str, Any]]) -> list[MeasureDelta]:
    return [MeasureDelta(measure=r["measure"], a=r["a"], b=r["b"], delta=r["delta"]) for r in rows]


def _suite_out(s: Session, row: EvalSuite, required: set[str]) -> SuiteOut:
    suite = suite_of(row)
    last = s.scalar(
        select(EvalRun.started_at)
        .join(EvalSuite, EvalSuite.id == EvalRun.suite_id)
        .where(EvalSuite.key == row.key)
        .order_by(EvalRun.started_at.desc())
        .limit(1)
    )
    return SuiteOut(
        id=row.id, key=row.key, name=row.name, version_seq=row.version_seq,
        managed_by=row.managed_by, cases=len(suite.cases), measures=suite.measures(),
        needs_agent=suite.needs_agent, required_by_gate=row.key in required,
        created_at=row.created_at, last_run_at=last,
    )


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["evals"])
    Any_ = Annotated[Caller, Depends(auth.require("explorer"))]
    Run_ = Annotated[Caller, Depends(auth.require("analyst"))]
    Cur = Annotated[Caller, Depends(auth.require("curator"))]
    eng, emb, st = auth.ctx.engine, auth.ctx.embedder, auth.ctx.settings

    def principal(who: Caller) -> Principal:
        if who.user_id is None:
            raise HTTPException(422, "running evals needs a signed-in user (not anonymous dev)")
        return Principal(who.workspace_id, who.user_id, who.effective_role)

    def provider_for(row: EvalSuite, override: str | None = None) -> EvalProvider | None:
        injected = auth.ctx.chat
        if injected is not None and hasattr(injected, "complete_with_tools"):
            return cast(EvalProvider, injected)
        return resolve_provider(suite_of(row).model, override, st,
                                workspace_id=row.workspace_id, engine=eng)

    def execute_ids(p: Principal, ids: list[uuid.UUID], override: str | None) -> None:
        for rid in ids:
            with workspace_session(p.workspace_id, p.user_id, engine=eng) as s:
                run = s.get(EvalRun, rid)
                row = s.get(EvalSuite, run.suite_id) if run else None
                if row is None:
                    continue
                s.expunge(row)
            execute_run(eng, emb, p, rid, provider=provider_for(row, override))

    def dispatch(p: Principal, ids: list[uuid.UUID], override: str | None, wait: bool) -> None:
        if wait:
            execute_ids(p, ids, override)
        else:
            _BG.submit(execute_ids, p, ids, override)

    # ---- suites ------------------------------------------------------------------------------

    @r.get("/v1/evals/suites", response_model=list[SuiteOut])
    def list_suites(who: Any_) -> list[SuiteOut]:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            required = set(load_gate_config(s, who.workspace_id).require)
            return [_suite_out(s, row, required) for row in latest_suites(s, who.workspace_id)]

    @r.get("/v1/evals/suites/{key:path}", response_model=SuiteDetail)
    def get_suite(key: str, who: Any_) -> SuiteDetail:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            row = latest_suite(s, who.workspace_id, key)
            if row is None:
                raise HTTPException(404, f"no such suite: {key}")
            required = set(load_gate_config(s, who.workspace_id).require)
            versions = list(s.scalars(
                select(EvalSuite.version_seq).where(EvalSuite.key == key)
                .order_by(EvalSuite.version_seq.desc())
            ))
            base = _suite_out(s, row, required)
            return SuiteDetail(
                **base.model_dump(), spec=row.spec, yaml=yaml.safe_dump(row.spec, sort_keys=False),
                versions=versions,
            )

    @r.post("/v1/evals/suites", response_model=SuiteOut, status_code=201)
    def put_suite(body: SuiteIn, who: Cur) -> SuiteOut:
        try:
            suite = parse_suite_text(body.yaml)
        except SuiteError as e:
            raise HTTPException(422, str(e)) from None
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            cur = latest_suite(s, who.workspace_id, suite.id)
            if cur is not None and cur.managed_by == "git":
                raise HTTPException(
                    409, f"suite {suite.id!r} is managed by git; change it in the context repo"
                )
            row, created = save_suite(s, who.workspace_id, suite, created_by=who.user_id)
            if created:
                audit(s, who, "eval.suite.save", {"key": row.key, "version": row.version_seq})
            required = set(load_gate_config(s, who.workspace_id).require)
            return _suite_out(s, row, required)

    # ---- runs --------------------------------------------------------------------------------

    @r.post("/v1/evals/runs", response_model=RunOut, status_code=201)
    def start_run(body: RunRequest, who: Run_) -> RunOut:
        p = principal(who)
        rid = body.release_id or body.preview_id
        if rid is None:
            raise HTTPException(422, "release_id (or preview_id) is required")
        if (body.suite_id is None) == (body.suite_key is None):
            raise HTTPException(422, "pass exactly one of suite_id or suite_key")
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            row = (
                s.get(EvalSuite, body.suite_id) if body.suite_id
                else latest_suite(s, who.workspace_id, str(body.suite_key))
            )
            if row is None:
                raise HTTPException(404, "no such suite")
            if s.get(Release, rid) is None:
                raise HTTPException(404, f"no such release: {rid}")
            s.expunge(row)
        run_id = create_run(eng, p, row, rid, provider=provider_for(row, body.model))
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            audit(s, who, "eval.run.start", {"run_id": run_id, "suite": row.key,
                                              "release_id": rid})
        dispatch(p, [run_id], body.model, body.wait)
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            run = s.get(EvalRun, run_id)
            assert run is not None
            return run_out(s, run)

    @r.get("/v1/evals/runs", response_model=list[RunOut])
    def list_runs(
        who: Any_,
        suite_key: Annotated[str | None, Query()] = None,
        release_id: Annotated[uuid.UUID | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[RunOut]:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            q = select(EvalRun).order_by(EvalRun.started_at.desc()).limit(limit)
            if suite_key:
                q = q.join(EvalSuite, EvalSuite.id == EvalRun.suite_id).where(
                    EvalSuite.key == suite_key
                )
            if release_id:
                q = q.where(EvalRun.release_id == release_id)
            return [run_out(s, x) for x in s.scalars(q)]

    @r.get("/v1/evals/runs/{run_id}", response_model=RunDetail)
    def get_run(run_id: uuid.UUID, who: Any_) -> RunDetail:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            run = s.get(EvalRun, run_id)
            if run is None:
                raise HTTPException(404, f"no such run: {run_id}")
            suite = suite_of(s.get(EvalSuite, run.suite_id))  # type: ignore[arg-type]
            prompts = {c.id: c for c in suite.cases}
            rows = list(s.scalars(
                select(EvalCaseResult).where(EvalCaseResult.run_id == run_id)
                .order_by(EvalCaseResult.id)
            ))
            outcomes = (run.summary or {}).get("case_outcomes") or {}
            cases: list[CaseOut] = []
            for key, case in prompts.items():
                attempts = [
                    AttemptOut(
                        attempt=x.attempt, outcome=x.outcome, session_id=x.session_id,
                        answer=x.answer, cost=x.cost,
                        grades=[GradeOut(**g) for g in x.grades],
                    )
                    for x in rows if x.case_key == key
                ]
                if not attempts and run.status == "completed":
                    continue
                cases.append(CaseOut(
                    case_key=key, prompt=case.prompt, tags=case.tags, attempts=attempts,
                    outcome=(outcomes.get(key) or {}).get("outcome", "running"),
                ))
            return RunDetail(**run_out(s, run).model_dump(), cases=cases)

    @r.get("/v1/evals/compare", response_model=CompareOut)
    def compare(a: uuid.UUID, b: uuid.UUID, who: Any_) -> CompareOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            ra, rb = s.get(EvalRun, a), s.get(EvalRun, b)
            if ra is None or rb is None:
                raise HTTPException(404, "no such run")
            for x in (ra, rb):
                if x.status != "completed":
                    raise HTTPException(409, f"run {x.id} is {x.status}, not completed")
            cmp = compare_summaries(ra.summary, rb.summary)
            return CompareOut(
                a=run_out(s, ra), b=run_out(s, rb), measures=_deltas(cmp["measures"]),
                newly_failing=[CaseMove(**m) for m in cmp["newly_failing"]],
                newly_passing=[CaseMove(**m) for m in cmp["newly_passing"]],
                only_in_a=cmp["only_in_a"], only_in_b=cmp["only_in_b"],
            )

    # ---- publish gate ------------------------------------------------------------------------

    def gate_out(s: Session, preview: Release, workspace_id: uuid.UUID) -> GateOut:
        g = evaluate_gate(s, workspace_id, preview)
        suites = []
        for sg in g.suites:
            cmp = sg.comparison or {"measures": [], "newly_failing": [], "newly_passing": []}
            suites.append(GateSuiteOut(
                suite_key=sg.suite_key, suite_version=sg.suite_version, status=sg.status,
                preview_run_id=sg.preview_run_id, baseline_run_id=sg.baseline_run_id,
                measures=_deltas(cmp["measures"]), regressions=_deltas(sg.regressions),
                newly_failing=[CaseMove(**m) for m in cmp["newly_failing"]],
                newly_passing=[CaseMove(**m) for m in cmp["newly_passing"]],
                message=sg.message,
            ))
        return GateOut(preview_id=preview.id, configured=g.configured, status=g.status,
                       threshold=g.threshold, suites=suites)

    def get_preview(s: Session, preview_id: uuid.UUID) -> Release:
        rel = s.get(Release, preview_id)
        if rel is None:
            raise HTTPException(404, f"no such release: {preview_id}")
        return rel

    @r.get("/v1/previews/{preview_id}/evals", response_model=GateOut)
    def preview_gate(preview_id: uuid.UUID, who: Any_) -> GateOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            return gate_out(s, get_preview(s, preview_id), who.workspace_id)

    @r.post("/v1/previews/{preview_id}/evals", response_model=GateOut)
    def run_preview_evals(
        preview_id: uuid.UUID, who: Run_, body: PreviewEvalsRequest | None = None
    ) -> GateOut:
        p = principal(who)
        req = body or PreviewEvalsRequest()
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            rel = get_preview(s, preview_id)
            if rel.kind != "preview":
                raise HTTPException(409, "evals gate previews; this release is published")
            if not load_gate_config(s, who.workspace_id).require:
                return gate_out(s, rel, who.workspace_id)
            ids = start_gate_runs(eng, s, p, rel, lambda row: provider_for(row, req.model))
            audit(s, who, "eval.gate.start", {"preview_id": preview_id,
                                               "runs": [str(i) for i in ids]})
        dispatch(p, ids, req.model, req.wait)
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            return gate_out(s, get_preview(s, preview_id), who.workspace_id)

    return r
