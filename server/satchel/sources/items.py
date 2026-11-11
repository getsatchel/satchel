"""Turn IR into context-item content, and merge the ``source`` and ``overlay`` blocks.

Every source-managed item version keeps two blocks in its ``meta``:

* ``source``: owned by the upstream tool, replaced on every sync, read-only in the UI;
* ``overlay``: analyst additions (notes, caveats, column notes, endorsement), never touched by a
  sync and carried into each new version.

The version ``body`` (what validation and retrieval chunking read) is rendered from both: the
source description is always shown, the overlay adds to it (or leads, when the analyst wrote a
replacement description).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from satchel_adapter_sdk import (
    SourceAsset,
    SourceColumn,
    SourceQuery,
    SourceSemanticModel,
    SourceTerm,
)

from satchel.core.item_types import AssetColumn, AssetDoc, normalized_meta, validate_item
from satchel.core.render import render_yaml_item
from satchel.data.semantic import SemanticError, model_tables, parse_mdl, summarize
from satchel.sync.parse import LinkSpec, slugify
from satchel.sync.sqlrefs import extract_tables


class ColumnOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str = Field(min_length=1)


class AssetOverlay(BaseModel):
    """What an analyst may add to a source-managed ``asset_doc``."""

    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, description="Replaces the lead description")
    notes: str | None = None
    caveats: list[str] = Field(default_factory=list)
    endorsed: bool | None = None
    columns: dict[str, ColumnOverlay] = Field(default_factory=dict)


class NotesOverlay(BaseModel):
    """Overlay of the other source-managed types: free notes served with the item."""

    model_config = ConfigDict(extra="forbid")
    notes: str | None = None


def overlay_model(item_type: str) -> type[BaseModel]:
    return AssetOverlay if item_type == "asset_doc" else NotesOverlay


def clean_overlay(item_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise an overlay; empty values are dropped so equal overlays hash equal."""
    model = overlay_model(item_type).model_validate(raw)
    data = model.model_dump(mode="json", exclude_none=True)
    return {k: v for k, v in data.items() if v not in ("", [], {})}


@dataclass
class ItemContent:
    key: str
    type: str
    body: str
    meta: dict[str, Any]
    chunks: list[str]
    source_key: str
    scope_kind: str = "workspace"
    scope_asset: str | None = None  # fqn, for asset-scoped items
    scope_database: str | None = None
    asset_columns: list[SourceColumn] = field(default_factory=list)
    links: list[LinkSpec] = field(default_factory=list)


def source_block(
    source_name: str, kind: str, key: str, priority: int, payload: dict[str, Any]
) -> dict[str, Any]:
    return {"source": source_name, "kind": kind, "key": key, "priority": priority, **payload}


# ---- asset_doc ---------------------------------------------------------------------------


def merge_asset_doc(asset: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """The AssetDoc dict rendered from the source asset and the overlay."""
    src_desc = (asset.get("description") or "").strip()
    ov_desc = (overlay.get("description") or "").strip()
    lead = ov_desc or src_desc
    parts = [lead] if lead else []
    if ov_desc and src_desc and ov_desc != src_desc:
        parts.append(f"Source description: {src_desc}")
    if overlay.get("notes"):
        parts.append(f"Analyst notes: {overlay['notes'].strip()}")
    col_notes = {k.lower(): v["notes"] for k, v in (overlay.get("columns") or {}).items()}
    columns: list[dict[str, Any]] = []
    for c in asset.get("columns") or []:
        desc = (c.get("description") or "").strip()
        note = col_notes.get(str(c["name"]).lower())
        if note:
            desc = f"{desc} Analyst note: {note.strip()}".strip()
        col: dict[str, Any] = {"name": c["name"]}
        if desc:
            col["description"] = desc
        if c.get("enum_values"):
            col["enum_values"] = c["enum_values"]
        if c.get("unit"):
            col["unit"] = c["unit"]
        if c.get("pii"):
            col["pii"] = True
        if c.get("data_type"):
            col["data_type"] = c["data_type"]
        if c.get("not_null"):
            col["required"] = True
        if c.get("unique"):
            col["unique"] = True
        columns.append(col)
    endorsed = overlay.get("endorsed")
    if endorsed is None:
        endorsed = bool(asset.get("endorsed"))
    doc: dict[str, Any] = {"asset": asset["fqn"], "endorsed": endorsed}
    if parts:
        doc["description"] = "\n\n".join(parts)
    if asset.get("grain"):
        doc["grain"] = ", ".join(asset["grain"])
    if asset.get("enum_values"):
        doc["enum_values"] = asset["enum_values"]
    caveats = [*(asset.get("caveats") or []), *(overlay.get("caveats") or [])]
    if caveats:
        doc["caveats"] = caveats
    if asset.get("owner"):
        doc["owner"] = asset["owner"]
    if asset.get("tags"):
        doc["tags"] = asset["tags"]
    joins = [
        f"{r['column']} -> {r['to_asset']}" + (f".{r['to_column']}" if r.get("to_column") else "")
        for r in asset.get("relationships") or []
    ]
    if joins:
        doc["joins"] = joins
    if asset.get("used_by"):
        doc["used_by"] = asset["used_by"]
    if columns:
        doc["columns"] = columns
    return doc


def render_asset(
    src_block: dict[str, Any], overlay: dict[str, Any]
) -> tuple[str, dict[str, Any], list[str]]:
    """``(body, meta, chunks)`` of an asset_doc version."""
    doc = merge_asset_doc(src_block["asset"], overlay)
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    meta = normalized_meta(
        "asset_doc", body,
        {"asset": doc["asset"], "endorsed": doc["endorsed"], "source": src_block,
         "overlay": overlay},
    )
    model = validate_item("asset_doc", body)
    assert isinstance(model, AssetDoc)
    return body, meta, [render_yaml_item("asset_doc", model)]


def asset_content(
    source_name: str, kind: str, priority: int, a: SourceAsset, overlay: dict[str, Any]
) -> ItemContent:
    block = source_block(
        source_name, kind, a.key, priority, {"asset": a.model_dump(mode="json")}
    )
    body, meta, chunks = render_asset(block, overlay)
    return ItemContent(
        key=f"sources/{source_name}/assets/{a.fqn}.yaml",
        type="asset_doc", body=body, meta=meta, chunks=chunks, source_key=a.key,
        scope_kind="asset", scope_asset=a.fqn, scope_database=a.database,
        asset_columns=list(a.columns),
        links=[(a.fqn, "defines")] + [(f"{a.fqn}.{c.name}", "defines") for c in a.columns],
    )


# ---- semantic_model -----------------------------------------------------------------------


def _notes_chunk(overlay: dict[str, Any]) -> list[str]:
    return [f"Analyst notes: {overlay['notes'].strip()}"] if overlay.get("notes") else []


def render_semantic(src_block: dict[str, Any], overlay: dict[str, Any]) -> tuple[str, list[str]]:
    body = json.dumps(src_block["mdl"], indent=2) + "\n"
    try:
        summary = summarize(parse_mdl(body))
    except SemanticError:
        summary = body[:2000]
    notes = src_block.get("notes") or []
    if notes:
        summary += "\nNot queryable through satchel:\n" + "\n".join(f"- {n}" for n in notes)
    return body, [summary, *_notes_chunk(overlay)]


def semantic_content(
    source_name: str, kind: str, priority: int, m: SourceSemanticModel, overlay: dict[str, Any]
) -> ItemContent:
    block = source_block(source_name, kind, m.key, priority, {
        "name": m.name, "mdl": m.mdl, "queryable_metrics": m.queryable_metrics, "notes": m.notes,
    })
    body, chunks = render_semantic(block, overlay)
    links: list[LinkSpec] = []
    for model in m.mdl.get("models") or []:
        links += [(t, "uses") for t in model_tables(m.mdl, model["name"])]
    return ItemContent(
        key=f"sources/{source_name}/semantic/{slugify(m.name)}.mdl.json", type="semantic_model",
        body=body, meta={"format": "wren_mdl", "source": block, "overlay": overlay},
        chunks=chunks, source_key=m.key, links=links,
    )


# ---- golden_query / glossary_term ---------------------------------------------------------


def query_content(
    source_name: str, kind: str, priority: int, q: SourceQuery, overlay: dict[str, Any],
    connection: str | None,
) -> ItemContent:
    block = source_block(source_name, kind, q.key, priority, {"query": q.model_dump(mode="json")})
    return _render_query(block, overlay, connection)


def _render_query(
    block: dict[str, Any], overlay: dict[str, Any], connection: str | None
) -> ItemContent:
    q = block["query"]
    doc: dict[str, Any] = {"question": q["question"]}
    if q.get("variants"):
        doc["variants"] = q["variants"]
    if q.get("sql"):
        doc["sql"] = q["sql"]
        if connection:
            doc["connection"] = connection
    if q.get("metric_query"):
        doc["metric_query"] = q["metric_query"]
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    model = validate_item("golden_query", body)
    tables = extract_tables(q["sql"]) if q.get("sql") else []
    return ItemContent(
        key=f"sources/{block['source']}/queries/{slugify(q['key'])}.yaml", type="golden_query",
        body=body,
        meta=normalized_meta(
            "golden_query", body,
            {"question": q["question"], "connection": doc.get("connection"),
             "source": block, "overlay": overlay},
        ),
        chunks=[render_yaml_item("golden_query", model), *_notes_chunk(overlay)],
        source_key=q["key"],
        scope_kind="connection" if connection and q.get("sql") else "workspace",
        links=[(t, "uses") for t in tables],
    )


def term_content(
    source_name: str, kind: str, priority: int, t: SourceTerm, overlay: dict[str, Any]
) -> ItemContent:
    block = source_block(source_name, kind, t.key, priority, {"term": t.model_dump(mode="json")})
    return _render_term(block, overlay)


def _render_term(block: dict[str, Any], overlay: dict[str, Any]) -> ItemContent:
    t = block["term"]
    doc: dict[str, Any] = {"term": t["term"], "definition": t["definition"]}
    if t.get("synonyms"):
        doc["synonyms"] = t["synonyms"]
    if t.get("related_assets"):
        doc["related_assets"] = t["related_assets"]
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    model = validate_item("glossary_term", body)
    return ItemContent(
        key=f"sources/{block['source']}/glossary/{slugify(t['key'])}.yaml", type="glossary_term",
        body=body,
        meta=normalized_meta(
            "glossary_term", body,
            {"term": t["term"], "synonyms": t.get("synonyms") or [], "source": block,
             "overlay": overlay},
        ),
        chunks=[render_yaml_item("glossary_term", model), *_notes_chunk(overlay)],
        source_key=t["key"], links=[(a, "mentions") for a in t.get("related_assets") or []],
    )


def rebuild(item_type: str, src_block: dict[str, Any], overlay: dict[str, Any],
            connection: str | None = None) -> tuple[str, dict[str, Any]]:
    """``(body, meta)`` of a new version from an existing ``source`` block and a new overlay.
    Used when an analyst edits the overlay: the source block is carried over untouched."""
    if item_type == "asset_doc":
        body, meta, _ = render_asset(src_block, overlay)
        return body, meta
    if item_type == "semantic_model":
        body, _ = render_semantic(src_block, overlay)
        return body, {"format": "wren_mdl", "source": src_block, "overlay": overlay}
    if item_type == "golden_query":
        c = _render_query(src_block, overlay, connection)
        return c.body, c.meta
    if item_type == "glossary_term":
        c2 = _render_term(src_block, overlay)
        return c2.body, c2.meta
    raise ValueError(f"{item_type} items cannot be source-managed")


def chunks_from_meta(item_type: str, body: str, meta: dict[str, Any]) -> list[str]:
    """Chunk texts of a stored source-managed version (mirrors what the sync produced)."""
    src, overlay = meta.get("source") or {}, meta.get("overlay") or {}
    if item_type == "asset_doc":
        return render_asset(src, overlay)[2]
    if item_type == "semantic_model":
        return render_semantic(src, overlay)[1]
    if item_type == "golden_query":
        return _render_query(src, overlay, meta.get("connection")).chunks
    if item_type == "glossary_term":
        return _render_term(src, overlay).chunks
    raise ValueError(f"{item_type} items cannot be source-managed")


__all__ = [
    "AssetColumn",
    "AssetOverlay",
    "ItemContent",
    "NotesOverlay",
    "asset_content",
    "chunks_from_meta",
    "clean_overlay",
    "merge_asset_doc",
    "overlay_model",
    "query_content",
    "rebuild",
    "semantic_content",
    "term_content",
]
