"""Semantic engine: wren-core compile + dialect step, checked against hand-written SQL on the
seeded DuckDB (golden tests that guard wren-core-py upgrades)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from satchel.data.semantic import (
    SemanticError,
    build_cube_query,
    compile_query,
    describe_metric,
    list_metrics,
    parse_mdl,
)
from tests.helpers import EXAMPLE_CONTEXT, build_jaffle_db

MDL = parse_mdl((EXAMPLE_CONTEXT / "semantic/core/jaffle.mdl.json").read_text())
VALID = "status not in ('returned','return_pending') and customer_id <> 0"


@pytest.fixture(scope="module")
def con(tmp_path_factory: pytest.TempPathFactory) -> duckdb.DuckDBPyConnection:
    db = build_jaffle_db(tmp_path_factory.mktemp("sem") / "jaffle.duckdb")
    return duckdb.connect(str(db), read_only=True)


def run(con: duckdb.DuckDBPyConnection, metrics: list[str], **kw: object) -> list[tuple]:
    q = build_cube_query(
        MDL, metrics, kw.get("dimensions", []), kw.get("filters", []),  # type: ignore[arg-type]
        kw.get("order_by", []), kw.get("limit"),  # type: ignore[arg-type]
    )
    return con.execute(compile_query(MDL, q, "duckdb").sql).fetchall()


def test_revenue_total_and_month(con: duckdb.DuckDBPyConnection) -> None:
    want = con.execute(f"select sum(amount)/100.0 from analytics.orders where {VALID}").fetchone()
    assert run(con, ["revenue"])[0][0] == pytest.approx(want[0])  # type: ignore[index]
    march = [
        {"dimension": "order_date", "operator": "gte", "value": "2025-03-01"},
        {"dimension": "order_date", "operator": "lt", "value": "2025-04-01"},
    ]
    got = run(con, ["revenue", "order_count"], filters=march)
    want = con.execute(
        f"select sum(amount)/100.0, count(*) from analytics.orders where {VALID} "
        "and order_date >= '2025-03-01' and order_date < '2025-04-01'"
    ).fetchone()
    assert got[0][0] == pytest.approx(want[0]) and got[0][1] == want[1]  # type: ignore[index]


def test_by_month_matches_hand_sql(con: duckdb.DuckDBPyConnection) -> None:
    got = run(con, ["revenue"], dimensions=["order_date:month"],
              filters=[{"dimension": "order_date", "operator": "gte", "value": "2025-01-01"}])
    want = con.execute(
        f"select date_trunc('month', order_date), sum(amount)/100.0 from analytics.orders "
        f"where {VALID} and order_date >= '2025-01-01' group by 1"
    ).fetchall()
    assert len(got) == len(want) > 3
    g = {str(r[0])[:10]: r[1] for r in got}
    assert all(g[str(r[0])[:10]] == pytest.approx(r[1]) for r in want)


def test_top_customers_join_order_limit(con: duckdb.DuckDBPyConnection) -> None:
    got = run(con, ["revenue"], dimensions=["customer_name"],
              order_by=[{"member": "revenue", "direction": "desc"}], limit=5)
    want = con.execute(
        "select c.first_name || ' ' || c.last_name n, sum(o.amount)/100.0 r "
        "from analytics.orders o join analytics.customers c using (customer_id) "
        "where o.status not in ('returned','return_pending') and o.customer_id <> 0 "
        "group by 1 order by r desc limit 5"
    ).fetchall()
    assert [r[0] for r in got] == [r[0] for r in want]
    assert [r[1] for r in got] == pytest.approx([r[1] for r in want])


def test_dialect_step_transpiles() -> None:
    q = build_cube_query(MDL, ["revenue"], ["order_date:month"], [], [], None)
    pg = compile_query(MDL, q, "postgres").sql
    bq = compile_query(MDL, q, "bigquery").sql
    assert "DATE_TRUNC" in pg.upper() and pg != bq


def test_catalog_and_errors() -> None:
    cubes = list_metrics(MDL)
    assert cubes[0]["cube"] == "sales"
    assert {m["name"] for m in cubes[0]["metrics"]} == {
        "revenue", "order_count", "average_order_value"}
    d = describe_metric(MDL, "revenue")
    assert d["source_tables"] == ["analytics.orders"] and "customer_id <> 0" in d["base_filter_sql"]
    with pytest.raises(SemanticError, match="Did you mean"):
        describe_metric(MDL, "revenu")
    with pytest.raises(SemanticError, match="Unknown dimension"):
        build_cube_query(MDL, ["revenue"], ["colour"], [], [], None)
    with pytest.raises(SemanticError, match="granularity"):
        build_cube_query(MDL, ["revenue"], ["order_date"], [], [], None)
    with pytest.raises(SemanticError, match="operator"):
        build_cube_query(MDL, ["revenue"],  [],
                         [{"dimension": "status", "operator": "~", "value": "x"}], [], None)
    assert json.loads(json.dumps(MDL))["cubes"][0]["name"] == "sales"
    assert Path(EXAMPLE_CONTEXT).exists()
