"""Data plane end to end against Postgres: crawl, result cache, RLS, MCP tools, signals."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import duckdb
import pytest
from mcp.client.session import ClientSession
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError

from satchel.data.connections import add_connection
from satchel.data.crawl import crawl_connection
from satchel.data.results import read_page, store_result
from satchel.llm.embeddings import HashEmbedder
from satchel.mcp.auth import Principal, create_token, verify_token
from satchel.store.db import admin_session, workspace_session
from satchel.store.models import (
    Asset,
    ContextItem,
    QueryResult,
    Signal,
    ToolCall,
    Workspace,
)
from satchel.sync import import_context
from satchel.sync.importer import get_or_create_workspace
from tests.helpers import build_jaffle_db, copy_example, unique_slug
from tests.test_mcp import Env, call, text_of, with_client

EMB = HashEmbedder()
VALID = "status not in ('returned','return_pending') and customer_id <> 0"


@pytest.fixture(scope="module", autouse=True)
def result_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    d = tmp_path_factory.mktemp("results")
    mp = pytest.MonkeyPatch()
    mp.setenv("SATCHEL_RESULT_DIR", str(d))
    yield d
    mp.undo()


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_jaffle_db(tmp_path_factory.mktemp("wh") / "jaffle.duckdb")


@pytest.fixture(scope="module")
def env(app_engine: Engine, warehouse: Path, tmp_path_factory: pytest.TempPathFactory) -> Env:
    slug = unique_slug("dp")
    r = import_context(copy_example(tmp_path_factory.mktemp("dp")), slug, embedder=EMB,
                       engine=app_engine)
    add_connection(slug, "warehouse", "duckdb", {"path": str(warehouse)}, engine=app_engine)
    crawl_connection(slug, "warehouse", engine=app_engine)
    email = f"{slug}@example.com"
    raw, _ = create_token(slug, email, "t", engine=app_engine)
    p = verify_token(raw, engine=app_engine)
    assert p is not None
    return Env(app_engine, slug, r.workspace_id, email, p)


@pytest.fixture(scope="module")
def bare_env(app_engine: Engine, warehouse: Path) -> Env:
    """A workspace with a connection and a crawl but no imported context."""
    slug = unique_slug("bare")
    with admin_session(engine=app_engine) as s:
        wid = get_or_create_workspace(s, slug).id
    add_connection(slug, "warehouse", "duckdb", {"path": str(warehouse)}, engine=app_engine)
    crawl_connection(slug, "warehouse", engine=app_engine)
    email = f"{slug}@example.com"
    raw, _ = create_token(slug, email, "t", engine=app_engine)
    p = verify_token(raw, engine=app_engine)
    assert p is not None
    return Env(app_engine, slug, wid, email, p)


def start(c: ClientSession, q: str) -> Any:
    async def go() -> str:
        r = await call(c, "start_analysis", question=q)
        return str(json.loads(text_of(r))["session_id"])

    return go()


def result_rows(env: Env, result_id: str) -> list[tuple[Any, ...]]:
    with admin_session(engine=env.engine) as s:
        row = s.get(QueryResult, uuid.UUID(result_id))
        assert row is not None
        return read_page(row, 0, 100000)[1]


def rid_of(out: str) -> str:
    m = re.search(r"result_id: ([0-9a-f-]{36})", out)
    assert m, out
    return m.group(1)


def hand(sql: str, warehouse: Path) -> list[tuple[Any, ...]]:
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


# ------------------------------------------------------------------ crawl


def test_crawl_upserts_matches_imported_assets_and_imports_nothing_when_docs_exist(
    env: Env,
) -> None:
    with admin_session(engine=env.engine) as s:
        tables = list(s.scalars(select(Asset).where(
            Asset.workspace_id == env.workspace_id, Asset.kind == "table")))
        assert sorted(t.fqn for t in tables) == [
            "analytics.customers", "analytics.orders", "analytics.payments"]  # no duplicates
        amount = s.scalars(select(Asset).where(
            Asset.workspace_id == env.workspace_id, Asset.fqn == "analytics.orders.amount"
        )).one()
        assert amount.data_type == "INTEGER" and amount.parent_id is not None
        kinds = {a.kind for a in s.scalars(select(Asset).where(
            Asset.workspace_id == env.workspace_id))}
        assert {"database", "schema", "table", "column"} <= kinds
        drafts = list(s.scalars(select(ContextItem).where(
            ContextItem.workspace_id == env.workspace_id, ContextItem.key.like("crawl/%"))))
        assert drafts == []  # published asset_docs already exist


def test_crawl_dropped_assets_and_draft_docs(
    app_engine: Engine, warehouse: Path, tmp_path: Path
) -> None:
    db = tmp_path / "w.duckdb"
    db.write_bytes(warehouse.read_bytes())
    slug = unique_slug("crawl")
    with admin_session(engine=app_engine) as s:
        get_or_create_workspace(s, slug)
    add_connection(slug, "wh", "duckdb", {"path": str(db)}, engine=app_engine)
    r1 = crawl_connection(slug, "wh", engine=app_engine)
    assert r1.created > 20 and r1.dropped == 0 and r1.draft_docs == 1  # only orders has comments

    with admin_session(engine=app_engine) as s:
        wid = s.scalars(select(Workspace).where(Workspace.slug == slug)).one().id
        item = s.scalars(select(ContextItem).where(
            ContextItem.workspace_id == wid, ContextItem.type == "asset_doc")).one()
        assert item.state == "draft" and item.published_version_id is None
        assert item.current_version_id is not None
    r1b = crawl_connection(slug, "wh", engine=app_engine)  # idempotent, no second draft
    assert r1b.created == 0 and r1b.draft_docs == 0 and r1b.dropped == 0

    con = duckdb.connect(str(db))
    con.execute("DROP TABLE analytics.payments")
    con.close()
    r2 = crawl_connection(slug, "wh", engine=app_engine)
    assert r2.dropped == 7  # table + 6 columns
    with admin_session(engine=app_engine) as s:
        dropped = {a.fqn for a in s.scalars(select(Asset).where(
            Asset.workspace_id == wid, Asset.dropped_at.is_not(None)))}
        assert "analytics.payments" in dropped and "analytics.orders" not in dropped
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE analytics.payments (payment_id INTEGER, order_id INTEGER, "
                "payment_method VARCHAR, status VARCHAR, amount INTEGER, created_at TIMESTAMP)")
    con.close()
    assert crawl_connection(slug, "wh", engine=app_engine).revived == 7


def test_connection_secret_never_stored(app_engine: Engine) -> None:
    slug = unique_slug("sec")
    with admin_session(engine=app_engine) as s:
        get_or_create_workspace(s, slug)
    with pytest.raises(ValueError, match="env:NAME"):
        add_connection(slug, "pg", "postgres", {"host": "h"}, secret_ref="hunter2",
                       engine=app_engine)
    c = add_connection(slug, "pg", "postgres", {"host": "h"}, secret_ref="env:SATCHEL_CONN_PG",
                       engine=app_engine)
    assert c.secret_ref == "env:SATCHEL_CONN_PG" and c.default_for_ai


# ------------------------------------------------------------------ result cache + RLS


def test_result_cache_paging_and_rls(
    app_engine: Engine, two_workspaces: dict[str, uuid.UUID], result_dir: Path
) -> None:
    import pyarrow as pa

    ids = two_workspaces
    table = pa.table({"n": list(range(250)), "s": [f"r{i}" for i in range(250)]})
    with workspace_session(ids["wa"], ids["u1"], engine=app_engine) as s:
        row = store_result(s, workspace_id=ids["wa"], session_id=None, tool_call_id=None,
                           connection_id=None, sql="select 1", table=table, truncated=False)
        rid, expires = row.id, row.expires_at
        assert row.path.startswith("file://")
        assert Path(urlparse(row.path).path).is_relative_to(result_dir.resolve())
        assert Path(urlparse(row.path).path).exists() and row.row_count == 250
        assert row.columns == [{"name": "n", "type": "int64"}, {"name": "s", "type": "string"}]
        cols, rows = read_page(row, 100, 50)
        assert cols == ["n", "s"] and rows[0] == (100, "r100") and len(rows) == 50
        assert read_page(row, 240, 50)[1][-1] == (249, "r249")
        assert read_page(row, 300, 50)[1] == []
        assert row.expires_at > row.created_at
    with workspace_session(ids["wb"], ids["ub"], engine=app_engine) as s:
        assert s.get(QueryResult, rid) is None
        assert list(s.scalars(select(QueryResult))) == []
    with pytest.raises(DBAPIError), workspace_session(
        ids["wb"], ids["ub"], engine=app_engine
    ) as s:
        s.add(QueryResult(workspace_id=ids["wa"], sql="x", row_count=0, path="p",
                          expires_at=expires))
        s.flush()
    with app_engine.begin() as c:  # no workspace context: nothing visible
        assert c.execute(text("select count(*) from query_result")).scalar() == 0


# ------------------------------------------------------------------ tools


def test_tools_hidden_without_connection(app_engine: Engine) -> None:
    slug = unique_slug("noconn")
    with admin_session(engine=app_engine) as s:
        get_or_create_workspace(s, slug)
    raw, _ = create_token(slug, f"{slug}@example.com", "t", engine=app_engine)
    p = verify_token(raw, engine=app_engine)
    assert p is not None
    e = Env(app_engine, slug, p.workspace_id, "x", p)

    async def fn(c: ClientSession) -> tuple[set[str], Any]:
        await c.initialize()
        names = {t.name for t in (await c.list_tools()).tools}
        sid = await start(c, "q")
        res = await call(c, "run_sql", session_id=sid, question="q", sql="select 1")
        return names, res

    names, res = with_client(e, fn)
    assert names == {"start_analysis", "search_context", "get_context_pack", "get_item",
                     "list_tables", "describe_table", "report_answer", "flag_context_gap",
                     "record_user_feedback", "propose_context_change"}
    assert res.isError and "no data connection" in text_of(res)


def test_query_metrics_accepts_cube_style_filters(env: Env, warehouse: Path) -> None:
    """The exact filter shapes Claude Desktop sent ({member|dimension, values: [...]})."""
    q = "provide the revenue in march 2025"

    async def fn(c: ClientSession) -> list[Any]:
        await c.initialize()
        sid = await start(c, q)
        shapes = [
            [{"member": "order_date", "operator": "gte", "values": ["2025-03-01"]},
             {"member": "order_date", "operator": "lt", "values": ["2025-04-01"]}],
            [{"dimension": "order_date", "operator": "gte", "values": ["2025-03-01"]},
             {"dimension": "order_date", "operator": "lt", "values": ["2025-04-01"]}],
            [{"member": "order_date", "operator": "inDateRange",
              "values": ["2025-03-01", "2025-03-31"]}],
        ]
        return [await call(c, "query_metrics", session_id=sid, question=q,
                           metrics=["revenue"], filters=f) for f in shapes]

    results = with_client(env, fn)
    want = hand(f"select sum(amount)/100.0 from analytics.orders where {VALID} "
                "and order_date >= '2025-03-01' and order_date < '2025-04-01'", warehouse)
    for r in results:
        assert not r.isError, text_of(r)
        assert result_rows(env, rid_of(text_of(r)))[0][0] == pytest.approx(want[0][0])


def test_query_metrics_matches_hand_sql(env: Env, warehouse: Path) -> None:
    q1 = "What was revenue in March 2025?"
    q2 = "Top 5 customers by lifetime revenue"

    async def fn(c: ClientSession) -> tuple[str, str, str, Any]:
        await c.initialize()
        sid = await start(c, q1)
        march = await call(c, "query_metrics", session_id=sid, question=q1, metrics=["revenue"],
                           filters=[{"dimension": "order_date", "operator": "gte",
                                     "value": "2025-03-01"},
                                    {"dimension": "order_date", "operator": "lt",
                                     "value": "2025-04-01"}])
        top = await call(c, "query_metrics", session_id=sid, question=q2, metrics=["revenue"],
                         dimensions=["customer_name"], order_by=["revenue desc"], limit=5)
        bad = await call(c, "query_metrics", session_id=sid, question=q2, metrics=["revenue"],
                         dimensions=["colour"])
        assert not march.isError and not top.isError, (text_of(march), text_of(top))
        return text_of(march), text_of(top), sid, bad

    march, top, sid, bad = with_client(env, fn)
    assert bad.isError and "Unknown dimension" in text_of(bad)
    want = hand(f"select sum(amount)/100.0 from analytics.orders where {VALID} "
                "and order_date >= '2025-03-01' and order_date < '2025-04-01'", warehouse)
    got = result_rows(env, rid_of(march))
    assert got[0][0] == pytest.approx(want[0][0]) and want[0][0] > 0
    assert "semantic_sql:" in march and "rows: 1" in march and "```sql" in march

    want5 = hand("select c.first_name || ' ' || c.last_name n, sum(o.amount)/100.0 r "
                 "from analytics.orders o join analytics.customers c using (customer_id) "
                 "where o.status not in ('returned','return_pending') and o.customer_id <> 0 "
                 "group by 1 order by r desc limit 5", warehouse)
    got5 = result_rows(env, rid_of(top))
    assert [r[0] for r in got5] == [r[0] for r in want5] and len(got5) == 5
    assert [r[1] for r in got5] == pytest.approx([r[1] for r in want5])

    with admin_session(engine=env.engine) as s:
        tools = list(s.scalars(select(ToolCall.tool).where(
            ToolCall.session_id == uuid.UUID(sid)).order_by(ToolCall.id)))
        assert tools == ["start_analysis", "query_metrics", "query_metrics", "query_metrics"]
        qr = list(s.scalars(select(QueryResult).where(QueryResult.session_id == uuid.UUID(sid))))
        assert len(qr) == 2 and all(r.tool_call_id for r in qr)


def test_list_describe_metrics(env: Env) -> None:
    async def fn(c: ClientSession) -> tuple[Any, Any]:
        await c.initialize()
        sid = await start(c, "metrics?")
        a = await call(c, "list_metrics", session_id=sid)
        b = await c.call_tool("describe_metric", {
            "session_id": sid, "question": "what is revenue", "name": "revenue"})
        return a, b

    a, b = with_client(env, fn)
    cubes = json.loads(text_of(a))["cubes"]
    assert cubes[0]["cube"] == "sales" and len(cubes[0]["metrics"]) == 3
    d = json.loads(text_of(b))
    assert d["glossary"]["tag"].startswith("[ctx:glossary/business-terms.yaml#revenue@")
    assert d["source_tables"] == ["analytics.orders"] and "order_date" in str(d["time_dimensions"])


def test_run_sql_paging_and_preview(env: Env) -> None:
    q = "list orders"

    async def fn(c: ClientSession) -> tuple[str, str, str]:
        await c.initialize()
        sid = await start(c, q)
        r = await call(c, "run_sql", session_id=sid, question=q,
                       sql="select order_id, amount from analytics.orders order by order_id")
        rid = rid_of(text_of(r))
        p = await call(c, "get_result", session_id=sid, result_id=rid, offset=100, limit=3)
        small = await call(c, "run_sql", session_id=sid, question=q,
                           sql="select 1 as one")
        return text_of(r), text_of(p), text_of(small)

    full, page, small = with_client(env, fn)
    assert "rows: 700" in full and "showing 50 of 700" in full and "TRUNCATED" not in full
    assert full.count("\n| ") <= 52  # header + separator + 50 rows
    assert "rows 101-103 of 700" in page and "| 101 |" in page
    assert "rows: 1" in small


def test_run_sql_row_cap_setting(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SATCHEL_SQL_ROW_CAP", "120")

    async def fn(c: ClientSession) -> str:
        await c.initialize()
        sid = await start(c, "cap")
        return text_of(await call(c, "run_sql", session_id=sid, question="cap",
                                  sql="select * from analytics.orders"))

    out = with_client(env, fn)
    assert "rows: 120" in out and "TRUNCATED" in out
    with admin_session(engine=env.engine) as s:
        row = s.get(QueryResult, uuid.UUID(rid_of(out)))
        assert row is not None and row.row_count == 120 and row.truncated


def test_get_result_is_private_to_the_owner(env: Env, app_engine: Engine) -> None:
    async def make(c: ClientSession) -> str:
        await c.initialize()
        sid = await start(c, "x")
        return rid_of(text_of(await call(c, "run_sql", session_id=sid, question="x",
                                         sql="select 1 as a")))

    rid = with_client(env, make)
    other_raw, _ = create_token(env.slug, "someone-else@example.com", "o", engine=app_engine)
    other = verify_token(other_raw, engine=app_engine)
    assert other is not None

    async def peek(c: ClientSession) -> Any:
        await c.initialize()
        sid = await start(c, "y")
        return await call(c, "get_result", session_id=sid, result_id=rid)

    res = with_client(env, peek, principal=other)
    assert res.isError and "Unknown result_id" in text_of(res)


def signals(env: Env, sid: str) -> list[Signal]:
    with admin_session(engine=env.engine) as s:
        rows = list(s.scalars(select(Signal).where(Signal.session_id == uuid.UUID(sid))
                              .order_by(Signal.id)))
        s.expunge_all()
        return rows


def test_guardrail_rejection_is_logged_not_a_sql_error(env: Env) -> None:
    async def fn(c: ClientSession) -> tuple[str, Any]:
        await c.initialize()
        sid = await start(c, "drop")
        res = await call(c, "run_sql", session_id=sid, question="drop", sql="drop table x")
        return sid, res

    sid, res = with_client(env, fn)
    assert res.isError and "rejected" in text_of(res)
    with admin_session(engine=env.engine) as s:
        err = s.scalars(select(ToolCall).where(ToolCall.session_id == uuid.UUID(sid),
                                               ToolCall.tool == "run_sql")).one()
        assert err.error and "rejected" in err.error  # exactly one row, not duplicated
    assert [x.kind for x in signals(env, sid)] == []


def test_sql_error_and_retry_storm(env: Env) -> None:
    async def fn(c: ClientSession) -> str:
        await c.initialize()
        sid = await start(c, "bad sql")
        for col in ("amout", "totl", "revnue", "sum_amt"):
            r = await call(c, "run_sql", session_id=sid, question="bad sql",
                           sql=f"select {col} from analytics.orders where order_id = 12345")
            assert r.isError and "SQL error" in text_of(r)
        return sid

    sid = with_client(env, fn)
    sigs = signals(env, sid)
    kinds = [x.kind for x in sigs]
    assert kinds.count("sql_error") == 4 and kinds.count("retry_storm") == 1
    first = next(x for x in sigs if x.kind == "sql_error")
    assert first.source == "implicit"
    assert "12345" not in first.payload["message"] and "n" in first.payload["message"].lower()
    storms = [x for x in sigs if x.kind == "retry_storm"]
    assert storms[0].payload["failed_attempts"] == 3
    with admin_session(engine=env.engine) as s:
        n_err = len(list(s.scalars(select(ToolCall).where(
            ToolCall.session_id == uuid.UUID(sid), ToolCall.tool == "run_sql",
            ToolCall.error.is_not(None)))))
        assert n_err == 4


def test_undocumented_asset(bare_env: Env) -> None:
    async def fn(c: ClientSession) -> str:
        await c.initialize()
        sid = await start(c, "u")
        r = await call(c, "run_sql", session_id=sid, question="u",
                       sql="select amount from analytics.orders limit 1")
        assert not r.isError, text_of(r)
        return sid

    sid = with_client(bare_env, fn)
    s_ = [x for x in signals(bare_env, sid) if x.kind == "undocumented_asset"]
    assert len(s_) == 1 and s_[0].payload["tables"] == ["analytics.orders"]
    assert len(s_[0].asset_ids) == 1


def test_run_sql_signals_respect_preview_release_id(bare_env: Env) -> None:
    """When run_sql is invoked against a pinned preview release, signal extraction reads
    asset_doc, endorsement, and semantic_model items from that release rather than only
    published items, and preserves columns for pre-enrichment asset_doc rows."""
    from satchel.curation.drafts import add_version
    from satchel.curation.releases import create_release
    from satchel.store.models import ContextVersion
    from satchel.tools.registry import InvokeContext, invoke_tool

    with admin_session(engine=bare_env.engine) as s:
        doc_item = s.scalars(
            select(ContextItem).where(
                ContextItem.workspace_id == bare_env.workspace_id,
                ContextItem.type == "asset_doc",
                ContextItem.key.like("%orders%"),
            )
        ).first()
        assert doc_item is not None and doc_item.current_version_id is not None
        assert doc_item.published_version_id is None  # draft only from crawl
        # Simulate a legacy pre-enrichment asset_doc row where meta has only {asset, endorsed}
        # and columns live only in the YAML body.
        doc_ver = s.get(ContextVersion, doc_item.current_version_id)
        assert doc_ver is not None
        doc_ver.meta = {"asset": "analytics.orders", "endorsed": False}

        endorse_item = ContextItem(
            workspace_id=bare_env.workspace_id,
            type="endorsement",
            key="endorsements/orders.yaml",
            scope_kind="workspace",
            state="draft",
            managed_by="ui",
        )
        s.add(endorse_item)
        s.flush()
        endorse_ver = add_version(
            s,
            endorse_item,
            "asset: analytics.orders\nendorsed: true\n",
            {},
            author=bare_env.principal.user_id,
            provenance={"source": "test"},
            embedder=EMB,
        )

        mdl_body = json.dumps({
            "models": [{
                "name": "orders_m",
                "refSql": f"select * from analytics.orders where {VALID}",
            }],
            "cubes": [{
                "name": "sales",
                "baseObject": "orders_m",
                "measures": [{"name": "revenue", "expression": "sum(amount)"}],
            }],
        })
        sem_item = ContextItem(
            workspace_id=bare_env.workspace_id,
            type="semantic_model",
            key="semantic/sales.json",
            scope_kind="workspace",
            state="draft",
            managed_by="ui",
        )
        s.add(sem_item)
        s.flush()
        sem_ver = add_version(
            s,
            sem_item,
            mdl_body,
            {"format": "wren_mdl"},
            author=bare_env.principal.user_id,
            provenance={"source": "test"},
            embedder=EMB,
        )

        preview = create_release(
            s,
            bare_env.workspace_id,
            kind="preview",
            mapping={
                doc_item.id: doc_item.current_version_id,
                endorse_item.id: endorse_ver.id,
                sem_item.id: sem_ver.id,
            },
            parent_id=None,
            note="preview with orders doc, endorsement, and semantic model",
            created_by=bare_env.principal.user_id,
        )
        preview_id = preview.id

    ctx = InvokeContext(
        engine=bare_env.engine,
        embedder=EMB,
        principal=bare_env.principal,
        release_id=preview_id,
    )

    def run_in_session(sql: str) -> list[Signal]:
        started = invoke_tool(ctx, "start_analysis", {"question": "preview check"})
        assert isinstance(started, str)
        sid = str(json.loads(started)["session_id"])
        invoke_tool(ctx, "run_sql", {"session_id": sid, "question": "preview check", "sql": sql})
        return signals(bare_env, sid)

    # 1. undocumented_asset: suppressed for documented table + column even with legacy meta
    s_doc = run_in_session("select amount, customer_id from analytics.orders limit 1")
    assert [x for x in s_doc if x.kind == "undocumented_asset"] == []

    # 2. unendorsed_used: fires against preview endorsement when querying overlapping payments table
    s_unend = run_in_session("select status, sum(amount) from analytics.payments group by 1")
    unend = [x for x in s_unend if x.kind == "unendorsed_used"]
    assert len(unend) == 1 and unend[0].payload["used"] == "analytics.payments"
    assert unend[0].payload["preferred"][0]["table"] == "analytics.orders"

    # 3. metric_divergence: fires against preview semantic_model when required filters are missing
    s_div = run_in_session(
        "select sum(amount) from analytics.orders where order_date >= '2025-01-01'"
    )
    div = [x for x in s_div if x.kind == "metric_divergence"]
    assert len(div) == 1 and div[0].payload["metric"] == "sales.revenue"



def test_documented_query_emits_no_undocumented_signal(env: Env) -> None:
    async def fn(c: ClientSession) -> str:
        await c.initialize()
        sid = await start(c, "d")
        await call(c, "run_sql", session_id=sid, question="d",
                   sql=f"select sum(amount) from analytics.orders where {VALID}")
        return sid

    sid = with_client(env, fn)
    assert signals(env, sid) == []


def test_unendorsed_used(env: Env) -> None:
    async def fn(c: ClientSession) -> str:
        await c.initialize()
        sid = await start(c, "payments")
        r = await call(c, "run_sql", session_id=sid, question="payments",
                       sql="select status, sum(amount) from analytics.payments group by 1")
        assert not r.isError, text_of(r)
        return sid

    sid = with_client(env, fn)
    u = [x for x in signals(env, sid) if x.kind == "unendorsed_used"]
    assert len(u) == 1
    assert u[0].payload["used"] == "analytics.payments"
    assert u[0].payload["preferred"][0]["table"] == "analytics.orders"
    assert u[0].item_ids  # points at the endorsed table's asset_doc


def test_metric_divergence(env: Env) -> None:
    async def fn(c: ClientSession) -> tuple[str, str]:
        await c.initialize()
        bad = await start(c, "raw sum")
        r = await call(c, "run_sql", session_id=bad, question="raw sum",
                       sql="select sum(amount) from analytics.orders "
                           "where order_date >= '2025-01-01'")
        assert not r.isError, text_of(r)
        good = await start(c, "right sum")
        await call(c, "run_sql", session_id=good, question="right sum",
                   sql="select sum(amount) from analytics.orders o where "
                       "o.customer_id <> 0 and o.status not in ('return_pending', 'returned')")
        other = await start(c, "other agg")
        await call(c, "run_sql", session_id=other, question="other agg",
                   sql="select max(order_date) from analytics.orders")
        return bad, good + "|" + other

    bad, rest = with_client(env, fn)
    div = [x for x in signals(env, bad) if x.kind == "metric_divergence"]
    assert len(div) == 1
    assert div[0].payload["metric"] == "sales.revenue"
    assert len(div[0].payload["missing_filters"]) == 2
    assert div[0].item_ids  # the semantic_model item
    for sid in rest.split("|"):
        assert [x for x in signals(env, sid) if x.kind == "metric_divergence"] == []


def test_principal_role_not_needed_for_data_tools(env: Env) -> None:
    explorer = Principal(env.workspace_id, env.principal.user_id, "explorer")

    async def fn(c: ClientSession) -> Any:
        await c.initialize()
        sid = await start(c, "x")
        return await call(c, "run_sql", session_id=sid, question="x", sql="select 1 as a")

    assert not with_client(env, fn, principal=explorer).isError


def test_postgres_connector(migrated: str) -> None:
    from sqlalchemy.engine import make_url

    from satchel.data.connectors import make_connector
    from satchel.data.gateway import run_guarded

    u = make_url(migrated)
    con = make_connector("postgres", {"host": u.host, "port": u.port, "database": u.database,
                                      "user": u.username}, u.password)
    specs = con.introspect()
    assert any(s.fqn == "public.workspace" and s.kind == "table" for s in specs)
    assert any(s.fqn == "public.workspace.slug" and s.data_type == "text" for s in specs)
    r = run_guarded(con, "select generate_series(1, 50) as n", row_cap=10, timeout_s=10)
    assert r.table.num_rows == 10 and r.truncated
    with pytest.raises(Exception, match="read-only"):
        con.execute("create table zz (a int)", limit=1, timeout_s=5)
    con.close()


def test_preview_keeps_cents() -> None:
    from satchel.data.results import markdown_table

    t = markdown_table(["a", "b", "c"], [(22777.05, 1716.15, 3.0), (None, 0.1 + 0.2, 1e-7)])
    assert "| 22777.05 | 1716.15 | 3 |" in t and "| 0.3 | 0 |" in t.replace("|  |", "|")
