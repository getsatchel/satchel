"""Guardrails, the DuckDB connector, row cap, timeout, parquet connections (no Postgres)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from satchel.data.connectors import SqlTimeout, make_connector, resolve_secret
from satchel.data.connectors.duckdb_conn import DuckDBConnector
from satchel.data.gateway import GuardrailError, check_sql, run_guarded
from tests.helpers import build_jaffle_db


@pytest.fixture(scope="module")
def jaffle_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_jaffle_db(tmp_path_factory.mktemp("wh") / "jaffle.duckdb")


@pytest.mark.parametrize(
    "sql",
    [
        "drop table analytics.orders",
        "delete from analytics.orders",
        "update analytics.orders set amount = 0",
        "insert into analytics.orders values (1, 1, '2025-01-01', 'x', 1)",
        "create table t as select 1",
        "alter table analytics.orders add column x int",
        "select 1; select 2",
        "select 1; drop table analytics.orders",
        "copy analytics.orders to 'x.csv'",
        "attach 'other.db'",
        "set threads = 1",
        "with d as (delete from analytics.orders returning *) select * from d",
        "select * into newt from analytics.orders",
        "select * from read_csv('/etc/passwd')",
        "select * from '/etc/passwd.csv'",
        "",
    ],
)
def test_guardrails_reject(sql: str) -> None:
    with pytest.raises(GuardrailError):
        check_sql(sql, "duckdb")


def test_guardrails_allow_reads() -> None:
    ok = check_sql(
        "with a as (select * from analytics.orders) select count(*) from a "
        "union all select count(*) from analytics.customers",
        "duckdb",
    )
    assert set(ok.tables) == {"analytics.orders", "analytics.customers"}
    check_sql("select 1", "duckdb")


def test_row_cap_and_truncation(jaffle_db: Path) -> None:
    con = DuckDBConnector({"path": str(jaffle_db)})
    try:
        r = run_guarded(con, "select * from analytics.orders", row_cap=100, timeout_s=10)
        assert r.table.num_rows == 100 and r.truncated and r.notes
        r = run_guarded(con, "select * from analytics.orders limit 5", row_cap=100, timeout_s=10)
        assert r.table.num_rows == 5 and not r.truncated
        r = run_guarded(con, "select * from analytics.orders", row_cap=700, timeout_s=10)
        assert r.table.num_rows == 700 and not r.truncated
    finally:
        con.close()


def test_connector_is_read_only_and_times_out(jaffle_db: Path) -> None:
    con = DuckDBConnector({"path": str(jaffle_db)})
    try:
        # Even bypassing the gateway, the connection itself cannot write.
        with pytest.raises(duckdb.Error):
            con.execute("create table analytics.x as select 1", limit=1, timeout_s=5)
        with pytest.raises(SqlTimeout):
            con.execute("select count(*) from range(100000000000)", limit=1, timeout_s=0.3)
    finally:
        con.close()


def test_introspect_shape(jaffle_db: Path) -> None:
    con = DuckDBConnector({"path": str(jaffle_db)})
    try:
        specs = con.introspect()
    finally:
        con.close()
    by = {s.fqn: s for s in specs}
    assert by["jaffle"].kind == "database"
    assert by["analytics"].kind == "schema" and by["analytics"].parent_fqn == "jaffle"
    assert by["analytics.orders"].parent_fqn == "analytics"
    assert by["analytics.orders"].comment == "One row per customer order (AUD cents)."
    amount = by["analytics.orders.amount"]
    assert amount.kind == "column" and amount.data_type == "INTEGER"
    assert amount.comment == "Order total in AUD cents"
    assert {s.fqn for s in specs if s.kind == "table"} == {
        "analytics.orders", "analytics.customers", "analytics.payments"}


def test_parquet_connection(tmp_path: Path, jaffle_db: Path) -> None:
    pq = tmp_path / "orders.parquet"
    src = duckdb.connect(str(jaffle_db), read_only=True)
    src.execute(f"COPY analytics.orders TO '{pq}' (FORMAT parquet)")
    src.close()
    con = make_connector("duckdb", {"parquet": {"orders": str(pq)}})
    try:
        assert any(s.fqn == "main.orders" for s in con.introspect())
        r = run_guarded(con, "select count(*) as n from orders", row_cap=10, timeout_s=10)
        assert r.table.to_pydict() == {"n": [700]}
    finally:
        con.close()


def test_unknown_kind_and_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="unknown connection kind"):
        make_connector("oracle", {})
    monkeypatch.setenv("SATCHEL_CONN_X", "s3cret")
    assert resolve_secret("env:SATCHEL_CONN_X") == "s3cret"
    assert resolve_secret(None) is None
    with pytest.raises(ValueError, match="not set"):
        resolve_secret("env:SATCHEL_NOPE_NOT_SET")
    with pytest.raises(ValueError, match="unsupported"):
        resolve_secret("plain-password")


def test_result_dir_default_is_absolute() -> None:
    """MCP clients (Claude Desktop) launch satchel with cwd "/", so no cwd-relative defaults."""
    from pathlib import Path

    from satchel.settings import Settings

    assert Path(Settings().result_dir).expanduser().is_absolute()
