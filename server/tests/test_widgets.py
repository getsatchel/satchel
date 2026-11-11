"""MCP Apps result widget (design §6.4): resource, tool _meta, structured payload, text
fallback, and human feedback through ``rate_result``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.client.session import ClientSession
from pydantic import AnyUrl
from sqlalchemy import select

from satchel.mcp import apps
from satchel.store.db import admin_session
from satchel.store.models import Feedback, Signal, ToolCall
from tests.test_data_plane import env, result_dir, start, warehouse  # noqa: F401  (fixtures)
from tests.test_mcp import Env, call, text_of, with_client

SQL = "select order_date, amount from analytics.orders order by order_date limit 80"
UI_CAPS = {"extensions": {apps.EXTENSION_ID: {"mimeTypes": [apps.MIME_TYPE]}}}


@pytest.fixture
def ui_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-memory test client cannot advertise extensions; pretend it negotiated the UI."""
    monkeypatch.setattr(apps, "ui_negotiation", lambda _caps: True)


def test_ui_negotiation() -> None:
    from mcp.types import ClientCapabilities

    assert apps.ui_negotiation(None) is None
    assert apps.ui_negotiation(ClientCapabilities()) is False
    assert apps.ui_negotiation(ClientCapabilities.model_validate(UI_CAPS)) is True
    other = {"extensions": {apps.EXTENSION_ID: {"mimeTypes": ["text/html"]}}}
    assert apps.ui_negotiation(ClientCapabilities.model_validate(other)) is False


def test_resource_is_served_with_the_mcp_app_mime_type(env: Env, ui_on: None) -> None:  # noqa: F811
    async def fn(c: ClientSession) -> tuple[Any, Any]:
        await c.initialize()
        listed = await c.list_resources()
        read = await c.read_resource(AnyUrl(apps.RESULT_URI))
        return listed, read

    listed, read = with_client(env, fn)
    res = next(r for r in listed.resources if str(r.uri) == apps.RESULT_URI)
    assert res.mimeType == "text/html;profile=mcp-app"
    (item,) = read.contents
    assert item.mimeType == "text/html;profile=mcp-app"
    html = item.text
    assert html.lstrip().startswith("<!doctype html>") and "rate_result" in html
    # one self-contained file: nothing to fetch, which the default CSP would block anyway
    assert "<link" not in html and "<script src" not in html


def test_tool_meta_and_visibility(env: Env, ui_on: None) -> None:  # noqa: F811
    async def fn(c: ClientSession) -> dict[str, Any]:
        await c.initialize()
        return {t.name: t.meta for t in (await c.list_tools()).tools}

    meta = with_client(env, fn)
    for name in ("run_sql", "query_metrics", "get_result"):
        assert meta[name]["ui"]["resourceUri"] == "ui://satchel/result"
    assert meta["rate_result"]["ui"]["visibility"] == ["app"]
    assert meta["list_metrics"] is None


def test_clients_without_the_extension_get_plain_tools(env: Env) -> None:  # noqa: F811
    """The default test client sends no extensions: no UI meta, no app-only tool, no payload,
    and the text answer is unchanged."""

    async def fn(c: ClientSession) -> tuple[Any, Any]:
        await c.initialize()
        tools = (await c.list_tools()).tools
        sid = await start(c, "orders")
        return tools, await call(c, "run_sql", session_id=sid, question="orders", sql=SQL)

    tools, res = with_client(env, fn)
    assert all(t.meta is None for t in tools) and "rate_result" not in {t.name for t in tools}
    assert res.structuredContent is None
    assert "result_id:" in text_of(res) and "| order_date | amount |" in text_of(res)


def test_structured_payload_and_complete_text(env: Env, ui_on: None) -> None:  # noqa: F811
    async def fn(c: ClientSession) -> tuple[Any, Any, Any]:
        await c.initialize()
        sid = await start(c, "orders")
        await call(c, "search_context", session_id=sid, question="orders", query="orders")
        res = await call(c, "run_sql", session_id=sid, question="orders", sql=SQL)
        rid = json.loads(json.dumps(res.structuredContent))["result_id"]
        page = await call(c, "get_result", session_id=sid, result_id=rid, offset=50, limit=20)
        return sid, res, page

    sid, res, page = with_client(env, fn)
    sc = res.structuredContent
    assert sc["satchel"] == 1 and sc["session_id"] == sid and sc["tool"] == "run_sql"
    assert sc["row_count"] == 80 and len(sc["rows"]) == 50 and sc["offset"] == 0
    assert [c["name"] for c in sc["columns"]] == ["order_date", "amount"]
    assert sc["columns"][0]["kind"] == "time"
    assert sc["sql"].lower().startswith("select\n  order_date")
    assert isinstance(sc["tool_call_id"], int) and sc["truncated"] is False
    assert any(t.startswith("[ctx:") for t in sc["ctx"])
    json.dumps(sc)  # JSON-safe
    t = text_of(res)  # the text stays complete for clients that ignore the widget
    assert "rows: 80" in t and f"result_id: {sc['result_id']}" in t and "50 of 80" in t
    # paging keeps the *original* tool call, so a rating lands on the query, not the paging call
    p = page.structuredContent
    assert p["offset"] == 50 and len(p["rows"]) == 20 and p["tool_call_id"] == sc["tool_call_id"]


def test_query_metrics_payload_has_filters_pretty_sql_and_chips(env: Env, ui_on: None) -> None:  # noqa: F811
    q = "revenue in 2025"

    async def fn(c: ClientSession) -> tuple[Any, Any]:
        await c.initialize()
        sid = await start(c, q)
        await call(c, "search_context", session_id=sid, question=q, query="revenue")
        res = await call(
            c, "query_metrics", session_id=sid, question=q, metrics=["revenue"],
            filters=[{"member": "order_date", "operator": "gte", "values": ["2025-01-01"]},
                     {"member": "order_date", "operator": "lt", "values": ["2026-01-01"]},
                     {"dimension": "status", "operator": "neq", "value": "returned"}])
        return res, text_of(res)

    res, t = with_client(env, fn)
    sc = res.structuredContent
    m = sc["metric"]
    assert m["metrics"] == ["revenue"] and m["cube"] == "sales"
    assert m["filters"] == [
        {"dimension": "order_date", "operator": "gte", "value": "2025-01-01"},
        {"dimension": "order_date", "operator": "lt", "value": "2026-01-01"},
        {"dimension": "status", "operator": "neq", "value": "returned"},
    ]
    assert "2025-01-01" in sc["sql"] and sc["sql"].count("\n") > 3, "SQL is pretty-printed"
    assert sc["formats"] == {}, "no unit metadata -> nothing invented"
    items = sc["ctx_items"]
    assert items and sc["ctx"] == [i["tag"] for i in items]
    for i in items:
        assert i["title"] and i["type_label"] and i["tag"].startswith("[ctx:")
        assert i["url"].startswith("http") and "/library/item/" in i["url"]
    assert "sql:" in t and "rows: 1" in t  # the model's text answer is unchanged


def test_query_metrics_payload_carries_metric_units(
    env: Env, ui_on: None, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    import copy

    from satchel.tools import data_tools

    real = data_tools._semantic

    def with_units(c: Any) -> Any:
        mdl, published = real(c)
        mdl = copy.deepcopy(mdl)
        for cube in mdl["cubes"]:
            for m in cube["measures"]:
                if m["name"] == "revenue":
                    m["properties"] = {"currency": "AUD"}
        return mdl, published

    monkeypatch.setattr(data_tools, "_semantic", with_units)

    async def fn(c: ClientSession) -> Any:
        await c.initialize()
        sid = await start(c, "revenue")
        return await call(c, "query_metrics", session_id=sid, question="revenue",
                          metrics=["revenue", "order_count"])

    sc = with_client(env, fn).structuredContent
    assert sc["formats"] == {"revenue": {"style": "currency", "currency": "AUD"}}


def test_format_spec_never_invents_a_currency() -> None:
    from satchel.tools.data_tools import _format_spec

    assert _format_spec({}) is None
    assert _format_spec({"unit": "AUD"}) == {"style": "currency", "currency": "AUD"}
    assert _format_spec({"format": "currency", "currency": "usd"}) == {
        "style": "currency", "currency": "USD"}
    assert _format_spec({"format": "currency"}) is None  # currency format, but which one?
    assert _format_spec({"unit": "AUD cents"}) == {"style": "unit", "unit": "AUD cents"}
    assert _format_spec({"unit": "$"}) == {"style": "prefix", "symbol": "$"}
    assert _format_spec({"unit": "percent", "decimals": 1}) == {"style": "percent", "decimals": 1}


def test_run_sql_units_come_from_asset_docs(env: Env, ui_on: None) -> None:  # noqa: F811
    async def fn(c: ClientSession) -> Any:
        await c.initialize()
        sid = await start(c, "orders")
        return await call(c, "run_sql", session_id=sid, question="orders",
                          sql="select amount from analytics.orders limit 3")

    sc = with_client(env, fn).structuredContent
    assert sc["formats"].get("amount") == {"style": "unit", "unit": "AUD cents"}


def test_rate_result_records_human_feedback_and_signal(env: Env, ui_on: None) -> None:  # noqa: F811
    async def fn(c: ClientSession) -> tuple[str, Any, Any, Any]:
        await c.initialize()
        sid = await start(c, "orders")
        res = await call(c, "run_sql", session_id=sid, question="orders", sql=SQL)
        sc = res.structuredContent
        base = {"session_id": sid, "tool_call_id": sc["tool_call_id"], "result_id": sc["result_id"]}
        up = await call(c, "rate_result", sentiment="up", **base)
        wrong = await c.call_tool("rate_result", {
            **base, "sentiment": "wrong", "comment": "excludes refunds",
            "client": "claude-desktop"})
        bad = await call(c, "rate_result", sentiment="down", **{**base, "tool_call_id": 999999})
        return sid, sc, wrong, (up, bad)

    sid, sc, wrong, (up, bad) = with_client(env, fn)
    assert not up.isError and not wrong.isError
    assert bad.isError and "Unknown tool_call_id" in text_of(bad)
    with admin_session(engine=env.engine) as s:
        (fb,) = s.scalars(select(Feedback).where(Feedback.session_id == sid)).all()
        # the second click on the same result updates the rating instead of duplicating it
        assert (fb.source, fb.rating, fb.tool_call_id) == ("human", -1, sc["tool_call_id"])
        assert fb.comment == "[wrong_number] excludes refunds" and fb.client == "claude-desktop"
        (sig,) = s.scalars(select(Signal).where(Signal.session_id == sid)
                           .where(Signal.kind == "user_doubt")).all()
        assert sig.source == "human" and sig.payload["via"] == "widget"
        assert sig.payload["tool_call_id"] == sc["tool_call_id"]
        assert sig.payload["sentiment"] == "correction"
        tc = s.get(ToolCall, sc["tool_call_id"])
        assert tc is not None and tc.tool == "run_sql"


def test_rate_result_reasons_update_in_place_and_legacy_wrong(env: Env, ui_on: None) -> None:  # noqa: F811
    async def fn(c: ClientSession) -> tuple[str, list[Any], Any]:
        await c.initialize()
        sid = await start(c, "orders")
        scs = []
        for _ in range(3):
            res = await call(c, "run_sql", session_id=sid, question="orders", sql=SQL)
            scs.append(res.structuredContent)

        def base(i: int) -> dict[str, Any]:
            return {"session_id": sid, "tool_call_id": scs[i]["tool_call_id"],
                    "result_id": scs[i]["result_id"]}

        await call(c, "rate_result", sentiment="down", **base(0))
        r2 = await call(c, "rate_result", sentiment="down", reasons=["wrong_definition"],
                        comment="net of refunds", **base(0))
        await call(c, "rate_result", sentiment="wrong", **base(1))  # legacy spelling
        await call(c, "rate_result", sentiment="down", reasons=["not_what_i_asked"], **base(2))
        return sid, scs, r2

    sid, scs, r2 = with_client(env, fn)
    assert not r2.isError
    t0, t1, t2 = (sc["tool_call_id"] for sc in scs)
    with admin_session(engine=env.engine) as s:
        fbs = {f.tool_call_id: f
               for f in s.scalars(select(Feedback).where(Feedback.session_id == sid))}
        assert fbs[t0].comment == "[wrong_definition] net of refunds"
        assert fbs[t1].comment == "[wrong_number]" and fbs[t1].rating == -1
        sigs = s.scalars(select(Signal).where(Signal.session_id == sid,
                                              Signal.kind == "user_doubt")).all()
        assert len(sigs) == 3  # the reasons update edited the first signal, no duplicate
        by_call = {g.payload["tool_call_id"]: g.payload for g in sigs}
        got = {t: (p["reasons"], p["sentiment"]) for t, p in by_call.items()}
        assert got == {
            t0: (["wrong_definition"], "correction"),
            t1: (["wrong_number"], "correction"),
            t2: (["not_what_i_asked"], "negative"),
        }


def test_agent_feedback_stays_agent_sourced(env: Env) -> None:  # noqa: F811
    async def fn(c: ClientSession) -> str:
        await c.initialize()
        sid = await start(c, "q")
        await call(c, "record_user_feedback", session_id=sid, sentiment="negative", comment="no")
        return sid

    sid = with_client(env, fn)
    with admin_session(engine=env.engine) as s:
        (fb,) = s.scalars(select(Feedback).where(Feedback.session_id == sid)).all()
        sig = s.scalars(select(Signal).where(Signal.session_id == sid)).one()
        assert fb.source == "agent" and sig.source == "agent"


def test_built_widget_matches_its_source() -> None:
    """Cheap local guard; CI runs the authoritative `npm run check` in ui-widgets/."""
    html = apps.result_widget_html()
    assert "/*__CSS__*/" not in html and "/*__JS__*/" not in html
    root = Path(__file__).resolve().parents[2] / "ui-widgets" / "src"
    assert (root / "style.css").read_text() in html, "run `npm run build` in ui-widgets/"
    template = (root / "index.html").read_text()
    assert template.split("/*__JS__*/")[1] in html  # the template tail is embedded verbatim
