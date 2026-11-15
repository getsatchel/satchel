"""Turns: the questions inside a session (``satchel.tools.turns``, migration 0017)."""

from __future__ import annotations

import json
from typing import Any

from alembic import command
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from sqlalchemy import create_engine, select, text
from testcontainers.postgres import PostgresContainer

from satchel.api.app import create_app
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import Session as SessionRow
from satchel.store.models import SessionTurn, Signal, ToolCall
from satchel.tools.turns import starts_new_turn
from tests.test_mcp import EMB, Env, call, env, text_of, with_client  # noqa: F401  (fixture)
from tests.test_migrations import _cfg

Q1 = "What was revenue last month?"
Q2 = "Which customers returned the most orders?"


def test_starts_new_turn_rule() -> None:
    # the same question, however it is spaced, cased or punctuated
    assert not starts_new_turn(Q1, False, "  what was revenue   last month ")
    assert not starts_new_turn(Q1, True, Q1)
    # calls without a question stay in the current turn
    assert not starts_new_turn(Q1, True, None) and not starts_new_turn(Q1, True, " ")
    # a different question after the answer is always a new turn
    assert starts_new_turn(Q1, True, "What was revenue last month, by region?")
    # before the answer a rewording stays; a clearly different question does not
    assert not starts_new_turn(Q1, False, "What was the revenue last month?")
    assert starts_new_turn(Q1, False, Q2)
    # nothing to compare with yet
    assert not starts_new_turn(None, False, Q1)


def test_follow_up_questions_get_their_own_turn(env: Env) -> None:  # noqa: F811
    async def fn(c: ClientSession) -> str:
        await c.initialize()
        sid = json.loads(text_of(await call(c, "start_analysis", question=Q1)))["session_id"]
        await call(c, "search_context", session_id=sid, question=Q1, query="revenue")
        # reworded before the answer: still the first question
        await call(c, "search_context", session_id=sid,
                   question="What was the revenue last month?", query="revenue definition")
        await call(c, "report_answer", session_id=sid, answer_summary="42", confidence="high")
        # the agent keeps the session for the next question
        await call(c, "search_context", session_id=sid, question=Q2, query="returned orders")
        await call(c, "list_tables", session_id=sid)  # no question: stays in the turn
        await call(c, "flag_context_gap", session_id=sid, what_was_missing="returned order",
                   why="no definition")
        return sid

    sid = with_client(env, fn)
    with admin_session(engine=env.engine) as s:
        turns = list(s.scalars(
            select(SessionTurn).where(SessionTurn.session_id == sid).order_by(SessionTurn.seq)))
        assert [(t.seq, t.question, t.status) for t in turns] == [
            (1, Q1, "answered"), (2, Q2, "active")]
        assert turns[0].ended_at is not None and turns[1].ended_at is None
        calls = s.execute(
            select(ToolCall.tool, ToolCall.turn_id).where(ToolCall.session_id == sid)
            .order_by(ToolCall.id)).all()
        first, second = turns[0].id, turns[1].id
        assert [tuple(r) for r in calls] == [
            ("start_analysis", first), ("search_context", first), ("search_context", first),
            ("report_answer", first), ("search_context", second), ("list_tables", second),
            ("flag_context_gap", second)]
        gap = s.scalars(select(Signal).where(
            Signal.session_id == sid, Signal.kind == "gap_flagged")).one()
        assert gap.turn_id == second
        sess = s.get(SessionRow, sid)
        assert sess is not None
        # the session keeps its first question; it is open again until the new one is answered
        assert sess.question == Q1 and sess.status == "active"

    api = TestClient(create_app(env.engine, EMB, settings=Settings(dev_auth=True)))
    h = {"X-Satchel-Workspace": env.slug, "X-Satchel-User": str(env.principal.user_id)}
    detail: dict[str, Any] = api.get(f"/v1/sessions/{sid}", headers=h).json()
    assert [(t["seq"], t["question"], t["status"], t["tool_call_count"])
            for t in detail["turns"]] == [(1, Q1, "answered", 4), (2, Q2, "active", 3)]
    assert detail["turn_count"] == 2
    assert [c["turn_id"] for c in detail["timeline"]] == [first] * 4 + [second] * 3
    assert [g["turn_id"] for g in detail["signals"] if g["kind"] == "gap_flagged"] == [second]
    listed = api.get("/v1/sessions", headers=h).json()["sessions"]
    assert next(x for x in listed if x["id"] == sid)["turn_count"] == 2
    # the list of questions has one row per turn, newest first
    rows = [x for x in api.get("/v1/turns", headers=h).json()["turns"] if x["session_id"] == sid]
    assert [(x["seq"], x["question"], x["status"], x["tool_call_count"], x["signal_count"],
             x["turn_count"]) for x in rows] == [
        (2, Q2, "active", 3, 1, 2), (1, Q1, "answered", 4, 0, 2)]
    answered = api.get("/v1/turns", params={"status": "answered"}, headers=h).json()["turns"]
    assert all(x["status"] == "answered" for x in answered)
    assert [x["seq"] for x in answered if x["session_id"] == sid] == [1]


def test_0017_backfills_turns_for_existing_sessions() -> None:
    with PostgresContainer("pgvector/pgvector:pg17", driver="psycopg") as pg:
        url = pg.get_connection_url()
        command.upgrade(_cfg(url), "0016")
        eng = create_engine(url)
        try:
            with eng.begin() as conn:
                q = lambda sql, **kw: conn.execute(text(sql), kw).scalar()  # noqa: E731
                ws = q("insert into workspace (slug, name) values ('t', 'T') returning id")
                multi = q("insert into session (workspace_id, question, status) "
                          "values (:w, :q, 'answered') returning id", w=ws, q=Q1)
                empty = q("insert into session (workspace_id, question) values (:w, 'Idle?') "
                          "returning id", w=ws)
                for tool, question in (
                    ("start_analysis", Q1), ("search_context", Q1), ("report_answer", None),
                    ("search_context", Q2), ("get_result", None),
                ):
                    args = {"question": question} if question else {}
                    conn.execute(
                        text("insert into tool_call (session_id, tool, args) "
                             "values (:s, :t, cast(:a as jsonb))"),
                        {"s": multi, "t": tool, "a": json.dumps(args)})
            command.upgrade(_cfg(url), "head")
            with eng.connect() as conn:
                turns = conn.execute(text(
                    "select seq, question, status from session_turn where session_id = :s "
                    "order by seq"), {"s": multi}).all()
                assert [tuple(r) for r in turns] == [(1, Q1, "answered"), (2, Q2, "active")]
                seqs = conn.execute(text(
                    "select t.seq from tool_call c join session_turn t on t.id = c.turn_id "
                    "where c.session_id = :s order by c.id"), {"s": multi}).scalars().all()
                assert seqs == [1, 1, 1, 2, 2]
                # a session without calls still gets its one turn
                idle = conn.execute(text(
                    "select seq, question, status from session_turn where session_id = :s"),
                    {"s": empty}).all()
                assert [tuple(r) for r in idle] == [(1, "Idle?", "active")]
        finally:
            eng.dispose()
