"""Facts computed from a result's rows: what changed, the peak and the low, the trend, a share of
the total, empty columns, and a last period that is not over yet. Also the chart the widget draws
(:func:`choose_chart`), so the chart and the facts are always about the same columns.

The numbers come from the data, never from a model. ``run_sql`` and ``query_metrics`` list them in
their text, so the agent can cite them, and the result and answer widgets show them next to the
chart, with the average as a reference line and the peak and low marked.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from statistics import mean, median, pstdev
from typing import Any

from satchel.data.results import column_kind

MAX_FACTS = 6
_CURRENCY = {"USD": "$", "AUD": "A$", "CAD": "C$", "NZD": "NZ$", "EUR": "€", "GBP": "£",
             "JPY": "¥", "INR": "₹"}
# Period name and the typical days between two periods.
_GRAINS = [("day", 1.0), ("week", 7.0), ("month", 30.4), ("quarter", 91.3), ("year", 365.2)]
# A bar chart with more bars than this is unreadable; the table says it better.
MAX_BARS = 30
# Numbers that name something rather than measure it: customer_id, store_key, postcode.
_IDENTIFIER = re.compile(r"(^|_)(id|ids|key|code|number|no|zip|postcode|sku)$", re.IGNORECASE)
# Words that mark a column as the measure people care about, before counts and averages.
_MONEY = re.compile(r"revenue|spend|sales|amount|total|value|price|cost|profit|aud|usd|eur|gbp",
                    re.IGNORECASE)


@dataclass
class Facts:
    facts: list[dict[str, str]] = field(default_factory=list)
    # For the chart: the column the facts are about, its average and marked points.
    annotations: dict[str, Any] | None = None
    # The chart the widget draws, or None for no chart (see choose_chart).
    chart: dict[str, Any] | None = None

    def lines(self) -> list[str]:
        return [f["text"] for f in self.facts]


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float | Decimal):
        f = float(v)
        return f if math.isfinite(f) else None
    return None


def _when(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=UTC)
    if isinstance(v, str):
        try:
            return _when(datetime.fromisoformat(v))
        except ValueError:
            return None
    return None


def fmt_number(v: float, f: dict[str, Any] | None = None) -> str:
    """Like the widget's fmtValue: units only when the server knows them."""
    dec = f.get("decimals") if f else None
    style = f.get("style") if f else None
    # Non-whole numbers get two decimals, so 3,258.20 sits next to 1,671.55; only short
    # values drop trailing zeros (19.5, not 19.50).
    trim = dec is None and style != "currency" and abs(v) < 100
    if dec is None:
        dec = 2 if style == "currency" or not float(v).is_integer() else 0
    body = f"{v:,.{dec}f}"
    if trim and "." in body:
        body = body.rstrip("0").rstrip(".")
    if not f:
        return body
    if style == "currency":
        cur = str(f.get("currency") or "")
        sym = _CURRENCY.get(cur.upper())
        return (f"-{sym}{body[1:]}" if body.startswith("-") else f"{sym}{body}") if sym \
            else f"{cur} {body}".strip()
    if style == "prefix":
        return f"{f.get('symbol') or ''}{body}"
    if style == "percent":
        return f"{body}%"
    if style == "unit":
        return f"{body} {f.get('unit') or ''}".strip()
    return body


def _pct(new: float, old: float) -> str | None:
    if old == 0:
        return None
    p = (new - old) / abs(old) * 100
    if abs(p) < 0.05:
        return "unchanged"
    digits = 0 if abs(p) >= 10 else 1
    return f"{'up' if p > 0 else 'down'} {abs(p):.{digits}f}%"


def _grain(times: list[datetime]) -> tuple[str, float] | None:
    gaps = [(b - a).total_seconds() / 86400 for a, b in zip(times, times[1:], strict=False)]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    g = median(gaps)
    for name, days in _GRAINS:
        if abs(g - days) <= days * 0.15:
            return name, days
    return None


def _label(t: datetime, grain: str | None) -> str:
    if grain == "month":
        return t.strftime("%Y-%m")
    if grain == "year":
        return t.strftime("%Y")
    if grain == "quarter":
        return f"{t.year}-Q{(t.month - 1) // 3 + 1}"
    if t.hour or t.minute:
        return t.strftime("%Y-%m-%d %H:%M")
    return t.strftime("%Y-%m-%d")


def _period_end(t: datetime, grain: str) -> datetime:
    if grain == "day":
        return t + timedelta(days=1)
    if grain == "week":
        return t + timedelta(days=7)
    months = {"month": 1, "quarter": 3, "year": 12}[grain]
    m = t.month - 1 + months
    return t.replace(year=t.year + m // 12, month=m % 12 + 1, day=1)


def _empty_facts(name: str, values: list[Any]) -> dict[str, str] | None:
    n = len(values)
    nulls = sum(v is None for v in values)
    if n >= 2 and nulls == n:
        return {"kind": "empty_column", "text": f"{name} is empty in every row."}
    if n >= 4 and nulls * 2 >= n:
        return {"kind": "sparse_column", "text": f"{name} is empty in {nulls} of {n} rows."}
    return None


def is_identifier(name: str) -> bool:
    """A number column that names a thing (``customer_id``) and is not a measure."""
    return bool(_IDENTIFIER.search(name))


def choose_chart(
    kinds: list[str], cols: list[str], rows: list[tuple[Any, ...]],
    formats: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The chart that fits these rows, or None when a chart would not help.

    - **line** ``{"type": "line", "x": [time], "y": measure}``: the rows are a time series, one
      row per period: a date column with no repeated dates, and no label that tells the rows
      apart. A list of customers with their first and last order dates is not a time series.
    - **bar** ``{"type": "bar", "x": [label, ...], "y": measure}``: one row per thing (a status, a
      customer), at most ``MAX_BARS``, labelled by its text columns (``first_name last_name``) or,
      with none, by its identifier. Bars keep the rows' order, so a ranking reads top-down.
    - None otherwise: one row, no measure, too many things, or several rows per date (a series
      per label, which one line can't show).

    The measure is a number that is not an identifier: a money column (by its format or name)
    first, then the first number column.
    """
    formats = formats or {}
    if len(rows) < 2:
        return None
    nums = [i for i, k in enumerate(kinds) if k == "number"]
    ids = [i for i in nums if is_identifier(cols[i])]
    measures = [i for i in nums if i not in ids
                and sum(_num(r[i]) is not None for r in rows) >= 2]
    if not measures:
        return None
    money = [i for i in measures if (formats.get(cols[i]) or {}).get("style") in
             ("currency", "prefix") or _MONEY.search(cols[i])]
    y = (money or measures)[0]
    labels = [i for i, k in enumerate(kinds) if k == "text"][:2] or ids[:1]
    keys = [tuple(r[i] for i in labels) for r in rows]
    one_per_thing = bool(labels) and len(set(keys)) == len(keys)
    for ti in (i for i, k in enumerate(kinds) if k == "time"):
        seen = [t for r in rows if (t := _when(r[ti])) is not None]
        if len(seen) >= 2 and len(set(seen)) == len(seen) and not one_per_thing:
            return {"type": "line", "x": [cols[ti]], "y": cols[y]}
    if one_per_thing and len(rows) <= MAX_BARS:
        return {"type": "bar", "x": [cols[i] for i in labels], "y": cols[y]}
    return None


def bar_label(row: tuple[Any, ...] | list[Any], idx: list[int]) -> str:
    """A bar's label: its label columns joined with a space, as the widget joins them."""
    return " ".join(str(row[i]) for i in idx if row[i] is not None)


def compute(
    columns: list[dict[str, str]],
    cols: list[str],
    rows: list[tuple[Any, ...]],
    formats: dict[str, dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> Facts:
    """Facts about ``rows`` (all of them, or as many as were read). Never raises on odd data."""
    try:
        return _compute(columns, cols, rows, formats or {}, now or datetime.now(UTC))
    except Exception:  # noqa: BLE001 - facts are a bonus; a bad cell must not fail the query
        return Facts()


def _compute(
    columns: list[dict[str, str]], cols: list[str], rows: list[tuple[Any, ...]],
    formats: dict[str, dict[str, Any]], now: datetime,
) -> Facts:
    out = Facts()
    if not rows:
        out.facts.append({"kind": "no_rows", "text": "No rows matched this query."})
        return out
    types = {c["name"]: c.get("type", "") for c in columns}
    kinds = [column_kind(types.get(c, "")) for c in cols]
    num_idx = [i for i, k in enumerate(kinds) if k == "number"]
    by_col = {i: [r[i] for r in rows] for i in range(len(cols))}

    for i in num_idx:
        e = _empty_facts(cols[i], by_col[i])
        if e:
            out.facts.append(e)
    # The facts describe the chart's measure, so the chart and the facts always agree.
    chart = choose_chart(kinds, cols, rows, formats)
    out.chart = chart
    if chart is None:
        return out
    y = cols.index(chart["y"])
    x = [cols.index(n) for n in chart["x"]]
    if chart["type"] == "line":
        others = [i for i in num_idx if i != y and not is_identifier(cols[i])
                  and sum(_num(v) is not None for v in by_col[i]) >= 2]
        _time_facts(out, cols, rows, x[0], y, others[:2], formats, now)
    else:
        _share_facts(out, cols, rows, x, y, formats)
    out.facts = out.facts[:MAX_FACTS]
    return out


def _time_facts(
    out: Facts, cols: list[str], rows: list[tuple[Any, ...]], ti: int, y: int,
    others: list[int], formats: dict[str, dict[str, Any]], now: datetime,
) -> None:
    pts: list[tuple[datetime, float, tuple[Any, ...]]] = []
    for r in rows:
        t, v = _when(r[ti]), _num(r[y])
        if t is not None and v is not None:
            pts.append((t, v, r))
    pts.sort(key=lambda p: p[0])
    if len(pts) < 2:
        return
    times = [p[0] for p in pts]
    vals = [p[1] for p in pts]
    gr = _grain(times)
    grain = gr[0] if gr else None
    name, f = cols[y], formats.get(cols[y])
    lab = [_label(t, grain) for t in times]

    def n(v: float) -> str:
        return fmt_number(v, f)

    first, last = vals[0], vals[-1]
    ch = _pct(last, first)
    out.facts.append({"kind": "change", "text": (
        f"{name} went from {n(first)} ({lab[0]}) to {n(last)} ({lab[-1]})"
        + (f", {ch}." if ch and ch != "unchanged" else ", unchanged." if ch else ".")
    )})
    if len(vals) >= 3:
        prev = _pct(last, vals[-2])
        if prev and prev != "unchanged":
            out.facts.append({"kind": "latest", "text": f"Latest {grain or 'period'} "
                              f"({lab[-1]}): {n(last)}, {prev} on {lab[-2]}."})
    hi = max(range(len(vals)), key=lambda i: vals[i])
    lo = min(range(len(vals)), key=lambda i: vals[i])
    avg = mean(vals)
    marks: list[dict[str, Any]] = []
    if hi != lo:
        out.facts.append({"kind": "range", "text":
                          f"Peak {n(vals[hi])} in {lab[hi]}; low {n(vals[lo])} in {lab[lo]}. "
                          f"Average {n(avg)}" + (f" a {grain}." if grain else ".")})
        marks = [{"x": _iso(times[hi]), "label": f"Peak {n(vals[hi])}"},
                 {"x": _iso(times[lo]), "label": f"Low {n(vals[lo])}"}]
    trend = _trend(vals)
    if trend:
        out.facts.append({"kind": "trend", "text": f"The trend is {trend} over {len(vals)} periods."
                          if not grain else f"The trend is {trend} over {len(vals)} {grain}s."})
    if len(vals) >= 8:
        sd = pstdev(vals)
        if sd > 0:
            for i, v in enumerate(vals):
                if abs(v - avg) / sd >= 2.5 and i not in (hi, lo):
                    out.facts.append({"kind": "outlier", "text":
                                      f"{lab[i]} stands out at {n(v)} (average {n(avg)})."})
                    break
    if grain and grain in ("day", "week", "month", "quarter", "year"):
        end = _period_end(times[-1], grain)
        if times[-1] <= now < end:
            out.facts.append({"kind": "partial", "text":
                              f"The last {grain} ({lab[-1]}) is not over yet, so it is partial."})
    for o in others:
        ov = [x for _, _, r in pts if (x := _num(r[o])) is not None]
        if len(ov) >= 2:
            c = _pct(ov[-1], ov[0])
            if c and c != "unchanged":
                fo = formats.get(cols[o])
                out.facts.append({"kind": "change", "text":
                                  f"{cols[o]} went from {fmt_number(ov[0], fo)} to "
                                  f"{fmt_number(ov[-1], fo)}, {c}."})
    out.annotations = {"column": name, "average": avg, "marks": marks}


def _iso(t: datetime) -> str:
    return t.date().isoformat()


def _trend(vals: list[float]) -> str | None:
    """Least-squares slope over the series, relative to its mean: rising, falling or flat."""
    k = len(vals)
    if k < 4:
        return None
    xs = range(k)
    mx, my = (k - 1) / 2, mean(vals)
    den = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (v - my) for x, v in zip(xs, vals, strict=True)) / den
    scale = abs(my) or (max(vals) - min(vals)) or 1
    rel = slope * (k - 1) / scale
    if rel > 0.15:
        return "rising"
    if rel < -0.15:
        return "falling"
    return "flat"


def _share_facts(
    out: Facts, cols: list[str], rows: list[tuple[Any, ...]], li: list[int], y: int,
    formats: dict[str, dict[str, Any]],
) -> None:
    items = [(bar_label(r, li), v) for r in rows if (v := _num(r[y])) is not None]
    if len(items) < 2:
        return
    # A bare identifier reads better with its column: "customer_id 27", not "27".
    by_id = all(is_identifier(cols[i]) for i in li)

    def said(lab: str) -> str:
        return f"{cols[li[0]]} {lab}" if by_id else lab

    name, f = cols[y], formats.get(cols[y])
    vals = [v for _, v in items]
    avg = mean(vals)
    ranked = sorted(items, key=lambda p: p[1], reverse=True)
    top, bottom, k = ranked[0], ranked[-1], len(ranked)
    total = sum(vals)
    if all(v >= 0 for v in vals) and total > 0:
        share = top[1] / total * 100
        out.facts.append({"kind": "top", "text":
                          f"{said(top[0])} has the most {name}: {fmt_number(top[1], f)}, "
                          f"{share:.0f}% of the {fmt_number(total, f)} total of these {k}."})
        if len(ranked) >= 6:
            top3 = sum(v for _, v in ranked[:3]) / total * 100
            out.facts.append({"kind": "concentration", "text":
                              f"The top 3 of these {k} make up {top3:.0f}%."})
    else:
        out.facts.append({"kind": "top", "text": f"{said(top[0])} has the most {name}: "
                          f"{fmt_number(top[1], f)}."})
    if bottom[1] != top[1]:
        out.facts.append({"kind": "bottom", "text": f"{said(bottom[0])} has the least of these "
                          f"{k}: {fmt_number(bottom[1], f)} (average {fmt_number(avg, f)})."})
    out.annotations = {"column": name, "average": avg,
                       "marks": [{"x": top[0], "label": f"Top {fmt_number(top[1], f)}"}]}
