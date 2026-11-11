"""Connector interface (design §11): introspect a source and run read-only SQL as Arrow."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import pyarrow as pa


@dataclass(frozen=True)
class AssetSpec:
    """One catalogued object. ``fqn`` is ``db`` / ``schema`` / ``schema.table`` /
    ``schema.table.column`` (the form used by context docs)."""

    kind: str  # database | schema | table | column
    fqn: str
    parent_fqn: str | None = None
    data_type: str | None = None
    comment: str | None = None


class SqlTimeout(Exception):
    pass


class Connector(Protocol):
    kind: str
    dialect: str  # sqlglot dialect of the SQL it accepts

    def introspect(self) -> list[AssetSpec]: ...

    def execute(self, sql: str, *, limit: int, timeout_s: float) -> pa.Table:
        """Run read-only ``sql``; return at most ``limit`` rows."""
        ...

    def close(self) -> None: ...


def split_list(value: Any) -> list[str]:
    """A config list given as ``"a, b"`` (one form field) or ``["a", "b"]``."""
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).replace("\n", ",").split(",")
    return [s for s in (str(i).strip() for i in items) if s]


def resolve_secret(ref: str | None) -> str | None:
    """``env:NAME`` -> the environment value. Secrets are never stored in the database."""
    if not ref:
        return None
    scheme, _, name = ref.partition(":")
    if scheme != "env" or not name:
        raise ValueError(f"unsupported secret_ref {ref!r}; use env:NAME")
    value = os.environ.get(name)
    if value is None:
        raise ValueError(f"secret_ref {ref!r}: environment variable {name} is not set")
    return value


def make_connector(kind: str, config: dict[str, Any], secret: str | None = None) -> Connector:
    if kind == "duckdb":
        from satchel.data.connectors.duckdb_conn import DuckDBConnector

        return DuckDBConnector(config)
    if kind == "postgres":
        from satchel.data.connectors.postgres import PostgresConnector

        return PostgresConnector(config, secret)
    if kind == "bigquery":
        from satchel.data.connectors.bigquery_conn import BigQueryConnector

        return BigQueryConnector(config, secret)
    if kind == "snowflake":
        from satchel.data.connectors.snowflake_conn import SnowflakeConnector

        return SnowflakeConnector(config, secret)
    raise ValueError(f"unknown connection kind {kind!r}")
