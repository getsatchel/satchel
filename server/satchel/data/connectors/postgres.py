"""Postgres connector (psycopg). Sessions are read-only with a statement timeout; set
``config.role`` to run under a restricted role (``SET ROLE``)."""

from __future__ import annotations

from typing import Any

import psycopg
import pyarrow as pa
from psycopg import sql as pgsql

from satchel.data.connectors.base import AssetSpec, SqlTimeout


class PostgresConnector:
    kind = "postgres"
    dialect = "postgres"

    def __init__(self, config: dict[str, Any], secret: str | None = None) -> None:
        self.config = config
        self._secret = secret

    def _connect(self, timeout_s: float = 30.0) -> psycopg.Connection[Any]:
        if self._secret and self._secret.startswith(("postgres://", "postgresql://")):
            conn = psycopg.connect(self._secret, autocommit=False)
        else:
            conn = psycopg.connect(
                host=self.config.get("host", "localhost"),
                port=int(self.config.get("port", 5432)),
                dbname=self.config.get("database") or self.config.get("dbname", "postgres"),
                user=self.config.get("user"),
                password=self._secret,
                autocommit=False,
            )
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(
                pgsql.SQL("SET LOCAL statement_timeout = {}").format(
                    pgsql.Literal(int(timeout_s * 1000))
                )
            )
            role = self.config.get("role")
            if role:
                cur.execute(pgsql.SQL("SET LOCAL ROLE {}").format(pgsql.Identifier(str(role))))
        return conn

    def introspect(self) -> list[AssetSpec]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("select current_database()")
            db = str(cur.fetchone()[0])  # type: ignore[index]
            cur.execute(
                "select c.table_schema, c.table_name, c.column_name, c.data_type, "
                "col_description(format('%I.%I', c.table_schema, c.table_name)::regclass, "
                "c.ordinal_position), obj_description("
                "format('%I.%I', c.table_schema, c.table_name)::regclass) "
                "from information_schema.columns c "
                "where c.table_schema not in ('pg_catalog','information_schema') "
                "order by 1, 2, c.ordinal_position"
            )
            rows = cur.fetchall()
        out: list[AssetSpec] = [AssetSpec("database", db)]
        ss: set[str] = set()
        ts: set[str] = set()
        for sch, tbl, col, dtype, ccom, tcom in rows:
            t = f"{sch}.{tbl}"
            if sch not in ss:
                ss.add(sch)
                out.append(AssetSpec("schema", sch, db))
            if t not in ts:
                ts.add(t)
                out.append(AssetSpec("table", t, sch, comment=tcom))
            out.append(AssetSpec("column", f"{t}.{col}", t, dtype, ccom))
        return out

    def execute(self, sql: str, *, limit: int, timeout_s: float) -> pa.Table:
        try:
            with self._connect(timeout_s) as conn, conn.cursor() as cur:
                cur.execute(sql)
                names = [d.name for d in (cur.description or [])]
                rows = cur.fetchmany(limit)
        except psycopg.errors.QueryCanceled as e:
            raise SqlTimeout(f"statement exceeded {timeout_s:g}s timeout") from e
        cols = {n: [r[i] for r in rows] for i, n in enumerate(names)}
        return pa.table(cols)

    def close(self) -> None:
        return None
