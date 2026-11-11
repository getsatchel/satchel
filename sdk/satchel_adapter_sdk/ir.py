"""The canonical intermediate representation (IR) every source adapter emits.

Adapters never write Satchel database rows. They turn an upstream tool's artifacts (a dbt
manifest, a Cube schema, ...) into a :class:`SourceSnapshot`; the Satchel server maps that
snapshot onto context items (``asset_doc``, ``semantic_model``, ``golden_query``,
``glossary_term``).

Versioning (see ``sdk/README.md``): :data:`IR_VERSION` is semver. The server accepts a snapshot
whose major version equals its own; minor versions only ever add optional fields.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IR_VERSION = "1.0.0"

AssetKind = Literal["table", "view"]


def ir_major(version: str) -> int:
    """Major component of a semver string (``"1.2.0"`` -> ``1``)."""
    m = re.match(r"^(\d+)\.\d+\.\d+", version)
    if m is None:
        raise ValueError(f"not a semver version: {version!r}")
    return int(m.group(1))


def is_compatible(version: str) -> bool:
    """True when a snapshot built for ``version`` can be read by this SDK (same major)."""
    try:
        return ir_major(version) == ir_major(IR_VERSION)
    except ValueError:
        return False


def normalise_fqn(name: str) -> str:
    """Canonical asset name: quotes and brackets removed, lower case, parts joined by ``.``.

    ``'"JAFFLE"."Analytics"."Orders"'`` -> ``'jaffle.analytics.orders'``. Adapters should emit
    ``schema.table`` (the database is kept in ``SourceAsset.database``); the server resolves it
    against the connection's crawled assets.
    """
    parts: list[str] = []
    cur: list[str] = []
    closer = ""
    for ch in name:
        if closer:
            if ch == closer:
                closer = ""
            else:
                cur.append(ch)
        elif ch in "\"`":
            closer = ch
        elif ch == "[":
            closer = "]"
        elif ch == ".":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return ".".join(p.strip().lower() for p in parts if p.strip())


class _IR(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRelationship(_IR):
    """A join hint: ``column`` of this asset references ``to_column`` of ``to_asset``."""

    column: str
    to_asset: str
    to_column: str | None = None


class SourceColumn(_IR):
    name: str = Field(min_length=1)
    description: str | None = None
    data_type: str | None = None
    enum_values: list[str] = Field(default_factory=list)
    not_null: bool = False
    unique: bool = False
    pii: bool = False
    unit: str | None = None
    tags: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class SourceAsset(_IR):
    """A table or view of the warehouse, as the upstream tool describes it."""

    key: str = Field(min_length=1, description="Stable id within the source")
    fqn: str = Field(min_length=1, description="Normalised schema.table (see normalise_fqn)")
    kind: AssetKind = "table"
    name: str | None = None
    database: str | None = None
    description: str | None = None
    columns: list[SourceColumn] = Field(default_factory=list)
    grain: list[str] = Field(
        default_factory=list, description="Columns that together identify one row, when known"
    )
    enum_values: list[str] = Field(default_factory=list)
    relationships: list[SourceRelationship] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    owner: str | None = None
    endorsed: bool | None = Field(
        default=None, description="Set only when the upstream author explicitly declared it"
    )
    endorsement_hint: str | None = Field(
        default=None, description="Why a heuristic suggests endorsing; never applied automatically"
    )
    caveats: list[str] = Field(default_factory=list)
    used_by: list[str] = Field(default_factory=list, description="e.g. dashboards, exposures")
    depends_on: list[str] = Field(default_factory=list, description="Upstream asset keys")
    source_meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("fqn")
    @classmethod
    def _fqn(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("fqn must not be empty")
        return v


class SourceSemanticModel(_IR):
    """Governed metrics, as a Wren MDL manifest (the format satchel's semantic engine consumes)."""

    key: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str | None = None
    mdl: dict[str, Any]
    queryable_metrics: list[str] = Field(default_factory=list)
    notes: list[str] = Field(
        default_factory=list, description="Constructs that could not be made queryable, and why"
    )

    @model_validator(mode="after")
    def _mdl_valid(self) -> SourceSemanticModel:
        from satchel_adapter_sdk.mdl import validate_mdl

        errors = validate_mdl(self.mdl)
        if errors:
            raise ValueError("invalid MDL: " + "; ".join(errors))
        return self


class SourceQuery(_IR):
    """A saved query -> ``golden_query``. Exactly one of ``sql`` / ``metric_query``."""

    key: str = Field(min_length=1)
    question: str = Field(min_length=1)
    variants: list[str] = Field(default_factory=list)
    description: str | None = None
    sql: str | None = None
    metric_query: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _one_of(self) -> SourceQuery:
        if bool(self.sql) == bool(self.metric_query):
            raise ValueError("exactly one of sql or metric_query is required")
        return self


class SourceTerm(_IR):
    """A glossary term -> ``glossary_term``."""

    key: str = Field(min_length=1)
    term: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)
    related_assets: list[str] = Field(default_factory=list)


class SourceWarning(_IR):
    """A construct the adapter skipped or degraded. Warnings never fail a sync."""

    code: str
    message: str
    key: str | None = None


class SourceSnapshot(_IR):
    """Everything an adapter knows after one ``load``. Deterministic for the same input."""

    ir_version: str = IR_VERSION
    kind: str
    fingerprint: str = Field(description="Hash of the inputs; equal fingerprints mean no change")
    project: str | None = Field(default=None, description="Upstream project name, if any")
    assets: list[SourceAsset] = Field(default_factory=list)
    semantic_models: list[SourceSemanticModel] = Field(default_factory=list)
    queries: list[SourceQuery] = Field(default_factory=list)
    terms: list[SourceTerm] = Field(default_factory=list)
    warnings: list[SourceWarning] = Field(default_factory=list)

    @field_validator("ir_version")
    @classmethod
    def _version(cls, v: str) -> str:
        ir_major(v)
        return v

    def summary(self) -> dict[str, int]:
        return {
            "assets": len(self.assets),
            "columns": sum(len(a.columns) for a in self.assets),
            "semantic_models": len(self.semantic_models),
            "metrics": sum(len(m.queryable_metrics) for m in self.semantic_models),
            "queries": len(self.queries),
            "terms": len(self.terms),
            "warnings": len(self.warnings),
        }

    def canonical_json(self, *, indent: int | None = 2) -> str:
        """Stable JSON (sorted keys) used for golden files and determinism checks."""
        return json.dumps(self.model_dump(mode="json"), indent=indent, sort_keys=True) + "\n"


def fingerprint_bytes(*parts: bytes) -> str:
    """sha256 over length-prefixed parts; adapters use it for ``SourceSnapshot.fingerprint``."""
    h = hashlib.sha256()
    for p in parts:
        h.update(str(len(p)).encode() + b":")
        h.update(p)
    return h.hexdigest()
