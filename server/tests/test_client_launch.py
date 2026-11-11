"""Launch ``satchel mcp stdio`` the way Claude Desktop does and run its real call sequence.

Desktop starts the server with cwd ``/`` and only the config's env, and its agent mode
dropped ``session_id`` and sent Cube-style filters. This runs all of that for real.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import anyio
import duckdb
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import Engine, select

from satchel.data.connections import add_connection
from satchel.data.crawl import crawl_connection
from satchel.llm.embeddings import HashEmbedder
from satchel.mcp.auth import create_token
from satchel.store.db import admin_session
from satchel.store.models import QueryResult, ToolCall
from satchel.store.models import Session as SessionRow
from satchel.sync import import_context
from tests.helpers import build_jaffle_db, copy_example, unique_slug

SATCHEL_BIN = Path(sys.executable).parent / "satchel"
VALID = "status not in ('returned','return_pending') and customer_id <> 0"
FILTERS = [
    {"member": "order_date", "operator": "gte", "values": ["2025-03-01"]},
    {"dimension": "order_date", "operator": "lt", "values": ["2025-04-01"]},
]
pytestmark = pytest.mark.skipif(
    not SATCHEL_BIN.exists(), reason="satchel console script not installed"
)


def _text(r: Any) -> str:
    return str(r.content[0].text)


def _params(engine: Engine, token: str, home: Path) -> StdioServerParameters:
    home.mkdir(parents=True, exist_ok=True)
    return StdioServerParameters(
        command=str(SATCHEL_BIN),
        args=["mcp", "stdio"],
        cwd="/",
        env={
            "SATCHEL_TOKEN": token,
            "SATCHEL_DATABASE_URL": engine.url.render_as_string(hide_password=False),
            "SATCHEL_EMBEDDING_PROVIDER": "hash",
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
        },
    )


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_jaffle_db(tmp_path_factory.mktemp("wh") / "jaffle.duckdb")


@pytest.fixture(scope="module")
def seeded(
    app_engine: Engine, warehouse: Path, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    slug = unique_slug("cl")
    r = import_context(copy_example(tmp_path_factory.mktemp("cl")), slug,
                       embedder=HashEmbedder(), engine=app_engine)
    add_connection(slug, "warehouse", "duckdb", {"path": str(warehouse)}, engine=app_engine)
    crawl_connection(slug, "warehouse", engine=app_engine)
    email = f"{slug}@example.com"
    raw, _ = create_token(slug, email, "t", engine=app_engine)
    raw2, _ = create_token(slug, f"fresh-{slug}@example.com", "t", engine=app_engine)
    return {"wid": r.workspace_id, "token": raw, "fresh_token": raw2}


@pytest.mark.slow
def test_desktop_launch_full_flow(
    app_engine: Engine, seeded: dict[str, Any], warehouse: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    q = "provide the revenue in march 2025"

    async def go() -> dict[str, Any]:
        out: dict[str, Any] = {}
        async with (
            stdio_client(_params(app_engine, seeded["token"], home)) as (rd, wr),
            ClientSession(rd, wr) as c,
        ):
            if True:
                await c.initialize()
                steps: dict[str, Any] = {}
                st = await c.call_tool("start_analysis", {"question": q})
                steps["start"] = st
                sid = json.loads(_text(st))["session_id"]
                out["sid"] = sid
                steps["pack"] = await c.call_tool("get_context_pack", {"question": q})
                steps["qm"] = await c.call_tool("query_metrics", {
                    "question": q, "metrics": ["revenue"], "filters": FILTERS})
                steps["sql"] = await c.call_tool("run_sql", {
                    "question": q,
                    "sql": f"select sum(amount)/100.0 as rev from analytics.orders where {VALID}"})
                rid = re.search(r"result_id: ([0-9a-f-]{36})", _text(steps["sql"]))
                assert rid, _text(steps["sql"])
                out["rid"] = rid.group(1)
                steps["get"] = await c.call_tool("get_result", {"result_id": out["rid"]})
                steps["rep"] = await c.call_tool("report_answer", {
                    "answer_summary": "March revenue", "metrics_used": ["revenue"],
                    "confidence": "high"})
                out["steps"] = steps
        return out

    out = anyio.run(go)
    for name, r in out["steps"].items():
        assert not r.isError, f"{name}: {_text(r)}"

    con = duckdb.connect(str(warehouse), read_only=True)
    want_march = con.execute(
        f"select sum(amount)/100.0 from analytics.orders where {VALID} "
        "and order_date >= '2025-03-01' and order_date < '2025-04-01'").fetchone()
    want_all = con.execute(
        f"select sum(amount)/100.0 from analytics.orders where {VALID}").fetchone()
    con.close()
    assert want_march and want_all and want_march[0] > 0

    sid = uuid.UUID(out["sid"])
    with admin_session(engine=app_engine) as s:
        assert s.get(SessionRow, sid) is not None
        calls = list(s.scalars(select(ToolCall).where(ToolCall.session_id == sid)))
        names = [t.tool for t in calls] if hasattr(ToolCall, "tool") else []
        sqls = [r.sql for r in s.scalars(select(QueryResult).where(QueryResult.session_id == sid))]
        all_sessions = s.scalars(
            select(SessionRow).where(SessionRow.workspace_id == seeded["wid"])).all()
    assert len(calls) >= 5, names
    assert len(all_sessions) == 1  # session-less calls attached to the one started session
    assert len(sqls) >= 2
    assert any(q.strip().lower().startswith("select sum(amount)/100.0") for q in sqls)
    from satchel.data.results import read_page
    with admin_session(engine=app_engine) as s:
        row = s.get(QueryResult, uuid.UUID(out["rid"]))
        assert row is not None
        rows = read_page(row, 0, 10, root=home / ".cache" / "satchel" / "results")[1]
    assert rows[0][0] == pytest.approx(want_all[0])

    parquet = list((home / ".cache" / "satchel" / "results").rglob("*.parquet"))
    assert parquet, list(home.rglob("*"))


@pytest.mark.slow
def test_desktop_launch_no_session_gives_corrective_error(
    app_engine: Engine, seeded: dict[str, Any], tmp_path: Path
) -> None:
    async def go() -> Any:
        async with (
            stdio_client(_params(app_engine, seeded["fresh_token"], tmp_path / "h")) as (rd, wr),
            ClientSession(rd, wr) as c,
        ):
            if True:
                await c.initialize()
                return await c.call_tool("get_context_pack", {"question": "revenue?"})

    r = anyio.run(go)
    assert r.isError
    msg = _text(r)
    assert "start_analysis" in msg and "Traceback" not in msg and "Internal" not in msg
