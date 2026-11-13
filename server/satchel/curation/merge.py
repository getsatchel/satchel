"""Merge open suggestions that propose exactly the same changes.

Two clusters (or two hygiene findings) can be drafted into the same edit: the same operation on
the same item, with the same proposed text. A curator would then review it twice. After each
drafting pass, :func:`merge_duplicates` keeps the oldest such suggestion and folds the others
into it: their signals become its evidence, and the duplicates are deleted.

Only suggestions nobody has acted on are merged: open, drafted, and with every change still
pending. A duplicate is deleted rather than dismissed, because dismissed suggestions (and their
reasons) are shown to the drafter as rejected ideas, which a merge is not.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import defaultdict

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from satchel.curation.gaps import refresh_stats
from satchel.store.models import Signal, Suggestion, SuggestionChange, SuggestionSignal

log = logging.getLogger(__name__)


def _change_sig(c: SuggestionChange) -> str:
    """What a change does, independent of which suggestion holds it or why."""
    return hashlib.sha256(json.dumps([
        c.op, c.item_type, c.proposed_key, str(c.target_item_id or ""),
        str(c.target_asset_id or ""), c.proposed_body, c.proposed_meta or {},
    ], sort_keys=True, default=str).encode()).hexdigest()


def merge_duplicates(s: Session, workspace_id: uuid.UUID) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Fold open suggestions with identical pending changes into the oldest one.

    Returns ``(merged, kept)`` id pairs."""
    open_ids = list(s.scalars(
        select(Suggestion.id).where(
            Suggestion.workspace_id == workspace_id, Suggestion.status == "open",
            Suggestion.drafted_at.is_not(None),
        ).order_by(Suggestion.created_at, Suggestion.id)
    ))
    if len(open_ids) < 2:
        return []
    changes: dict[uuid.UUID, list[SuggestionChange]] = defaultdict(list)
    rows = s.scalars(select(SuggestionChange).where(SuggestionChange.suggestion_id.in_(open_ids)))
    for c in rows:
        changes[c.suggestion_id].append(c)

    by_sig: dict[frozenset[str], list[uuid.UUID]] = defaultdict(list)
    for sid in open_ids:  # oldest first, so the first of each group is kept
        cs = changes.get(sid, [])
        if not cs or any(c.status != "pending" for c in cs):
            continue  # nothing proposed, or a curator already decided part of it
        by_sig[frozenset(_change_sig(c) for c in cs)].append(sid)

    merged: list[tuple[uuid.UUID, uuid.UUID]] = []
    for group in by_sig.values():
        keep, *dupes = group
        for dup in dupes:
            _fold(s, dup, keep)
            merged.append((dup, keep))
    if merged:
        for keep_id in {k for _, k in merged}:
            kept = s.get(Suggestion, keep_id)
            if kept is not None:
                refresh_stats(s, kept)
        log.info("merged %d duplicate suggestion(s)", len(merged))
    return merged


def _fold(s: Session, dup: uuid.UUID, keep: uuid.UUID) -> None:
    """Move ``dup``'s signals to ``keep`` and delete ``dup``."""
    have = set(s.scalars(select(SuggestionSignal.signal_id).where(
        SuggestionSignal.suggestion_id == keep)))
    for sig_id in s.scalars(select(SuggestionSignal.signal_id).where(
            SuggestionSignal.suggestion_id == dup)):
        if sig_id not in have:
            s.add(SuggestionSignal(suggestion_id=keep, signal_id=sig_id))
    s.flush()
    s.execute(update(Signal).where(Signal.clustered_into == dup).values(clustered_into=keep))
    s.execute(delete(SuggestionSignal).where(SuggestionSignal.suggestion_id == dup))
    s.execute(delete(SuggestionChange).where(SuggestionChange.suggestion_id == dup))
    s.execute(delete(Suggestion).where(Suggestion.id == dup))
    s.flush()
