"""Turns: the questions inside a session.

Agents are told to call ``start_analysis`` for every question, but they often keep one
``session_id`` for a whole conversation. A session's tool calls are therefore grouped into
turns. A call starts a new turn when it carries a question that differs from the current
turn's: always once that turn was answered (``report_answer``), and otherwise only when the
wording is clearly a different question, since agents sometimes reword the same one between
calls. Calls without a question (``get_result``, ``report_answer``, feedback) stay in the
current turn.

Migration 0017 backfills turns for older sessions with its own frozen copy of this rule.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from satchel.store.models import Session, SessionTurn

# Below this similarity an unanswered turn's question counts as replaced by a new one.
REWORD_RATIO = 0.5
_WS = re.compile(r"\s+")


def normalize(question: object) -> str:
    if not isinstance(question, str):
        return ""
    return _WS.sub(" ", question).strip().rstrip("?.! ").casefold()


def starts_new_turn(current: str | None, answered: bool, incoming: object) -> bool:
    new, cur = normalize(incoming), normalize(current)
    if not new or not cur or new == cur:
        return False
    return answered or SequenceMatcher(None, cur, new).ratio() < REWORD_RATIO


def latest_turn(s: OrmSession, session_id: uuid.UUID) -> SessionTurn | None:
    return s.scalars(
        select(SessionTurn).where(SessionTurn.session_id == session_id)
        .order_by(SessionTurn.seq.desc()).limit(1)
    ).first()


def open_turn(s: OrmSession, sess: Session, question: str | None, seq: int = 1) -> SessionTurn:
    turn = SessionTurn(session_id=sess.id, seq=seq, question=question)
    s.add(turn)
    s.flush()
    return turn


def turn_for_call(s: OrmSession, sess: Session, question: object) -> SessionTurn:
    """The turn a call with this ``question`` belongs to, opening a new one when needed.

    Run it in its own short transaction: opening a turn locks the session row so two parallel
    calls cannot both open one, and that lock must not be held while a tool runs.
    """
    turn = latest_turn(s, sess.id)
    if turn is not None and not starts_new_turn(
        turn.question, turn.status == "answered", question
    ):
        if not turn.question and normalize(question):
            turn.question = str(question).strip()
        return turn
    s.execute(select(Session.id).where(Session.id == sess.id).with_for_update())
    turn = latest_turn(s, sess.id)  # again: a parallel call may have opened it meanwhile
    if turn is not None and not starts_new_turn(
        turn.question, turn.status == "answered", question
    ):
        return turn
    asked = str(question).strip() if normalize(question) else sess.question
    if sess.status == "answered":
        sess.status = "active"  # the new question has no answer yet
    return open_turn(s, sess, asked, seq=(turn.seq + 1) if turn is not None else 1)


def mark_answered(s: OrmSession, turn_id: int | None) -> None:
    turn = s.get(SessionTurn, turn_id) if turn_id is not None else None
    if turn is not None:
        turn.status = "answered"
        turn.ended_at = datetime.now(UTC)
