"""``session_turn``: the questions inside a session.

Agents often keep one ``session_id`` for a whole conversation, so a session's tool calls are
grouped into turns (``satchel.tools.turns``). ``tool_call.turn_id`` and ``signal.turn_id`` say
which question a call or a signal belongs to.

Existing sessions are backfilled with the rule the server uses, copied here so this revision
stays frozen: a call starts a new turn when it carries a question that differs from the
current turn's, always after ``report_answer`` and otherwise only when the wording is clearly
different. Signals made before this revision keep ``turn_id`` NULL (they do not record the
call they came from) and fall back to the session's question.

Frozen: explicit DDL, no model imports.

Revision ID: 0017
Revises: 0016
"""

import re
from difflib import SequenceMatcher

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

EXPR = "exists (select 1 from session p where p.id = session_id)"
REWORD_RATIO = 0.5
_WS = re.compile(r"\s+")
BATCH = 1000


def _norm(q: object) -> str:
    if not isinstance(q, str):
        return ""
    return _WS.sub(" ", q).strip().rstrip("?.! ").casefold()


def _new_turn(current: str | None, answered: bool, incoming: object) -> bool:
    new, cur = _norm(incoming), _norm(current)
    if not new or not cur or new == cur:
        return False
    return answered or SequenceMatcher(None, cur, new).ratio() < REWORD_RATIO


def _backfill() -> None:
    conn = op.get_bind()
    insert = sa.text(
        "insert into session_turn (session_id, seq, question, status, started_at, ended_at) "
        "values (:sid, :seq, :q, :status, :started, :ended) returning id"
    )
    assign = sa.text("update tool_call set turn_id = :turn where id = any(:ids)")

    def flush(sid: object, turns: list[dict[str, object]]) -> None:
        for n, t in enumerate(turns, start=1):
            turn_id = conn.execute(insert, {
                "sid": sid, "seq": n, "q": t["q"], "started": t["started"], "ended": t["ended"],
                "status": "answered" if t["ended"] is not None else "active",
            }).scalar_one()
            if t["calls"]:
                conn.execute(assign, {"turn": turn_id, "ids": t["calls"]})

    last = None
    while True:
        page = conn.execute(sa.text(
            "select id, question, started_at from session "
            "where (:last is null or id > :last) order by id limit :n"
        ).bindparams(sa.bindparam("last", type_=UUID(as_uuid=True))),
            {"last": last, "n": BATCH}).all()
        if not page:
            return
        last = page[-1].id
        calls: dict[object, list[sa.Row[tuple[int, str, str | None, object]]]] = {}
        for row in conn.execute(sa.text(
            "select id, session_id, tool, args->>'question' as q, created_at from tool_call "
            "where session_id = any(:ids) order by id"
        ).bindparams(sa.bindparam("ids", type_=sa.ARRAY(UUID(as_uuid=True)))),
            {"ids": [r.id for r in page]}):
            calls.setdefault(row.session_id, []).append(row)
        for sess in page:
            turns: list[dict[str, object]] = [
                {"q": sess.question, "started": sess.started_at, "ended": None, "calls": []}
            ]
            for c in calls.get(sess.id, []):
                cur = turns[-1]
                if _new_turn(cur["q"], cur["ended"] is not None, c.q):  # type: ignore[arg-type]
                    cur = {"q": c.q.strip(), "started": c.created_at, "ended": None, "calls": []}
                    turns.append(cur)
                elif not cur["q"] and _norm(c.q):
                    cur["q"] = c.q.strip()
                cur["calls"].append(c.id)  # type: ignore[attr-defined]
                if c.tool == "report_answer":
                    cur["ended"] = c.created_at
            flush(sess.id, turns)


def upgrade() -> None:
    op.create_table(
        "session_turn",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("session.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("session_id", "seq", name="session_turn_session_seq_uq"),
        sa.CheckConstraint("status in ('active','answered')", name="session_turn_status_ck"),
    )
    op.execute("ALTER TABLE session_turn ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE session_turn FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY session_turn_rls ON session_turn USING ({EXPR}) WITH CHECK ({EXPR})")
    op.execute(
        "CREATE POLICY session_turn_worker ON session_turn FOR ALL TO satchel_worker "
        "USING (true) WITH CHECK (true)"
    )
    op.add_column("tool_call", sa.Column(
        "turn_id", sa.BigInteger(), sa.ForeignKey("session_turn.id"), nullable=True))
    op.create_index("tool_call_turn_idx", "tool_call", ["turn_id"])
    op.add_column("signal", sa.Column(
        "turn_id", sa.BigInteger(), sa.ForeignKey("session_turn.id"), nullable=True))
    _backfill()


def downgrade() -> None:
    op.drop_column("signal", "turn_id")
    op.drop_index("tool_call_turn_idx", table_name="tool_call")
    op.drop_column("tool_call", "turn_id")
    op.drop_table("session_turn")
