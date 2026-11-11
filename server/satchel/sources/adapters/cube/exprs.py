"""Cube SQL snippets -> plain SQL over the cube's table.

Cube writes ``{CUBE}.status``, ``{TABLE}.amount``, ``{CUBE.status}`` and ``{revenue}`` (a member of
the same cube, inlined). Anything that needs Cube at run time (``COMPILE_CONTEXT``,
``SECURITY_CONTEXT``, ``FILTER_PARAMS``, Jinja, members of other cubes) cannot be expressed in a
Wren MDL and raises :class:`Unsupported`; the caller turns that into a note and a warning.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from satchel.sources.adapters.cube.model import Cube

_SELF = re.compile(r"\{(?:CUBE|TABLE)\}\s*\.\s*")
_SELF_MEMBER = re.compile(r"\{(?:CUBE|TABLE)\.([A-Za-z_][A-Za-z0-9_]*)\}")
_REF = re.compile(r"\{([^{}]+)\}")
_RUNTIME = re.compile(
    r"COMPILE_CONTEXT|SECURITY_CONTEXT|FILTER_PARAMS|FILTER_GROUP|SQL_UTILS|USER_CONTEXT|\{%|\{\{"
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Unsupported(Exception):
    """A construct the MDL cannot express; the member becomes a note."""


def clean(sql: str) -> str:
    """Normalise old ``${CUBE}`` spellings and whitespace."""
    return " ".join(sql.replace("${", "{").split())


class Resolver:
    """Resolves the SQL of one cube's members against each other."""

    def __init__(self, cube: Cube) -> None:
        self.cube = cube
        self.dims = {d.name: d for d in cube.dimensions}
        self.measures = {m.name: m for m in cube.measures}

    def sql(self, raw: str, seen: tuple[str, ...] = ()) -> str:
        """The expression with table prefixes dropped and member references inlined."""
        text = clean(raw)
        if _RUNTIME.search(text):
            raise Unsupported(f"needs Cube at run time: {text[:70]!r}")
        text = _SELF_MEMBER.sub(lambda m: "{" + m.group(1) + "}", text)
        text = _SELF.sub("", text)
        text = re.sub(rf"\{{{re.escape(self.cube.name)}\}}\s*\.\s*", "", text)

        def sub(m: re.Match[str]) -> str:
            ref = m.group(1).strip()
            if "." in ref:
                raise Unsupported(f"references another cube ({{{ref}}})")
            if ref in seen:
                raise Unsupported(f"{ref!r} refers to itself")
            if ref in self.measures:
                return f"({self.measure_expr(ref, (*seen, ref))})"
            if ref in self.dims:
                d = self.dims[ref]
                if d.sql is None:
                    return ref
                return f"({self.sql(d.sql, (*seen, ref))})"
            raise Unsupported(f"unknown member {{{ref}}}")

        text = _REF.sub(sub, text)
        if "{" in text or "}" in text:
            raise Unsupported(f"cannot read {raw.strip()[:70]!r}")
        return text

    def column_or_expr(self, name: str, sql: str | None) -> str:
        """A dimension's expression: its ``sql``, or the member name when omitted."""
        return self.sql(sql) if sql else name

    # ---- measures ------------------------------------------------------------------------

    def measure_expr(self, name: str, seen: tuple[str, ...] = ()) -> str:
        m = self.measures[name]
        kind = m.type.lower()
        if m.rolling_window or kind == "running_total":
            raise Unsupported(f"measure {name!r} is a rolling window / running total")
        if m.multi_stage:
            raise Unsupported(f"measure {name!r} is multi-stage")
        flt = " AND ".join(f"({self.sql(f.sql, seen)})" for f in m.filters)
        arg = self.sql(m.sql, seen) if m.sql else None
        if kind == "number":
            if arg is None:
                raise Unsupported(f"number measure {name!r} has no sql")
            if flt:
                raise Unsupported(f"number measure {name!r} has filters (not applicable)")
            return arg
        aggs = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX"}
        if kind == "count":
            body = arg if arg is not None else "1"
            if flt:
                return f"COUNT(CASE WHEN {flt} THEN {body} END)"
            return f"COUNT({arg})" if arg is not None else "COUNT(*)"
        if kind in ("count_distinct", "count_distinct_approx"):
            if arg is None:
                raise Unsupported(f"{kind} measure {name!r} has no sql")
            return f"COUNT(DISTINCT {f'CASE WHEN {flt} THEN {arg} END' if flt else arg})"
        if kind in aggs:
            if arg is None:
                raise Unsupported(f"{kind} measure {name!r} has no sql")
            return f"{aggs[kind]}({f'CASE WHEN {flt} THEN {arg} END' if flt else arg})"
        raise Unsupported(f"measure type {m.type!r} is not supported")


def columns_in(sql: str) -> set[str]:
    """Bare column names an expression reads (also proves it parses)."""
    try:
        tree = sqlglot.parse_one(sql)
    except SqlglotError as e:
        raise Unsupported(f"cannot parse {sql[:60]!r}: {str(e).splitlines()[0]}") from None
    return {c.name for c in tree.find_all(exp.Column)}


def plain_column(sql: str | None, name: str) -> str | None:
    """The column a dimension reads when its sql is just a column (``{CUBE}.status``), else None.
    A dimension without ``sql`` reads the column of its own name."""
    if not sql:
        return name if _IDENT.match(name) else None
    text = _SELF.sub("", clean(sql)).strip().strip('"`')
    return text if _IDENT.match(text) else None
