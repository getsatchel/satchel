"""DuckDB connector: a local database file (opened read-only) or Parquet files exposed as
views (``config.parquet = {view: path_or_url}``; http(s)/s3/gcs URLs load ``httpfs``)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from satchel.data.connectors.base import AssetSpec, SqlTimeout

_REMOTE = ("http://", "https://", "s3://", "gcs://", "gs://", "r2://", "az://", "abfss://")


class DuckDBConnector:
    kind = "duckdb"
    dialect = "duckdb"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        parquet: dict[str, str] = dict(config.get("parquet") or {})
        path = config.get("path")
        if parquet:
            self._con = duckdb.connect(":memory:")
            if any(str(p).startswith(_REMOTE) for p in parquet.values()):
                self._con.execute("INSTALL httpfs")
                self._con.execute("LOAD httpfs")
            for view, src in parquet.items():
                if not view.isidentifier():
                    raise ValueError(f"invalid parquet view name {view!r}")
                esc = str(src).replace("'", "''")
                self._con.execute(f"CREATE VIEW {view} AS SELECT * FROM read_parquet('{esc}')")
        elif path:
            p = Path(str(path)).expanduser()
            if not p.exists():
                raise FileNotFoundError(f"DuckDB file not found: {p}")
            # External access is off: the guardrails already block file functions, this is
            # defence in depth. Read-only: no statement can write.
            self._con = duckdb.connect(
                str(p), read_only=True, config={"enable_external_access": False}
            )
        else:
            raise ValueError("duckdb connection needs config.path or config.parquet")

    def introspect(self) -> list[AssetSpec]:
        con = self._con
        out: list[AssetSpec] = []
        catalogs = {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT database_name FROM duckdb_schemas() WHERE NOT internal"
            ).fetchall()
        }
        db = sorted(catalogs - {"system", "temp"})[0] if catalogs else "main"
        out.append(AssetSpec("database", db))
        comments: dict[str, str | None] = {}
        for sql in (
            "SELECT schema_name, table_name, comment FROM duckdb_tables()",
            "SELECT schema_name, view_name, comment FROM duckdb_views() WHERE NOT internal",
        ):
            for sch, tbl, com in con.execute(sql).fetchall():
                comments[f"{sch}.{tbl}"] = com
        cols = con.execute(
            "SELECT schema_name, table_name, column_name, data_type, comment, column_index "
            "FROM duckdb_columns() WHERE NOT internal ORDER BY schema_name, table_name, "
            "column_index"
        ).fetchall()
        seen_schema: set[str] = set()
        seen_table: set[str] = set()
        for sch, tbl, col, dtype, com, _i in cols:
            tfqn = f"{sch}.{tbl}"
            if sch not in seen_schema:
                seen_schema.add(sch)
                out.append(AssetSpec("schema", sch, db))
            if tfqn not in seen_table:
                seen_table.add(tfqn)
                out.append(AssetSpec("table", tfqn, sch, comment=comments.get(tfqn)))
            out.append(AssetSpec("column", f"{tfqn}.{col}", tfqn, str(dtype), com))
        return out

    def execute(self, sql: str, *, limit: int, timeout_s: float) -> pa.Table:
        cur = self._con.cursor()
        timed_out = False

        def _fire() -> None:
            nonlocal timed_out
            timed_out = True
            cur.interrupt()

        timer = threading.Timer(timeout_s, _fire)
        timer.start()
        try:
            reader = cur.sql(sql).to_arrow_reader(max(limit, 1))
            batches: list[pa.RecordBatch] = []
            n = 0
            for b in reader:
                batches.append(b)
                n += b.num_rows
                if n >= limit:
                    break
            table = (
                pa.Table.from_batches(batches, schema=reader.schema)
                if batches
                else reader.schema.empty_table()
            )
            return table.slice(0, limit)
        except duckdb.InterruptException as e:
            raise SqlTimeout(f"statement exceeded {timeout_s:g}s timeout") from e
        except duckdb.Error:
            if timed_out:
                raise SqlTimeout(f"statement exceeded {timeout_s:g}s timeout") from None
            raise
        finally:
            timer.cancel()
            cur.close()

    def close(self) -> None:
        self._con.close()
