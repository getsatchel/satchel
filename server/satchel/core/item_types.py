"""Pydantic schemas for context item bodies/meta (design §3.2)."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceRule(_Strict):
    body: str = Field(min_length=1)
    domain: str | None = None


class Guide(_Strict):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    body: str = Field(min_length=1)


class GlossaryTerm(_Strict):
    term: str = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)
    definition: str = Field(min_length=1)
    related_assets: list[str] = Field(default_factory=list)


class GoldenQuery(_Strict):
    question: str = Field(min_length=1)
    variants: list[str] = Field(default_factory=list)
    sql: str | None = None
    metric_query: dict[str, Any] | None = None
    connection: str | None = None
    verified_by: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if bool(self.sql) == bool(self.metric_query):
            raise ValueError("exactly one of sql or metric_query is required")


class AssetColumn(_Strict):
    name: str
    description: str | None = None
    enum_values: list[str] = Field(default_factory=list)
    unit: str | None = None
    pii: bool = False
    data_type: str | None = None
    required: bool = False  # not null
    unique: bool = False


class AssetDoc(_Strict):
    asset: str = Field(min_length=1, description="fqn of the documented asset")
    description: str | None = None
    enum_values: list[str] = Field(default_factory=list)
    unit: str | None = None
    grain: str | None = None
    caveats: list[str] = Field(default_factory=list)
    pii: bool = False
    endorsed: bool = False
    columns: list[AssetColumn] = Field(default_factory=list)
    owner: str | None = None
    tags: list[str] = Field(default_factory=list)
    joins: list[str] = Field(default_factory=list)  # "customer_id -> schema.table.column"
    used_by: list[str] = Field(default_factory=list)  # dashboards and other consumers


class Endorsement(_Strict):
    asset: str = Field(min_length=1)
    endorsed: bool = False
    excluded_from_ai: bool = False
    reason: str | None = None


class SemanticModel(_Strict):
    format: Literal["ossie", "wren_mdl"]
    body: str = Field(min_length=1)


class Caveat(_Strict):
    body: str = Field(min_length=1)
    applies_to: list[str] = Field(min_length=1)
    valid_from: date | None = None
    valid_to: date | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")


class UserMemory(_Strict):
    body: str = Field(min_length=1)
    user_id: str = Field(min_length=1)


ITEM_TYPES: dict[str, type[BaseModel]] = {
    "workspace_rule": WorkspaceRule,
    "guide": Guide,
    "glossary_term": GlossaryTerm,
    "golden_query": GoldenQuery,
    "asset_doc": AssetDoc,
    "endorsement": Endorsement,
    "semantic_model": SemanticModel,
    "caveat": Caveat,
    "user_memory": UserMemory,
}

# Meta keys that belong to source-managed items (design §8.3): ``source`` is owned by the upstream
# tool and replaced on every sync, ``overlay`` holds analyst additions. They are not part of an
# item's own schema, so validation ignores them.
SOURCE_META_KEYS = ("source", "overlay")

# Types whose body is markdown text; meta carries the remaining fields.
_MARKDOWN_BODY = {"workspace_rule", "guide", "caveat", "user_memory", "semantic_model"}


class ItemValidationError(ValueError):
    pass


def validate_item(item_type: str, body: str, meta: dict[str, Any] | None = None) -> BaseModel:
    """Validate a (body, meta) pair for the given type; return the parsed model.

    Markdown-bodied types take ``body`` as text and the rest from ``meta``; YAML-bodied
    types (glossary_term, golden_query, asset_doc, endorsement) parse ``body`` as YAML and
    merge ``meta`` over it.
    """
    model = ITEM_TYPES.get(item_type)
    if model is None:
        raise ItemValidationError(f"unknown item type: {item_type}")
    meta = {k: v for k, v in (meta or {}).items() if k not in SOURCE_META_KEYS}
    data: dict[str, Any]
    if item_type in _MARKDOWN_BODY:
        data = {**meta, "body": body}
    else:
        try:
            parsed = yaml.safe_load(body) if body.strip() else {}
        except yaml.YAMLError as e:
            raise ItemValidationError(f"{item_type}: invalid YAML: {e}") from e
        if not isinstance(parsed, dict):
            raise ItemValidationError(f"{item_type}: body must be a YAML mapping")
        data = {**parsed, **meta}
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise ItemValidationError(f"{item_type}: {e}") from e


def normalized_meta(
    item_type: str, body: str, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate and return ``meta`` enriched with structured YAML fields for fast DB queries.

    Preserves all caller-supplied ``meta`` keys while ensuring YAML-bodied items (`glossary_term`,
    `golden_query`, `asset_doc`, `endorsement`) store their structured fields (`term`, `synonyms`,
    `asset`, `endorsed`, `excluded_from_ai`, `columns`, etc.) in `ContextVersion.meta` so runtime
    retrieval and signal extractors never need to call ``yaml.safe_load`` on hot paths.
    """
    raw_meta = dict(meta or {})
    model = validate_item(item_type, body, raw_meta)
    if item_type in _MARKDOWN_BODY:
        return raw_meta
    dumped = model.model_dump(mode="json", exclude_none=True)
    return {**dumped, **raw_meta}


def _is_enriched(item_type: str, meta: dict[str, Any]) -> bool:
    if item_type == "asset_doc":
        return "asset" in meta and "columns" in meta
    if item_type == "endorsement":
        return "asset" in meta and "excluded_from_ai" in meta
    if item_type == "glossary_term":
        return "term" in meta and "definition" in meta
    if item_type == "golden_query":
        return "question" in meta and ("sql" in meta or "metric_query" in meta)
    return bool(meta)


def yaml_item_dict(
    item_type: str, body: str, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the structured dict for a YAML-bodied item, reading from ``meta`` when already
    enriched by :func:`normalized_meta` and falling back to ``yaml.safe_load(body)`` for
    pre-enrichment rows."""
    m = dict(meta or {})
    if _is_enriched(item_type, m):
        return m
    try:
        parsed = yaml.safe_load(body) if body.strip() else {}
    except yaml.YAMLError:
        return m
    if not isinstance(parsed, dict):
        return m
    return {**parsed, **m}


