"""Cubes -> ``SourceAsset``: the warehouse table a cube reads, with its dimensions as columns,
joins as join hints, segments as caveats and views as "used by"."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from satchel_adapter_sdk import (
    SourceAsset,
    SourceColumn,
    SourceRelationship,
    normalise_fqn,
)
from sqlglot import exp
from sqlglot.errors import SqlglotError

from satchel.sources.adapters.cube.exprs import Resolver, Unsupported, clean, plain_column
from satchel.sources.adapters.cube.model import Cube, Model

_ID = r"([A-Za-z_]\w*)"
_EQ = re.compile(rf"{_ID}\s*\.\s*{_ID}\s*=\s*{_ID}\s*\.\s*{_ID}")
_BRACES = re.compile(rf"\{{\s*{_ID}\s*\}}\s*\.\s*|\{{\s*{_ID}\s*\.\s*{_ID}\s*\}}")
_AND = re.compile(r"\s+AND\s+", re.IGNORECASE)


@dataclass
class Base:
    """The warehouse table a cube reads."""

    fqn: str
    database: str | None = None
    where: str | None = None  # a cube defined by ``sql:`` that filters one table


def _split(name: str, default_schema: str | None) -> tuple[str, str | None]:
    parts = normalise_fqn(name).split(".")
    if len(parts) == 1:
        return (f"{default_schema}.{parts[0]}" if default_schema else parts[0]), None
    if len(parts) == 2:
        return ".".join(parts), None
    return ".".join(parts[-2:]), ".".join(parts[:-2])


def base_of(cube: Cube, default_schema: str | None, model: Model) -> Base | None:
    """``sql_table`` or a ``sql:`` query over exactly one table (no joins or aggregation)."""
    if cube.sql_table:
        t = clean(cube.sql_table)
        if "{" in t or "$" in t:
            model.warn("cube_no_table", f"cube {cube.name}: sql_table {t!r} is dynamic", cube.name)
            return None
        fqn, db = _split(t, default_schema)
        return Base(fqn, db)
    if not cube.sql:
        model.warn("cube_no_table", f"cube {cube.name} has neither sql_table nor sql", cube.name)
        return None
    try:
        tree = sqlglot.parse_one(clean(cube.sql))
    except SqlglotError:
        tree = None
    if not isinstance(tree, exp.Select):
        model.warn("cube_no_table", f"cube {cube.name}: sql is not a plain SELECT", cube.name)
        return None
    tables = list(tree.find_all(exp.Table))
    simple = (
        len(tables) == 1 and not tree.args.get("joins") and not tree.args.get("group")
        and not tree.args.get("with_") and not tree.args.get("distinct")
        and not list(tree.find_all(exp.Subquery)) and not list(tree.find_all(exp.AggFunc))
    )
    if not simple:
        model.warn("cube_no_table",
                   f"cube {cube.name}: sql joins or aggregates, so it is not documented as a "
                   "table (its metrics are still queryable)", cube.name)
        return None
    table = tables[0]
    name = ".".join(p for p in (table.catalog, table.db, table.name) if p)
    fqn, db = _split(name, default_schema)
    where = tree.args.get("where")
    return Base(fqn, db, where.this.sql() if where is not None else None)


def _join_column(cube: Cube, ref: str) -> str:
    """A join side names a column or one of the cube's dimensions; return the column."""
    for d in cube.dimensions:
        if d.name == ref:
            return plain_column(d.sql, d.name) or ref
    return ref


def parse_join(
    cube: Cube, join_name: str, sql: str, target: Cube | None
) -> list[tuple[str, str]] | None:
    """``[(this column, joined column), ...]`` of the equality conditions, or None."""
    text = _BRACES.sub(lambda m: f"{m.group(1)}." if m.group(1) else f"{m.group(2)}.{m.group(3)}",
                       clean(sql))
    pairs: list[tuple[str, str]] = []
    for part in _AND.split(text):
        m = _EQ.fullmatch(part.strip().strip("()").strip())
        if m is None:
            return None
        (a, ac), (b, bc) = (m.group(1), m.group(2)), (m.group(3), m.group(4))
        selves = {"CUBE", "TABLE", cube.name}
        if a in selves and b == join_name and a != b:
            this, other = ac, bc
        elif b in selves and a == join_name and a != b:
            this, other = bc, ac
        else:
            return None
        pairs.append((_join_column(cube, this), _join_column(target, other) if target else other))
    return pairs or None


def build_assets(
    model: Model, default_schema: str | None, bases: dict[str, Base]
) -> list[SourceAsset]:
    """One asset per table; cubes over the same table are merged into it."""
    views_of: dict[str, list[str]] = {}
    for v in model.views.values():
        for vc in v.cubes:
            views_of.setdefault(vc.join_path.split(".")[0], []).append(
                f"Cube view {v.name}" + (f" ({v.title})" if v.title else "")
            )
    groups: dict[str, list[Cube]] = {}
    for name in sorted(model.cubes):
        base = bases.get(name)
        if base is not None:
            groups.setdefault(base.fqn, []).append(model.cubes[name])
    out: list[SourceAsset] = []
    for fqn, cubes in sorted(groups.items()):
        # a cube over the whole table describes it best; filtered ones follow
        cubes.sort(key=lambda c: (bases[c.name].where is not None, c.name))
        out.append(_asset(model, fqn, cubes, bases, views_of))
    return out


def _satchel(meta: dict[str, Any]) -> dict[str, Any]:
    raw = meta.get("satchel")
    return raw if isinstance(raw, dict) else {}


def _strs(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list | tuple):
        return [str(x) for x in v if x is not None and str(x).strip()]
    return []


def _asset(
    model: Model, fqn: str, cubes: list[Cube], bases: dict[str, Base],
    views_of: dict[str, list[str]],
) -> SourceAsset:
    first = cubes[0]
    cols: dict[str, SourceColumn] = {}
    grain: list[str] = []
    rels: dict[tuple[str, str, str | None], SourceRelationship] = {}
    caveats: list[str] = []
    used_by: list[str] = []
    tags: set[str] = set()
    owner: str | None = None
    endorsed: bool | None = None
    for cube in cubes:
        base = bases[cube.name]
        meta, satchel = cube.meta, _satchel(cube.meta)
        res = Resolver(cube)
        if base.where:
            caveats.append(f"Cube {cube.name} only covers rows where {base.where}")
        caveats += _strs(satchel.get("caveats"))
        for seg in cube.segments:
            try:
                cond = res.sql(seg.sql)
            except Unsupported:
                cond = clean(seg.sql)
            extra = f": {seg.description.strip()}" if seg.description else ""
            caveats.append(
                f"Cube segment {seg.name} filters {cube.name} to rows where {cond}{extra}"
            )
        tags |= set(_strs(meta.get("tags")))
        if owner is None:
            o = satchel.get("owner", meta.get("owner"))
            owner = o.strip() if isinstance(o, str) and o.strip() else (
                str(o.get("email") or o.get("name")) if isinstance(o, dict) and (
                    o.get("email") or o.get("name")) else None)
        if endorsed is None and isinstance(satchel.get("endorsed"), bool):
            endorsed = satchel["endorsed"]
        if cube is first or not grain:
            grain = _strs(satchel.get("grain")) or grain
        used_by += views_of.get(cube.name, [])
        for d in cube.dimensions:
            col = plain_column(d.sql, d.name)
            if col is None or d.sub_query:
                continue
            dl = _satchel(d.meta)
            desc = (d.description or "").strip() or None
            cur = cols.get(col.lower())
            if cur is None:
                cols[col.lower()] = SourceColumn(
                    name=col, description=desc,
                    unique=d.primary_key,
                    pii=bool(dl.get("pii", d.meta.get("pii", False))),
                    unit=str(dl["unit"]) if dl.get("unit") else None,
                    enum_values=_strs(dl.get("enum_values")),
                    tags=sorted(set(_strs(d.meta.get("tags")))),
                    meta={"cube_type": d.type, "cube_dimension": d.name,
                          **{k: v for k, v in d.meta.items()
                             if k not in ("satchel", "tags")}},
                )
            elif cur.description is None and desc:
                cur.description = desc
            if d.primary_key and col not in grain and "grain" not in satchel:
                grain.append(col)
        for m in cube.measures:  # columns that measures aggregate
            col = plain_column(m.sql, "") if m.sql else None
            if col and col.lower() not in cols:
                cols[col.lower()] = SourceColumn(
                    name=col, description=f"Aggregated by Cube measure {m.name} ({m.type})."
                )
        for j in cube.joins:
            target = model.cubes.get(j.name)
            tb = bases.get(j.name)
            pairs = parse_join(cube, j.name, j.sql, target)
            if pairs is None:
                model.warn("join_unparsed", f"cube {cube.name} join {j.name}: only equality "
                           f"conditions on the two cubes are read ({clean(j.sql)[:70]!r})",
                           cube.name)
            elif tb is None:
                model.warn("join_target", f"cube {cube.name} joins {j.name}, which has no "
                           "warehouse table; the join hint is skipped", cube.name)
            else:
                for this, other in pairs:
                    rels[(this, tb.fqn, other)] = SourceRelationship(
                        column=this, to_asset=tb.fqn, to_column=other)
    description = next(
        ((c.description or "").strip() for c in cubes if (c.description or "").strip()), None
    )
    sm: dict[str, Any] = {
        "cubes": [c.name for c in cubes],
        "data_source": first.data_source,
        "files": sorted({c.file for c in cubes if c.file}),
        "measures": sorted({f"{c.name}.{m.name} ({m.type})" for c in cubes for m in c.measures}),
        "segments": sorted({f"{c.name}.{s.name}" for c in cubes for s in c.segments}),
    }
    base0 = bases[first.name]
    return SourceAsset(
        key=f"cube.{first.name}", fqn=fqn, kind="table", name=fqn.rsplit(".", 1)[-1],
        database=base0.database,
        description=description or None,
        columns=sorted(cols.values(), key=lambda c: c.name),
        grain=grain,
        relationships=sorted(
            rels.values(), key=lambda r: (r.column, r.to_asset, r.to_column or "")
        ),
        tags=sorted(tags), owner=owner, endorsed=endorsed,
        caveats=list(dict.fromkeys(caveats)),
        used_by=sorted(set(used_by)),
        source_meta={k: v for k, v in sm.items() if v not in (None, "", [])},
    )
