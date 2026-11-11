"""Evidence redaction (design §9): what a curator may see of other people's sessions.

Curators get SQL string and numeric literals replaced with ``?`` (sqlglot; IDs and phone
numbers are PII) and free text truncated to 120 characters. LIMIT / OFFSET / FETCH counts,
INTERVAL operands and GROUP BY / ORDER BY ordinals stay readable: they are not PII and help
curators read the query. Admins see raw values; sensitive sessions are metadata only.
"""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp

TRUNCATE = 120
# Regex fallback, one left-to-right pass. Alternatives that are kept verbatim come first so the
# number/string alternatives never see their digits: quoted identifiers, LIMIT/OFFSET/FETCH
# counts, INTERVAL operands and the first GROUP/ORDER BY ordinal.
_FALLBACK = re.compile(
    r"""(?P<keep>
          "(?:[^"]|"")*"                                        # quoted identifier
        | `[^`]*`
        | \b(?:limit|offset|top)\s+\d+(?:\s*,\s*\d+)?           # LIMIT 10 / LIMIT 5, 10
        | \bfetch\s+(?:first|next)\s+\d+
        | \b(?:group|order)\s+by\s+\d+\b                     # ordinal
        | \binterval\s+(?:'(?:[^']|'')*'|\d+)(?:\s+[a-z]+)?   # INTERVAL '7' day / INTERVAL 7 DAY
        )
      | (?P<str>'(?:[^']|'')*')
      | (?P<num>(?<![\w.$])(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?(?![\w]))
    """,
    re.IGNORECASE | re.VERBOSE,
)
_KEEP_CONTEXT = (exp.Limit, exp.Offset, exp.Fetch, exp.Interval)


def _kept_literal(lit: exp.Expression) -> bool:
    """LIMIT/OFFSET/FETCH counts and INTERVAL operands are not PII; neither are GROUP/ORDER BY
    ordinals (``group by 1``)."""
    parent = lit.parent
    if lit.is_int and (
        isinstance(parent, exp.Group)
        or (isinstance(parent, exp.Ordered) and isinstance(parent.parent, exp.Order))
    ):
        return True
    node = parent
    while node is not None and not isinstance(node, exp.Select | exp.Subquery):
        if isinstance(node, _KEEP_CONTEXT):
            return True
        node = node.parent
    return False


def _fallback_sub(m: re.Match[str]) -> str:
    if m.group("keep") is not None:
        return m.group("keep")
    return "'?'" if m.group("str") is not None else "?"


def truncate(text: str, n: int = TRUNCATE) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def redact_sql(sql: str) -> str:
    """Replace string and numeric literals with ``?``. LIMIT/OFFSET/FETCH counts, INTERVAL
    operands and GROUP/ORDER BY ordinals are kept. Falls back to a regex when sqlglot can't
    parse. This is for curator/LLM views only; never store or promote the result."""
    try:
        trees = [t for t in sqlglot.parse(sql) if t is not None]
        if not trees:
            raise sqlglot.errors.SqlglotError("empty")
        out: list[str] = []
        for tree in trees:
            for lit in list(tree.find_all(exp.Literal)):
                if _kept_literal(lit):
                    continue
                lit.replace(exp.Literal.string("?") if lit.is_string else exp.Placeholder())
            out.append(tree.sql())
        return "; ".join(out)
    except sqlglot.errors.SqlglotError:
        return _FALLBACK.sub(_fallback_sub, sql)


def _walk(value: Any, key: str | None) -> Any:
    if isinstance(value, str):
        if key in {"sql", "filter", "where"}:
            return truncate(redact_sql(value), 2000)
        return truncate(value)
    if isinstance(value, dict):
        return {k: _walk(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, key) for v in value]
    return value


def redact_json(value: dict[str, Any]) -> dict[str, Any]:
    """Curator view of a JSON payload (tool args, signal payload, result meta)."""
    out = _walk(value, None)
    assert isinstance(out, dict)
    return out


def view_text(level: str, value: str | None) -> str | None:
    """A string as the viewer may see it (``level`` is none | redacted | metadata_only)."""
    if value is None or level == "none":
        return value
    return truncate(value) if level == "redacted" else None


def view_obj(level: str, value: dict[str, Any]) -> dict[str, Any] | None:
    if level == "none":
        return value
    return redact_json(value) if level == "redacted" else None


def metadata_only(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only counts and flags (numbers, booleans); drop every string and structure."""
    return {k: v for k, v in value.items() if isinstance(v, bool | int | float)}
