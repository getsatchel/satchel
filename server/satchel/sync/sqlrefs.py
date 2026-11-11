"""Table references from SQL via sqlglot (used for entity links)."""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

_DIALECTS = {"bigquery", "snowflake", "postgres", "duckdb", "clickhouse", "databricks", "mysql"}


def extract_tables(sql: str, dialect: str | None = None) -> list[str]:
    """Return distinct ``schema.table`` / ``table`` names referenced by ``sql`` (CTEs excluded).

    Unparseable SQL yields an empty list rather than failing an import.
    """
    d = dialect if dialect in _DIALECTS else None
    try:
        trees = [t for t in sqlglot.parse(sql, read=d) if t is not None]
    except SqlglotError:
        return []
    out: list[str] = []
    for tree in trees:
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            if not t.name or (not t.db and t.name.lower() in ctes):
                continue
            name = f"{t.db}.{t.name}" if t.db else t.name
            if name not in out:
                out.append(name)
    return out
