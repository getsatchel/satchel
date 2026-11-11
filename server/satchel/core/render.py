"""Render validated items to readable text for indexing (design §4, chunking)."""

from __future__ import annotations

from pydantic import BaseModel

from satchel.core.item_types import AssetDoc, GlossaryTerm, GoldenQuery


def render_glossary(t: GlossaryTerm) -> str:
    lines = [f"Glossary term: {t.term}"]
    if t.synonyms:
        lines.append(f"Also known as: {', '.join(t.synonyms)}")
    lines.append(f"Definition: {t.definition.strip()}")
    if t.related_assets:
        lines.append(f"Related tables: {', '.join(t.related_assets)}")
    return "\n".join(lines)


def render_golden_query(q: GoldenQuery) -> str:
    lines = [f"Verified query for: {q.question}"]
    if q.variants:
        lines.append(f"Also asked as: {'; '.join(q.variants)}")
    if q.sql:
        lines.append(f"SQL:\n{q.sql.strip()}")
    if q.metric_query:
        lines.append(f"Metric query: {q.metric_query}")
    if q.verified_by:
        lines.append(f"Verified by: {q.verified_by}")
    return "\n".join(lines)


def render_asset_doc(a: AssetDoc) -> str:
    lines = [f"Table {a.asset}" + (" (endorsed: preferred source)" if a.endorsed else "")]
    if a.description:
        lines.append(a.description.strip())
    if a.grain:
        lines.append(f"Grain: one row per {a.grain}")
    if a.unit:
        lines.append(f"Unit: {a.unit}")
    if a.enum_values:
        lines.append(f"Values: {', '.join(a.enum_values)}")
    if a.caveats:
        lines.append(f"See caveats: {', '.join(a.caveats)}")
    if a.owner:
        lines.append(f"Owner: {a.owner}")
    if a.tags:
        lines.append(f"Tags: {', '.join(a.tags)}")
    if a.joins:
        lines.append(f"Joins: {'; '.join(a.joins)}")
    if a.used_by:
        lines.append(f"Used by: {'; '.join(a.used_by)}")
    for c in a.columns:
        bits = [c.description or ""]
        if c.data_type:
            bits.append(f"type: {c.data_type}")
        if c.required:
            bits.append("not null")
        if c.unique:
            bits.append("unique")
        if c.unit:
            bits.append(f"unit: {c.unit}")
        if c.enum_values:
            bits.append(f"values: {', '.join(c.enum_values)}")
        if c.pii:
            bits.append("PII")
        lines.append(f"- {c.name}: {'; '.join(b for b in bits if b)}")
    return "\n".join(lines)


def render_yaml_item(item_type: str, model: BaseModel) -> str:
    if isinstance(model, GlossaryTerm):
        return render_glossary(model)
    if isinstance(model, GoldenQuery):
        return render_golden_query(model)
    if isinstance(model, AssetDoc):
        return render_asset_doc(model)
    return model.model_dump_json()
