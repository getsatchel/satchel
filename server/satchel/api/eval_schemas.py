"""Request/response models for the evals API (design §10)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SuiteIn(BaseModel):
    yaml: str = Field(min_length=1, description="Suite YAML (design §10)")


class SuiteOut(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    version_seq: int
    managed_by: str
    cases: int
    measures: list[str]
    needs_agent: bool
    required_by_gate: bool = False
    created_at: datetime
    last_run_at: datetime | None = None
    schedule_hours: int | None = Field(default=None, description="schedule.interval_hours")
    next_run_at: datetime | None = Field(
        default=None, description="When the worker next runs it on a schedule (past: due)")


class SuiteDetail(SuiteOut):
    spec: dict[str, Any]
    yaml: str
    versions: list[int]


class RunRequest(BaseModel):
    suite_id: uuid.UUID | None = None
    suite_key: str | None = None
    release_id: uuid.UUID | None = Field(default=None, description="Any release id")
    preview_id: uuid.UUID | None = Field(default=None, description="Alias of release_id")
    model: str | None = Field(default=None, description="e.g. claude-haiku-4-5 or provider:name")
    wait: bool = Field(default=False, description="Block until the run finishes")


class MeasureStat(BaseModel):
    measure: str
    passed: int
    failed: int
    errored: int
    skipped: int
    warnings: int
    pass_rate: float | None


class RunOut(BaseModel):
    id: uuid.UUID
    suite_id: uuid.UUID
    suite_key: str
    suite_version: int
    release_id: uuid.UUID
    release_seq: int | None
    release_kind: str | None
    model: str | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    started_by: uuid.UUID | None
    cases_total: int = 0
    cases_pass: int = 0
    cases_fail: int = 0
    cases_error: int = 0
    cases_skipped: int = 0
    measures: list[MeasureStat] = []
    tokens: dict[str, int] = {}
    seconds: float | None = None
    llm: bool = False
    error: str | None = None
    trigger: str = Field(default="manual", description="manual | schedule | gate")
    previous_run_id: uuid.UUID | None = Field(
        default=None, description="Scheduled runs: the previous completed scheduled run")


class GradeOut(BaseModel):
    grader: str
    measure: str
    warn_only: bool
    outcome: str
    reasoning: str
    details: dict[str, Any] = {}


class AttemptOut(BaseModel):
    attempt: int
    outcome: str
    session_id: uuid.UUID | None
    answer: str | None
    grades: list[GradeOut]
    cost: dict[str, Any]


class CaseOut(BaseModel):
    case_key: str
    prompt: str
    outcome: str
    tags: list[str]
    attempts: list[AttemptOut]


class RunDetail(RunOut):
    cases: list[CaseOut]


class MeasureDelta(BaseModel):
    measure: str
    a: float | None
    b: float | None
    delta: float | None


class CaseMove(BaseModel):
    case: str
    a: str
    b: str


class CompareOut(BaseModel):
    a: RunOut
    b: RunOut
    measures: list[MeasureDelta]
    newly_failing: list[CaseMove]
    newly_passing: list[CaseMove]
    only_in_a: list[str]
    only_in_b: list[str]


class GateSuiteOut(BaseModel):
    suite_key: str
    suite_version: int
    status: str
    preview_run_id: uuid.UUID | None
    baseline_run_id: uuid.UUID | None
    measures: list[MeasureDelta]
    regressions: list[MeasureDelta]
    newly_failing: list[CaseMove]
    newly_passing: list[CaseMove]
    message: str = ""


class GateOut(BaseModel):
    preview_id: uuid.UUID
    configured: bool
    status: str  # none | pending | running | pass | fail
    threshold: float
    suites: list[GateSuiteOut]


class PreviewEvalsRequest(BaseModel):
    model: str | None = None
    wait: bool = False


class PublishRequest(BaseModel):
    override_reason: str | None = Field(
        default=None, description="Admins only: publish despite a failing or missing eval gate"
    )
