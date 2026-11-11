"""MCP server v1: registry tools, logging, corrective errors, PAT auth, RLS, HTTP e2e."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import mcp.types as types
import pytest
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import Engine, func, select, update

from satchel.api.app import create_app
from satchel.llm.embeddings import HashEmbedder
from satchel.mcp.auth import Principal, create_token, revoke_token, verify_token
from satchel.mcp.server import build_server
from satchel.store.db import admin_session
from satchel.store.models import (
    ApiToken,
    ContextItem,
    Feedback,
    ServedContext,
    Signal,
    ToolCall,
)
from satchel.store.models import Session as SessionRow
from satchel.sync import import_context
from tests.helpers import copy_example, unique_slug

EMB = HashEmbedder()


@dataclass
class Env:
    engine: Engine
    slug: str
    workspace_id: uuid.UUID
    email: str
    principal: Principal


@pytest.fixture(scope="module")
def env(app_engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Env]:
    slug = unique_slug("mcp")
    r = import_context(
        copy_example(tmp_path_factory.mktemp("mcp")), slug, embedder=EMB, engine=app_engine
    )
    email = f"{slug}@example.com"
    raw, _ = create_token(slug, email, "t", engine=app_engine)
    p = verify_token(raw, engine=app_engine)
    assert p is not None
    yield Env(app_engine, slug, r.workspace_id, email, p)


def with_client(
    env: Env, fn: Callable[[ClientSession], Awaitable[Any]], principal: Principal | None = None
) -> Any:
    server = build_server(env.engine, EMB, lambda _rc: principal or env.principal)

    async def go() -> Any:
        async with create_connected_server_and_client_session(
            server, client_info=types.Implementation(name="test-client", version="9.9")
        ) as client:
            return await fn(client)

    return anyio.run(go)


def call(client: ClientSession, name: str, **args: Any) -> Any:
    return client.call_tool(name, args)


def text_of(res: types.CallToolResult) -> str:
    c = res.content[0]
    assert isinstance(c, types.TextContent)
    return c.text


def test_registry_lists_all_tools_prompts_resources(env: Env) -> None:
    async def fn(c: ClientSession) -> tuple[Any, Any, Any, Any]:
        init = await c.initialize()
        return (
            init,
            await c.list_tools(),
            await c.list_prompts(),
            await c.list_resources(),
        )

    init, tools, prompts, resources = with_client(env, fn)
    assert "start_analysis" in (init.instructions or "")
    assert "record_user_feedback" in init.instructions
    names = {t.name for t in tools.tools}
    assert names == {
        "start_analysis", "search_context", "get_context_pack", "get_item", "list_tables",
        "describe_table", "report_answer", "flag_context_gap", "record_user_feedback",
        "propose_context_change",
        # the example imports a connection, so the data plane is offered
        "list_metrics", "describe_metric", "query_metrics", "run_sql", "get_result",
    }
    by = {t.name: t for t in tools.tools}
    assert set(by["search_context"].inputSchema["required"]) >= {"session_id", "question", "query"}
    assert "BEFORE" in (by["search_context"].description or "")
    assert {p.name for p in prompts.prompts} >= {"satchel_workflow"}
    assert [str(r.uri) for r in resources.resources] == ["satchel://workspace-rules"]


def test_full_flow_logs_rows(env: Env) -> None:
    async def fn(c: ClientSession) -> str:
        r = await call(c, "start_analysis", question="What was revenue last month?")
        assert not r.isError
        sid = json.loads(text_of(r))["session_id"]
        q = "What was revenue last month?"
        s = await call(c, "search_context", session_id=sid, question=q, query="revenue")
        assert not s.isError and "[ctx:" in text_of(s)
        p = await call(c, "get_context_pack", session_id=sid, question=q)
        assert not p.isError and "## Rules" in text_of(p)
        g = await call(c, "flag_context_gap", session_id=sid, what_was_missing="churn",
                       why="no definition")
        assert not g.isError
        f = await call(c, "record_user_feedback", session_id=sid, sentiment="doubt",
                       comment="looks wrong")
        assert not f.isError
        rep = await call(c, "report_answer", session_id=sid, answer_summary="x",
                         metrics_used=["revenue"], confidence="high",
                         caveats="No official definition of churn; derived one.")
        assert not rep.isError
        return str(sid)

    sid = uuid.UUID(with_client(env, fn))
    with admin_session(engine=env.engine) as s:
        sess = s.get(SessionRow, sid)
        assert sess is not None
        assert sess.client == "test-client" and sess.client_version == "9.9"
        assert sess.release_id is not None and sess.status == "answered"
        tools = list(s.scalars(select(ToolCall.tool).where(ToolCall.session_id == sid)
                               .order_by(ToolCall.id)))
        assert tools == ["start_analysis", "search_context", "get_context_pack",
                         "flag_context_gap", "record_user_feedback", "report_answer"]
        assert all(
            lat is not None
            for lat in s.scalars(select(ToolCall.latency_ms).where(ToolCall.session_id == sid))
        )
        served = s.scalar(
            select(func.count()).select_from(ServedContext)
            .join(ToolCall, ToolCall.id == ServedContext.tool_call_id)
            .where(ToolCall.session_id == sid, ToolCall.tool.in_(
                ["search_context", "get_context_pack"]))
        )
        assert served and served > 2
        kinds = sorted(s.scalars(select(Signal.kind).where(Signal.session_id == sid)))
        # explicit gap + user_doubt + low_confidence; the mined gap is suppressed
        assert kinds == ["gap_flagged", "low_confidence", "user_doubt"]
        assert all(
            src == "agent"
            for src in s.scalars(select(Signal.source).where(Signal.session_id == sid))
        )
        fb = s.scalars(select(Feedback).where(Feedback.session_id == sid)).one()
        assert fb.rating == -1 and fb.tool_call_id is not None


def test_report_answer_mines_gap_from_caveats(env: Env) -> None:
    async def fn(c: ClientSession) -> str:
        sid = json.loads(text_of(await call(c, "start_analysis", question="churn?")))["session_id"]
        await call(c, "report_answer", session_id=sid, answer_summary="x", confidence="medium",
                   caveats=["There is no definition of churn in context"])
        return str(sid)

    sid = uuid.UUID(with_client(env, fn))
    with admin_session(engine=env.engine) as s:
        rows = list(s.scalars(select(Signal).where(Signal.session_id == sid)))
        assert {r.kind for r in rows} == {"low_confidence", "gap_flagged"}
        gap = next(r for r in rows if r.kind == "gap_flagged")
        assert gap.payload["mined_from"] == "report_answer.caveats"


def test_required_args_give_corrective_errors(env: Env) -> None:
    async def fn(c: ClientSession) -> list[types.CallToolResult]:
        sid = json.loads(text_of(await call(c, "start_analysis", question="q")))["session_id"]
        return [
            await call(c, "search_context", question="q", query="revenue"),
            await call(c, "search_context", analysis_id=sid, question="q", query="revenue"),
            await call(c, "search_context", session_id="not-a-session", question="q",
                       query="revenue"),
            await call(c, "search_context", session_id=sid, query="revenue"),
            await call(c, "describe_table", session_id=sid, table="analytics.orders"),
            await call(c, "describe_table", session_id=sid, question="q", table="nope"),
            await call(c, "report_answer", session_id=sid, answer_summary="x",
                       confidence="certain"),
        ]

    no_sid, alias_sid, bad_sid, no_q, desc_no_q, bad_table, bad_conf = with_client(env, fn)
    # A dropped session_id falls back to the caller's latest session (Claude Desktop agent mode).
    assert not no_sid.isError, text_of(no_sid)
    assert not alias_sid.isError, text_of(alias_sid)
    assert bad_sid.isError and "start_analysis" in text_of(bad_sid)
    assert no_q.isError and "user's original question" in text_of(no_q)
    assert desc_no_q.isError and "question" in text_of(desc_no_q)
    assert bad_table.isError and "list_tables" in text_of(bad_table)
    assert bad_conf.isError and "confidence" in text_of(bad_conf)
    with admin_session(engine=env.engine) as s:
        errs = list(s.scalars(select(ToolCall.error).where(ToolCall.error.is_not(None))))
        assert any("original question" in e for e in errs)


def test_tables_and_items(env: Env) -> None:
    async def fn(c: ClientSession) -> tuple[Any, Any, Any]:
        sid = json.loads(text_of(await call(c, "start_analysis", question="orders")))["session_id"]
        lst = await call(c, "list_tables", session_id=sid)
        desc = await call(c, "describe_table", session_id=sid, question="orders",
                          table="orders")
        item = await call(c, "get_item", session_id=sid,
                          key="glossary/business-terms.yaml#revenue")
        return lst, desc, item

    lst, desc, item = with_client(env, fn)
    tables = json.loads(text_of(lst))["tables"]
    assert "analytics.orders" in {t["table"] for t in tables}
    d = json.loads(text_of(desc))
    assert d["table"] == "analytics.orders" and d["doc"]["endorsed"] is True
    assert d["caveats"] and d["caveats"][0]["tag"].startswith("[ctx:caveats/")
    assert "AUD cents" in json.loads(text_of(item))["body"]


def test_propose_context_change_never_publishes(env: Env) -> None:
    body = "A churned customer has no completed order in the last 180 days."
    meta = {"name": "Churn", "description": "Churn definition"}

    async def fn(c: ClientSession) -> str:
        sid = json.loads(text_of(await call(c, "start_analysis", question="churn")))["session_id"]
        bad = await call(c, "propose_context_change", session_id=sid, type="guide", body=body,
                         rationale="r")
        assert bad.isError and "Invalid proposal" in text_of(bad)
        ok = await call(c, "propose_context_change", session_id=sid, type="guide", body=body,
                        meta=meta, rationale="user defined churn")
        assert not ok.isError
        return str(json.loads(text_of(ok))["proposal_key"])

    key = with_client(env, fn)
    with admin_session(engine=env.engine) as s:
        it = s.scalars(select(ContextItem).where(ContextItem.key == key)).one()
        assert it.state == "proposed" and it.published_version_id is None
        assert it.current_version_id is not None

    explorer = Principal(env.workspace_id, env.principal.user_id, "explorer")

    async def fn2(c: ClientSession) -> types.CallToolResult:
        sid = json.loads(text_of(await call(c, "start_analysis", question="x")))["session_id"]
        return await call(c, "propose_context_change", session_id=sid, type="guide", body=body,
                          meta=meta, rationale="r")

    res = with_client(env, fn2, principal=explorer)
    assert res.isError and "analyst" in text_of(res)


def test_proposal_not_served_by_search(env: Env) -> None:
    async def fn(c: ClientSession) -> str:
        sid = json.loads(text_of(await call(c, "start_analysis", question="q")))["session_id"]
        return text_of(await call(c, "search_context", session_id=sid, question="q",
                                  query="churned customer 180 days"))

    assert "proposals/" not in with_client(env, fn)


# ------------------------------------------------------------------------ tokens / http


def test_parse_user_agent() -> None:
    from satchel.mcp.server import parse_user_agent

    assert parse_user_agent("claude-code/2.1.311 (cli)") == ("claude-code", "2.1.311")
    assert parse_user_agent("curl") == ("curl", None)
    assert parse_user_agent(None) == (None, None)


def test_token_lifecycle(env: Env) -> None:
    raw, tid = create_token(env.slug, env.email, "lifecycle", engine=env.engine)
    assert raw.startswith("satchel_pat_")
    p = verify_token(raw, engine=env.engine)
    assert p is not None and p.workspace_id == env.workspace_id and p.role == "analyst"
    with admin_session(engine=env.engine) as s:
        tok = s.get(ApiToken, tid)
        assert tok is not None and tok.hash != raw and tok.last_used_at is not None
    assert verify_token("satchel_pat_nope", engine=env.engine) is None
    assert verify_token("garbage", engine=env.engine) is None
    # expired
    with admin_session(engine=env.engine) as s:
        s.execute(update(ApiToken).where(ApiToken.id == tid)
                  .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert verify_token(raw, engine=env.engine) is None
    # revoked
    raw2, _ = create_token(env.slug, env.email, "to-revoke", engine=env.engine)
    assert verify_token(raw2, engine=env.engine) is not None
    assert revoke_token(workspace_slug=env.slug, name="to-revoke", engine=env.engine) == 1
    assert verify_token(raw2, engine=env.engine) is None


def _sse_json(r: Any) -> dict[str, Any]:
    if r.headers["content-type"].startswith("application/json"):
        return dict(r.json())
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return dict(json.loads(line[5:]))
    raise AssertionError(r.text)


def _rpc(c: TestClient, token: str, method: str, params: dict[str, Any], *, id_: int | None,
         sid: str | None = None, ua: str = "httpx-test/1") -> Any:
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
         "User-Agent": ua}
    if sid:
        h["mcp-session-id"] = sid
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
    if id_ is not None:
        body["id"] = id_
    return c.post("/mcp", json=body, headers=h)


def test_http_end_to_end_auth_and_rls(env: Env, app_engine: Engine,
                                      tmp_path_factory: pytest.TempPathFactory) -> None:
    # A second workspace with its own token.
    slug_b = unique_slug("mcpb")
    import_context(copy_example(tmp_path_factory.mktemp("mcpb")), slug_b, embedder=EMB,
                   engine=app_engine)
    raw_a, _ = create_token(env.slug, env.email, "http-a", engine=app_engine)
    raw_b, _ = create_token(slug_b, f"{slug_b}@example.com", "http-b", engine=app_engine)

    with TestClient(create_app(app_engine, EMB)) as c:
        assert c.post("/mcp", json={}).status_code == 401
        assert c.post("/mcp", json={}, headers={"Authorization": "Bearer satchel_pat_bad"}
                      ).status_code == 401
        init = _rpc(c, raw_a, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "httpx-test", "version": "1"}}, id_=1)
        assert init.status_code == 200, init.text
        # Stateless: no mcp-session-id is issued and none is needed on later requests.
        assert "mcp-session-id" not in init.headers
        sid_hdr = None
        res = _rpc(c, raw_a, "tools/call", {
            "name": "start_analysis", "arguments": {"question": "revenue?"}}, id_=2, sid=sid_hdr)
        out = _sse_json(res)["result"]
        assert not out.get("isError")
        sess_id = uuid.UUID(json.loads(out["content"][0]["text"])["session_id"])

        # Workspace B's token cannot use A's session (RLS + owner check).
        init_b = _rpc(c, raw_b, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "b", "version": "1"}}, id_=1, ua="b-client/2")
        assert init_b.status_code == 200
        sid_b = None
        res_b = _rpc(c, raw_b, "tools/call", {
            "name": "search_context", "arguments": {
                "session_id": str(sess_id), "question": "q", "query": "revenue"}},
            id_=2, sid=sid_b, ua="b-client/2")
        out_b = _sse_json(res_b)["result"]
        assert out_b["isError"] and "Unknown session_id" in out_b["content"][0]["text"]
        # ...and its own start_analysis lands in B.
        res_b2 = _rpc(c, raw_b, "tools/call", {
            "name": "start_analysis", "arguments": {"question": "b?"}}, id_=3, sid=sid_b,
            ua="b-client/2")
        b_sess = uuid.UUID(json.loads(_sse_json(res_b2)["result"]["content"][0]["text"])
                           ["session_id"])

        # Revoked tokens stop working immediately.
        assert revoke_token(workspace_slug=env.slug, name="http-a", engine=app_engine) == 1
        assert _rpc(c, raw_a, "tools/list", {}, id_=9, sid=sid_hdr).status_code == 401

    with admin_session(engine=app_engine) as s:
        a = s.get(SessionRow, sess_id)
        b = s.get(SessionRow, b_sess)
        assert a is not None and b is not None
        assert a.workspace_id == env.workspace_id and b.workspace_id != env.workspace_id
        assert a.client == "httpx-test" and a.client_version == "1" and a.mcp_session_id is None
        assert b.client == "b-client" and b.client_version == "2"
