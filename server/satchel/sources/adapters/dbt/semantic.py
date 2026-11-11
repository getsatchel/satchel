"""dbt's semantic layer (MetricFlow) -> one Wren MDL manifest per dbt project, plus golden queries
from saved queries.

Mapping: a semantic model becomes an MDL *model* over its warehouse relation and a *cube* of the
same name; entities and dimensions become cube dimensions, time dimensions become cube time
dimensions (plus a ``metric_time`` alias of the default time dimension) and each metric becomes a
cube measure whose expression inlines its measure aggregation and filters. Metric types that cannot
be expressed that way (cumulative, conversion, offsets, metrics spanning several semantic models,
dimensions joined from another semantic model) are imported as notes, never as queryable metrics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from satchel_adapter_sdk import MdlBuilder, SourceQuery, SourceSemanticModel, SourceWarning
from sqlglot import exp
from sqlglot.errors import SqlglotError

from satchel.sources.adapters.dbt.manifest import Dbt

DATA_SOURCES = {
    "duckdb": "duckdb", "postgres": "postgres", "bigquery": "bigquery", "snowflake": "snowflake",
    "redshift": "postgres", "mysql": "mysql", "sqlserver": "mssql", "databricks": "databricks",
    "spark": "spark", "trino": "trino",
}
_CALL = re.compile(
    r"\{\{\s*(Dimension|Entity|TimeDimension|Metric)\(\s*'([^']*)'(?:\s*,\s*'([^']*)')?[^)]*\)"
    r"(?:\.grain\(\s*'([^']*)'\s*\))?\s*\}\}"
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _T(BaseModel):
    model_config = ConfigDict(extra="ignore")


class NodeRelation(_T):
    alias: str | None = None
    schema_name: str | None = None
    database: str | None = None


class Entity(_T):
    name: str
    type: str = "foreign"
    expr: str | None = None


class DimensionDef(_T):
    name: str
    type: str = "categorical"
    description: str | None = None
    expr: str | None = None
    type_params: dict[str, Any] | None = None


class MeasureDef(_T):
    name: str
    agg: str
    description: str | None = None
    expr: str | None = None
    create_metric: bool | None = None
    non_additive_dimension: dict[str, Any] | None = None
    agg_time_dimension: str | None = None


class SemanticModelDef(_T):
    name: str
    description: str | None = None
    node_relation: NodeRelation = Field(default_factory=NodeRelation)
    defaults: dict[str, Any] | None = None
    entities: list[Entity] = Field(default_factory=list)
    dimensions: list[DimensionDef] = Field(default_factory=list)
    measures: list[MeasureDef] = Field(default_factory=list)
    depends_on: dict[str, Any] | None = None


class MetricDef(_T):
    name: str
    type: str = "simple"
    label: str | None = None
    description: str | None = None
    type_params: dict[str, Any] = Field(default_factory=dict)
    filter: dict[str, Any] | None = None
    config: dict[str, Any] | None = None


class SavedQueryDef(_T):
    name: str
    label: str | None = None
    description: str | None = None
    query_params: dict[str, Any] = Field(default_factory=dict)


class Unsupported(Exception):
    """A construct the MDL cannot express; the metric becomes a note."""


@dataclass
class _Sm:
    d: SemanticModelDef
    entities: dict[str, Entity] = field(default_factory=dict)
    dims: dict[str, DimensionDef] = field(default_factory=dict)
    measures: dict[str, MeasureDef] = field(default_factory=dict)
    time_dim: str | None = None

    @property
    def name(self) -> str:
        return self.d.name


def _load[T: BaseModel](dbt: Dbt, cls: type[T], rows: list[Any], what: str) -> list[T]:
    out: list[T] = []
    for raw in rows:
        try:
            out.append(cls.model_validate(raw))
        except ValidationError as e:
            name = raw.get("name") if isinstance(raw, dict) else "?"
            dbt.warn("invalid_node", f"{what} {name}: {e.errors()[0]['msg']}", f"{what}.{name}")
    return out


def _rows(src: Any) -> list[Any]:
    if isinstance(src, dict):  # manifest.json keys these by unique_id
        return [v for _, v in sorted(src.items())]
    return list(src or [])


def _where(filter_: dict[str, Any] | None) -> list[str]:
    return [
        str(f.get("where_sql_template") or "").strip()
        for f in (filter_ or {}).get("where_filters") or []
        if str(f.get("where_sql_template") or "").strip()
    ]


class _Builder:
    def __init__(self, dbt: Dbt, sms: list[_Sm], metrics: list[MetricDef]) -> None:
        self.dbt = dbt
        self.sms = {s.name: s for s in sms}
        self.measure_owner = {m: s for s in sms for m in s.measures}
        self.metrics = {m.name: m for m in metrics}

    # ---- filters -------------------------------------------------------------------------

    def member(self, sm: _Sm, ref: str, grain: str | None = None) -> str:
        if ref == "metric_time":
            if sm.time_dim is None:
                raise Unsupported("metric_time is used but the model has no time dimension")
            expr = self._dim_expr(sm, sm.time_dim)
        else:
            parts = ref.split("__")
            if len(parts) >= 2:
                if parts[0] not in sm.entities:
                    raise Unsupported(
                        f"dimension {ref!r} comes from another semantic model (joins between "
                        "semantic models are not supported)"
                    )
                name = "__".join(parts[1:])
            else:
                name = ref
            if name in sm.dims:
                expr = self._dim_expr(sm, name)
            elif name in sm.entities:
                expr = sm.entities[name].expr or name
            else:
                raise Unsupported(f"unknown dimension or entity {ref!r} in {sm.name}")
        return f"DATE_TRUNC('{grain}', {expr})" if grain else expr

    @staticmethod
    def _dim_expr(sm: _Sm, name: str) -> str:
        d = sm.dims[name]
        return d.expr or d.name

    def render_filter(self, sm: _Sm, template: str) -> str:
        def sub(m: re.Match[str]) -> str:
            call, ref, grain_arg, grain_chain = m.group(1), m.group(2), m.group(3), m.group(4)
            if call == "Metric":
                raise Unsupported("filters on Metric(...) are not supported")
            grain = grain_chain or (grain_arg if call == "TimeDimension" else None)
            return self.member(sm, ref, grain)

        sql = _CALL.sub(sub, template)
        if "{{" in sql or "{%" in sql:
            raise Unsupported(f"unsupported Jinja in filter: {template.strip()[:80]!r}")
        return f"({' '.join(sql.split())})"

    # ---- expressions ---------------------------------------------------------------------

    def aggregate(self, sm: _Sm, measure: MeasureDef, filters: list[str]) -> str:
        expr = measure.expr or measure.name
        if measure.non_additive_dimension:
            raise Unsupported(f"measure {measure.name!r} is semi-additive")
        flt = " AND ".join(self.render_filter(sm, f) for f in filters)
        agg = measure.agg.lower()
        if agg == "sum_boolean":
            expr = f"CASE WHEN {expr} THEN 1 ELSE 0 END"
        arg = f"CASE WHEN {flt} THEN {expr} END" if flt else expr
        fn = {
            "sum": "SUM", "sum_boolean": "SUM", "count": "COUNT", "min": "MIN", "max": "MAX",
            "average": "AVG", "avg": "AVG", "median": "MEDIAN",
        }
        if agg == "count_distinct":
            return f"COUNT(DISTINCT {arg})"
        if agg in fn:
            return f"{fn[agg]}({arg})"
        raise Unsupported(f"aggregation {measure.agg!r} is not supported")

    def metric_expr(
        self, name: str, extra: list[str], seen: tuple[str, ...] = ()
    ) -> tuple[str, _Sm]:
        if name in seen:
            raise Unsupported(f"metric {name!r} depends on itself")
        m = self.metrics.get(name)
        if m is None:
            raise Unsupported(f"unknown metric {name!r}")
        tp = m.type_params
        own = [*_where(m.filter), *extra]
        kind = m.type.lower()
        if kind == "simple":
            ref = tp.get("measure") or {}
            mname = ref.get("name") if isinstance(ref, dict) else None
            owner = self.measure_owner.get(str(mname))
            if owner is None:
                raise Unsupported(f"measure {mname!r} not found in any semantic model")
            sm = owner
            expr = self.aggregate(sm, sm.measures[str(mname)], [*_where(ref.get("filter")), *own])
            fill = ref.get("fill_nulls_with") if isinstance(ref, dict) else None
            if isinstance(fill, int | float) and not isinstance(fill, bool):
                expr = f"COALESCE({expr}, {fill})"
            return expr, sm
        if kind == "ratio":
            parts = []
            owners: set[str] = set()
            sm_r: _Sm | None = None
            for side in ("numerator", "denominator"):
                ref = tp.get(side) or {}
                sub, sm_r = self.metric_expr(
                    str(ref.get("name")), [*_where(ref.get("filter")), *own], (*seen, name)
                )
                parts.append(sub)
                owners.add(sm_r.name)
            if len(owners) != 1 or sm_r is None:
                raise Unsupported("a ratio across semantic models cannot be queried")
            return f"CAST({parts[0]} AS DOUBLE) / NULLIF(CAST({parts[1]} AS DOUBLE), 0)", sm_r
        if kind == "derived":
            refs = tp.get("metrics") or []
            subs: dict[str, str] = {}
            owners = set()
            sm_d: _Sm | None = None
            for ref in refs:
                if ref.get("offset_window") or ref.get("offset_to_grain"):
                    raise Unsupported("derived metrics with offset_window/offset_to_grain")
                sub, sm_d = self.metric_expr(
                    str(ref.get("name")), [*_where(ref.get("filter")), *own], (*seen, name)
                )
                subs[str(ref.get("alias") or ref.get("name"))] = f"({sub})"
                owners.add(sm_d.name)
            if len(owners) != 1 or sm_d is None:
                raise Unsupported("a derived metric across semantic models cannot be queried")
            return self._substitute(str(tp.get("expr") or ""), subs), sm_d
        raise Unsupported(f"{kind} metrics cannot be queried through satchel")

    @staticmethod
    def _substitute(expr: str, subs: dict[str, str]) -> str:
        try:
            tree = sqlglot.parse_one(expr)
        except SqlglotError as e:
            raise Unsupported(f"cannot parse derived expression {expr!r}: {e}") from None

        def repl(node: exp.Expr) -> exp.Expr:
            if isinstance(node, exp.Column) and not node.table and node.name in subs:
                return sqlglot.parse_one(subs[node.name])
            return node

        return str(tree.transform(repl).sql())


def _columns_in(sql: str) -> set[str]:
    try:
        tree = sqlglot.parse_one(sql)
    except SqlglotError as e:
        raise Unsupported(f"cannot parse expression {sql[:60]!r}: {e}") from None
    return {c.name for c in tree.find_all(exp.Column)}


def _mdl_type(agg_or_kind: str) -> str:
    return "BIGINT" if agg_or_kind in ("count", "count_distinct", "sum_boolean") else "DOUBLE"


def build_semantic(dbt: Dbt) -> tuple[list[SourceSemanticModel], list[SourceQuery]]:
    warnings = dbt.warnings
    raw = dbt.semantic or {}
    manifest = dbt.raw_manifest
    sm_rows = _rows(raw.get("semantic_models")) or _rows(manifest.get("semantic_models"))
    metric_rows = _rows(raw.get("metrics")) or _rows(manifest.get("metrics"))
    saved_rows = _rows(raw.get("saved_queries")) or _rows(manifest.get("saved_queries"))
    if not sm_rows and not metric_rows:
        return [], []

    sm_defs = _load(dbt, SemanticModelDef, sm_rows, "semantic_model")
    metrics = _load(dbt, MetricDef, metric_rows, "metric")
    sms: list[_Sm] = []
    for d in sm_defs:
        sm = _Sm(d, {e.name: e for e in d.entities}, {x.name: x for x in d.dimensions},
                 {x.name: x for x in d.measures})
        default = (d.defaults or {}).get("agg_time_dimension")
        times = [x.name for x in d.dimensions if x.type == "time"]
        sm.time_dim = str(default) if default in sm.dims else (times[0] if times else None)
        sms.append(sm)
    sms.sort(key=lambda s: s.name)
    builder = _Builder(dbt, sms, metrics)

    # measures with create_metric become metrics of the same name
    for sm in sms:
        for ms in sm.measures.values():
            if ms.create_metric and ms.name not in builder.metrics:
                builder.metrics[ms.name] = MetricDef(
                    name=ms.name, description=ms.description,
                    type_params={"measure": {"name": ms.name}},
                )

    notes: list[str] = []
    per_sm: dict[str, list[tuple[MetricDef, str]]] = {}
    for name in sorted(builder.metrics):
        m = builder.metrics[name]
        try:
            expr, sm = builder.metric_expr(name, [])
            _columns_in(expr)
        except Unsupported as e:
            notes.append(f"metric {name} ({m.type}) is not queryable through satchel: {e}")
            warnings.append(SourceWarning(
                code="metric_not_queryable", message=str(e), key=f"metric.{name}"))
            continue
        per_sm.setdefault(sm.name, []).append((m, expr))

    mdl_builder = MdlBuilder(
        data_source=DATA_SOURCES.get((dbt.adapter_type or "").lower()),
        catalog=dbt.project_name, schema="public",
    )
    queryable: list[str] = []
    guessed = False
    for sm in sms:
        entries = per_sm.get(sm.name)
        if not entries:
            continue
        try:
            guessed |= _add_cube(dbt, mdl_builder, builder, sm, entries)
        except Unsupported as e:
            notes.append(f"semantic model {sm.name} is not queryable through satchel: {e}")
            warnings.append(SourceWarning(
                code="semantic_model_skipped", message=str(e), key=f"semantic_model.{sm.name}"))
            continue
        queryable += [m.name for m, _ in entries]
    if guessed:
        warnings.append(SourceWarning(
            code="types_guessed",
            message="no catalog.json: semantic model column types were guessed; pass --catalog "
                    "(from `dbt docs generate`) for exact types",
        ))
    models: list[SourceSemanticModel] = []
    if queryable:
        models.append(SourceSemanticModel(
            key=f"semantic.{dbt.project_name}",
            name=dbt.project_name,
            description=f"Metrics defined in the dbt project {dbt.project_name}",
            mdl=mdl_builder.build(),
            queryable_metrics=sorted(queryable),
            notes=notes,
        ))
    elif notes:
        warnings.append(SourceWarning(
            code="no_queryable_metrics", message="no dbt metric could be made queryable"))
    return models, _saved_queries(
        dbt, _load(dbt, SavedQueryDef, saved_rows, "saved_query"), set(queryable)
    )


def _add_cube(
    dbt: Dbt, b: MdlBuilder, builder: _Builder, sm: _Sm, entries: list[tuple[MetricDef, str]]
) -> bool:
    """Add the MDL model and cube of one semantic model. Returns True if types were guessed."""
    rel = sm.d.node_relation
    if not rel.alias or not rel.schema_name:
        raise Unsupported("dbt recorded no relation for the semantic model")
    model_uid = next(
        (n for n in (sm.d.depends_on or {}).get("nodes", []) if n in dbt.nodes), None
    ) or next(
        (
            uid for uid, n in dbt.nodes.items()
            if (n.schema_ or "").lower() == rel.schema_name.lower()
            and (n.alias or n.name).lower() == rel.alias.lower()
        ),
        None,
    )
    known: dict[str, str] = {}
    if model_uid:
        known = {k.lower(): v for k, v in dbt.catalog_columns(model_uid).items() if v}
        node = dbt.nodes[model_uid]
        for c in node.columns.values():
            if c.data_type:
                known.setdefault(c.name.lower(), c.data_type.lower())

    Member = tuple[str, str, str, str | None]
    dims: list[Member] = []
    tdims: list[Member] = []
    used: set[str] = set()
    inferred: dict[str, str] = {}
    for d in sorted(sm.d.dimensions, key=lambda x: x.name):
        expr = d.expr or d.name
        used |= _columns_in(expr)
        if d.type == "time":
            tdims.append((d.name, expr, "DATE", d.description))
            for col in _columns_in(expr):
                inferred.setdefault(col.lower(), "date")
        else:
            dims.append((d.name, expr, "VARCHAR", d.description))
            if _IDENT.match(expr):
                inferred.setdefault(expr.lower(), "varchar")
    for e in sorted(sm.d.entities, key=lambda x: x.name):
        if e.name in {x[0] for x in dims} | {x[0] for x in tdims}:
            continue
        expr = e.expr or e.name
        used |= _columns_in(expr)
        dims.append((e.name, expr, "VARCHAR", f"{e.type} entity"))
        if _IDENT.match(expr):
            inferred.setdefault(
                expr.lower(), "integer" if expr.lower().endswith("id") else "varchar"
            )
    if sm.time_dim and "metric_time" not in {x[0] for x in tdims}:
        td = next(x for x in tdims if x[0] == sm.time_dim)
        tdims.append(
            ("metric_time", td[1], "DATE", f"Alias of {sm.time_dim}, the default time dimension")
        )
    measures: list[Member] = []
    for m, expr in sorted(entries, key=lambda x: x[0].name):
        used |= _columns_in(expr)
        kind = m.type.lower()
        tp = m.type_params.get("measure") or {}
        ms = sm.measures.get(str(tp.get("name"))) if kind == "simple" else None
        for col in _columns_in(expr):
            inferred.setdefault(col.lower(), "double")
        measures.append(
            (m.name, expr, _mdl_type(ms.agg.lower()) if ms else "DOUBLE",
             (m.description or m.label or "").strip() or None)
        )
    cols: dict[str, str] = {}
    guessed = False
    for col in sorted(used):
        t = known.get(col.lower())
        if t is None:
            t = inferred.get(col.lower(), "varchar")
            guessed = True
        cols[col] = t
    pk = next((e.expr or e.name for e in sm.d.entities if e.type == "primary"), None)
    b.model(
        sm.name, schema=rel.schema_name, table=rel.alias, columns=cols,
        primary_key=pk if pk and _IDENT.match(pk) and pk in cols else None,
        description=sm.d.description,
    )
    b.cube(sm.name, base=sm.name, measures=measures, dimensions=dims, time_dimensions=tdims,
           description=sm.d.description)
    return guessed


# ---- saved queries -> golden queries ---------------------------------------------------------


def _member_ref(item: str) -> str:
    """``TimeDimension('metric_time', 'month')`` / ``Dimension('a__b')`` / ``a__b`` -> a satchel
    query_metrics dimension (``metric_time:month``, ``b``)."""
    m = _CALL.search("{{ " + item + " }}") if "(" in item else None
    if m:
        call, ref, grain_arg, grain_chain = m.group(1), m.group(2), m.group(3), m.group(4)
        grain = grain_chain or (grain_arg if call == "TimeDimension" else None)
    else:
        ref, grain = item.strip(), None
    if "__" in ref and ref != "metric_time":
        ref = ref.split("__", 1)[1]
    return f"{ref}:{grain}" if grain else ref


def _saved_queries(
    dbt: Dbt, saved: list[SavedQueryDef], queryable: set[str]
) -> list[SourceQuery]:
    out: list[SourceQuery] = []
    for sq in sorted(saved, key=lambda s: s.name):
        key = f"saved_query.{sq.name}"
        qp = sq.query_params
        metrics = [str(m) for m in qp.get("metrics") or []]
        if not metrics or not all(m in queryable for m in metrics):
            dbt.warn("saved_query_skipped",
                     f"saved query {sq.name} uses metrics that are not queryable", key)
            continue
        if qp.get("where"):
            dbt.warn("saved_query_skipped",
                     f"saved query {sq.name} has a where filter; not representable as a metric "
                     "query", key)
            continue
        try:
            dims = [_member_ref(str(g)) for g in qp.get("group_by") or []]
        except ValueError as e:
            dbt.warn("saved_query_skipped", f"saved query {sq.name}: {e}", key)
            continue
        mq: dict[str, Any] = {"metrics": metrics}
        if dims:
            mq["dimensions"] = dims
        order = []
        for o in qp.get("order_by") or []:
            s = str(o)
            desc = s.startswith("-") or "descending=True" in s or "descending=true" in s
            member = _member_ref(s.lstrip("-+")).split(":")[0]
            order.append({"member": member, "direction": "desc" if desc else "asc"})
        if order:
            mq["order_by"] = order
        if qp.get("limit"):
            mq["limit"] = int(qp["limit"])
        question = (sq.description or sq.label or sq.name.replace("_", " ")).strip()
        out.append(SourceQuery(
            key=key, question=question, variants=sorted({sq.name.replace("_", " ")} - {question}),
            description=sq.description, metric_query=mq,
        ))
    return out
