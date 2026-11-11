"""Semantic engine (design §11, spike A): published ``semantic_model`` items (format
``wren_mdl``) -> a manifest -> ``wren-core-py``.

Pipeline for a metric query::

    metric query --cube_query_to_sql--> semantic SQL over the base model
                 --SessionContext.transform_sql--> DataFusion-flavoured SQL, models expanded
                 --sqlglot.transpile(read=postgres, write=<dialect>)--> dialect SQL

The last step replaces ``wrenai``'s ``WrenEngine.dry_plan`` so the full SDK (pandas, boto3,
~300 MB) is not a dependency; ``transform_sql`` output parses cleanly as Postgres-style SQL.
``wren-core-py`` is pinned exactly in pyproject.toml; golden tests guard upgrades.
"""

from __future__ import annotations

import base64
import difflib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import sqlglot
import yaml
from sqlalchemy.orm import Session as OrmSession

from satchel.curation.releases import release_items_stmt
from satchel.data.gateway import DIALECTS
from satchel.sources.precedence import doc_ref, load_sources
from satchel.store.models import ContextItem

GRANULARITIES = ("year", "quarter", "month", "week", "day", "hour", "minute")
OPERATORS = ("eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte", "contains",
             "starts_with", "is_null", "is_not_null")


class SemanticError(Exception):
    """A fixable problem with a metric query or model. Shown to the agent."""


def parse_mdl(body: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(body)  # JSON is a YAML subset
    except yaml.YAMLError as e:
        raise SemanticError(f"semantic model is not valid JSON/YAML: {e}") from None
    if not isinstance(data, dict):
        raise SemanticError("semantic model must be a mapping with models/cubes")
    return data


_PARTS = ("models", "relationships", "cubes", "views")


@dataclass
class PublishedModel:
    item_id: uuid.UUID
    key: str
    seq: int
    mdl: dict[str, Any]
    # precedence (satchel.sources.precedence): best first; hand-written beats a source at a tie
    rank: tuple[int, int, str, str] = (0, 0, "", "")
    handwritten: bool = True


def load_published(s: OrmSession, release_id: uuid.UUID | None = None) -> list[PublishedModel]:
    """Semantic models of the latest published state, or of ``release_id`` (evals), best
    precedence first (``sources[].priority``, lower wins; see ``satchel.sources.precedence``)."""
    out: list[PublishedModel] = []
    stmt = release_items_stmt(release_id)
    rows = s.execute(stmt.where(ContextItem.type == "semantic_model").order_by(ContextItem.key))
    sources = load_sources(s)
    for item, ver in rows:
        if (ver.meta or {}).get("format") != "wren_mdl":
            continue
        ref = doc_ref(item, sources)
        out.append(PublishedModel(item.id, item.key, ver.seq, parse_mdl(ver.body),
                                  rank=ref.sort_key(), handwritten=ref.handwritten))
    out.sort(key=lambda m: m.rank)
    return out


def _claims(m: PublishedModel) -> dict[tuple[str, str], str]:
    return {(part, obj["name"]): m.key for part in _PARTS for obj in m.mdl.get(part) or []}


def shadowed_models(models: list[PublishedModel]) -> dict[str, str]:
    """``{item key: reason}`` of the semantic models that :func:`merge_manifests` leaves out.

    Two source-managed (or a source and a hand-written) semantic models that define the same
    model, cube or relationship name describe the same thing twice; the one with the better
    precedence is served whole and the other is skipped, since mixing halves of two definitions
    could return numbers neither tool stands behind. Two hand-written models that collide stay
    an error (see :func:`merge_manifests`)."""
    claimed: dict[tuple[str, str], PublishedModel] = {}
    skipped: dict[str, str] = {}
    for m in models:
        own = _claims(m)
        clash = sorted(n for n in own if n in claimed)
        if clash and not (m.handwritten and all(claimed[n].handwritten for n in clash)):
            first = claimed[clash[0]]
            names = ", ".join(sorted({n[1] for n in clash})[:4])
            skipped[m.key] = f"shares {names} with {first.key}, which has precedence"
            continue
        for n in own:
            claimed.setdefault(n, m)
    return skipped


def merge_manifests(models: list[PublishedModel]) -> dict[str, Any]:
    """One manifest from several items (lists are concatenated). Duplicate names between two
    hand-written items are an error; a source-managed item that overlaps one with better
    precedence is skipped whole (:func:`shadowed_models`)."""
    if not models:
        raise SemanticError("No published semantic model (format wren_mdl) in this workspace.")
    skipped = shadowed_models(models)
    models = [m for m in models if m.key not in skipped]
    merged: dict[str, Any] = {
        k: v for k, v in models[0].mdl.items() if k not in _PARTS
    }
    for part in _PARTS:
        seen: set[str] = set()
        items: list[Any] = []
        for m in models:
            for obj in m.mdl.get(part) or []:
                if obj["name"] in seen:
                    raise SemanticError(f"duplicate {part} name {obj['name']!r} across models")
                seen.add(obj["name"])
                items.append(obj)
        merged[part] = items
    return merged


def summarize(mdl: dict[str, Any]) -> str:
    """Searchable text for chunking/embedding."""
    lines = ["Semantic model (governed metrics)"]
    for cube in mdl.get("cubes") or []:
        lines.append(f"Cube {cube['name']}: {cube.get('description', '')}".strip())
        for m in cube.get("measures") or []:
            lines.append(f"- metric {m['name']} = {m['expression']}. {m.get('description', '')}")
        dims = [d["name"] for d in (cube.get("dimensions") or [])]
        tdims = [d["name"] for d in (cube.get("timeDimensions") or [])]
        lines.append(f"  dimensions: {', '.join(dims + tdims)}")
    return "\n".join(lines)


def model_tables(mdl: dict[str, Any], model_name: str) -> list[str]:
    """Underlying warehouse tables of a model (refSql tables or its tableReference)."""
    from satchel.sync.sqlrefs import extract_tables

    for m in mdl.get("models") or []:
        if m["name"] == model_name:
            if m.get("refSql"):
                return extract_tables(m["refSql"])
            ref = m.get("tableReference") or {}
            if ref.get("table"):
                return [f"{ref['schema']}.{ref['table']}" if ref.get("schema") else ref["table"]]
    return []


def _cubes(mdl: dict[str, Any]) -> list[dict[str, Any]]:
    return list(mdl.get("cubes") or [])


def list_metrics(mdl: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in _cubes(mdl):
        out.append({
            "cube": c["name"],
            "description": c.get("description"),
            "metrics": [
                {"name": m["name"], "description": m.get("description")}
                for m in c.get("measures") or []
            ],
            "dimensions": [d["name"] for d in c.get("dimensions") or []],
            "time_dimensions": [d["name"] for d in c.get("timeDimensions") or []],
        })
    return out


def _find_measure(mdl: dict[str, Any], name: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    cube_name, _, bare = name.rpartition(".")
    return [
        (c, m)
        for c in _cubes(mdl)
        for m in c.get("measures") or []
        if m["name"] == bare and (not cube_name or c["name"] == cube_name)
    ]


def all_metric_names(mdl: dict[str, Any]) -> list[str]:
    return [m["name"] for c in _cubes(mdl) for m in c.get("measures") or []]


def describe_metric(mdl: dict[str, Any], name: str) -> dict[str, Any]:
    hits = _find_measure(mdl, name)
    if not hits:
        close = difflib.get_close_matches(name, all_metric_names(mdl), n=5, cutoff=0.3)
        raise SemanticError(
            f"Unknown metric {name!r}. Did you mean: {close or all_metric_names(mdl)}? "
            "Call list_metrics to see all metrics."
        )
    if len(hits) > 1:
        raise SemanticError(
            f"Ambiguous metric {name!r}; qualify it as cube.metric: "
            f"{[f'{c['name']}.{m['name']}' for c, m in hits]}"
        )
    cube, m = hits[0]
    base: dict[str, Any] = next(
        (x for x in mdl.get("models") or [] if x["name"] == cube["baseObject"]), {}
    )
    return {
        "metric": m["name"],
        "cube": cube["name"],
        "description": m.get("description"),
        "expression": m["expression"],
        "type": m.get("type"),
        "base_model": cube["baseObject"],
        "base_filter_sql": base.get("refSql"),
        "source_tables": model_tables(mdl, cube["baseObject"]),
        "dimensions": [
            {"name": d["name"], "description": d.get("description")}
            for d in cube.get("dimensions") or []
        ],
        "time_dimensions": [
            {"name": d["name"], "description": d.get("description"),
             "granularities": list(GRANULARITIES)}
            for d in cube.get("timeDimensions") or []
        ],
        "filter_operators": list(OPERATORS),
    }


def build_cube_query(
    mdl: dict[str, Any],
    metrics: list[str],
    dimensions: list[str],
    filters: list[dict[str, Any]],
    order_by: list[dict[str, str]],
    limit: int | None,
) -> dict[str, Any]:
    """Resolve names to a cube and build the ``cube_query_to_sql`` JSON.

    ``dimensions`` entries may carry a grain: ``order_date:month``.
    """
    if not metrics:
        raise SemanticError("Pass at least one metric (see list_metrics).")
    cube: dict[str, Any] | None = None
    measures: list[str] = []
    for name in metrics:
        hits = _find_measure(mdl, name)
        if not hits:
            close = difflib.get_close_matches(name, all_metric_names(mdl), n=5, cutoff=0.3)
            raise SemanticError(f"Unknown metric {name!r}. Did you mean: {close}? "
                                "Call list_metrics.")
        if len(hits) > 1:
            raise SemanticError(f"Ambiguous metric {name!r}; use cube.metric.")
        c, m = hits[0]
        if cube is not None and c["name"] != cube["name"]:
            raise SemanticError(
                f"Metrics {metrics} belong to different cubes ({cube['name']}, {c['name']}); "
                "query them separately."
            )
        cube = c
        measures.append(m["name"])
    assert cube is not None
    dim_names = {d["name"] for d in cube.get("dimensions") or []}
    tdim_names = {d["name"] for d in cube.get("timeDimensions") or []}

    def check_dim(n: str, *, allow_time: bool = True) -> None:
        if n in dim_names or (allow_time and n in tdim_names):
            return
        close = difflib.get_close_matches(n, sorted(dim_names | tdim_names), n=5, cutoff=0.3)
        raise SemanticError(
            f"Unknown dimension {n!r} for cube {cube['name']!r}. Valid: "
            f"{sorted(dim_names)} and time dimensions {sorted(tdim_names)} "
            f"(append :{'|'.join(GRANULARITIES)}). Close: {close}"
        )

    q: dict[str, Any] = {"cube": cube["name"], "measures": measures, "dimensions": [],
                         "timeDimensions": [], "filters": []}
    for d in dimensions:
        name, _, grain = d.partition(":")
        check_dim(name)
        if name in tdim_names:
            if grain not in GRANULARITIES:
                raise SemanticError(
                    f"Time dimension {name!r} needs a granularity: {name}:month "
                    f"(one of {', '.join(GRANULARITIES)})."
                )
            q["timeDimensions"].append({"dimension": name, "granularity": grain})
        else:
            if grain:
                raise SemanticError(f"{name!r} is not a time dimension; drop ':{grain}'.")
            q["dimensions"].append(name)
    for f in filters:
        check_dim(str(f.get("dimension")))
        op = str(f.get("operator"))
        if op not in OPERATORS:
            raise SemanticError(f"Unknown filter operator {op!r}. Use one of {list(OPERATORS)}.")
        item: dict[str, Any] = {"dimension": f["dimension"], "operator": op}
        if op not in ("is_null", "is_not_null"):
            if f.get("value") is None:
                raise SemanticError(f"Filter on {f['dimension']!r} needs a value.")
            item["value"] = f["value"]
        q["filters"].append(item)
    valid_members = set(measures) | dim_names | tdim_names
    ords = []
    for o in order_by:
        member = o["member"]
        if member.split(":")[0] not in valid_members:
            raise SemanticError(f"order_by member {member!r} is not a selected metric/dimension.")
        ords.append({"member": member.split(":")[0], "direction": o.get("direction", "asc")})
    if ords:
        q["orderBy"] = ords
    if limit:
        q["limit"] = limit
    for k in ("dimensions", "timeDimensions", "filters"):
        if not q[k]:
            del q[k]
    return q


@dataclass
class Compiled:
    cube_query: dict[str, Any]
    semantic_sql: str
    sql: str


def to_dialect(mdl: dict[str, Any], semantic_sql: str, dialect: str) -> str:
    from wren_core import SessionContext

    b64 = base64.b64encode(json.dumps(mdl).encode()).decode()
    try:
        expanded = SessionContext(b64, None).transform_sql(semantic_sql)
    except Exception as e:
        raise SemanticError(f"Could not plan the metric query: {e}") from None
    return sqlglot.transpile(expanded, read="postgres", write=DIALECTS.get(dialect, dialect))[0]


def compile_query(mdl: dict[str, Any], cube_query: dict[str, Any], dialect: str) -> Compiled:
    from wren_core import cube_query_to_sql

    try:
        semantic = cube_query_to_sql(json.dumps(cube_query), json.dumps(mdl))
    except Exception as e:
        raise SemanticError(f"Invalid metric query: {e}") from None
    return Compiled(cube_query, semantic, to_dialect(mdl, semantic, dialect))
