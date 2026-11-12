"""Applying an accepted suggestion change (design §7.3): the same draft path as the workbench
and accepted agent proposals. Nothing here publishes; the result is a draft version (or draft
item) that shows up under ``/v1/changes`` and goes through preview -> publish.

Changes whose target item is ``managed_by='git'`` are not written. They return a ``manual``
result (the proposed body, to be edited in the context repository) and the change is marked
``applied`` by the caller; opening PRs from the UI is a later milestone.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from satchel.core.item_types import ITEM_TYPES
from satchel.curation.drafts import ItemValidationError, add_version, validate_item
from satchel.llm.embeddings import EmbeddingProvider
from satchel.store.models import ContextItem, Suggestion, SuggestionChange

MARKDOWN_TYPES = {"workspace_rule", "guide", "caveat", "semantic_model"}
KEY_PREFIX = {
    "guide": "guides", "glossary_term": "glossary", "golden_query": "queries",
    "caveat": "caveats", "asset_doc": "assets", "endorsement": "endorsements",
    "semantic_model": "semantic", "workspace_rule": "rules",
}


class ApplyError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def slugify(s: str, n: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n].strip("-") or "item"


def default_key(item_type: str, name: str) -> str:
    ext = ".md" if item_type in MARKDOWN_TYPES else ".yaml"
    return f"{KEY_PREFIX.get(item_type, 'misc')}/{slugify(name)}{ext}"


def clean_key(key: str) -> str | None:
    """A relative, traversal-free item key, or ``None`` when it is unusable."""
    k = key.strip().replace("\\", "/")
    parts = k.split("/")
    if not k or k.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return None
    return k


def unique_key(s: Session, key: str) -> str:
    """``key``, or ``key`` with a numeric suffix before the extension when it is taken."""
    if s.scalars(select(ContextItem.id).where(ContextItem.key == key)).first() is None:
        return key
    stem, dot, ext = key.rpartition(".")
    stem, ext = (stem, "." + ext) if dot else (key, "")
    for n in range(2, 100):
        cand = f"{stem}-{n}{ext}"
        if s.scalars(select(ContextItem.id).where(ContextItem.key == cand)).first() is None:
            return cand
    return f"{stem}-{uuid.uuid4().hex[:6]}{ext}"


def check_valid(item_type: str, body: str, meta: dict[str, Any]) -> None:
    if item_type not in ITEM_TYPES:
        raise ItemValidationError(f"unknown item type: {item_type}")
    validate_item(item_type, body, meta)


def apply_change(
    s: Session, *, workspace_id: uuid.UUID, user_id: uuid.UUID | None, sug: Suggestion,
    change: SuggestionChange, embedder: EmbeddingProvider, body: str | None = None,
    meta: dict[str, Any] | None = None, key: str | None = None,
) -> dict[str, Any]:
    """Create the draft for ``change``. Returns the change's ``result`` payload."""
    new_body = change.proposed_body if body is None else body
    new_meta = dict(change.proposed_meta if meta is None else meta)
    prov = {"source": "suggestion", "ref": str(sug.id), "change": str(change.id),
            "rationale": change.rationale}

    if change.target_item_id is not None:
        target = s.get(ContextItem, change.target_item_id)
        if target is None:
            raise ApplyError(409, "the target item no longer exists")
        if target.managed_by in ("git", "source"):
            where = (
                "in git. Edit it in the context repository and sync"
                if target.managed_by == "git"
                else "by an upstream source. Change it there and sync, or add the note to its "
                     "analyst overlay"
            )
            return {
                "manual": True, "item_key": target.key,
                "message": f"{target.key} is managed {where}; the proposed content is returned "
                           "here for copying.",
                "proposed_body": new_body if change.op != "deprecate_item" else "",
                "proposed_meta": new_meta,
            }
        if target.state in ("deprecated", "proposed"):
            raise ApplyError(409, f"{target.key} is {target.state} and cannot be changed")
        if change.op == "deprecate_item":
            prior = target.state
            target.state = "deprecated"
            target.updated_at = func.now()
            s.flush()
            return {"item_key": target.key, "deprecated": True, "prior_state": prior}
        try:
            check_valid(target.type, new_body, new_meta)
        except ItemValidationError as e:
            raise ApplyError(422, str(e)) from None
        ver = add_version(s, target, new_body, new_meta, author=user_id, provenance=prov,
                          embedder=embedder)
        return {"item_key": target.key, "seq": ver.seq}

    if change.op == "deprecate_item":
        raise ApplyError(409, "this change has no target item to deprecate")
    item_type = change.item_type
    if item_type is None:
        raise ApplyError(409, "this change has no item type")
    new_key = clean_key(key if key else (change.proposed_key or ""))
    if new_key is None:
        raise ApplyError(422, "key must be a relative path without empty or '..' segments")
    if s.scalars(select(ContextItem.id).where(ContextItem.key == new_key)).first():
        raise ApplyError(409, f"an item with key {new_key!r} already exists; pick another key")
    try:
        check_valid(item_type, new_body, new_meta)
    except ItemValidationError as e:
        raise ApplyError(422, str(e)) from None
    scoped = change.target_asset_id is not None and item_type in ("asset_doc", "endorsement")
    item = ContextItem(
        workspace_id=workspace_id, type=item_type, key=new_key,
        scope_kind="asset" if scoped else "workspace",
        scope_id=change.target_asset_id if scoped else None,
        state="draft", managed_by="ui", owner_user_id=user_id,
    )
    s.add(item)
    s.flush()
    ver = add_version(s, item, new_body, new_meta, author=user_id, provenance=prov,
                      embedder=embedder)
    return {"item_key": item.key, "seq": ver.seq, "created": True}
