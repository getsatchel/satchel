"""Query filters that keep eval (reference-agent) traffic out of real usage data.

Eval sessions carry ``session.eval_run_id``; observability, signals and curation exclude them
by default (design §10).
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, exists
from sqlalchemy.orm import aliased

from satchel.store.models import Session as SessionRow
from satchel.store.models import Signal, ToolCall


def session_not_eval() -> ColumnElement[bool]:
    return SessionRow.eval_run_id.is_(None)


def signal_not_eval() -> ColumnElement[bool]:
    """True for signals outside eval sessions (signals with no session are kept)."""
    es = aliased(SessionRow)  # aliased so the subquery never auto-correlates with the outer query
    return ~exists().where(es.id == Signal.session_id, es.eval_run_id.is_not(None))


def tool_call_not_eval() -> ColumnElement[bool]:
    es = aliased(SessionRow)
    return ~exists().where(es.id == ToolCall.session_id, es.eval_run_id.is_not(None))
