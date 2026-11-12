"""Hygiene jobs (design §3 hygiene list, §6.2): look at the context itself rather than at
sessions, and emit ``source='hygiene'`` signals that the gap detector turns into suggestions
(hygiene signals bypass the user/session/signal thresholds).

- ``stale_reference``: an item is linked to (or scoped to) an asset with ``dropped_at`` set.
- ``dead_item``: a published item that was never served in ``dead_item_days`` (60).
- ``duplicate``: two published items of the same type whose mean chunk embeddings have
  cosine >= ``duplicate_similarity`` (0.95).
- ``contradiction``: an LLM pairwise check over the most similar item pairs; only runs when a
  chat provider is configured, at most ``contradiction_pairs`` pairs per run, and a pair whose
  two versions were already checked is not checked again.

Run with an admin session (satchel_worker); every query
filters by ``workspace_id`` explicitly. Signals carry ``payload.dedupe = "<kind>:<target>"`` and
are not re-emitted for the same target within ``REPEAT_DAYS``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from satchel.curation.config import CurationConfig
from satchel.curation.gaps import unit_vector
from satchel.llm.chat import ChatError, ChatProvider
from satchel.llm.embeddings import EmbeddingProvider
from satchel.store.models import (
    Asset,
    ContextChunk,
    ContextEntityLink,
    ContextItem,
    ContextVersion,
    ServedContext,
    Signal,
    ToolCall,
    Workspace,
)
from satchel.store.scopes import tool_call_not_eval

log = logging.getLogger("satchel.curation")

REPEAT_DAYS = 90
# Served implicitly or not text: never "dead".
DEAD_EXEMPT_TYPES = {"workspace_rule", "endorsement", "semantic_model"}
MAX_CHECKED_PAIRS = 5000
MAX_PAIR_CHARS = 3000
CONTRADICTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conflict": {"type": "boolean"},
        "explanation": {"type": "string"},
    },
    "required": ["conflict", "explanation"],
}
CONTRADICTION_SYSTEM = (
    "You review two pieces of context from the knowledge base that an analytics agent reads "
    "before answering data questions. Decide whether they make conflicting factual claims "
    "about the same thing: different definitions, incompatible numbers or filters, or opposite "
    "instructions. Different wording, different scope, or one being more detailed is NOT a "
    "conflict. Answer conflict=false unless you can point to the two conflicting statements. "
    "When there is a conflict, explain it in one or two sentences that quote both statements."
)


@dataclass
class HygieneReport:
    emitted: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def bump(self, kind: str) -> None:
        self.emitted[kind] = self.emitted.get(kind, 0) + 1

    def line(self) -> str:
        body = ", ".join(f"{k}={n}" for k, n in sorted(self.emitted.items())) or "no findings"
        return body + ("; " + "; ".join(self.notes) if self.notes else "")


def _emit(
    s: Session, workspace_id: uuid.UUID, report: HygieneReport, kind: str, target: str,
    text: str, extra: dict[str, Any], *, item_ids: list[uuid.UUID] | None = None,
    asset_ids: list[uuid.UUID] | None = None, now: datetime,
) -> bool:
    dedupe = f"{kind}:{target}"
    seen = s.scalar(
        select(Signal.id).where(
            Signal.workspace_id == workspace_id, Signal.source == "hygiene",
            Signal.kind == kind, Signal.payload["dedupe"].astext == dedupe,
            Signal.created_at >= now - timedelta(days=REPEAT_DAYS),
        ).limit(1)
    )
    if seen is not None:
        return False
    s.add(Signal(
        workspace_id=workspace_id, kind=kind, source="hygiene", user_id=None, session_id=None,
        payload={"text": text, "target": target, "dedupe": dedupe, **extra},
        item_ids=item_ids or [], asset_ids=asset_ids or [],
    ))
    s.flush()
    report.bump(kind)
    return True


emit_finding = _emit  # public: also used by the source sync (overlay_out_of_sync, ...)


# ----------------------------------------------------------------------------- stale / dead


def check_stale_references(
    s: Session, workspace_id: uuid.UUID, report: HygieneReport, now: datetime
) -> None:
    live = ContextItem.state.in_(["published", "draft"])
    by_item: dict[uuid.UUID, tuple[ContextItem, dict[uuid.UUID, Asset]]] = {}
    linked = s.execute(
        select(ContextItem, Asset)
        .join(ContextVersion, ContextVersion.item_id == ContextItem.id)
        .join(ContextEntityLink, ContextEntityLink.version_id == ContextVersion.id)
        .join(Asset, Asset.id == ContextEntityLink.asset_id)
        .where(
            ContextItem.workspace_id == workspace_id, live, Asset.dropped_at.is_not(None),
            or_(ContextVersion.id == ContextItem.published_version_id,
                ContextVersion.id == ContextItem.current_version_id),
        )
    )
    scoped = s.execute(
        select(ContextItem, Asset)
        .join(Asset, Asset.id == ContextItem.scope_id)
        .where(ContextItem.workspace_id == workspace_id, live,
               ContextItem.scope_kind == "asset", Asset.dropped_at.is_not(None))
    )
    for item, asset in [*linked, *scoped]:
        by_item.setdefault(item.id, (item, {}))[1][asset.id] = asset
    for item, assets in by_item.values():
        fqns = sorted(a.fqn for a in assets.values())
        _emit(
            s, workspace_id, report, "stale_reference", item.key,
            f"{item.key} refers to dropped asset(s): {', '.join(fqns)}",
            {"dropped_assets": fqns}, item_ids=[item.id], asset_ids=list(assets), now=now,
        )


def check_dead_items(
    s: Session, workspace_id: uuid.UUID, cfg: CurationConfig, report: HygieneReport,
    now: datetime,
) -> None:
    cutoff = now - timedelta(days=cfg.dead_item_days)
    served = (
        select(ContextVersion.item_id)
        .join(ServedContext, ServedContext.version_id == ContextVersion.id)
        .join(ToolCall, ToolCall.id == ServedContext.tool_call_id)
        .where(ToolCall.created_at >= cutoff, tool_call_not_eval())
    )
    dead = s.scalars(
        select(ContextItem).where(
            ContextItem.workspace_id == workspace_id, ContextItem.state == "published",
            ContextItem.scope_kind != "user", ContextItem.type.notin_(DEAD_EXEMPT_TYPES),
            ContextItem.managed_by != "source",  # the upstream tool owns the lifecycle
            ContextItem.created_at < cutoff, ContextItem.id.notin_(served),
        ).order_by(ContextItem.key)
    )
    for item in dead:
        _emit(
            s, workspace_id, report, "dead_item", item.key,
            f"{item.key} was not served in the last {cfg.dead_item_days} days",
            {"days": cfg.dead_item_days}, item_ids=[item.id], now=now,
        )


# ----------------------------------------------------------------------------- duplicates


@dataclass
class _ItemVec:
    item: ContextItem
    version_id: uuid.UUID
    model: str
    vec: np.ndarray


def _published_vectors(s: Session, workspace_id: uuid.UUID) -> list[_ItemVec]:
    """Mean chunk embedding of every live item's published version (same-model chunks only)."""
    rows = s.execute(
        select(ContextItem, ContextChunk.version_id, ContextChunk.embedding,
               ContextChunk.embedding_model)
        .join(ContextChunk, ContextChunk.version_id == ContextItem.published_version_id)
        .where(ContextItem.workspace_id == workspace_id, ContextItem.state == "published",
               ContextItem.scope_kind != "user", ContextChunk.embedding.is_not(None))
        .order_by(ContextItem.key, ContextChunk.ord)
    )
    acc: dict[uuid.UUID, tuple[ContextItem, uuid.UUID, str, list[np.ndarray]]] = {}
    for item, ver_id, emb, model in rows:
        v = unit_vector(emb)
        if v is None:
            continue
        entry = acc.setdefault(item.id, (item, ver_id, model or "", []))
        entry[3].append(v)
    out: list[_ItemVec] = []
    for item, ver_id, model, vecs in acc.values():
        mean = unit_vector(np.mean(vecs, axis=0))
        if mean is not None:
            out.append(_ItemVec(item, ver_id, model, mean))
    return out


def _pairs(vectors: list[_ItemVec], min_sim: float) -> list[tuple[float, _ItemVec, _ItemVec]]:
    """Same-model pairs with cosine similarity >= ``min_sim``, most similar first."""
    out: list[tuple[float, _ItemVec, _ItemVec]] = []
    by_model: dict[str, list[_ItemVec]] = {}
    for v in vectors:
        by_model.setdefault(v.model, []).append(v)
    for group in by_model.values():
        if len(group) < 2:
            continue
        mat = np.stack([g.vec for g in group])
        sims = mat @ mat.T
        ii, jj = np.triu_indices(len(group), k=1)
        for i, j in zip(ii[sims[ii, jj] >= min_sim], jj[sims[ii, jj] >= min_sim], strict=True):
            out.append((float(sims[i, j]), group[i], group[j]))
    out.sort(key=lambda t: (-t[0], t[1].item.key, t[2].item.key))
    return out


def _pair_target(a: _ItemVec, b: _ItemVec) -> str:
    return "|".join(sorted((a.item.key, b.item.key)))


def check_duplicates(
    s: Session, workspace_id: uuid.UUID, cfg: CurationConfig, report: HygieneReport,
    pairs: list[tuple[float, _ItemVec, _ItemVec]], now: datetime,
) -> set[str]:
    """Emit ``duplicate`` signals; returns the targets of pairs treated as duplicates."""
    dups: set[str] = set()
    for sim, a, b in pairs:
        if sim < cfg.duplicate_similarity:
            break
        if a.item.type != b.item.type:
            continue
        target = _pair_target(a, b)
        dups.add(target)
        _emit(
            s, workspace_id, report, "duplicate", target,
            f"{a.item.key} and {b.item.key} are near-duplicates (cosine {sim:.2f})",
            {"keys": sorted((a.item.key, b.item.key)), "similarity": round(sim, 3)},
            item_ids=[a.item.id, b.item.id], now=now,
        )
    return dups


# ----------------------------------------------------------------------------- contradictions


def _body(s: Session, version_id: uuid.UUID) -> str:
    v = s.get(ContextVersion, version_id)
    return (v.body if v else "")[:MAX_PAIR_CHARS]


def check_contradictions(
    s: Session, workspace_id: uuid.UUID, cfg: CurationConfig, report: HygieneReport,
    pairs: list[tuple[float, _ItemVec, _ItemVec]], skip: set[str], chat: ChatProvider | None,
    now: datetime,
) -> None:
    if cfg.contradiction_pairs <= 0:
        return
    if chat is None:
        report.notes.append("contradiction check skipped: no chat provider configured")
        return
    ws = s.get(Workspace, workspace_id)
    assert ws is not None
    state: dict[str, Any] = dict((ws.settings or {}).get("curation_state") or {})
    checked: dict[str, str] = dict(state.get("contradiction_checked") or {})
    budget = cfg.contradiction_pairs
    for sim, a, b in pairs:
        if sim < cfg.contradiction_min_similarity:
            break
        target = _pair_target(a, b)
        if target in skip:
            continue
        stamp = "|".join(sorted((str(a.version_id), str(b.version_id))))
        if checked.get(target) == stamp:
            continue
        if budget <= 0:
            break
        budget -= 1
        try:
            verdict = chat.complete_json(
                CONTRADICTION_SYSTEM,
                [{"role": "user", "content": (
                    f"ITEM A ({a.item.type}, key {a.item.key}):\n{_body(s, a.version_id)}\n\n"
                    f"ITEM B ({b.item.type}, key {b.item.key}):\n{_body(s, b.version_id)}"
                )}],
                CONTRADICTION_SCHEMA, max_tokens=600,
            )
        except ChatError as e:
            log.warning("contradiction check failed for %s: %s", target, e)
            report.notes.append(f"contradiction check failed for {target}")
            continue
        checked[target] = stamp
        report.bump("contradiction_pairs_checked")
        if verdict.get("conflict") is True:
            explanation = str(verdict.get("explanation") or "").strip() or "conflicting claims"
            _emit(
                s, workspace_id, report, "contradiction", target,
                f"{a.item.key} and {b.item.key} conflict: {explanation}",
                {"keys": sorted((a.item.key, b.item.key)), "similarity": round(sim, 3),
                 "explanation": explanation},
                item_ids=[a.item.id, b.item.id], now=now,
            )
    while len(checked) > MAX_CHECKED_PAIRS:
        checked.pop(next(iter(checked)))
    ws.settings = {**(ws.settings or {}),
                   "curation_state": {**state, "contradiction_checked": checked}}
    s.flush()


# ----------------------------------------------------------------------------- entry point


def run_hygiene(
    s: Session, workspace_id: uuid.UUID, cfg: CurationConfig, embedder: EmbeddingProvider,
    chat: ChatProvider | None, *, now: datetime | None = None,
) -> HygieneReport:
    """Run every hygiene check for one workspace. ``s`` must be an admin session
    (satchel_worker)."""
    now = now or datetime.now(UTC)
    report = HygieneReport()
    check_stale_references(s, workspace_id, report, now)
    check_dead_items(s, workspace_id, cfg, report, now)
    pairs = _pairs(
        _published_vectors(s, workspace_id),
        min(cfg.duplicate_similarity, cfg.contradiction_min_similarity),
    )
    dups = check_duplicates(s, workspace_id, cfg, report, pairs, now)
    check_contradictions(s, workspace_id, cfg, report, pairs, dups, chat, now)
    log.info("hygiene: %s", report.line())
    return report
