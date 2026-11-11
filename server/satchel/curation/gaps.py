"""Gap detection (design §7.1): cluster unclustered signals into suggestions.

1. Load unclustered signals from the last ``window_days`` (default 30).
2. Behavioural signals are clustered by embedding similarity. Greedy centroid clustering with
   one refinement pass, numpy only: a signal joins the cluster whose centroid it is most
   similar to when ``cosine + bonus >= similarity`` (default 0.72), where the bonus is +0.10
   for a shared asset and +0.03 for a shared context item. Clusters that share an asset and
   whose centroids are at least ``merge_similarity`` (0.8) similar are merged.
   (The design names HDBSCAN; for a signal window of a few thousand rows and a clear
   similarity threshold the greedy method is deterministic, dependency-free and easier to
   explain and tune.)
3. A cluster may become a *new* suggestion only if it meets the thresholds (distinct users,
   sessions, signals; workspace-configurable). Hygiene findings and golden-query candidates
   bypass them and are grouped by (kind, target) instead of by embedding.
4. A cluster attaches to an existing open suggestion when more than half of its signals fall
   in that suggestion's neighbourhood (same affinity rule against the suggestion's centroid),
   whether or not it meets the thresholds on its own; otherwise a new suggestion is created.
5. ``signal.clustered_into`` and ``suggestion_signal`` record the membership.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from satchel.curation.config import CurationConfig
from satchel.curation.signal_text import signal_text
from satchel.store.models import Signal, Suggestion, SuggestionSignal
from satchel.store.scopes import signal_not_eval

log = logging.getLogger("satchel.curation")

# A person's own signal (a 👎 / "this is wrong" in the result widget) is worth this many
# agent-reported signals toward ``min_signals``.
HUMAN_WEIGHT = 2
ASSET_BONUS = 0.10
ITEM_BONUS = 0.03
# Signals that bypass the thresholds and are grouped by (kind, target) rather than embedding.
BYPASS_KINDS = {"golden_candidate"}
KIND_LABEL = {
    "gap_flagged": "Context gap",
    "user_doubt": "User doubt",
    "low_confidence": "Low confidence",
    "sql_error": "SQL errors",
    "retry_storm": "Retry storm",
    "retrieval_miss": "Retrieval miss",
    "unendorsed_used": "Unendorsed table used",
    "metric_divergence": "Metric divergence",
    "undocumented_asset": "Undocumented asset",
    "stale_reference": "Stale reference",
    "contradiction": "Contradiction",
    "dead_item": "Dead context",
    "duplicate": "Duplicate context",
    "memory_convergence": "Shared memory",
    "overlay_out_of_sync": "Overlay out of sync",
    "endorsement_candidate": "Endorsement suggested",
    "golden_candidate": "Golden query candidate",
}


@dataclass
class SigRow:
    """The parts of a ``Signal`` the clustering needs (vector normalised to unit length)."""

    id: int
    kind: str
    source: str
    user_id: uuid.UUID | None
    session_id: uuid.UUID | None
    payload: dict[str, Any]
    assets: set[uuid.UUID]
    items: set[uuid.UUID]
    vec: np.ndarray | None


@dataclass
class Cluster:
    members: list[SigRow] = field(default_factory=list)
    total: np.ndarray = field(default_factory=lambda: np.zeros(0))
    assets: set[uuid.UUID] = field(default_factory=set)
    items: set[uuid.UUID] = field(default_factory=set)

    def add(self, sig: SigRow) -> None:
        assert sig.vec is not None
        self.members.append(sig)
        self.total = sig.vec.copy() if self.total.size == 0 else self.total + sig.vec
        self.assets |= sig.assets
        self.items |= sig.items

    @property
    def unit(self) -> np.ndarray:
        n = float(np.linalg.norm(self.total))
        return self.total / n if n else self.total

    @property
    def users(self) -> set[uuid.UUID]:
        return {m.user_id for m in self.members if m.user_id is not None}

    @property
    def sessions(self) -> set[uuid.UUID]:
        return {m.session_id for m in self.members if m.session_id is not None}


@dataclass
class GapReport:
    signals_considered: int = 0
    clusters_found: int = 0
    below_threshold: int = 0
    created: list[uuid.UUID] = field(default_factory=list)
    attached: list[uuid.UUID] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"{self.signals_considered} signals -> {self.clusters_found} clusters; "
            f"{len(self.created)} new suggestion(s), {len(self.attached)} attached to existing, "
            f"{self.below_threshold} below thresholds"
        )


def unit_vector(v: Any) -> np.ndarray | None:
    if v is None:
        return None
    a = np.asarray(v, dtype=np.float64)
    n = float(np.linalg.norm(a))
    return a / n if n else None


def affinity(
    sig: SigRow, centroid: np.ndarray, assets: set[uuid.UUID], items: set[uuid.UUID]
) -> float:
    assert sig.vec is not None
    score = float(sig.vec @ centroid)
    if sig.assets & assets:
        score += ASSET_BONUS
    if sig.items & items:
        score += ITEM_BONUS
    return score


def cluster_signals(sigs: list[SigRow], cfg: CurationConfig) -> list[Cluster]:
    """Greedy centroid clustering, one refinement pass, then a shared-asset merge pass."""
    embedded = [x for x in sigs if x.vec is not None]

    def best(sig: SigRow, pool: list[Cluster]) -> tuple[int, float]:
        top, top_i = -1.0, -1
        for i, c in enumerate(pool):
            a = affinity(sig, c.unit, c.assets, c.items)
            if a > top:
                top, top_i = a, i
        return top_i, top

    clusters: list[Cluster] = []
    home: dict[int, int] = {}
    for sig in embedded:
        i, a = best(sig, clusters)
        if i < 0 or a < cfg.similarity:
            clusters.append(Cluster())
            i = len(clusters) - 1
        clusters[i].add(sig)
        home[sig.id] = i

    # Refinement: re-assign each signal against the final centroids so the result does not
    # depend on arrival order. A signal that fits no centroid keeps its first cluster.
    refined = [Cluster() for _ in clusters]
    for sig in embedded:
        i, a = best(sig, clusters)
        refined[i if a >= cfg.similarity else home[sig.id]].add(sig)
    clusters = [c for c in refined if c.members]

    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                ca, cb = clusters[i], clusters[j]
                if ca.assets & cb.assets and float(ca.unit @ cb.unit) >= cfg.merge_similarity:
                    for m in cb.members:
                        ca.add(m)
                    del clusters[j]
                    merged = True
                    break
            if merged:
                break
    return clusters


def meets_thresholds(c: Cluster, cfg: CurationConfig) -> bool:
    return (
        sum(HUMAN_WEIGHT if m.source == "human" else 1 for m in c.members) >= cfg.min_signals
        and len(c.users) >= cfg.min_users
        and len(c.sessions) >= cfg.min_sessions
    )


def refresh_stats(s: Session, sug: Suggestion) -> None:
    n, users, sessions = s.execute(
        select(
            func.count(),
            func.count(func.distinct(Signal.user_id)),
            func.count(func.distinct(Signal.session_id)),
        )
        .select_from(SuggestionSignal)
        .join(Signal, Signal.id == SuggestionSignal.signal_id)
        .where(SuggestionSignal.suggestion_id == sug.id)
    ).one()
    sug.evidence_count, sug.distinct_users, sug.distinct_sessions = n, users, sessions
    sug.updated_at = func.now()
    s.flush()


def _link(s: Session, sug: Suggestion, sigs: list[SigRow]) -> None:
    ids = [m.id for m in sigs]
    for sid in ids:
        s.add(SuggestionSignal(suggestion_id=sug.id, signal_id=sid))
    s.flush()
    for row in s.scalars(select(Signal).where(Signal.id.in_(ids))):
        row.clustered_into = sug.id
    s.flush()
    refresh_stats(s, sug)


def _title(sigs: list[SigRow], centroid: np.ndarray | None) -> tuple[str, str]:
    """Placeholder title and summary until the LLM drafts better ones."""
    pick = sigs[0]
    if centroid is not None:
        scored = [(float(m.vec @ centroid), -m.id, m) for m in sigs if m.vec is not None]
        if scored:
            pick = max(scored, key=lambda t: (t[0], t[1]))[2]
    kinds = Counter(m.kind for m in sigs)
    label = KIND_LABEL.get(pick.kind, pick.kind.replace("_", " ").capitalize())
    text = signal_text(pick.kind, pick.payload) or label
    title = f"{label}: {text}"
    title = title if len(title) <= 110 else title[:109] + "…"
    users = len({m.user_id for m in sigs if m.user_id})
    sessions = len({m.session_id for m in sigs if m.session_id})
    summary = (
        f"{len(sigs)} signal(s) ({', '.join(f'{n} {k}' for k, n in kinds.most_common())})"
        + (f" from {users} user(s) across {sessions} session(s)" if sessions else "")
        + ". No draft yet."
    )
    return title, summary


def _load_sigs(s: Session, workspace_id: uuid.UUID, since: datetime) -> list[SigRow]:
    rows = s.scalars(
        select(Signal)
        .where(Signal.workspace_id == workspace_id, Signal.clustered_into.is_(None),
               Signal.created_at >= since, signal_not_eval())
        .order_by(Signal.id)
    )
    return [
        SigRow(r.id, r.kind, r.source, r.user_id, r.session_id, r.payload, set(r.asset_ids),
               set(r.item_ids), unit_vector(r.embedding))
        for r in rows
    ]


def _open_neighbourhoods(s: Session, workspace_id: uuid.UUID) -> list[tuple[Suggestion, Cluster]]:
    out: list[tuple[Suggestion, Cluster]] = []
    for sug in s.scalars(
        select(Suggestion).where(
            Suggestion.workspace_id == workspace_id, Suggestion.status == "open",
            Suggestion.source == "signals",
        )
    ):
        c = Cluster()
        for r in s.scalars(
            select(Signal).join(SuggestionSignal, SuggestionSignal.signal_id == Signal.id)
            .where(SuggestionSignal.suggestion_id == sug.id, Signal.embedding.is_not(None))
            .limit(200)
        ):
            vec = unit_vector(r.embedding)
            if vec is not None:
                c.add(SigRow(r.id, r.kind, r.source, r.user_id, r.session_id, r.payload,
                             set(r.asset_ids), set(r.item_ids), vec))
        if c.members:
            out.append((sug, c))
    return out


def _overlap(cluster: Cluster, hood: Cluster, cfg: CurationConfig) -> float:
    cen = hood.unit
    hits = sum(
        1 for m in cluster.members
        if m.vec is not None and affinity(m, cen, hood.assets, hood.items) >= cfg.similarity
    )
    return hits / len(cluster.members)


def _bypass_key(sig: SigRow) -> tuple[str, str]:
    p = sig.payload
    return sig.kind, str(p.get("dedupe") or p.get("target") or sig.id)


def detect_gaps(
    s: Session, workspace_id: uuid.UUID, cfg: CurationConfig, *, now: datetime | None = None
) -> GapReport:
    """Cluster the workspace's unclustered signals; create or extend suggestions."""
    since = (now or datetime.now(UTC)) - timedelta(days=cfg.window_days)
    sigs = _load_sigs(s, workspace_id, since)
    report = GapReport(signals_considered=len(sigs))
    if not sigs:
        return report

    behavioural = [x for x in sigs if x.source != "hygiene" and x.kind not in BYPASS_KINDS]
    bypass = [x for x in sigs if x.source == "hygiene" or x.kind in BYPASS_KINDS]

    # ---- behavioural clusters --------------------------------------------------------------
    clusters = cluster_signals(behavioural, cfg)
    report.clusters_found += len(clusters)
    hoods = _open_neighbourhoods(s, workspace_id)
    for c in clusters:
        scored = [(_overlap(c, h, cfg), sug, h) for sug, h in hoods]
        scored = [t for t in scored if t[0] > 0.5]
        if scored:
            _, sug, hood = max(scored, key=lambda t: t[0])
            _link(s, sug, c.members)
            for m in c.members:
                hood.add(m)
            report.attached.append(sug.id)
            continue
        if not meets_thresholds(c, cfg):
            report.below_threshold += 1
            continue
        title, summary = _title(c.members, c.unit)
        sug = Suggestion(workspace_id=workspace_id, title=title, summary=summary,
                         source="signals")
        s.add(sug)
        s.flush()
        _link(s, sug, c.members)
        hoods.append((sug, c))
        report.created.append(sug.id)

    # ---- hygiene findings and golden candidates: grouped by (kind, target), no thresholds --
    groups: dict[tuple[str, str], list[SigRow]] = {}
    for sig in bypass:
        groups.setdefault(_bypass_key(sig), []).append(sig)
    report.clusters_found += len(groups)
    for (kind, key), members in groups.items():
        source = "golden" if kind == "golden_candidate" else "hygiene"
        existing = s.scalars(
            select(Suggestion)
            .join(SuggestionSignal, SuggestionSignal.suggestion_id == Suggestion.id)
            .join(Signal, Signal.id == SuggestionSignal.signal_id)
            .where(Suggestion.workspace_id == workspace_id, Suggestion.status == "open",
                   Suggestion.source == source, Signal.kind == kind,
                   Signal.payload["dedupe"].astext == key)
            .limit(1)
        ).first()
        if existing is not None:
            _link(s, existing, members)
            report.attached.append(existing.id)
            continue
        title, summary = _title(members, None)
        sug = Suggestion(workspace_id=workspace_id, title=title, summary=summary, source=source)
        s.add(sug)
        s.flush()
        _link(s, sug, members)
        report.created.append(sug.id)

    log.info("gap detection: %s", report.line())
    return report
