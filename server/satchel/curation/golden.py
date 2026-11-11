"""Golden-query candidates (design §7.2): a session that ended well becomes a suggestion.

A session qualifies when it has positive feedback (and no negative feedback), a
``report_answer`` with ``confidence = high``, is not flagged sensitive, and its last
successful ``run_sql`` / ``query_metrics`` call is not already covered by a golden query. Each
qualifying session yields one ``golden_candidate`` signal (``source='human'``, because a person
rated it; a 👍 from the result widget, ``feedback.source = 'human'``, pins the query to the call
it rated and sets ``rated_by = 'human'``); the gap detector turns it into a suggestion that
bypasses the thresholds, and the
drafter builds the ``promote_golden_query`` change without needing an LLM.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from satchel.curation.config import CurationConfig
from satchel.store.models import ContextItem, ContextVersion, Feedback, Signal, ToolCall
from satchel.store.models import Session as SessionRow
from satchel.store.scopes import session_not_eval

log = logging.getLogger("satchel.curation")


def _norm_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()


def _existing_queries(s: Session, workspace_id: uuid.UUID) -> set[str]:
    """Normalised SQL / metric-query fingerprints of golden queries that already exist."""
    out: set[str] = set()
    rows = s.execute(
        select(ContextVersion.body)
        .join(ContextItem, ContextItem.current_version_id == ContextVersion.id)
        .where(ContextItem.workspace_id == workspace_id, ContextItem.type == "golden_query",
               ContextItem.state != "deprecated")
    )
    for (body,) in rows:
        try:
            d = yaml.safe_load(body)
        except yaml.YAMLError:
            continue
        if isinstance(d, dict):
            if d.get("sql"):
                out.add(_norm_sql(str(d["sql"])))
            if d.get("metric_query"):
                out.add(json.dumps(d["metric_query"], sort_keys=True, default=str))
    return out


def query_of(call: ToolCall) -> tuple[str, Any] | None:
    """('sql', text) or ('metric_query', dict) for a successful data-plane call."""
    if call.tool == "run_sql" and isinstance(call.args.get("sql"), str):
        return "sql", call.args["sql"]
    if call.tool == "query_metrics" and call.args.get("metrics"):
        mq = {k: call.args[k] for k in ("metrics", "dimensions", "filters", "order_by", "limit")
              if call.args.get(k)}
        return "metric_query", mq
    return None


def detect_golden_candidates(
    s: Session, workspace_id: uuid.UUID, cfg: CurationConfig, *, now: datetime | None = None
) -> int:
    """Emit ``golden_candidate`` signals for qualifying sessions. Returns the number emitted."""
    since = (now or datetime.now(UTC)) - timedelta(days=cfg.window_days)
    sessions = list(s.scalars(
        select(SessionRow).where(
            SessionRow.workspace_id == workspace_id, SessionRow.started_at >= since,
            SessionRow.sensitive.is_(False), SessionRow.question.is_not(None),
            SessionRow.user_id.is_not(None), session_not_eval(),
        ).order_by(SessionRow.started_at)
    ))
    if not sessions:
        return 0
    known = _existing_queries(s, workspace_id)
    emitted = 0
    for sess in sessions:
        dedupe = f"session:{sess.id}"
        if s.scalar(select(Signal.id).where(
            Signal.workspace_id == workspace_id, Signal.kind == "golden_candidate",
            Signal.payload["dedupe"].astext == dedupe).limit(1)) is not None:
            continue
        fbs = list(s.scalars(select(Feedback).where(Feedback.session_id == sess.id)
                             .order_by(Feedback.id)))
        if (not fbs or any(f.rating < 0 for f in fbs) or not any(f.rating > 0 for f in fbs)):
            continue
        human_up = [f for f in fbs if f.rating > 0 and f.source == "human"]
        calls = list(s.scalars(
            select(ToolCall).where(ToolCall.session_id == sess.id).order_by(ToolCall.id)))
        if not any(c.tool == "report_answer" and c.error is None
                   and c.args.get("confidence") == "high" for c in calls):
            continue
        ok = [c for c in calls if c.error is None and c.tool in ("run_sql", "query_metrics")
              and query_of(c) is not None]
        # A 👍 on a specific result (result widget) pins the golden query to that very call.
        rated = {f.tool_call_id for f in human_up if f.tool_call_id is not None}
        last = next((c for c in reversed(ok) if c.id in rated), None) or (
            ok[-1] if ok else None)
        if last is None:
            continue
        kind, value = query_of(last)  # type: ignore[misc]
        fingerprint = _norm_sql(value) if kind == "sql" else json.dumps(value, sort_keys=True,
                                                                         default=str)
        if fingerprint in known:
            continue
        known.add(fingerprint)
        connection = last.result_meta.get("connection") or last.args.get("connection")
        s.add(Signal(
            workspace_id=workspace_id, session_id=sess.id, user_id=sess.user_id,
            kind="golden_candidate", source="human",
            payload={"text": f"Promote to a golden query: {sess.question}",
                     "question": sess.question, kind: value, "connection": connection,
                     "tool_call_id": last.id, "dedupe": dedupe,
                     "rated_by": "human" if human_up else "agent"},
        ))
        emitted += 1
    s.flush()
    return emitted
