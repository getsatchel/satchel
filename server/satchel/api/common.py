"""Helpers shared by the REST routers: diffs, item summaries with usage, audit, guards."""

from __future__ import annotations

import difflib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import case, distinct, func, select
from sqlalchemy.orm import Session

from satchel.api.auth import Caller
from satchel.api.schemas import DiffOut, ItemSummary, ItemUsage
from satchel.audit import record as record_audit
from satchel.store.models import (
    ContextItem,
    ContextVersion,
    Feedback,
    ServedContext,
    ToolCall,
)
from satchel.store.scopes import tool_call_not_eval

USAGE_DAYS = 30


def escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def audit(s: Session, who: Caller, action: str, target: dict[str, Any]) -> None:
    record_audit(s, who.workspace_id, who.user_id, action, target)


def get_item(s: Session, key: str) -> ContextItem:
    item = s.scalars(select(ContextItem).where(ContextItem.key == key)).first()
    if item is None:
        raise HTTPException(404, f"no such item: {key}")
    return item


def require_ui_managed(item: ContextItem) -> None:
    if item.managed_by == "source":
        raise HTTPException(
            409,
            f"{item.key} is synced from an upstream source ({item.source_key}); its source "
            "block is read-only here. Add notes with PUT /v1/items/{key}/overlay, or change it "
            "upstream and sync.",
        )
    if item.managed_by == "git":
        raise HTTPException(
            409,
            f"{item.key} is managed by git and is read-only here. Change it in the context "
            "repository and sync it; opening pull requests from the UI is a later milestone.",
        )


def get_version(s: Session, item: ContextItem, seq: int) -> ContextVersion:
    v = s.scalars(
        select(ContextVersion).where(ContextVersion.item_id == item.id, ContextVersion.seq == seq)
    ).first()
    if v is None:
        raise HTTPException(404, f"{item.key} has no version {seq}")
    return v


def diff_bodies(
    key: str,
    a_text: str,
    b_text: str,
    a_label: str,
    b_label: str,
    *,
    from_seq: int | None = None,
    to_seq: int | None = None,
    meta_changed: bool = False,
) -> DiffOut:
    lines = list(
        difflib.unified_diff(
            a_text.splitlines(), b_text.splitlines(), fromfile=a_label, tofile=b_label,
            lineterm="",
        )
    )
    adds = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return DiffOut(
        key=key, from_seq=from_seq, to_seq=to_seq, unified="\n".join(lines),
        additions=adds, deletions=dels, meta_changed=meta_changed,
    )


def make_diff(key: str, a: ContextVersion | None, b: ContextVersion | None) -> DiffOut:
    """Unified diff of two versions' bodies (``None`` = empty)."""
    return diff_bodies(
        key,
        a.body if a else "",
        b.body if b else "",
        f"{key}@v{a.seq}" if a else "/dev/null",
        f"{key}@v{b.seq}" if b else "/dev/null",
        from_seq=a.seq if a else None,
        to_seq=b.seq if b else None,
        meta_changed=bool(a and b and a.meta != b.meta),
    )


def has_changes(i: ContextItem) -> bool:
    if i.state == "deprecated":
        return i.published_version_id is not None
    if i.state == "proposed":
        return False
    return i.current_version_id is not None and i.current_version_id != i.published_version_id


def usage_for(s: Session, item_ids: list[uuid.UUID]) -> dict[uuid.UUID, ItemUsage]:
    if not item_ids:
        return {}
    since = datetime.now(UTC) - timedelta(days=USAGE_DAYS)
    out = {i: ItemUsage() for i in item_ids}
    served = (
        select(ContextVersion.item_id, func.count())
        .select_from(ServedContext)
        .join(ToolCall, ToolCall.id == ServedContext.tool_call_id)
        .join(ContextVersion, ContextVersion.id == ServedContext.version_id)
        .where(ContextVersion.item_id.in_(item_ids), ToolCall.created_at >= since,
               tool_call_not_eval())
        .group_by(ContextVersion.item_id)
    )
    for item_id, n in s.execute(served):
        out[item_id].served_30d = n
    thumbs = (
        select(
            ContextVersion.item_id,
            func.count(distinct(case((Feedback.rating > 0, Feedback.id)))),
            func.count(distinct(case((Feedback.rating < 0, Feedback.id)))),
        )
        .select_from(Feedback)
        .join(ServedContext, ServedContext.tool_call_id == Feedback.tool_call_id)
        .join(ToolCall, ToolCall.id == ServedContext.tool_call_id)
        .join(ContextVersion, ContextVersion.id == ServedContext.version_id)
        .where(ContextVersion.item_id.in_(item_ids), ToolCall.created_at >= since,
               tool_call_not_eval())
        .group_by(ContextVersion.item_id)
    )
    for item_id, up, down in s.execute(thumbs):
        out[item_id].thumbs_up, out[item_id].thumbs_down = up, down
    return out


def summaries(s: Session, items: list[ContextItem]) -> list[ItemSummary]:
    ids = [v for i in items for v in (i.current_version_id, i.published_version_id) if v]
    seqs: dict[uuid.UUID, int] = {}
    if ids:
        seqs = {
            r[0]: r[1]
            for r in s.execute(
                select(ContextVersion.id, ContextVersion.seq).where(ContextVersion.id.in_(ids))
            )
        }
    usage = usage_for(s, [i.id for i in items])
    return [
        ItemSummary(
            key=i.key,
            type=i.type,
            state=i.state,
            managed_by=i.managed_by,
            source_key=i.source_key,
            scope_kind=i.scope_kind,
            current_seq=seqs.get(i.current_version_id) if i.current_version_id else None,
            published_seq=seqs.get(i.published_version_id) if i.published_version_id else None,
            has_changes=has_changes(i),
            updated_at=i.updated_at,
            usage=usage[i.id],
        )
        for i in items
    ]
