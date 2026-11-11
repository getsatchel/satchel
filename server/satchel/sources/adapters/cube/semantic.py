"""Cube measures and dimensions -> one Wren MDL manifest (a model and a cube per Cube cube).

A Cube ``cube`` becomes an MDL *model* over its table (or ``refSql`` for a ``sql:`` cube) and an
MDL *cube* of the same name. Measures inline their aggregation and filters
(``SUM(CASE WHEN status <> 'x' THEN amount END)``), ``number`` measures inline the measures they
reference, string/number/boolean dimensions become dimensions and time dimensions become time
dimensions. Rolling windows, multi-stage measures, sub-query dimensions and anything that needs
members of another cube (joined dimensions) are noted, not queryable. Joins are not modelled in
the MDL (satchel's metric queries stay within one cube); they are join hints on the asset docs.
"""

from __future__ import annotations

from satchel_adapter_sdk import MdlBuilder, SourceSemanticModel, SourceWarning

from satchel.sources.adapters.cube.assets import Base
from satchel.sources.adapters.cube.exprs import Resolver, Unsupported, columns_in, plain_column
from satchel.sources.adapters.cube.model import Model

DATA_SOURCES = {
    "duckdb": "duckdb", "postgres": "postgres", "bigquery": "bigquery", "snowflake": "snowflake",
    "redshift": "postgres", "mysql": "mysql", "mssql": "mssql", "sqlserver": "mssql",
    "databricks": "databricks", "spark": "spark", "trino": "trino", "clickhouse": "clickhouse",
}
_DIM_TYPES = {"string": "VARCHAR", "number": "DOUBLE", "boolean": "BOOLEAN"}
_COL_TYPES = {"string": "varchar", "number": "double", "boolean": "boolean", "time": "date"}

Member = tuple[str, str, str, str | None]


def _desc(*parts: str | None) -> str | None:
    for p in parts:
        if p and p.strip():
            return p.strip()
    return None


def build_semantic(
    model: Model, bases: dict[str, Base], project: str, dialect: str | None
) -> list[SourceSemanticModel]:
    b = MdlBuilder(
        data_source=DATA_SOURCES.get((dialect or "").lower()), catalog=project, schema="public"
    )
    notes: list[str] = []
    queryable: list[tuple[str, str]] = []
    guessed = False

    def skip(msg: str, key: str, code: str) -> None:
        notes.append(msg)
        model.warn(code, msg, key)

    for name in sorted(model.cubes):
        cube = model.cubes[name]
        if not cube.public:
            continue
        res = Resolver(cube)
        dims: list[Member] = []
        tdims: list[Member] = []
        measures: list[Member] = []
        col_types: dict[str, str] = {}
        used: set[str] = set()

        for d in sorted(cube.dimensions, key=lambda x: x.name):
            if not d.public:
                continue
            if d.sub_query or d.type.lower() not in (*_DIM_TYPES, "time"):
                skip(f"dimension {name}.{d.name} ({d.type}) is not queryable through satchel: "
                     "sub-query and geo dimensions are not supported",
                     f"{name}.{d.name}", "dimension_not_queryable")
                continue
            try:
                expr = res.column_or_expr(d.name, d.sql)
                cols = columns_in(expr)
            except Unsupported as e:
                skip(f"dimension {name}.{d.name} is not queryable through satchel: {e}",
                     f"{name}.{d.name}", "dimension_not_queryable")
                continue
            used |= cols
            kind = d.type.lower()
            for c in cols:
                guess = "integer" if kind == "number" and c.lower().endswith("id") \
                    else _COL_TYPES[kind]
                col_types.setdefault(c.lower(), guess)
            text = _desc(d.description)
            if kind == "time":
                tdims.append((d.name, expr, "DATE", text))
            else:
                dims.append((d.name, expr, _DIM_TYPES[kind], text))

        for m in sorted(cube.measures, key=lambda x: x.name):
            if not m.public:
                continue
            try:
                expr = res.measure_expr(m.name)
                cols = columns_in(expr)
            except Unsupported as e:
                skip(f"metric {name}.{m.name} ({m.type}) is not queryable through satchel: {e}",
                     f"{name}.{m.name}", "metric_not_queryable")
                continue
            used |= cols
            for c in cols:
                col_types.setdefault(c.lower(), "double")
            kind = m.type.lower()
            measures.append((
                m.name, expr, "BIGINT" if kind.startswith("count") else "DOUBLE",
                _desc(m.description),
            ))

        if not measures:
            continue
        pk = next(
            (plain_column(d.sql, d.name) for d in cube.dimensions if d.primary_key and d.public),
            None,
        )
        cols_map = {c: col_types.get(c.lower(), "varchar") for c in sorted(used)}
        text = _desc(cube.description)
        if cube.sql and not cube.sql_table:  # a query: keep it as the model's refSql
            try:
                ref_sql = res.sql(cube.sql)
            except Unsupported as e:
                skip(f"cube {name} is not queryable through satchel: its sql {e}", name,
                     "cube_skipped")
                continue
            b.model(name, ref_sql=ref_sql, columns=cols_map,
                    primary_key=pk if pk in used else None, description=text)
        else:
            base = bases.get(name)
            if base is None:
                skip(f"cube {name} is not queryable through satchel: no readable sql_table", name,
                     "cube_skipped")
                continue
            schema, _, table = base.fqn.rpartition(".")
            b.model(name, table=table, schema=schema or None, columns=cols_map,
                    primary_key=pk if pk in used else None, description=text)
        guessed = True
        b.cube(name, base=name, measures=measures, dimensions=dims, time_dimensions=tdims,
               description=_desc(cube.description))
        queryable += [(name, m[0]) for m in measures]

    if guessed:
        model.warnings.append(SourceWarning(
            code="types_guessed",
            message="Cube data models carry no column types: the metric model's column types "
                    "were guessed from the dimension and measure types",
        ))
    if not queryable:
        if notes:
            model.warn("no_queryable_metrics", "no Cube measure could be made queryable")
        return []
    names = [n for _, n in queryable]
    metrics = sorted(f"{c}.{n}" if names.count(n) > 1 else n for c, n in queryable)
    return [SourceSemanticModel(
        key=f"semantic.{project}", name=project,
        description=f"Metrics defined in the Cube data model {project}",
        mdl=b.build(), queryable_metrics=metrics, notes=sorted(notes),
    )]
