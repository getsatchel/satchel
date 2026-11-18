"""Build the result widget's test fixtures from the Jaffle Shop demo data.

Runs the queries an agent writes for the example questions against a fresh ``jaffle.duckdb``,
then makes each result's widget payload with the server's own code (column kinds, facts and the
chart choice), so the widget tests and the screenshot gallery see what Claude would show.

    cd server && uv run python ../scripts/widget_fixtures.py

Writes ``ui-widgets/test/fixtures/results.json``. ``cd ui-widgets && npm run gallery`` draws them.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "jaffle-shop"))

from seed import build_database  # noqa: E402

from satchel.data.facts import compute  # noqa: E402
from satchel.data.results import column_kind, json_cell  # noqa: E402

OUT = ROOT / "ui-widgets" / "test" / "fixtures" / "results.json"
# Fixed, so the facts about a partial last period don't change from day to day.
NOW = datetime(2026, 3, 1, tzinfo=UTC)
AUD = {"style": "currency", "currency": "AUD"}
VALID = "status NOT IN ('returned', 'return_pending')"

# name, title, sql, formats. Amounts are cents in the warehouse.
QUERIES: list[tuple[str, str, str, dict[str, dict[str, str]]]] = [
    ("monthly_revenue", "Monthly revenue (AUD)", f"""
        SELECT date_trunc('month', order_date) AS month, sum(amount) / 100.0 AS revenue_aud
        FROM analytics.orders WHERE {VALID} GROUP BY 1 ORDER BY 1""", {"revenue_aud": AUD}),
    ("top_customers", "Top 10 customers by lifetime spend (AUD)", f"""
        SELECT c.customer_id, c.first_name, c.last_name,
               sum(o.amount) / 100.0 AS lifetime_spend_aud, count(*) AS valid_orders,
               min(o.order_date) AS first_order, max(o.order_date) AS last_order
        FROM analytics.orders o JOIN analytics.customers c USING (customer_id)
        WHERE o.{VALID} GROUP BY ALL ORDER BY lifetime_spend_aud DESC LIMIT 10""", {}),
    ("payment_methods", "Payments by method", """
        SELECT payment_method, count(*) AS payments, sum(amount) / 100.0 AS amount_aud
        FROM analytics.payments GROUP BY 1 ORDER BY amount_aud DESC""", {"amount_aud": AUD}),
    ("revenue_by_status", "Order value by status", """
        SELECT status, sum(amount) / 100.0 AS order_value FROM analytics.orders
        GROUP BY 1 ORDER BY 2 DESC""", {"order_value": AUD}),
    ("monthly_orders_and_revenue", "Orders and revenue by month", f"""
        SELECT date_trunc('month', order_date) AS month, count(*) AS orders,
               sum(amount) / 100.0 AS revenue_aud, avg(amount) / 100.0 AS avg_order_aud
        FROM analytics.orders WHERE {VALID} AND order_date >= DATE '2025-01-01'
        GROUP BY 1 ORDER BY 1""", {"revenue_aud": AUD, "avg_order_aud": AUD}),
    ("monthly_by_method", "Monthly payments by method", """
        SELECT date_trunc('month', created_at) AS month, payment_method,
               sum(amount) / 100.0 AS amount_aud
        FROM analytics.payments WHERE created_at >= TIMESTAMP '2025-07-01'
        GROUP BY 1, 2 ORDER BY 1, 2""", {"amount_aud": AUD}),
    ("net_revenue_total", "Net revenue, all time", f"""
        SELECT sum(amount) / 100.0 AS net_revenue_aud FROM analytics.orders WHERE {VALID}""",
     {"net_revenue_aud": AUD}),
    ("customer_list", "Customers", """
        SELECT customer_id, first_name, last_name, email, signup_date FROM analytics.customers
        ORDER BY customer_id""", {}),
    ("orders_by_customer_id", "Orders per customer (top 10)", f"""
        SELECT customer_id, count(*) AS orders FROM analytics.orders WHERE {VALID}
        GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10""", {}),
    ("daily_orders", "Daily orders, last 60 days", """
        SELECT order_date, count(*) AS orders FROM analytics.orders
        WHERE order_date > DATE '2025-12-31' - 60 GROUP BY 1 ORDER BY 1""", {}),
    ("customers_by_last_order", "Customers by most recent order", f"""
        SELECT c.first_name || ' ' || c.last_name AS customer, max(o.order_date) AS last_order,
               sum(o.amount) / 100.0 AS spend_aud
        FROM analytics.orders o JOIN analytics.customers c USING (customer_id)
        WHERE o.{VALID} GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", {"spend_aud": AUD}),
]


def payload(name: str, title: str, sql: str, formats: dict, con: duckdb.DuckDBPyConnection) -> dict:
    table = con.sql(sql).to_arrow_table()
    columns = [{"name": f.name, "type": str(f.type)} for f in table.schema]
    cols = [c["name"] for c in columns]
    rows = [tuple(r.values()) for r in table.to_pylist()]
    facts = compute(columns, cols, rows, formats, now=NOW)
    return {
        "satchel": 1, "tool": "run_sql", "session_id": "S1", "result_id": name,
        "tool_call_id": 1, "connection": "warehouse", "question": None, "title": title,
        "sql": " ".join(sql.split()), "metric": None, "formats": formats,
        "columns": [{"name": c["name"], "type": c["type"], "kind": column_kind(c["type"])}
                    for c in columns],
        "rows": [[json_cell(v) for v in r] for r in rows], "offset": 0,
        "row_count": len(rows), "truncated": False, "ctx": [], "ctx_items": [],
        "facts": facts.facts, "annotations": facts.annotations, "chart": facts.chart,
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "jaffle.duckdb"
        build_database(db)
        con = duckdb.connect(str(db), read_only=True)
        results = {n: payload(n, t, q, f, con) for n, t, q, f in QUERIES}
        con.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=1) + "\n")
    for n, p in results.items():
        print(f"{n:28} chart={p['chart']}")


if __name__ == "__main__":
    main()
