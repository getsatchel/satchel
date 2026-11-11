"""Data-plane tools (design §6.1, §11): run_sql, query_metrics, get_result, list_metrics,
describe_metric. Only offered when the workspace has at least one connection."""

from __future__ import annotations

import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

import pyarrow as pa
import sqlglot
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from satchel import limits
from satchel.core.item_types import yaml_item_dict
from satchel.data.connections import (
    ConnectionError_,
    open_connector,
    pick_connection,
    sqlglot_dialect,
)
from satchel.data.connectors import SqlTimeout
from satchel.data.gateway import GatewayResult, GuardrailError, run_guarded
from satchel.data.results import column_kind, json_cell, markdown_table, read_page, store_result
from satchel.data.semantic import (
    OPERATORS,
    SemanticError,
    _find_measure,
    build_cube_query,
    compile_query,
    describe_metric,
    list_metrics,
    load_published,
    merge_manifests,
    shadowed_models,
)
from satchel.settings import get_settings
from satchel.signals.extractors import SqlRun, extract_sql_signals
from satchel.store.models import (
    Connection,
    ContextItem,
    ContextVersion,
    Feedback,
    QueryResult,
    ServedContext,
    Signal,
    ToolCall,
)
from satchel.store.models import Session as SessionRow
from satchel.tools import text
from satchel.tools.definitions import (
    _Q,
    _SID,
    _Base,
    _log_served,
    _release_published,
    _session_item_ids,
    _signal,
)
from satchel.tools.registry import Call, Handler, ToolDef, ToolUserError

PREVIEW_ROWS = 50

_OP_ALIASES = {"=": "eq", "==": "eq", "!=": "neq", "<>": "neq", ">": "gt", ">=": "gte",
               "<": "lt", "<=": "lte", "not in": "not_in", "like": "contains",
               # Cube-style names
               "equals": "eq", "notequals": "neq",
               "set": "is_not_null", "notset": "is_null", "afterdate": "gt", "beforedate": "lt",
               "afterorondate": "gte", "beforeorondate": "lte"}


class RunSqlArgs(_Base):
    session_id: str = Field(description=_SID)
    question: str = Field(description=_Q)
    sql: str = Field(min_length=1, description="One read-only SELECT / WITH statement")
    connection: str | None = Field(default=None, description="Connection name (if several)")


class FilterArg(_Base):
    dimension: str = Field(description="Dimension or time dimension name")
    operator: Literal[
        "eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte", "contains", "starts_with",
        "is_null", "is_not_null",
    ]
    value: Any = Field(default=None, description="Value, or a list for in / not_in")

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, v: Any) -> Any:
        """Accept Cube-style filters ({member, operator, values: [...]}) as agents often send."""
        if not isinstance(v, dict):
            return v
        v = dict(v)
        for alt in ("member", "name", "field", "column"):
            if "dimension" not in v and alt in v:
                v["dimension"] = v.pop(alt)
        if "value" not in v and "values" in v:
            vals = v.pop("values")
            op = str(v.get("operator", "")).strip().lower()
            op = _OP_ALIASES.get(op, op)
            single = isinstance(vals, list) and len(vals) == 1 and op not in ("in", "not_in")
            v["value"] = vals[0] if single else vals
        return v

    @field_validator("operator", mode="before")
    @classmethod
    def _op(cls, v: Any) -> Any:
        v = str(v).strip().lower()
        return _OP_ALIASES.get(v, v)


class OrderArg(_Base):
    member: str = Field(description="A selected metric or dimension")
    direction: Literal["asc", "desc"] = "asc"


class QueryMetricsArgs(_Base):
    session_id: str = Field(description=_SID)
    question: str = Field(description=_Q)
    metrics: list[str] = Field(min_length=1, description="Metric names from list_metrics")
    dimensions: list[str] = Field(
        default_factory=list, description="Dimensions; time dims as name:grain (order_date:month)"
    )
    filters: list[FilterArg] = Field(
        default_factory=list,
        description='Filters, e.g. [{"dimension": "order_date", "operator": "gte", '
        '"value": "2025-03-01"}, {"dimension": "order_date", "operator": "lt", '
        '"value": "2025-04-01"}]',
    )
    order_by: list[OrderArg] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=100_000)
    connection: str | None = None

    @field_validator("metrics", "dimensions", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> Any:
        if v is None:
            return []
        return [v] if isinstance(v, str) else v

    @field_validator("filters", mode="before")
    @classmethod
    def _date_ranges(cls, v: Any) -> Any:
        """Expand Cube's inDateRange [start, end] into gte start + lte end."""
        if not isinstance(v, list):
            return [] if v is None else v
        out: list[Any] = []
        for f in v:
            op = str(f.get("operator", "")).replace("_", "").lower() if isinstance(f, dict) else ""
            vals = f.get("values", f.get("value")) if isinstance(f, dict) else None
            if op == "indaterange" and isinstance(vals, list) and len(vals) == 2:
                dim = next((f[k] for k in ("dimension", "member", "name") if k in f), None)
                out += [{"dimension": dim, "operator": "gte", "value": vals[0]},
                        {"dimension": dim, "operator": "lte", "value": vals[1]}]
            else:
                out.append(f)
        return out

    @field_validator("order_by", mode="before")
    @classmethod
    def _order(cls, v: Any) -> Any:
        if v is None:
            return []
        out = []
        for o in [v] if isinstance(v, str | dict) else v:
            if isinstance(o, str):
                m = re.match(r"^\s*(-)?\s*([\w.:]+)\s*(asc|desc)?\s*$", o, re.I)
                if not m:
                    return v
                desc = bool(m.group(1)) or (m.group(3) or "").lower() == "desc"
                out.append({"member": m.group(2), "direction": "desc" if desc else "asc"})
            else:
                out.append(o)
        return out


class GetResultArgs(_Base):
    session_id: str = Field(description=_SID)
    result_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=1000)
    question: str | None = Field(default=None, description=_Q)


class RateResultArgs(_Base):
    """Sent by the result widget (never by the model: the tool is app-only)."""

    session_id: str = Field(description=_SID)
    tool_call_id: int = Field(description="The tool call whose result is being rated")
    sentiment: Literal["up", "down", "wrong"]
    reasons: list[Literal["wrong_number", "wrong_definition", "missing_filter",
                          "not_what_i_asked"]] = Field(default_factory=list, max_length=4)
    comment: str | None = Field(default=None, max_length=2000)
    result_id: str | None = None
    client: str | None = Field(default=None, description="Host that rendered the widget")


class ListMetricsArgs(_Base):
    session_id: str = Field(description=_SID)
    question: str | None = Field(default=None, description=_Q)


class DescribeMetricArgs(_Base):
    session_id: str = Field(description=_SID)
    question: str = Field(description=_Q)
    name: str = Field(min_length=1, description="Metric name, e.g. revenue")


# ----------------------------------------------------------------------------- helpers


def _rows(table: pa.Table, n: int) -> tuple[list[str], list[tuple[Any, ...]]]:
    t = table.slice(0, n)
    cols = t.column_names
    data = t.to_pydict()
    return cols, [tuple(data[c][i] for c in cols) for i in range(t.num_rows)]


def _format(
    result_id: uuid.UUID, conn_name: str, sql: str, gr: GatewayResult, extra: str = ""
) -> str:
    cols, rows = _rows(gr.table, PREVIEW_ROWS)
    n = gr.table.num_rows
    head = [
        f"result_id: {result_id}",
        f"connection: {conn_name}",
        f"rows: {n}" + (" (TRUNCATED at the row cap; narrow the query)" if gr.truncated else ""),
        f"columns: {', '.join(cols)}",
    ]
    head += gr.notes
    body = markdown_table(cols, rows)
    more = (f"\n(showing {len(rows)} of {n} rows; call get_result(result_id, offset, limit) "
            "for more)" if n > len(rows) else "")
    return "\n".join(head) + f"\n\n{extra}sql:\n```sql\n{sql.strip()}\n```\n\n{body}{more}"


_CTX_TYPE_LABEL = {
    "asset_doc": "table doc", "glossary_term": "glossary", "guide": "guide",
    "golden_query": "golden query", "workspace_rule": "rule", "caveat": "caveat",
    "semantic_model": "metric model", "endorsement": "endorsement", "user_memory": "memory",
}


def _first_line(text_: str, n: int = 70) -> str:
    line = next((x.strip(" #*-\t") for x in text_.splitlines() if x.strip(" #*-\t")), "")
    return line if len(line) <= n else line[: n - 1] + "…"


def _ctx_title(item_type: str, key: str, body: str, meta: dict[str, Any]) -> str:
    """A readable title for a context item (never raises: falls back to the key)."""
    try:
        if item_type in ("glossary_term", "golden_query", "asset_doc", "endorsement"):
            field = {"glossary_term": "term", "golden_query": "question",
                     "asset_doc": "asset", "endorsement": "asset"}[item_type]
            d = yaml_item_dict(item_type, body, meta)
            if d.get(field):
                return str(d[field])
        elif item_type == "guide" and meta.get("name"):
            return str(meta["name"])
        elif body.strip():
            return _first_line(body)
    except Exception:
        pass
    return key


def _ctx_items(c: Call) -> list[dict[str, Any]]:
    """Context served in this session as chips for the widget: readable title and type, plus a
    link to the curator UI item page when the server's public URL is configured."""
    assert c.session is not None
    rows = c.s.execute(
        select(ContextItem.key, ContextItem.type, ContextVersion.seq, ContextVersion.body,
               ContextVersion.meta)
        .join(ContextVersion, ContextVersion.item_id == ContextItem.id)
        .join(ServedContext, ServedContext.version_id == ContextVersion.id)
        .join(ToolCall, ToolCall.id == ServedContext.tool_call_id)
        .where(ToolCall.session_id == c.session.id)
        .distinct()
        .order_by(ContextItem.key, ContextVersion.seq)
        .limit(12)
    ).all()
    base = (get_settings().public_url or "").rstrip("/")
    out: list[dict[str, Any]] = []
    for key, typ, seq, body, meta in rows:
        out.append({
            "tag": f"[ctx:{key}@v{seq}]", "key": key, "version": seq, "type": typ,
            "type_label": _CTX_TYPE_LABEL.get(typ, typ.replace("_", " ")),
            "title": _ctx_title(typ, key, body or "", meta or {}),
            "url": f"{base}/library/item/{'/'.join(quote(p, safe='') for p in key.split('/'))}"
            if base else None,
        })
    return out


_CURRENCY_SYMBOLS = {"$", "€", "£", "¥"}


def _format_spec(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise unit/format metadata (``unit``, ``format``, ``currency``, ``decimals``) into
    what the widget renders. Nothing is inferred: no metadata, no format."""
    unit = str(raw.get("unit") or "").strip()
    fmt = str(raw.get("format") or "").strip().lower()
    cur = str(raw.get("currency") or "").strip()
    out: dict[str, Any] | None = None
    code = cur or (unit if re.fullmatch(r"[A-Z]{3}", unit) else "")
    if code:
        out = {"style": "currency", "currency": code.upper()}
    if out is None and unit in _CURRENCY_SYMBOLS:
        out = {"style": "prefix", "symbol": unit}
    if out is None and (fmt in ("percent", "percentage", "%") or unit.lower() in
                        ("%", "percent", "percentage")):
        out = {"style": "percent"}
    if out is None and unit:
        out = {"style": "unit", "unit": unit}
    dec = raw.get("decimals", raw.get("precision"))
    if isinstance(dec, int) and not isinstance(dec, bool) and 0 <= dec <= 6:
        out = {**(out or {"style": "number"}), "decimals": dec}
    elif fmt == "integer":
        out = {**(out or {"style": "number"}), "decimals": 0}
    return out


def _metric_formats(mdl: dict[str, Any], metrics: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in metrics:
        hits = _find_measure(mdl, name)
        if len(hits) != 1:
            continue
        m = hits[0][1]
        spec = _format_spec({**(m.get("properties") or {}), **m})
        if spec:
            out[m["name"]] = spec
    return out


def _asset_doc_formats(c: Call, tables: list[str], cols: list[str]) -> dict[str, dict[str, Any]]:
    """Column units from published asset docs of the tables a SQL query read (best effort:
    only for result columns that carry the source column's name)."""
    if not tables or not cols:
        return {}
    wanted = {t.lower().rsplit(".", 1)[-1] for t in tables}
    full = {t.lower() for t in tables}
    out: dict[str, dict[str, Any]] = {}
    for _item, ver in c.s.execute(_release_published(c).where(ContextItem.type == "asset_doc")):
        try:
            d = yaml_item_dict("asset_doc", ver.body, ver.meta)
            fqn = str(d.get("asset", "")).lower()
            if not fqn or (fqn not in full and fqn.rsplit(".", 1)[-1] not in wanted):
                continue
            raw_cols = d.get("columns") or []
            pairs = ([(k, v) for k, v in raw_cols.items()] if isinstance(raw_cols, dict)
                     else [(x.get("name"), x) for x in raw_cols])
            for name, col in pairs:
                if name in cols and name not in out and isinstance(col, dict) and col.get("unit"):
                    spec = _format_spec({"unit": col["unit"]})
                    if spec:
                        out[name] = spec
        except Exception:
            continue
    return out


def _pretty_sql(sql: str, dialect: str | None) -> str:
    """Pretty-print for the widget; the original text if sqlglot cannot parse it."""
    try:
        return sqlglot.transpile(sql, read=dialect, write=dialect, pretty=True)[0]
    except Exception:
        return sql.strip()


def _set_widget(
    c: Call,
    *,
    tool: str,
    result_id: uuid.UUID,
    tool_call_id: int | None,
    sql: str,
    connection: str | None,
    columns: list[dict[str, str]],
    cols: list[str],
    rows: list[tuple[Any, ...]],
    row_count: int,
    truncated: bool,
    offset: int = 0,
    metric: dict[str, Any] | None = None,
    formats: dict[str, dict[str, Any]] | None = None,
    dialect: str | None = None,
) -> None:
    """Attach the MCP Apps payload the ui://satchel/result widget renders (design §6.4)."""
    if c.ctx.ui_supported is False or c.session is None:
        return
    types_by_name = {x["name"]: x["type"] for x in columns}
    ctx_items = _ctx_items(c)
    c.structured = {
        "satchel": 1,
        "tool": tool,
        "session_id": str(c.session.id),
        "result_id": str(result_id),
        "tool_call_id": tool_call_id,
        "connection": connection,
        "question": c.session.question,
        "sql": _pretty_sql(sql, dialect),
        "metric": metric,
        "formats": formats or {},
        "columns": [
            {"name": n, "type": types_by_name.get(n, ""),
             "kind": column_kind(types_by_name.get(n, ""))}
            for n in cols
        ],
        "rows": [[json_cell(v) for v in r] for r in rows],
        "offset": offset,
        "row_count": row_count,
        "truncated": truncated,
        "ctx": [i["tag"] for i in ctx_items],
        "ctx_items": ctx_items,
    }


def _conn(c: Call, name: str | None) -> Connection:
    try:
        return pick_connection(c.s, name)
    except ConnectionError_ as e:
        raise ToolUserError(str(e)) from None


def _execute(
    c: Call, conn: Connection, sql: str, *, row_cap: int | None = None
) -> tuple[GatewayResult | None, str | None, str | None]:
    """-> (result, error message, kind) where kind is 'guardrail' or 'execution'."""
    lim = limits.for_workspace(c.s, c.ctx.principal.workspace_id, get_settings())
    try:
        connector = open_connector(conn)
    except ConnectionError_ as e:
        raise ToolUserError(str(e)) from None
    try:
        return (
            run_guarded(connector, sql, row_cap=row_cap or lim.sql_row_cap,
                        timeout_s=lim.sql_timeout_s),
            None,
            None,
        )
    except GuardrailError as e:
        return None, str(e), "guardrail"
    except SqlTimeout as e:
        return None, str(e), "execution"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", "execution"
    finally:
        connector.close()


# ----------------------------------------------------------------------------- handlers


def run_sql(c: Call) -> str:
    a: RunSqlArgs = c.args
    assert c.session is not None
    conn = _conn(c, a.connection)
    gr, err, kind = _execute(c, conn, a.sql)
    tcid = c.ensure_tool_call()
    run = SqlRun(a.sql, conn.kind, conn.id, err, kind)
    for sp in extract_sql_signals(
        c.s,
        session_id=c.session.id,
        run=run,
        current_tool_call_id=tcid,
        release_id=c.ctx.release_id,
    ):
        _signal(c, sp.kind, sp.payload, source="implicit", item_ids=sp.item_ids,
                asset_ids=sp.asset_ids)
    c.result_meta.update(connection=conn.name)
    if err is not None or gr is None:
        c.result_meta["error_kind"] = kind
        hint = (" Check describe_table for exact column names and types."
                if kind == "execution" else " Send a single read-only SELECT.")
        prefix = "SQL rejected" if kind == "guardrail" else "SQL error"
        raise ToolUserError(f"{prefix}: {err}.{hint}", persist=True)
    row = store_result(
        c.s, workspace_id=c.ctx.principal.workspace_id, session_id=c.session.id,
        tool_call_id=tcid, connection_id=conn.id, sql=a.sql, table=gr.table,
        truncated=gr.truncated,
    )
    c.result_meta.update(rows=row.row_count, truncated=gr.truncated, result_id=str(row.id),
                         tables=gr.tables)
    cols, rows = _rows(gr.table, PREVIEW_ROWS)
    _set_widget(c, tool="run_sql", result_id=row.id, tool_call_id=tcid, sql=a.sql,
                connection=conn.name, columns=row.columns, cols=cols, rows=rows,
                row_count=row.row_count, truncated=gr.truncated,
                formats=_asset_doc_formats(c, gr.tables, cols),
                dialect=sqlglot_dialect(conn.kind) or conn.kind)
    return _format(row.id, conn.name, a.sql, gr)


def _semantic(c: Call) -> tuple[dict[str, Any], list[Any]]:
    published = load_published(c.s, c.ctx.release_id)
    try:
        return merge_manifests(published), published
    except SemanticError as e:
        raise ToolUserError(f"{e} Use run_sql with the documented tables instead.") from None


def list_metrics_tool(c: Call) -> dict[str, Any]:
    try:
        mdl, published = _semantic(c)
    except ToolUserError:
        return {"metrics": [], "note": "No semantic model is published; use run_sql."}
    c.result_meta["metrics"] = sum(len(x["metrics"]) for x in list_metrics(mdl))
    shadowed = shadowed_models(published)
    out: dict[str, Any] = {
        "cubes": list_metrics(mdl),
        "next": "Call query_metrics(metrics=[...], dimensions=[...]) with these names.",
        "models": [p.key for p in published if p.key not in shadowed],
    }
    if shadowed:  # another source defines the same names with better precedence
        out["shadowed_models"] = shadowed
    return out


def _glossary(c: Call, name: str) -> dict[str, Any] | None:
    want = name.rsplit(".", 1)[-1].replace("_", " ").lower()
    rows = c.s.execute(_release_published(c).where(ContextItem.type == "glossary_term"))
    for item, ver in rows:
        d = yaml_item_dict("glossary_term", ver.body, ver.meta)
        if not d:
            continue
        names = {str(d.get("term", "")).lower(), *(str(x).lower() for x in d.get("synonyms", []))}
        if want in names:
            _log_served(c, [(ver.id, None)])
            return {"tag": f"[ctx:{item.key}@v{ver.seq}]", "definition": d.get("definition")}
    return None


def describe_metric_tool(c: Call) -> dict[str, Any]:
    a: DescribeMetricArgs = c.args
    mdl, _ = _semantic(c)
    try:
        out = describe_metric(mdl, a.name)
    except SemanticError as e:
        raise ToolUserError(str(e)) from None
    out["glossary"] = _glossary(c, a.name)
    return out


def query_metrics(c: Call) -> str:
    a: QueryMetricsArgs = c.args
    assert c.session is not None
    mdl, _ = _semantic(c)
    conn = _conn(c, a.connection)
    try:
        q = build_cube_query(
            mdl, a.metrics, a.dimensions, [f.model_dump() for f in a.filters],
            [o.model_dump() for o in a.order_by], a.limit,
        )
        compiled = compile_query(mdl, q, sqlglot_dialect(conn.kind) or conn.kind)
    except SemanticError as e:
        raise ToolUserError(f"{e} (call list_metrics / describe_metric for valid names)") from None
    gr, err, kind = _execute(c, conn, compiled.sql)
    if err is not None or gr is None:
        raise ToolUserError(f"Metric query failed on {conn.name}: {err}")
    tcid = c.ensure_tool_call()
    row = store_result(
        c.s, workspace_id=c.ctx.principal.workspace_id, session_id=c.session.id,
        tool_call_id=tcid, connection_id=conn.id, sql=compiled.sql,
        table=gr.table, truncated=gr.truncated,
    )
    c.result_meta.update(rows=row.row_count, truncated=gr.truncated, result_id=str(row.id),
                         metrics=a.metrics, cube=q["cube"], connection=conn.name)
    cols, rows = _rows(gr.table, PREVIEW_ROWS)
    _set_widget(c, tool="query_metrics", result_id=row.id, tool_call_id=tcid, sql=compiled.sql,
                connection=conn.name, columns=row.columns, cols=cols, rows=rows,
                row_count=row.row_count, truncated=gr.truncated,
                metric={"metrics": a.metrics, "dimensions": a.dimensions, "cube": q["cube"],
                        "filters": q.get("filters", []), "order_by": q.get("orderBy", []),
                        "limit": q.get("limit")},
                formats=_metric_formats(mdl, a.metrics),
                dialect=sqlglot_dialect(conn.kind) or conn.kind)
    extra = f"cube_query: {q}\nsemantic_sql: {compiled.semantic_sql}\n"
    return _format(row.id, conn.name, compiled.sql, gr, extra)


def get_result(c: Call) -> str:
    a: GetResultArgs = c.args
    try:
        rid = uuid.UUID(a.result_id)
    except ValueError:
        raise ToolUserError(f"Invalid result_id {a.result_id!r}.") from None
    row = c.s.scalars(
        select(QueryResult)
        .join(SessionRow, SessionRow.id == QueryResult.session_id)
        .where(QueryResult.id == rid, SessionRow.user_id == c.ctx.principal.user_id)
    ).first()
    if row is None:
        raise ToolUserError(
            f"Unknown result_id {a.result_id!r}. Use the result_id from run_sql / query_metrics."
        )
    if row.expires_at <= datetime.now(UTC):
        raise ToolUserError("That result has expired; run the query again.")
    try:
        cols, rows = read_page(row, a.offset, a.limit)
    except FileNotFoundError as e:
        raise ToolUserError(f"{e}. Run the query again.") from None
    nxt = a.offset + len(rows)
    c.result_meta.update(result_id=a.result_id, offset=a.offset, rows=len(rows))
    src = c.s.get(Connection, row.connection_id) if row.connection_id else None
    _set_widget(c, tool="get_result", result_id=row.id, tool_call_id=row.tool_call_id,
                sql=row.sql, connection=None, columns=row.columns, cols=cols, rows=rows,
                row_count=row.row_count, truncated=row.truncated, offset=a.offset,
                dialect=(sqlglot_dialect(src.kind) or src.kind) if src else None)
    tail = (f"\n(next page: get_result(result_id, offset={nxt}))" if nxt < row.row_count else "")
    return (f"result_id: {row.id}\nrows {a.offset + 1 if rows else 0}-{nxt} of {row.row_count}"
            f"{' (truncated at the row cap)' if row.truncated else ''}\n\n"
            f"{markdown_table(cols, rows)}{tail}")


def rate_result(c: Call) -> dict[str, Any]:
    """Human feedback from the result widget: a ``feedback`` row (source='human') on the tool
    call that produced the result, plus a ``user_doubt`` signal for a 👎 or "this is wrong"."""
    a: RateResultArgs = c.args
    assert c.session is not None
    tc = c.s.get(ToolCall, a.tool_call_id)
    if tc is None or tc.session_id != c.session.id:
        raise ToolUserError("Unknown tool_call_id for this session.")
    rating = 1 if a.sentiment == "up" else -1
    # "wrong" is the legacy spelling of a 👎 that says the number is wrong.
    reasons = list(dict.fromkeys(a.reasons)) if rating < 0 else []
    if a.sentiment == "wrong" and not reasons:
        reasons = ["wrong_number"]
    note = (a.comment or "").strip()
    # No column for reasons (no migration): they prefix the comment, "[wrong_definition] ...".
    prefix = "".join(f"[{r}]" for r in reasons)
    comment = (f"{prefix} {note}".strip() if prefix else note) or None
    client = (a.client or c.ctx.client_name or "")[:100] or None
    uid = c.ctx.principal.user_id
    fb = c.s.scalars(
        select(Feedback).where(
            Feedback.session_id == c.session.id, Feedback.tool_call_id == tc.id,
            Feedback.user_id == uid, Feedback.source == "human",
        )
    ).first()
    was_down = fb is not None and fb.rating < 0
    changed = fb is None or fb.rating != rating or fb.comment != comment
    if fb is None:
        fb = Feedback(session_id=c.session.id, tool_call_id=tc.id, user_id=uid, rating=rating,
                      comment=comment, source="human", client=client)
        c.s.add(fb)
    else:
        fb.rating, fb.comment, fb.client = rating, comment, client
    if rating < 0 and changed:
        payload: dict[str, Any] = {
            "sentiment": ("correction" if {"wrong_number", "wrong_definition"} & set(reasons)
                          else "negative"),
            "reasons": reasons, "comment": comment, "via": "widget", "tool_call_id": tc.id,
            "result_id": a.result_id, "tool": tc.tool, "client": client,
        }
        existing = None
        if was_down:  # one signal per 👎; a reasons/comment update edits it in place
            existing = next(
                (g for g in c.s.scalars(
                    select(Signal).where(
                        Signal.session_id == c.session.id, Signal.kind == "user_doubt",
                        Signal.source == "human", Signal.user_id == uid,
                    ).order_by(Signal.id.desc())
                ) if g.payload.get("tool_call_id") == tc.id), None)
        if existing is not None:
            existing.payload = payload
            existing.embedding = None  # re-embedded from the new text by the curation job
        else:
            _signal(c, "user_doubt", payload, source="human",
                    item_ids=_session_item_ids(c.s, c.session.id))
    sentiment = "down" if a.sentiment == "wrong" else a.sentiment
    c.result_meta.update(rated_tool_call_id=tc.id, sentiment=sentiment, reasons=reasons,
                         client=client)
    c.s.flush()
    return {"recorded": True, "sentiment": sentiment, "reasons": reasons}


def build_data_tools() -> list[ToolDef]:
    def tool(
        name: str, desc: str, model: type[BaseModel], fn: Handler, *, question: bool = False
    ) -> ToolDef:
        return ToolDef(name, desc, model, fn, requires_question=question,
                       requires_connection=True)

    return [
        tool("list_metrics", text.D_LIST_METRICS, ListMetricsArgs, list_metrics_tool),
        tool("describe_metric", text.D_DESCRIBE_METRIC, DescribeMetricArgs, describe_metric_tool,
             question=True),
        replace(tool("query_metrics", text.D_QUERY_METRICS, QueryMetricsArgs, query_metrics,
                     question=True), widget=True),
        replace(tool("run_sql", text.D_RUN_SQL, RunSqlArgs, run_sql, question=True),
                widget=True),
        replace(tool("get_result", text.D_GET_RESULT, GetResultArgs, get_result), widget=True),
        ToolDef("rate_result", text.D_RATE_RESULT, RateResultArgs, rate_result,
                requires_connection=True, app_only=True),
    ]


__all__ = ["OPERATORS", "build_data_tools"]
