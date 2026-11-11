"""Deterministically build ``jaffle.duckdb``, the warehouse behind the Jaffle Shop example.

The schema matches ``context/assets/warehouse/analytics/*.yaml``: schema ``analytics`` with
``orders``, ``customers`` and ``payments``. Amounts are AUD cents. ``customer_id`` 0 is the
internal test customer (it has orders but no row in ``customers``). Orders with status
``returned`` or ``return_pending`` are excluded from revenue by the semantic model.

    python seed.py [PATH]      # default: jaffle.duckdb next to this file
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

DEFAULT_PATH = Path(__file__).parent / "jaffle.duckdb"
SEED = 20261028
N_CUSTOMERS = 40
N_ORDERS = 700
FIRST = ["Ava", "Noah", "Mia", "Liam", "Zoe", "Ethan", "Ruby", "Jack", "Ella", "Oscar"]
LAST = ["Nguyen", "Smith", "Patel", "Brown", "Wilson", "Taylor", "Lee", "Walker", "Hall", "King"]
STATUSES = ["placed", "shipped", "completed", "return_pending", "returned"]
STATUS_WEIGHTS = [0.05, 0.10, 0.65, 0.08, 0.12]
METHODS = ["credit_card", "bank_transfer", "gift_card", "coupon"]

DDL = """
CREATE SCHEMA analytics;
CREATE TABLE analytics.customers (
  customer_id INTEGER PRIMARY KEY, first_name VARCHAR, last_name VARCHAR, email VARCHAR,
  signup_date DATE, first_order_date DATE, number_of_orders INTEGER);
CREATE TABLE analytics.orders (
  order_id INTEGER PRIMARY KEY, customer_id INTEGER, order_date DATE, status VARCHAR,
  amount INTEGER);
CREATE TABLE analytics.payments (
  payment_id INTEGER PRIMARY KEY, order_id INTEGER, payment_method VARCHAR, status VARCHAR,
  amount INTEGER, created_at TIMESTAMP);
COMMENT ON TABLE analytics.orders IS 'One row per customer order (AUD cents).';
COMMENT ON COLUMN analytics.orders.amount IS 'Order total in AUD cents';
COMMENT ON COLUMN analytics.orders.order_id IS 'Primary key of the order';
COMMENT ON COLUMN analytics.orders.customer_id IS 'Customer who placed the order';
"""


def build_database(path: Path = DEFAULT_PATH) -> dict[str, int]:
    rnd = random.Random(SEED)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    con.execute(DDL)

    signup = {
        cid: date(2024, 1, 1) + timedelta(days=rnd.randint(0, 300))
        for cid in range(1, N_CUSTOMERS + 1)
    }
    orders: list[tuple[int, int, date, str, int]] = []
    for oid in range(1, N_ORDERS + 1):
        cid = 0 if rnd.random() < 0.04 else rnd.randint(1, N_CUSTOMERS)
        start = signup.get(cid, date(2024, 1, 1))
        day = min(start + timedelta(days=rnd.randint(0, 600)), date(2025, 12, 31))
        status = rnd.choices(STATUSES, STATUS_WEIGHTS)[0]
        amount = rnd.randint(8, 120) * 100 + rnd.choice([0, 50, 95])
        orders.append((oid, cid, day, status, amount))

    first_order: dict[int, date] = {}
    counts: dict[int, int] = {}
    for _oid, cid, day, status, _a in sorted(orders, key=lambda o: o[2]):
        if cid == 0:
            continue
        first_order.setdefault(cid, day)
        if status not in ("returned", "return_pending"):
            counts[cid] = counts.get(cid, 0) + 1
    customers = [
        (
            cid,
            (fn := rnd.choice(FIRST)),
            (ln := rnd.choice(LAST)),
            f"{fn}.{ln}{cid}@jaffle.example".lower(),
            signup[cid],
            first_order.get(cid),
            counts.get(cid, 0),
        )
        for cid in range(1, N_CUSTOMERS + 1)
    ]

    payments: list[tuple[int, int, str, str, int, datetime]] = []
    pid = 1
    for oid, _cid, day, status, amount in orders:
        ts = datetime(day.year, day.month, day.day, 9, 0) + timedelta(minutes=rnd.randint(0, 600))
        pay_status = "failed" if status == "placed" and rnd.random() < 0.3 else "succeeded"
        payments.append((pid, oid, rnd.choice(METHODS), pay_status, amount, ts))
        pid += 1
        if status == "returned":
            payments.append((pid, oid, rnd.choice(METHODS), "refunded", amount,
                             ts + timedelta(days=rnd.randint(1, 20))))
            pid += 1

    con.executemany("INSERT INTO analytics.customers VALUES (?,?,?,?,?,?,?)", customers)
    con.executemany("INSERT INTO analytics.orders VALUES (?,?,?,?,?)", orders)
    con.executemany("INSERT INTO analytics.payments VALUES (?,?,?,?,?,?)", payments)
    out = {
        t: int(con.execute(f"SELECT count(*) FROM analytics.{t}").fetchone()[0])  # type: ignore[index]
        for t in ("customers", "orders", "payments")
    }
    con.close()
    return out


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    for table, n in build_database(target).items():
        print(f"{table}: {n}")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
