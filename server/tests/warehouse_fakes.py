"""Stand-ins for the BigQuery client and the Snowflake connector, backed by the seeded Jaffle
DuckDB: SQL in the warehouse dialect is transpiled with sqlglot and run on DuckDB, so what the
connectors send is parsed and executed, and the calls they make (job config, session
parameters, key) are recorded. No cloud access."""

from __future__ import annotations

import concurrent.futures
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import sqlglot
from sqlglot import exp


def run_duck(db: Path, sql: str, params: list[Any] | None = None) -> pa.Table:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute(sql, params or []).to_arrow_table()
    finally:
        con.close()


# ------------------------------------------------------------------------------- BigQuery


@dataclass
class FakeRows:
    table: pa.Table

    def to_arrow(self, create_bqstorage_client: bool = True) -> pa.Table:
        assert create_bqstorage_client is False  # plain REST reads, no Storage API
        return self.table


@dataclass
class FakeJob:
    client: FakeBigQuery
    sql: str
    job_config: Any
    cancelled: bool = False

    def result(self, timeout: float | None = None, max_results: int | None = None) -> FakeRows:
        c = self.client
        c.result_calls.append({"timeout": timeout, "max_results": max_results})
        if c.raise_on_result is not None:
            raise c.raise_on_result
        if c.slow:
            raise concurrent.futures.TimeoutError()
        if c.bytes_billed > (self.job_config.maximum_bytes_billed or 0):
            from google.api_core.exceptions import BadRequest

            raise BadRequest("Query exceeded limit for bytes billed",
                             errors=[{"reason": "bytesBilledLimitExceeded"}])
        if "INFORMATION_SCHEMA" in self.sql:
            table = c.information_schema(self.sql)
        else:
            table = run_duck(c.db, self.duck_sql())
        return FakeRows(table if max_results is None else table.slice(0, max_results))

    def duck_sql(self) -> str:
        """BigQuery name resolution on DuckDB: drop the project, apply the default dataset."""
        tree = sqlglot.parse_one(self.sql, read="bigquery")
        default = self.job_config.default_dataset
        ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            t.set("catalog", None)
            if not t.db and default is not None and t.name not in ctes:
                t.set("db", exp.to_identifier(default.dataset_id))
        return tree.sql(dialect="duckdb")

    def cancel(self) -> bool:
        self.cancelled = True
        return True


@dataclass
class Dataset:
    dataset_id: str


@dataclass
class FakeBigQuery:
    """``bigquery.Client`` as far as the connector uses it."""

    db: Path
    bytes_billed: int = 1024
    slow: bool = False
    raise_on_result: Exception | None = None
    jobs: list[FakeJob] = field(default_factory=list)
    result_calls: list[dict[str, Any]] = field(default_factory=list)
    query_kwargs: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False

    def query(self, sql: str, job_config: Any = None, **kw: Any) -> FakeJob:
        self.query_kwargs.append(kw)
        job = FakeJob(self, sql, job_config)
        self.jobs.append(job)
        return job

    def list_datasets(self, project: str) -> list[Dataset]:
        rows = run_duck(self.db, "select distinct table_schema from information_schema.tables")
        return [Dataset(s) for s in rows.column(0).to_pylist()]

    def information_schema(self, sql: str) -> pa.Table:
        m = re.search(r"`[^`.]+\.([^`.]+)\.INFORMATION_SCHEMA\.COLUMNS`", sql)
        assert m, sql
        # TABLE_OPTIONS.option_value is a string literal: quoted, with escapes.
        return run_duck(self.db, (
            "select c.table_name, c.column_name, upper(c.data_type) as data_type, "
            "c.column_comment, case when t.table_comment is null then null else "
            "'\"' || replace(t.table_comment, '\"', '\\\"') || '\"' end as table_comment "
            "from information_schema.columns c join information_schema.tables t "
            "using (table_schema, table_name) where c.table_schema = ? "
            "order by c.table_name, c.ordinal_position"), [m.group(1)])

    def close(self) -> None:
        self.closed = True


# ------------------------------------------------------------------------------ Snowflake


class FakeCursor:
    def __init__(self, conn: FakeSnowflakeConn) -> None:
        self.conn = conn
        self.description: list[tuple[Any, ...]] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: list[Any] | None = None) -> FakeCursor:
        fake = self.conn.fake
        fake.statements.append((sql, list(params or [])))
        if fake.raise_on_execute is not None:
            raise fake.raise_on_execute
        if "INFORMATION_SCHEMA" in sql:
            # DuckDB names the comment columns differently and has one database per file.
            duck = re.sub(r"\S+\.INFORMATION_SCHEMA\.", "information_schema.", sql)
            duck = duck.replace("c.comment", "c.column_comment").replace(
                "t.comment", "t.table_comment").replace("%s", "?")
            duck = duck.replace("CURRENT_DATABASE()", "upper(current_database())")
        else:
            duck = sqlglot.transpile(sql, read="snowflake", write="duckdb")[0]
        table = run_duck(fake.db, duck, params)
        self.description = [(n, None) for n in table.column_names]
        cols = [table.column(i).to_pylist() for i in range(table.num_columns)]
        self._rows = list(zip(*cols, strict=True)) if cols else []
        return self

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        self.conn.fake.fetch_sizes.append(size)
        out, self._rows = self._rows[:size], self._rows[size:]
        return out

    def fetchall(self) -> list[tuple[Any, ...]]:
        out, self._rows = self._rows, []
        return out

    def close(self) -> None:
        return None


@dataclass
class FakeSnowflakeConn:
    fake: FakeSnowflake
    closed: bool = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeSnowflake:
    """``snowflake.connector.connect`` as far as the connector uses it."""

    db: Path
    raise_on_connect: Exception | None = None
    raise_on_execute: Exception | None = None
    connects: list[dict[str, Any]] = field(default_factory=list)
    statements: list[tuple[str, list[Any]]] = field(default_factory=list)
    fetch_sizes: list[int] = field(default_factory=list)
    conns: list[FakeSnowflakeConn] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> FakeSnowflakeConn:
        self.connects.append(kwargs)
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        conn = FakeSnowflakeConn(self)
        self.conns.append(conn)
        return conn
