"""The text of a signal: what gets embedded for clustering and what curators read as its
message. One function per direction so both stay in step (design §7.1 step 2).

``signal_text`` is the human message (also shown in the Inbox evidence list, after redaction of
the payload). ``embedding_text`` adds the session question for kinds whose own text is generic
(an error message says little about the topic).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from satchel.llm.embeddings import EmbeddingProvider
from satchel.store.models import Session as SessionRow
from satchel.store.models import Signal
from satchel.store.scopes import signal_not_eval

MAX_EMBED_CHARS = 1500
_TEXT_KEYS = ("text", "what_was_missing", "comment", "message", "last_error", "query", "question")


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _list(v: Any) -> str:
    return "; ".join(str(x) for x in v if x) if isinstance(v, list) else ""


def signal_text(kind: str, payload: dict[str, Any]) -> str:
    """A one-paragraph description of the signal, from its (possibly redacted) payload."""
    p = payload
    if kind == "gap_flagged":
        parts = [_s(p.get("what_was_missing")), _s(p.get("why"))]
        if not any(parts):  # mined from report_answer caveats
            parts = [_s(p.get("answer_summary")), _list(p.get("caveats"))]
        return ". ".join(x for x in parts if x)
    if kind == "user_doubt":
        return _s(p.get("comment")) or f"user {p.get('sentiment') or 'doubted'} the answer"
    if kind == "low_confidence":
        return ". ".join(x for x in (_s(p.get("answer_summary")), _list(p.get("caveats"))) if x)
    if kind == "sql_error":
        return _s(p.get("message")) or _s(p.get("raw"))
    if kind == "retry_storm":
        return f"repeated failing SQL: {_s(p.get('last_error'))}"
    if kind == "retrieval_miss":
        return f"no good context found for: {_s(p.get('query')) or _s(p.get('question'))}"
    if kind == "unendorsed_used":
        pref = ", ".join(
            str(x.get("table")) for x in p.get("preferred") or [] if isinstance(x, dict)
        )
        return f"used unendorsed table {_s(p.get('used'))} although {pref} is endorsed"
    if kind == "metric_divergence":
        return (
            f"ad-hoc aggregate {_s(p.get('adhoc_aggregate'))} diverges from metric "
            f"{_s(p.get('metric'))}: missing filters {_list(p.get('missing_filters'))}"
        )
    if kind == "undocumented_asset":
        return "undocumented assets used: " + ", ".join(
            [*(p.get("tables") or []), *(p.get("columns") or [])]
        )
    for key in _TEXT_KEYS:  # hygiene / golden_candidate / anything new
        if _s(p.get(key)):
            return _s(p[key])
    return kind.replace("_", " ")


# Kinds whose text says little about the topic: add the session's question.
_NEEDS_QUESTION = {"sql_error", "retry_storm", "undocumented_asset", "unendorsed_used",
                   "metric_divergence"}


def embedding_text(sig: Signal, session_question: str | None) -> str:
    text = signal_text(sig.kind, sig.payload)
    if session_question and (sig.kind in _NEEDS_QUESTION or not text):
        text = f"{session_question}. {text}".strip(". ")
    return (text or sig.kind)[:MAX_EMBED_CHARS]


def embed_pending_signals(
    s: Session, workspace_id: uuid.UUID, embedder: EmbeddingProvider, *, limit: int = 2000
) -> int:
    """Embed signals of the workspace that have no embedding yet. Returns the count embedded."""
    rows = list(
        s.execute(
            select(Signal, SessionRow.question)
            .outerjoin(SessionRow, SessionRow.id == Signal.session_id)
            .where(Signal.workspace_id == workspace_id, Signal.embedding.is_(None),
                   signal_not_eval())
            .order_by(Signal.id)
            .limit(limit)
        )
    )
    if not rows:
        return 0
    vectors = embedder.embed([embedding_text(sig, q) for sig, q in rows])
    for (sig, _), vec in zip(rows, vectors, strict=True):
        sig.embedding = vec
    s.flush()
    return len(rows)
