"""Facts computed from result rows (satchel.data.facts)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from satchel.data.facts import choose_chart, compute, fmt_number

MONTHLY = [{"name": "m", "type": "timestamp[us]"}, {"name": "buyers", "type": "int64"},
           {"name": "revenue_from_new", "type": "double"}]
VALUES = [16, 19, 23, 24, 21, 18, 20, 21, 17, 18, 16, 20]


def _monthly(now: datetime) -> list[str]:
    rows = [(datetime(2025, i + 1, 1), v, 106.95 if i == 1 else None)
            for i, v in enumerate(VALUES)]
    return compute(MONTHLY, ["m", "buyers", "revenue_from_new"], rows, now=now).lines()


def test_time_series_facts() -> None:
    lines = _monthly(datetime(2026, 3, 1, tzinfo=UTC))
    assert lines == [
        "revenue_from_new is empty in 11 of 12 rows.",
        "buyers went from 16 (2025-01) to 20 (2025-12), up 25%.",
        "Latest month (2025-12): 20, up 25% on 2025-11.",
        "Peak 24 in 2025-04; low 16 in 2025-01. Average 19.42 a month.",
        "The trend is flat over 12 months.",
    ]


def test_partial_last_period() -> None:
    assert _monthly(datetime(2025, 12, 15, tzinfo=UTC))[-1] == (
        "The last month (2025-12) is not over yet, so it is partial.")


def test_annotations_mark_peak_and_low() -> None:
    rows = [(date(2025, 1, d), Decimal(v)) for d, v in [(1, 5), (2, 9), (3, 2)]]
    f = compute([{"name": "d", "type": "date32[day]"}, {"name": "n", "type": "decimal128(10,0)"}],
                ["d", "n"], rows, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert f.annotations == {"column": "n", "average": 16 / 3, "marks": [
        {"x": "2025-01-02", "label": "Peak 9"}, {"x": "2025-01-03", "label": "Low 2"}]}
    assert "went from 5 (2025-01-01) to 2 (2025-01-03), down 60%." in f.lines()[0]


def test_breakdown_share_and_units() -> None:
    cols = [{"name": "status", "type": "string"}, {"name": "revenue", "type": "double"}]
    rows = [("completed", 800.0), ("returned", 120.0), ("placed", 80.0)]
    lines = compute(cols, ["status", "revenue"], rows,
                    {"revenue": {"style": "currency", "currency": "USD"}}).lines()
    assert lines == [
        "completed has the most revenue: $800.00, 80% of the $1,000.00 total of these 3.",
        "placed has the least of these 3: $80.00 (average $333.33).",
    ]


def test_no_rows_single_row_and_bad_cells() -> None:
    cols = [{"name": "n", "type": "int64"}]
    assert compute(cols, ["n"], []).lines() == ["No rows matched this query."]
    assert compute(cols, ["n"], [(5,)]).lines() == []
    weird = [{"name": "t", "type": "timestamp[us]"}, {"name": "n", "type": "double"}]
    assert compute(weird, ["t", "n"], [("x", float("nan")), (None, 1.0)]).lines() == []


def test_fmt_number() -> None:
    assert fmt_number(1234.5) == "1,234.50" and fmt_number(19.5) == "19.5"
    assert fmt_number(19.4166) == "19.42"
    assert fmt_number(-3.5, {"style": "currency", "currency": "EUR"}) == "-€3.50"
    assert fmt_number(12, {"style": "percent"}) == "12%"
    assert fmt_number(3, {"style": "unit", "unit": "days", "decimals": 1}) == "3.0 days"


TOP = [{"name": "customer_id", "type": "int32"}, {"name": "first_name", "type": "string"},
       {"name": "last_name", "type": "string"}, {"name": "lifetime_spend_aud", "type": "double"},
       {"name": "valid_orders", "type": "int64"}, {"name": "first_order", "type": "date32[day]"},
       {"name": "last_order", "type": "date32[day]"}]
TOP_ROWS = [(31, "Ava", "Taylor", 1361.70, 21, date(2024, 11, 1), date(2025, 12, 31)),
            (1, "Mia", "Brown", 1329.75, 19, date(2024, 10, 23), date(2025, 12, 24)),
            (37, "Liam", "Brown", 1133.95, 16, date(2024, 3, 15), date(2025, 10, 13)),
            (2, "Oscar", "Hall", 1066.25, 15, date(2024, 11, 18), date(2025, 12, 31))]


def test_ranking_with_dates_is_bars_of_the_measure_by_name() -> None:
    """The bug: dates made it a line, and customer_id was the measure."""
    f = compute(TOP, [c["name"] for c in TOP], TOP_ROWS)
    assert f.chart == {"type": "bar", "x": ["first_name", "last_name"], "y": "lifetime_spend_aud"}
    assert f.lines()[0].startswith("Ava Taylor has the most lifetime_spend_aud: 1,361.70")
    assert not any("customer_id" in line for line in f.lines())


def test_chart_choice() -> None:
    def chart(cols: list[tuple[str, str]], rows: list[tuple[object, ...]]) -> object:
        kinds = [{"int64": "number", "double": "number", "string": "text",
                  "date32[day]": "time"}[t] for _, t in cols]
        return choose_chart(kinds, [n for n, _ in cols], rows)

    months = [date(2025, m, 1) for m in range(1, 5)]
    # one row per month: a line; the money column wins over a count
    assert chart([("month", "date32[day]"), ("orders", "int64"), ("revenue", "double")],
                 [(m, 10 + i, 100.0 * i) for i, m in enumerate(months)]) == \
        {"type": "line", "x": ["month"], "y": "revenue"}
    # several rows per month: a series per method, so no chart
    assert chart([("month", "date32[day]"), ("method", "string"), ("amount", "double")],
                 [(m, k, 1.0) for m in months for k in ("card", "coupon")]) is None
    # labelled things with one date each, in date order: still a list of things
    assert chart([("customer", "string"), ("last_order", "date32[day]"), ("spend", "double")],
                 [(f"c{i}", m, 5.0 + i) for i, m in enumerate(months)]) == \
        {"type": "bar", "x": ["customer"], "y": "spend"}
    # identifiers only: bars by the id; an id alone is not a measure
    assert chart([("customer_id", "int64"), ("orders", "int64")], [(7, 3), (9, 2)]) == \
        {"type": "bar", "x": ["customer_id"], "y": "orders"}
    assert chart([("customer_id", "int64"), ("name", "string")], [(7, "a"), (9, "b")]) is None
    # too many things for bars, and a single row
    assert chart([("k", "string"), ("v", "double")], [(str(i), 1.0) for i in range(31)]) is None
    assert chart([("k", "string"), ("v", "double")], [("a", 1.0)]) is None
