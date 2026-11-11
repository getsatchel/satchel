"""Eval suite format (design §10), intentionally close to Hex's: YAML -> validated spec."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_CASES = 100
MAX_RUBRICS = 15
MAX_ATTEMPTS = 5

Grader = Literal[
    "numeric_value", "sql_equivalent", "context_used", "judge_final_answer", "judge_session"
]
DEFAULT_MEASURE: dict[str, str] = {
    "numeric_value": "correctness",
    "sql_equivalent": "correctness",
    "context_used": "retrieval",
    "judge_final_answer": "answer_quality",
    "judge_session": "method",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class SuiteError(ValueError):
    """The suite file is invalid; the message lists every problem."""


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["anthropic", "openai_compat"] = "anthropic"
    name: str


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grader: Grader
    measure: str | None = None
    warn_only: bool = False
    # numeric_value / sql_equivalent / judge_*: ground truth
    expected: float | str | None = None
    expected_sql: str | None = None
    connection: str | None = None
    tolerance_pct: float | None = Field(default=None, ge=0)
    tolerance_abs: float | None = Field(default=None, ge=0)
    extraction_guidance: str | None = None
    ordered: bool = False
    # judges
    criteria: str | None = None
    # context_used
    items: list[str] = Field(default_factory=list)
    top_n: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check(self) -> Rubric:
        if self.measure is None:
            self.measure = DEFAULT_MEASURE[self.grader]
        g = self.grader
        if g == "numeric_value" and self.expected is None and not self.expected_sql:
            raise ValueError("numeric_value needs expected or expected_sql")
        if g == "numeric_value" and self.expected is not None:
            try:
                float(self.expected)
            except (TypeError, ValueError):
                raise ValueError("numeric_value expected must be a number") from None
        if g == "sql_equivalent" and not self.expected_sql:
            raise ValueError("sql_equivalent needs expected_sql")
        if g == "context_used" and not self.items:
            raise ValueError("context_used needs items (context item keys)")
        if g in ("judge_final_answer", "judge_session") and not (self.criteria or "").strip():
            raise ValueError(f"{g} needs criteria")
        return self


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    prompt: str = Field(min_length=1)
    attempts: int = Field(default=1, ge=1, le=MAX_ATTEMPTS)
    rubrics: list[Rubric] = Field(min_length=1, max_length=MAX_RUBRICS)
    tags: list[str] = Field(default_factory=list)  # e.g. baseline / hill-climb

    @model_validator(mode="after")
    def _id(self) -> Case:
        if not _ID.match(self.id):
            raise ValueError(f"case id {self.id!r} must be letters, digits, '_', '-' or '.'")
        return self

    @property
    def needs_agent(self) -> bool:
        return any(r.grader != "context_used" for r in self.rubrics)


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str = ""
    description: str = ""
    model: ModelSpec | None = None
    cases: list[Case] = Field(min_length=1, max_length=MAX_CASES)

    @model_validator(mode="after")
    def _unique(self) -> Suite:
        if not _ID.match(self.id):
            raise ValueError(f"suite id {self.id!r} must be letters, digits, '_', '-' or '.'")
        ids = [c.id for c in self.cases]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"duplicate case ids: {', '.join(dup)}")
        return self

    @property
    def needs_agent(self) -> bool:
        return any(c.needs_agent for c in self.cases)

    def measures(self) -> list[str]:
        return sorted({str(r.measure) for c in self.cases for r in c.rubrics})


def parse_suite(raw: Any) -> Suite:
    if not isinstance(raw, dict):
        raise SuiteError("a suite must be a YAML mapping with id and cases")
    try:
        return Suite.model_validate(raw)
    except ValidationError as e:
        parts = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            parts.append(f"{loc}: {err['msg']}")
        raise SuiteError("; ".join(parts)) from None


def parse_suite_text(text: str) -> Suite:
    try:
        return parse_suite(yaml.safe_load(text))
    except yaml.YAMLError as e:
        raise SuiteError(f"invalid YAML: {e}") from None


def load_suite_file(path: str | Path) -> Suite:
    return parse_suite_text(Path(path).read_text(encoding="utf-8"))


def suite_spec(suite: Suite) -> dict[str, Any]:
    out: dict[str, Any] = suite.model_dump(mode="json", exclude_none=True)
    return out


def spec_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
