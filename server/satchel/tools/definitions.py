"""The context and signal tools (design §6.1): one ToolDef per tool. Data-plane tools live in
satchel.tools.data_tools."""

from __future__ import annotations

import difflib
import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from satchel.core.item_types import ItemValidationError, normalized_meta, yaml_item_dict
from satchel.curation.releases import release_items_stmt
from satchel.retrieval import build_context_pack, search_context
from satchel.retrieval.pack import always_on_entries
from satchel.retrieval.search import resolve_release
from satchel.sources.precedence import DocGroup, describe, fold_facts, group_docs, load_sources
from satchel.store.models import (
    Asset,
    ContextItem,
    ContextVersion,
    Feedback,
    Release,
    ServedContext,
    Signal,
    ToolCall,
)
from satchel.store.models import Session as SessionRow
from satchel.tools import text
from satchel.tools.registry import Call, ToolDef, ToolUserError

_SID = "session_id returned by start_analysis"
_Q = "The user's original question, verbatim (not your search query)"


class _Base(BaseModel):
    model_config = {"extra": "ignore"}


def _str_list(v: Any) -> Any:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return v


class StartArgs(_Base):
    question: str = Field(min_length=1, description=_Q)
    client_hint: str | None = Field(default=None, description="Optional client/tool name")


class SearchArgs(_Base):
    session_id: str = Field(description=_SID)
    question: str = Field(description=_Q)
    query: str = Field(min_length=1, description="Search text, e.g. a business term")
    types: list[str] | None = Field(default=None, description="Restrict to item types")
    k: int = Field(default=8, ge=1, le=50)


class PackArgs(_Base):
    session_id: str = Field(description=_SID)
    question: str = Field(description=_Q)
    budget_tokens: int = Field(default=4000, ge=100, le=100_000)


class ItemArgs(_Base):
    session_id: str = Field(description=_SID)
    key: str = Field(min_length=1, description="Item key, e.g. glossary/terms.yaml#revenue")
    question: str | None = Field(default=None, description=_Q)


class ListArgs(_Base):
    session_id: str = Field(description=_SID)
    question: str | None = Field(default=None, description=_Q)
    connection: str | None = None
    endorsed_only: bool = False
    filter: str | None = Field(default=None, description="Substring of the table name")


class DescribeArgs(_Base):
    session_id: str = Field(description=_SID)
    question: str = Field(description=_Q)
    table: str = Field(min_length=1, description="Table name, e.g. analytics.orders")


class ReportArgs(_Base):
    session_id: str = Field(description=_SID)
    answer_summary: str = Field(min_length=1)
    metrics_used: list[str] = Field(default_factory=list)
    tables_used: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]
    caveats: list[str] = Field(
        default_factory=list, description="Assumptions, missing definitions, data limits"
    )

    @field_validator("metrics_used", "tables_used", "caveats", mode="before")
    @classmethod
    def _lists(cls, v: Any) -> Any:
        return _str_list(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> Any:
        v = str(v).strip().lower()
        return {"med": "medium", "mid": "medium"}.get(v, v)


class GapArgs(_Base):
    session_id: str = Field(description=_SID)
    what_was_missing: str = Field(min_length=1)
    why: str = Field(min_length=1, description="Why it mattered for the answer")
    kind: Literal["missing_context", "data_limitation", "ambiguous_term"] = "missing_context"


class FeedbackArgs(_Base):
    session_id: str = Field(description=_SID)
    sentiment: Literal["positive", "negative", "correction", "doubt"]
    comment: str = Field(min_length=1)


class ProposeArgs(_Base):
    session_id: str = Field(description=_SID)
    op: Literal["create_item", "update_item"] = "create_item"
    type: str = Field(description="Item type, e.g. glossary_term, guide, caveat")
    body: str = Field(min_length=1, description="Item body (markdown, or YAML for YAML types)")
    meta: dict[str, Any] | None = Field(default=None, description="Extra fields for the type")
    target: str | None = Field(default=None, description="Key of the item to change (update)")
    rationale: str = Field(min_length=1)


# ----------------------------------------------------------------------------- helpers


def _log_served(c: Call, rows: list[tuple[uuid.UUID, float | None]]) -> None:
    seen: set[uuid.UUID] = set()
    rank = 0
    for vid, score in rows:
        if vid in seen:
            continue
        seen.add(vid)
        rank += 1
        c.s.add(
            ServedContext(
                tool_call_id=c.ensure_tool_call(), version_id=vid, rank=rank, score=score
            )
        )
    c.s.flush()
    c.result_meta["served"] = len(seen)


def _signal(
    c: Call,
    kind: str,
    payload: dict[str, Any],
    *,
    source: str = "agent",
    item_ids: list[uuid.UUID] | None = None,
    asset_ids: list[uuid.UUID] | None = None,
) -> int:
    assert c.session is not None
    sig = Signal(
        workspace_id=c.ctx.principal.workspace_id,
        session_id=c.session.id,
        user_id=c.ctx.principal.user_id,
        kind=kind,
        source=source,
        payload=payload,
        item_ids=item_ids or [],
        asset_ids=asset_ids or [],
    )
    c.s.add(sig)
    c.s.flush()
    c.result_meta.setdefault("signals", []).append(kind)
    return sig.id


def _session_item_ids(s: OrmSession, session_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        s.scalars(
            select(ContextVersion.item_id)
            .join(ServedContext, ServedContext.version_id == ContextVersion.id)
            .join(ToolCall, ToolCall.id == ServedContext.tool_call_id)
            .where(ToolCall.session_id == session_id)
            .distinct()
        )
    )


def _session_release(c: Call) -> uuid.UUID | None:
    """The release pinned for this call (evals), else ``None`` = latest published."""
    return c.ctx.release_id


def _release_published(c: Call) -> Any:
    """(item, version) rows visible to this call: the pinned release, else published."""
    return release_items_stmt(c.ctx.release_id)


# ----------------------------------------------------------------------------- handlers


def start_analysis(c: Call) -> dict[str, Any]:
    a: StartArgs = c.args
    wid = c.ctx.principal.workspace_id
    rid = resolve_release(c.s, wid, c.ctx.release_id)
    sess = SessionRow(
        workspace_id=wid,
        user_id=c.ctx.principal.user_id,
        client=c.ctx.client_name or a.client_hint,
        client_version=c.ctx.client_version,
        mcp_session_id=c.ctx.mcp_session_id,
        release_id=rid,
        question=a.question,
        eval_run_id=c.ctx.eval_run_id,
    )
    c.s.add(sess)
    c.s.flush()
    c.session = sess
    always = always_on_entries(c.s, rid, c.ctx.principal.user_id)
    c.ensure_tool_call()
    _log_served(c, [(e.version_id, None) for e in always])
    seq = c.s.scalar(select(Release.seq).where(Release.id == rid)) if rid else None
    return {
        "session_id": str(sess.id),
        "release": seq,
        "always_on_context": "\n\n".join(f"{e.tag}\n{e.text}" for e in always)
        or "(no workspace rules published)",
        "next": "Call get_context_pack(session_id, question) before writing SQL; finish with "
        "report_answer.",
    }


def search_context_tool(c: Call) -> dict[str, Any]:
    a: SearchArgs = c.args
    hits = search_context(
        c.s, a.query, types=a.types, k=a.k, embedder=c.ctx.embedder,
        release_id=_session_release(c),
    )
    _log_served(c, [(h.version_id, h.score) for h in hits])
    c.result_meta.update(hits=len(hits), query=a.query)
    out: dict[str, Any] = {
        "hits": [
            {"tag": h.tag, "type": h.type, "score": round(h.score, 4), "snippet": h.snippet}
            for h in hits
        ]
    }
    if not hits:
        assert c.session is not None
        _signal(c, "retrieval_miss", {"query": a.query, "question": a.question}, source="implicit")
        out["note"] = (
            "No matching context. If the user's term is undefined, call flag_context_gap."
        )
    return out


def get_context_pack(c: Call) -> str:
    a: PackArgs = c.args
    pack = build_context_pack(
        c.s,
        a.question,
        c.ctx.principal.user_id,
        embedder=c.ctx.embedder,
        budget_tokens=a.budget_tokens,
        release_id=_session_release(c),
        tool_call_id=c.ensure_tool_call(),
    )
    c.result_meta.update(entries=len(pack.entries), tokens=pack.used_tokens)
    return f"<!-- {len(pack.entries)} items, ~{pack.used_tokens} tokens -->\n{pack.text}"


def get_item(c: Call) -> dict[str, Any]:
    a: ItemArgs = c.args
    visible = list(c.s.execute(_release_published(c)))
    found = next(((i, v) for i, v in visible if i.key == a.key), None)
    item, ver = found if found else (None, None)
    if item is None or ver is None:
        keys = [i.key for i, _ in visible]
        close = difflib.get_close_matches(a.key, keys, n=5, cutoff=0.3)
        raise ToolUserError(
            f"No published item with key {a.key!r}. Close keys: {close or 'none'}. "
            "Use search_context to find keys."
        )
    _log_served(c, [(ver.id, None)])
    return {
        "tag": f"[ctx:{item.key}@v{ver.seq}]",
        "type": item.type,
        "body": ver.body,
        "meta": ver.meta,
    }


def _doc_groups(s: OrmSession, release_id: uuid.UUID | None = None) -> dict[uuid.UUID, DocGroup]:
    """Published asset docs by asset, best first: the primary is ``group.primary`` (M7b)."""
    stmt = release_items_stmt(release_id)
    rows = s.execute(stmt.where(ContextItem.type == "asset_doc", ContextItem.scope_kind == "asset"))
    return group_docs(((i, v) for i, v in rows), load_sources(s))


def _table_docs(
    s: OrmSession, wid: uuid.UUID, release_id: uuid.UUID | None = None
) -> list[tuple[Asset, ContextItem | None, ContextVersion | None]]:
    assets = list(
        s.scalars(
            select(Asset)
            .where(Asset.workspace_id == wid, Asset.kind == "table", Asset.dropped_at.is_(None))
            .order_by(Asset.fqn)
        )
    )
    docs = {
        sid: (g.primary.item, g.primary.version) for sid, g in _doc_groups(s, release_id).items()
    }
    return [(a, *(docs.get(a.id) or (None, None))) for a in assets]


def _doc(ver: ContextVersion | None) -> dict[str, Any]:
    if ver is None:
        return {}
    return yaml_item_dict("asset_doc", ver.body, ver.meta)


def list_tables(c: Call) -> dict[str, Any]:
    a: ListArgs = c.args
    rows = _table_docs(c.s, c.ctx.principal.workspace_id, c.ctx.release_id)
    conn_names = {}
    if rows:
        from satchel.store.models import Connection

        conn_names = {x.id: x.name for x in c.s.scalars(select(Connection))}
    out = []
    served: list[tuple[uuid.UUID, float | None]] = []
    for asset, _item, ver in rows:
        doc = _doc(ver)
        cname = conn_names.get(asset.connection_id) if asset.connection_id else None
        if a.connection and cname != a.connection:
            continue
        if a.filter and a.filter.lower() not in asset.fqn.lower():
            continue
        if a.endorsed_only and not doc.get("endorsed"):
            continue
        out.append(
            {
                "table": asset.fqn,
                "connection": cname,
                "endorsed": bool(doc.get("endorsed")),
                "description": doc.get("description"),
                "documented": ver is not None,
            }
        )
        if ver is not None:
            served.append((ver.id, None))
    _log_served(c, served)
    c.result_meta["tables"] = len(out)
    return {"tables": out}


def describe_table(c: Call) -> dict[str, Any]:
    a: DescribeArgs = c.args
    rows = _table_docs(c.s, c.ctx.principal.workspace_id, c.ctx.release_id)
    want = a.table.lower()
    exact = [r for r in rows if r[0].fqn.lower() == want]
    found = exact or [r for r in rows if r[0].fqn.lower().endswith("." + want)]
    if not found:
        names = [r[0].fqn for r in rows]
        close = difflib.get_close_matches(a.table, names, n=5, cutoff=0.3)
        raise ToolUserError(
            f"Unknown table {a.table!r}. Did you mean: {close or names[:10]}? "
            "Call list_tables to see documented tables."
        )
    if len(found) > 1:
        raise ToolUserError(
            f"Ambiguous table {a.table!r}: {[r[0].fqn for r in found]}. Use the full name."
        )
    asset, item, ver = found[0]
    doc = _doc(ver)
    served: list[tuple[uuid.UUID, float | None]] = []
    caveats = []
    if ver is not None:
        served.append((ver.id, None))
    for key in doc.get("caveats") or []:
        pair = next(
            ((i, v) for i, v in c.s.execute(_release_published(c)) if i.key == str(key)), None
        )
        cit, cver = pair if pair else (None, None)
        if cit is not None and cver is not None:
            caveats.append({"tag": f"[ctx:{cit.key}@v{cver.seq}]", "text": cver.body.strip()})
            served.append((cver.id, None))
        else:
            caveats.append({"key": key, "text": "(caveat item not found)"})
    _log_served(c, served)
    live = [
        {"name": col.fqn.rsplit(".", 1)[1], "type": col.data_type}
        for col in c.s.scalars(
            select(Asset).where(Asset.parent_id == asset.id, Asset.kind == "column",
                                Asset.dropped_at.is_(None)).order_by(Asset.fqn)
        )
    ]
    out: dict[str, Any] = {
        "table": asset.fqn,
        "tag": f"[ctx:{item.key}@v{ver.seq}]" if item and ver else None,
        "documented": ver is not None,
        "doc": doc,
        "caveats": caveats,
    }
    group = _doc_groups(c.s, c.ctx.release_id).get(asset.id)
    if group is not None and group.overlapping:  # several sources describe it; this is the primary
        out["also_described_by"] = [describe(m.ref) for m in group.others]
        out["also_described_facts"] = fold_facts(group)
    if live:
        out["columns_live"] = live
    else:
        out["note"] = "Not crawled yet: column types are unavailable."
    return out


_GAP_RE = re.compile(
    r"no (official |formal |governed |explicit )?definition|not defined|undefined|"
    r"isn't defined|not documented|couldn'?t find|could not find|not found in|"
    r"no (matching )?context|(definition|context|glossary)[^.]{0,40}(missing|absent)",
    re.I,
)


def report_answer(c: Call) -> dict[str, Any]:
    a: ReportArgs = c.args
    assert c.session is not None
    items = _session_item_ids(c.s, c.session.id)
    c.session.status = "answered"
    payload = {
        "confidence": a.confidence,
        "caveats": a.caveats,
        "answer_summary": a.answer_summary,
        "metrics_used": a.metrics_used,
        "tables_used": a.tables_used,
    }
    if a.confidence == "low" or a.caveats:
        _signal(c, "low_confidence", payload, item_ids=items)
    if a.caveats and _GAP_RE.search(" ".join(a.caveats)):
        already = c.s.scalar(
            select(Signal.id).where(
                Signal.session_id == c.session.id, Signal.kind == "gap_flagged"
            ).limit(1)
        )
        if already is None:
            _signal(
                c,
                "gap_flagged",
                {**payload, "mined_from": "report_answer.caveats"},
                item_ids=items,
            )
    return {"recorded": True, "signals": c.result_meta.get("signals", [])}


def flag_context_gap(c: Call) -> dict[str, Any]:
    a: GapArgs = c.args
    assert c.session is not None
    _signal(
        c,
        "gap_flagged",
        {"kind": a.kind, "what_was_missing": a.what_was_missing, "why": a.why},
        item_ids=_session_item_ids(c.s, c.session.id),
    )
    return {"recorded": True, "note": "Thanks. The data team will see this gap."}


def record_user_feedback(c: Call) -> dict[str, Any]:
    a: FeedbackArgs = c.args
    assert c.session is not None
    rating = 1 if a.sentiment == "positive" else -1
    last_ctx = c.s.scalar(
        select(ToolCall.id)
        .where(
            ToolCall.session_id == c.session.id,
            ToolCall.tool.in_(["search_context", "get_context_pack"]),
        )
        .order_by(ToolCall.id.desc())
        .limit(1)
    )
    c.s.add(
        Feedback(
            session_id=c.session.id,
            tool_call_id=last_ctx,
            user_id=c.ctx.principal.user_id,
            rating=rating,
            comment=a.comment,
        )
    )
    if rating < 0:
        _signal(
            c,
            "user_doubt",
            {"sentiment": a.sentiment, "comment": a.comment},
            item_ids=_session_item_ids(c.s, c.session.id),
        )
    c.s.flush()
    return {"recorded": True}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "item"


_MARKDOWN_TYPES = {"workspace_rule", "guide", "caveat", "user_memory", "semantic_model"}


def propose_context_change(c: Call) -> dict[str, Any]:
    from satchel.sync.importer import content_hash

    a: ProposeArgs = c.args
    assert c.session is not None
    meta = dict(a.meta or {})
    try:
        enriched_meta = normalized_meta(a.type, a.body, meta)
    except ItemValidationError as e:
        raise ToolUserError(
            f"Invalid proposal: {e}. Fix the body/meta for item type {a.type!r} and retry."
        ) from None
    if a.op == "update_item" and not a.target:
        raise ToolUserError("op=update_item requires target (the key of the item to change).")
    ext = ".md" if a.type in _MARKDOWN_TYPES else ".yaml"
    key = f"proposals/{_slug(a.target or a.type)}-{uuid.uuid4().hex[:8]}{ext}"
    wid = c.ctx.principal.workspace_id
    item = ContextItem(
        workspace_id=wid,
        type=a.type,
        key=key,
        scope_kind="workspace",
        state="proposed",
        managed_by="ui",
        owner_user_id=c.ctx.principal.user_id,
    )
    c.s.add(item)
    c.s.flush()
    ver = ContextVersion(
        item_id=item.id,
        seq=1,
        body=a.body,
        meta=enriched_meta,
        content_hash=content_hash(a.body, enriched_meta),
        author_user_id=c.ctx.principal.user_id,
        provenance={
            "source": "agent",
            "ref": str(c.session.id),
            "op": a.op,
            "target": a.target,
            "rationale": a.rationale,
        },
    )
    c.s.add(ver)
    c.s.flush()
    item.current_version_id = ver.id  # never published_version_id: a curator decides
    c.s.flush()
    c.result_meta["proposal_key"] = key
    return {
        "proposal_key": key,
        "state": "proposed",
        "note": "Saved for curator review. It is not live context.",
    }


def build_tools() -> list[ToolDef]:
    from satchel.tools.data_tools import build_data_tools

    return [*_context_tools(), *build_data_tools()]


def _context_tools() -> list[ToolDef]:
    return [
        ToolDef("start_analysis", text.D_START, StartArgs, start_analysis, requires_session=False),
        ToolDef("search_context", text.D_SEARCH, SearchArgs, search_context_tool,
                requires_question=True),
        ToolDef("get_context_pack", text.D_PACK, PackArgs, get_context_pack,
                requires_question=True),
        ToolDef("get_item", text.D_ITEM, ItemArgs, get_item),
        ToolDef("list_tables", text.D_LIST, ListArgs, list_tables),
        ToolDef("describe_table", text.D_DESCRIBE, DescribeArgs, describe_table,
                requires_question=True),
        ToolDef("report_answer", text.D_REPORT, ReportArgs, report_answer),
        ToolDef("flag_context_gap", text.D_GAP, GapArgs, flag_context_gap),
        ToolDef("record_user_feedback", text.D_FEEDBACK, FeedbackArgs, record_user_feedback),
        ToolDef("propose_context_change", text.D_PROPOSE, ProposeArgs, propose_context_change,
                min_role="analyst"),
    ]
