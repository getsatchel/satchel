"""SQL gateway (design §11): read-only guardrails, row cap and timeout around a Connector."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pyarrow as pa
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from satchel.data.connectors.base import Connector

DIALECTS = {"duckdb": "duckdb", "postgres": "postgres", "bigquery": "bigquery",
            "snowflake": "snowflake"}

# Anything in the tree that writes, defines, or reaches outside the database.
_FORBIDDEN: tuple[type[exp.Expression], ...] = tuple(
    t for t in (
        getattr(exp, n, None) for n in (
            "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable",
            "Command", "Copy", "Into", "Set", "Pragma", "Attach", "Detach", "Use", "Grant",
            "Revoke", "Commit", "Rollback", "Transaction", "LoadData", "Export",
        )
    ) if isinstance(t, type)
)
_FILE_FUNCS = {
    "read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json", "read_json_auto",
    "read_ndjson", "read_text", "read_blob", "glob", "sniff_csv", "iceberg_scan", "delta_scan",
    "read_xlsx", "pg_read_file", "pg_ls_dir", "lo_import", "dblink", "postgres_scan",
    "sqlite_scan", "query", "query_table",
    "external_query",  # BigQuery federated query to another database
    "infer_schema",  # Snowflake: reads staged files
}


class GuardrailError(Exception):
    """The statement is not allowed. The message is shown to the agent."""


@dataclass
class Checked:
    tree: exp.Expression
    tables: list[str]
    notes: list[str] = field(default_factory=list)


def check_sql(sql: str, dialect: str = "duckdb") -> Checked:
    """Allow exactly one read-only SELECT / WITH / set-operation statement."""
    if not sql or not sql.strip():
        raise GuardrailError("Empty SQL.")
    d = DIALECTS.get(dialect)
    try:
        trees = [t for t in sqlglot.parse(sql, read=d) if t is not None]
    except SqlglotError as e:
        raise GuardrailError(f"Could not parse SQL ({dialect}): {str(e)[:300]}") from None
    if len(trees) != 1:
        raise GuardrailError(
            f"Exactly one statement is allowed; got {len(trees)}. Send one SELECT at a time."
        )
    tree = trees[0]
    if not isinstance(tree, exp.Select | exp.SetOperation):
        raise GuardrailError(
            f"Only read-only SELECT / WITH queries are allowed (got {type(tree).__name__.upper()})."
        )
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN):
            raise GuardrailError(
                f"Only read-only queries are allowed ({type(node).__name__.upper()} is not)."
            )
        if isinstance(node, exp.Anonymous) and node.name.lower() in _FILE_FUNCS:
            raise GuardrailError(f"Function {node.name}() is not allowed (file/remote access).")
        if isinstance(node, exp.Anonymous) and node.name.lower().startswith("system$"):
            # Snowflake system functions can cancel queries, abort sessions, change state.
            raise GuardrailError(f"Function {node.name}() is not allowed.")
        if isinstance(node, exp.Table) and node.name.startswith("@"):
            raise GuardrailError("Reading files from a stage is not allowed; query a table.")
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Literal):
            raise GuardrailError("Reading files by path is not allowed; query a table.")
        if isinstance(node, exp.ReadCSV):
            raise GuardrailError("File-reading functions are not allowed.")
        if isinstance(node, exp.Table) and re.search(
            r"[/\\]|://|\.(csv|parquet|json|ndjson|tsv|txt|xlsx?)$", node.name, re.I
        ):
            raise GuardrailError("Reading files by path is not allowed; query a table.")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables: list[str] = []
    for t in tree.find_all(exp.Table):
        if not t.name or (not t.db and t.name.lower() in ctes):
            continue
        name = f"{t.db}.{t.name}" if t.db else t.name
        if name not in tables:
            tables.append(name)
    return Checked(tree, tables)


@dataclass
class GatewayResult:
    table: pa.Table  # at most row_cap rows
    truncated: bool
    tables: list[str]
    notes: list[str]


def run_guarded(
    connector: Connector, sql: str, *, row_cap: int, timeout_s: float
) -> GatewayResult:
    """Validate, then execute with a row cap (fetches cap+1 to detect truncation)."""
    checked = check_sql(sql, connector.dialect)
    table = connector.execute(sql, limit=row_cap + 1, timeout_s=timeout_s)
    truncated = table.num_rows > row_cap
    if truncated:
        table = table.slice(0, row_cap)
    notes = [f"Row cap {row_cap}: result truncated; add filters/aggregation."] if truncated else []
    return GatewayResult(table, truncated, checked.tables, notes)
