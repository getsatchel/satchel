"""Publish gate (design §8.2, §10): ``publish.require_evals`` suites are run against a preview
and its parent published release; publishing is refused when a measure regresses.

Workspace settings: ``settings.publish = {require_evals: [suite keys], eval_threshold: points}``
(also set from ``satchel.config.yaml`` ``publish:`` on import). Threshold is in percentage points
of pass rate (default 0: any drop blocks). No required suites = no gate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from satchel.evals.runner import EvalProvider, compare_summaries, create_run
from satchel.evals.store import latest_suite
from satchel.store.models import EvalRun, EvalSuite, Release, Workspace

REUSE_WINDOW = timedelta(hours=24)


@dataclass
class GateConfig:
    require: list[str] = field(default_factory=list)
    threshold: float = 0.0  # percentage points


def gate_config(ws_settings: dict[str, Any] | None) -> GateConfig:
    raw = (ws_settings or {}).get("publish")
    if not isinstance(raw, dict):
        return GateConfig()
    req = raw.get("require_evals")
    thr = raw.get("eval_threshold", 0)
    return GateConfig(
        [str(k) for k in req] if isinstance(req, list) else [],
        float(thr) if isinstance(thr, int | float) and not isinstance(thr, bool) else 0.0,
    )


def load_gate_config(s: Session, workspace_id: uuid.UUID) -> GateConfig:
    ws = s.get(Workspace, workspace_id)
    return gate_config(ws.settings if ws else None)


def _latest_run(s: Session, suite_id: uuid.UUID, release_id: uuid.UUID) -> EvalRun | None:
    return s.scalars(
        select(EvalRun)
        .where(EvalRun.suite_id == suite_id, EvalRun.release_id == release_id)
        .order_by(EvalRun.started_at.desc())
    ).first()


def regressions(cmp: dict[str, Any], threshold: float) -> list[dict[str, Any]]:
    out = []
    for m in cmp["measures"]:
        a, b = m["a"], m["b"]
        if a is None:
            continue
        if b is None:
            out.append({**m, "reason": "not evaluated on the preview"})
        elif (a - b) * 100 > threshold:
            out.append({**m, "reason": f"pass rate fell {(a - b) * 100:.1f} points"})
    return out


@dataclass
class SuiteGate:
    suite_key: str
    suite_version: int
    status: str  # running | completed | failed | missing | stale
    preview_run_id: uuid.UUID | None
    baseline_run_id: uuid.UUID | None
    comparison: dict[str, Any] | None
    regressions: list[dict[str, Any]]
    message: str = ""


@dataclass
class GateState:
    configured: bool
    threshold: float
    status: str  # none | pending | running | pass | fail
    suites: list[SuiteGate]
    message: str = ""


def evaluate_gate(s: Session, workspace_id: uuid.UUID, preview: Release) -> GateState:
    cfg = load_gate_config(s, workspace_id)
    if not cfg.require:
        return GateState(False, cfg.threshold, "none", [])
    suites: list[SuiteGate] = []
    for key in cfg.require:
        row = latest_suite(s, workspace_id, key)
        if row is None:
            suites.append(SuiteGate(key, 0, "missing", None, None, None, [],
                                    "required suite is not imported"))
            continue
        run = _latest_run(s, row.id, preview.id)
        if run is None:
            stale = s.scalars(
                select(EvalRun).join(EvalSuite, EvalSuite.id == EvalRun.suite_id)
                .where(EvalSuite.key == key, EvalRun.release_id == preview.id)
            ).first()
            suites.append(SuiteGate(
                key, row.version_seq, "stale" if stale else "missing", None, None, None, [],
                "the suite changed since the last run" if stale else "evals have not been run",
            ))
            continue
        base_id = (run.summary or {}).get("baseline_run_id")
        base = s.get(EvalRun, uuid.UUID(base_id)) if base_id else None
        if run.status != "completed":
            suites.append(SuiteGate(
                key, row.version_seq, run.status, run.id, base.id if base else None, None, [],
                (run.summary or {}).get("error", ""),
            ))
            continue
        cmp = None
        regs: list[dict[str, Any]] = []
        if base is not None and base.status == "completed":
            cmp = compare_summaries(base.summary, run.summary)
            regs = regressions(cmp, cfg.threshold)
        elif base is not None and base.status == "running":
            suites.append(SuiteGate(key, row.version_seq, "running", run.id, base.id, None, [],
                                    "baseline run in progress"))
            continue
        suites.append(SuiteGate(key, row.version_seq, "completed", run.id,
                                base.id if base else None, cmp, regs))
    if any(g.status == "running" for g in suites):
        status = "running"
    elif any(g.status in ("missing", "stale", "failed") for g in suites):
        status = "pending"
    elif any(g.regressions for g in suites):
        status = "fail"
    else:
        status = "pass"
    return GateState(True, cfg.threshold, status, suites)


def start_gate_runs(
    engine: Any, s: Session, principal: Any, preview: Release,
    provider_for: Any, *, now: datetime | None = None,
) -> list[uuid.UUID]:
    """Create (not execute) the runs the gate needs; returns run ids to execute, preview runs
    after their baseline. A baseline run is reused if recent and for the same suite version.
    ``provider_for(suite_row) -> EvalProvider | None``."""
    cfg = load_gate_config(s, preview.workspace_id)
    to_run: list[uuid.UUID] = []
    cutoff = (now or datetime.now(UTC)) - REUSE_WINDOW
    for key in cfg.require:
        row = latest_suite(s, preview.workspace_id, key)
        if row is None:
            continue
        provider: EvalProvider | None = provider_for(row)
        base_id: uuid.UUID | None = None
        if preview.parent_release_id is not None:
            old = _latest_run(s, row.id, preview.parent_release_id)
            if old is not None and old.status in ("completed", "running") and (
                old.started_at >= cutoff
            ):
                base_id = old.id
            else:
                base_id = create_run(engine, principal, row, preview.parent_release_id,
                                     provider=provider, note={"role": "baseline"})
                to_run.append(base_id)
        note: dict[str, Any] = {"role": "preview"}
        if base_id is not None:
            note["baseline_run_id"] = str(base_id)
        to_run.append(create_run(engine, principal, row, preview.id, provider=provider, note=note))
    return to_run
