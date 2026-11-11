"""dbt models, sources, seeds and snapshots -> ``SourceAsset`` (tests, exposures and groups
folded in)."""

from __future__ import annotations

import re
from typing import Any

from satchel_adapter_sdk import (
    SourceAsset,
    SourceColumn,
    SourceRelationship,
    normalise_fqn,
)

from satchel.sources.adapters.dbt.manifest import Dbt, Node

_REF = re.compile(r"""ref\(\s*(?:['"][^'"]+['"]\s*,\s*)?['"]([^'"]+)['"]""")
_SOURCE = re.compile(r"""source\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)""")


def asset_fqn(node: Node) -> str | None:
    """``schema.table`` of a node, or ``None`` when dbt recorded no schema."""
    if not node.schema_ or not node.table:
        return None
    return normalise_fqn(f"{node.schema_}.{node.table}")


def _strs(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list | tuple):
        return [str(x) for x in v if x is not None and str(x).strip()]
    return []


def _satchel_meta(meta: dict[str, Any]) -> dict[str, Any]:
    raw = meta.get("satchel")
    return raw if isinstance(raw, dict) else {}


def build_assets(dbt: Dbt) -> list[SourceAsset]:
    """Every enabled, non-ephemeral model/seed/snapshot, plus documented source tables."""
    candidates: list[Node] = []
    for node in dbt.nodes.values():
        if not node.config.enabled:
            continue
        if node.config.materialized == "ephemeral":
            continue
        candidates.append(node)
    for src in dbt.sources.values():
        documented = bool(src.description or src.source_description) or any(
            c.description for c in src.columns.values()
        )
        if src.config.enabled and documented:
            candidates.append(src)

    key_to_fqn: dict[str, str] = {}
    by_name: dict[str, list[Node]] = {}
    for node in candidates:
        fqn = asset_fqn(node)
        if fqn is None:
            dbt.warn("no_relation", "no schema/relation recorded; skipped", node.unique_id)
            continue
        key_to_fqn[node.unique_id] = fqn
        by_name.setdefault(node.name, []).append(node)
    # a node's fqn is also reachable via the ref()/source() names used in tests
    all_nodes = {**dbt.nodes, **dbt.sources}

    exposures_by_node: dict[str, list[str]] = {}
    for ex in dbt.exposures:
        if not ex.config.enabled:
            continue
        label = ex.label or ex.name
        text = f"{label} ({ex.type})" if ex.type else label
        if ex.url:
            text += f" {ex.url}"
        for uid in ex.depends_on.nodes:
            exposures_by_node.setdefault(uid, []).append(text)

    tests_by_node: dict[str, list[Node]] = {}
    for t in dbt.tests:
        if t.attached_node and t.config.enabled:
            tests_by_node.setdefault(t.attached_node, []).append(t)

    out: list[SourceAsset] = []
    for node in candidates:
        fqn = key_to_fqn.get(node.unique_id)
        if fqn is None:
            continue
        out.append(
            _asset(dbt, node, fqn, key_to_fqn, all_nodes, tests_by_node.get(node.unique_id, []),
                   exposures_by_node.get(node.unique_id, []))
        )
    out.sort(key=lambda a: a.key)
    return out


def _asset(
    dbt: Dbt,
    node: Node,
    fqn: str,
    key_to_fqn: dict[str, str],
    all_nodes: dict[str, Node],
    tests: list[Node],
    used_by: list[str],
) -> SourceAsset:
    meta = node.all_meta()
    satchel = _satchel_meta(meta)
    catalog_cols = dbt.catalog_columns(node.unique_id)
    catalog_lc = {n.lower(): (n, t) for n, t in catalog_cols.items()}

    cols: dict[str, SourceColumn] = {}
    for c in node.columns.values():
        cmeta = {**c.meta}
        csatchel = _satchel_meta(cmeta)
        cat = catalog_lc.get(c.name.lower())
        dtype = (c.data_type or (cat[1] if cat else None)) or None
        cols[c.name.lower()] = SourceColumn(
            name=c.name,
            description=(c.description or "").strip() or None,
            data_type=dtype.lower() if dtype else None,
            pii=bool(csatchel.get("pii", cmeta.get("pii", False))),
            unit=str(csatchel["unit"]) if csatchel.get("unit") else None,
            enum_values=_strs(csatchel.get("enum_values")),
            tags=sorted(set(c.tags)),
        )
    for lc, (name, dtype) in catalog_lc.items():  # undocumented columns the warehouse has
        if lc not in cols:
            cols[lc] = SourceColumn(name=name, data_type=dtype or None)

    relationships: list[SourceRelationship] = []
    grain_cols: list[str] = []
    combo_grain: list[str] = []
    for t in sorted(tests, key=lambda x: x.unique_id):
        md = t.test_metadata
        if md is None:
            continue
        kwargs = md.kwargs_
        col = t.column_name or kwargs.get("column_name")
        where = t.config.where
        kind = md.name
        if kind == "unique_combination_of_columns" and not where:
            combo = _strs(kwargs.get("combination_of_columns"))
            if combo:
                combo_grain = combo
            continue
        if not col or str(col).lower() not in cols:
            if kind in ("accepted_values", "not_null", "unique", "relationships") and col:
                dbt.warn("test_column", f"test {t.name} targets unknown column {col!r}",
                         t.unique_id)
            continue
        column = cols[str(col).lower()]
        if kind == "not_null" and not where:
            column.not_null = True
        elif kind == "unique" and not where:
            column.unique = True
        elif kind == "accepted_values":
            values = kwargs.get("values")
            if where:
                dbt.warn("filtered_test", "accepted_values with `where` ignored", t.unique_id)
            elif isinstance(values, list):
                column.enum_values = [str(v) for v in values]
            else:
                dbt.warn("malformed_test", "accepted_values without a values list", t.unique_id)
        elif kind == "relationships":
            target = _resolve_ref(str(kwargs.get("to") or ""), all_nodes, key_to_fqn)
            field_ = kwargs.get("field")
            if target is None:
                dbt.warn("unresolved_ref", f"cannot resolve relationships.to={kwargs.get('to')!r}",
                         t.unique_id)
            else:
                relationships.append(
                    SourceRelationship(
                        column=column.name, to_asset=target,
                        to_column=str(field_) if field_ else None,
                    )
                )

    for pk in node.primary_key:
        if pk.lower() in cols:
            grain_cols.append(cols[pk.lower()].name)
    if not grain_cols:
        grain_cols = combo_grain or [c.name for c in cols.values() if c.unique and c.not_null][:1]
    if "grain" in satchel:
        grain_cols = _strs(satchel["grain"])

    owner = _owner(dbt, node, meta)
    endorsed = satchel.get("endorsed")
    hint = None
    if not isinstance(endorsed, bool):
        endorsed = None
        if node.effective_access() == "public" and node.contract_enforced():
            hint = "dbt marks this model access: public with an enforced contract"

    depends = sorted(
        {d for d in node.depends_on.nodes if d in key_to_fqn and d != node.unique_id}
    )
    kind = "view" if node.config.materialized == "view" else "table"
    description = (node.description or "").strip() or (
        (node.source_description or "").strip() if node.resource_type == "source" else ""
    )
    source_meta: dict[str, Any] = {
        "unique_id": node.unique_id,
        "resource_type": node.resource_type,
        "package": node.package_name,
        "path": node.original_file_path,
        "materialized": node.config.materialized,
        "access": node.effective_access(),
        "contract_enforced": node.contract_enforced(),
        "group": node.group or node.config.group,
        "version": node.version,
    }
    return SourceAsset(
        key=node.unique_id,
        fqn=fqn,
        kind=kind,
        name=node.name,
        database=node.database,
        description=description or None,
        columns=list(cols.values()),
        grain=grain_cols,
        relationships=sorted(
            relationships, key=lambda r: (r.column, r.to_asset, r.to_column or "")
        ),
        tags=sorted(set(node.tags) | set(_strs(node.config.meta.get("tags")))),
        owner=owner,
        endorsed=endorsed,
        endorsement_hint=hint,
        caveats=_strs(satchel.get("caveats")),
        used_by=sorted(set(used_by)),
        depends_on=depends,
        source_meta={k: v for k, v in source_meta.items() if v not in (None, "")},
    )


def _owner(dbt: Dbt, node: Node, meta: dict[str, Any]) -> str | None:
    owner = meta.get("owner")
    if isinstance(owner, str) and owner.strip():
        return owner.strip()
    if isinstance(owner, dict):
        label = owner.get("email") or owner.get("name")
        if label:
            return str(label)
    gname = node.group or node.config.group
    if gname:
        for uid, g in dbt.groups.items():
            if g.name == gname or uid.endswith(f".{gname}"):
                return g.owner.label()
    return None


def _resolve_ref(expr: str, all_nodes: dict[str, Node], key_to_fqn: dict[str, str]) -> str | None:
    """``ref('customers')`` / ``source('raw', 'orders')`` -> the target's normalised fqn."""
    m = _SOURCE.search(expr)
    if m:
        for n in all_nodes.values():
            if n.resource_type == "source" and n.source_name == m.group(1) and n.name == m.group(2):
                return key_to_fqn.get(n.unique_id) or asset_fqn(n)
        return None
    m = _REF.search(expr)
    if m:
        for n in all_nodes.values():
            if n.resource_type != "source" and n.name == m.group(1):
                return key_to_fqn.get(n.unique_id) or asset_fqn(n)
    return None
