"""Hybrid candidate generation, fusion and priors (design §5.1 steps 2-4)."""

from __future__ import annotations

import difflib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, case, distinct, func, or_, select, text
from sqlalchemy.orm import Session

from satchel.core.item_types import yaml_item_dict
from satchel.llm.embeddings import EmbeddingProvider, make_embedder
from satchel.retrieval.precedence import apply_precedence
from satchel.store.models import (
    Asset,
    Connection,
    ContextChunk,
    ContextEntityLink,
    ContextItem,
    ContextVersion,
    Feedback,
    Release,
    ReleaseItem,
    ServedContext,
    ToolCall,
)

RRF_K = 60
CANDIDATES = 50
COMMON_DF = 0.35
# A person's 👍 / 👎 in the result widget weighs this many agent-relayed votes in the
# helpfulness prior.
HUMAN_VOTE = 3.0
# Exact glossary matches are rare and high precision, so they carry extra RRF weight.
LIST_WEIGHTS = {"bm25": 1.0, "vector": 1.0, "glossary": 2.0, "asset": 1.0}

# pgvector's default hnsw.ef_search (40) caps how many rows an HNSW scan can return, and the
# release / embedding_model / type filters apply *after* the index scan, so a large library
# silently yields fewer than CANDIDATES (and worse) hits. Widen the scan, and on pgvector >= 0.8
# let it keep scanning until the LIMIT is met (iterative scan).
HNSW_EF_SEARCH = max(100, 2 * CANDIDATES)  # pgvector allows at most 1000
HNSW_MAX_SCAN_TUPLES = 40_000
_ITERATIVE_SCAN_MIN = (0, 8)
_pgvector_version: tuple[int, ...] | None = None

_TOKEN = re.compile(r"[a-z0-9]+")


def _stem(tok: str) -> str:
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss") else tok


def _tokens(s: str) -> list[str]:
    return [_stem(t) for t in _TOKEN.findall(s.lower())]


@dataclass
class RankedItem:
    version_id: uuid.UUID
    item_id: uuid.UUID
    key: str
    type: str
    seq: int
    scope_kind: str
    scope_id: uuid.UUID | None
    meta: dict[str, Any]
    rrf: float = 0.0
    score: float = 0.0
    sources: dict[str, int] = field(default_factory=dict)  # candidate list -> 1-based rank
    best_chunk_id: int | None = None
    # facts other sources add to this asset doc (see satchel.retrieval.precedence), served after it
    folded: str = ""

    @property
    def tag(self) -> str:
        return f"[ctx:{self.key}@v{self.seq}]"


def workspace_id_of(session: Session) -> uuid.UUID:
    wid = session.execute(
        text("select nullif(current_setting('satchel.workspace_id', true), '')::uuid")
    ).scalar_one()
    if wid is None:
        raise RuntimeError("retrieval requires a workspace_session (satchel.workspace_id unset)")
    assert isinstance(wid, uuid.UUID)
    return wid


def resolve_release(
    session: Session, workspace_id: uuid.UUID, release_id: uuid.UUID | None
) -> uuid.UUID | None:
    if release_id is not None:
        return session.scalars(
            select(Release.id).where(Release.id == release_id, Release.workspace_id == workspace_id)
        ).first()
    return session.scalars(
        select(Release.id)
        .where(Release.workspace_id == workspace_id, Release.kind == "published")
        .order_by(Release.seq.desc())
    ).first()


def _or_tsquery(session: Session, rid: uuid.UUID, question: str) -> str | None:
    """OR-query of the question's words, minus corpus-common words (cheap idf).

    ``ts_rank_cd`` has no idf, so a word present in most chunks ("month" in a revenue repo)
    would otherwise dominate the ranking. Words matching more than ``COMMON_DF`` of the
    release's chunks are dropped, as long as at least two other words remain.
    """
    words = list(dict.fromkeys(_TOKEN.findall(question.lower())))
    if not words:
        return None
    total = session.scalar(
        select(func.count())
        .select_from(ContextChunk)
        .join(ReleaseItem, ReleaseItem.version_id == ContextChunk.version_id)
        .where(ReleaseItem.release_id == rid)
    )
    if total and total >= 10:
        counts: dict[str, int] = {
            str(w): int(n)
            for w, n in session.execute(
                text(
                    "select w, count(*) from unnest(cast(:words as text[])) as w "
                    "join release_item ri on ri.release_id = :rid "
                    "join context_chunk cc on cc.version_id = ri.version_id "
                    "and cc.tsv @@ plainto_tsquery('english', w) "
                    "group by w"
                ),
                {"words": words, "rid": rid},
            )
        }
        kept = [w for w in words if (counts.get(w, 0) / total) <= COMMON_DF]
        if len(kept) >= 2 or (kept and len(words) == 1):
            words = kept
    return " | ".join(words)


def _bm25(
    session: Session, rid: uuid.UUID, question: str, types: list[str] | None
) -> list[tuple[int, uuid.UUID]]:
    q = _or_tsquery(session, rid, question)
    if q is None:
        return []
    tsq = func.to_tsquery("english", q)
    stmt = (
        select(ContextChunk.id, ContextChunk.version_id)
        .join(ReleaseItem, ReleaseItem.version_id == ContextChunk.version_id)
        .join(ContextItem, ContextItem.id == ReleaseItem.item_id)
        .where(ReleaseItem.release_id == rid, ContextChunk.tsv.op("@@")(tsq))
        .order_by(func.ts_rank_cd(ContextChunk.tsv, tsq, 1).desc(), ContextChunk.id)
        .limit(CANDIDATES)
    )
    if types:
        stmt = stmt.where(ContextItem.type.in_(types))
    return [(r[0], r[1]) for r in session.execute(stmt)]


def _pgvector_ver(session: Session) -> tuple[int, ...]:
    """Installed pgvector version, read once per process (``()`` if unknown)."""
    global _pgvector_version
    if _pgvector_version is None:
        raw = session.scalar(text("select extversion from pg_extension where extname = 'vector'"))
        nums = re.findall(r"\d+", str(raw or ""))[:3]
        _pgvector_version = tuple(int(n) for n in nums)
    return _pgvector_version


def tune_vector_scan(session: Session) -> None:
    """Transaction-local HNSW settings so a filtered ANN query can still return CANDIDATES rows.

    ``SET LOCAL`` ends with the retrieval transaction, so nothing leaks to pooled connections.
    The iterative-scan settings only exist from pgvector 0.8; older versions get ef_search only.
    """
    session.execute(text(f"set local hnsw.ef_search = {int(HNSW_EF_SEARCH)}"))
    if _pgvector_ver(session)[:2] >= _ITERATIVE_SCAN_MIN:
        session.execute(text("set local hnsw.iterative_scan = relaxed_order"))
        session.execute(text(f"set local hnsw.max_scan_tuples = {int(HNSW_MAX_SCAN_TUPLES)}"))


def _vector(
    session: Session,
    rid: uuid.UUID,
    qvec: list[float],
    model_id: str,
    types: list[str] | None,
) -> list[tuple[int, uuid.UUID]]:
    tune_vector_scan(session)
    stmt = (
        select(ContextChunk.id, ContextChunk.version_id)
        .join(ReleaseItem, ReleaseItem.version_id == ContextChunk.version_id)
        .join(ContextItem, ContextItem.id == ReleaseItem.item_id)
        .where(ReleaseItem.release_id == rid, ContextChunk.embedding_model == model_id)
        .order_by(ContextChunk.embedding.cosine_distance(qvec), ContextChunk.id)
        .limit(CANDIDATES)
    )
    if types:
        stmt = stmt.where(ContextItem.type.in_(types))
    return [(r[0], r[1]) for r in session.execute(stmt)]


def _glossary_matches(session: Session, rid: uuid.UUID, question: str) -> list[uuid.UUID]:
    qnorm = " " + " ".join(_tokens(question)) + " "
    rows = session.execute(
        select(ContextVersion.id, ContextVersion.meta)
        .join(ReleaseItem, ReleaseItem.version_id == ContextVersion.id)
        .join(ContextItem, ContextItem.id == ReleaseItem.item_id)
        .where(ReleaseItem.release_id == rid, ContextItem.type == "glossary_term")
    )
    hits: list[tuple[int, uuid.UUID]] = []
    for vid, meta in rows:
        variants = [str(meta.get("term", "")), *map(str, meta.get("synonyms", []))]
        best = 0
        for v in variants:
            norm = " ".join(_tokens(v))
            if norm and f" {norm} " in qnorm:
                best = max(best, len(norm))
        if best:
            hits.append((best, vid))
    hits.sort(key=lambda h: -h[0])
    return [v for _, v in hits]


def _asset_matches(session: Session, workspace_id: uuid.UUID, question: str) -> list[uuid.UUID]:
    qtoks = set(_tokens(question))
    if not qtoks:
        return []
    qlower = question.lower()
    # Pre-filter in SQL: tokens < 5 chars must match verbatim; tokens >= 5 chars allow fuzzy
    # matching (difflib ratio >= 0.85, at most 1-2 edits), so by the pigeonhole principle at
    # least one of the disjoint 2-char slices (prefix, middle, suffix) must appear verbatim.
    substrings: set[str] = set()
    for t in qtoks:
        if len(t) < 5:
            substrings.add(t)
        else:
            mid = len(t) // 2
            substrings.update((t[:2], t[mid - 1 : mid + 1], t[-2:]))
    stmt = select(Asset.id, Asset.fqn, Asset.kind).where(
        Asset.workspace_id == workspace_id,
        Asset.dropped_at.is_(None),
        Asset.kind.in_(["table", "column"]),
    )
    if substrings:
        stmt = stmt.where(or_(*(Asset.fqn.ilike(f"%{p}%") for p in sorted(substrings))))
    scored: list[tuple[float, uuid.UUID]] = []
    for aid, fqn, kind in session.execute(stmt):
        name = fqn.split(".")[-1]
        ntoks = _tokens(name.replace("_", " "))
        if not ntoks:
            continue
        s = 0.0
        if fqn.lower() in qlower:
            s = 3.0 if kind == "table" else 1.5
        elif all(
            t in qtoks
            or (len(t) >= 5 and difflib.get_close_matches(t, qtoks, n=1, cutoff=0.85))
            for t in ntoks
        ):
            multiword = len(ntoks) > 1
            s = 2.0 if kind == "table" else (1.0 if multiword else 0.0)
        if s:
            scored.append((s, aid))
    scored.sort(key=lambda x: -x[0])
    return [a for _, a in scored]


def _versions_for_assets(
    session: Session, rid: uuid.UUID, asset_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    if not asset_ids:
        return []
    rows = session.execute(
        select(ContextEntityLink.asset_id, ContextEntityLink.version_id)
        .join(ReleaseItem, ReleaseItem.version_id == ContextEntityLink.version_id)
        .join(ContextItem, ContextItem.id == ReleaseItem.item_id)
        .where(
            ReleaseItem.release_id == rid,
            ContextEntityLink.asset_id.in_(asset_ids),
            ContextEntityLink.relation.in_(["defines", "applies_to"]),
            ContextItem.type.in_(["asset_doc", "caveat"]),
        )
    )
    order = {a: i for i, a in enumerate(asset_ids)}
    pairs = sorted(((order[a], v) for a, v in rows), key=lambda p: p[0])
    return list(dict.fromkeys(v for _, v in pairs))


def _excluded_assets(session: Session, rid: uuid.UUID, workspace_id: uuid.UUID) -> set[str]:
    out: set[str] = set()
    rows = session.execute(
        select(ContextVersion.meta, ContextVersion.body)
        .join(ReleaseItem, ReleaseItem.version_id == ContextVersion.id)
        .join(ContextItem, ContextItem.id == ReleaseItem.item_id)
        .where(ReleaseItem.release_id == rid, ContextItem.type == "endorsement")
    )
    for meta, body in rows:
        d = yaml_item_dict("endorsement", body, meta)
        if d.get("excluded_from_ai") and d.get("asset"):
            out.add(str(d["asset"]).lower())
    return out


def rank_items(
    session: Session,
    question: str,
    embedder: EmbeddingProvider,
    *,
    release_id: uuid.UUID | None = None,
    types: list[str] | None = None,
    connection: str | None = None,
    limit: int = 50,
) -> tuple[uuid.UUID | None, list[RankedItem]]:
    """Run candidate generation + RRF + priors; return (release id, items by score desc)."""
    wid = workspace_id_of(session)
    rid = resolve_release(session, wid, release_id)
    if rid is None:
        return None, []

    lists: dict[str, list[uuid.UUID]] = {}
    chunk_rank: dict[uuid.UUID, tuple[int, int]] = {}  # version -> (best rank, chunk id)

    def add_chunks(name: str, rows: list[tuple[int, uuid.UUID]]) -> None:
        seen: list[uuid.UUID] = []
        for rank, (cid, vid) in enumerate(rows, start=1):
            if vid not in seen:
                seen.append(vid)
            if vid not in chunk_rank or rank < chunk_rank[vid][0]:
                chunk_rank[vid] = (rank, cid)
        lists[name] = seen

    add_chunks("bm25", _bm25(session, rid, question, types))
    add_chunks(
        "vector",
        _vector(session, rid, embedder.embed_query(question), embedder.model_id, types),
    )
    if types is None or "glossary_term" in types:
        lists["glossary"] = _glossary_matches(session, rid, question)
    asset_ids = _asset_matches(session, wid, question)
    lists["asset"] = _versions_for_assets(session, rid, asset_ids)

    vids = {v for lst in lists.values() for v in lst}
    if not vids:
        return rid, []

    rows = session.execute(
        select(ContextVersion, ContextItem)
        .join(ContextItem, ContextItem.id == ContextVersion.item_id)
        .where(ContextVersion.id.in_(vids))
    )
    items: dict[uuid.UUID, RankedItem] = {}
    for ver, item in rows:
        if types and item.type not in types:
            continue
        items[ver.id] = RankedItem(
            version_id=ver.id,
            item_id=item.id,
            key=item.key,
            type=item.type,
            seq=ver.seq,
            scope_kind=item.scope_kind,
            scope_id=item.scope_id,
            meta=ver.meta,
            best_chunk_id=chunk_rank[ver.id][1] if ver.id in chunk_rank else None,
        )
    for name, lst in lists.items():
        w = LIST_WEIGHTS[name]
        rank = 0
        for vid in lst:
            if vid not in items:
                continue
            rank += 1
            items[vid].rrf += w / (RRF_K + rank)
            items[vid].sources[name] = rank

    apply_precedence(session, rid, items)  # one served doc per asset (M7b)
    _apply_exclusions(session, rid, wid, question, items)
    _apply_priors(session, items, connection, wid)
    ranked = sorted(items.values(), key=lambda r: (-r.score, r.key))
    return rid, ranked[:limit]


def _apply_exclusions(
    session: Session,
    rid: uuid.UUID,
    wid: uuid.UUID,
    question: str,
    items: dict[uuid.UUID, RankedItem],
) -> None:
    excluded = _excluded_assets(session, rid, wid)
    excluded = {a for a in excluded if f"@{a}" not in question.lower()}
    if not excluded:
        return
    bad = session.scalars(
        select(ContextEntityLink.version_id)
        .join(Asset, Asset.id == ContextEntityLink.asset_id)
        .where(
            ContextEntityLink.version_id.in_(list(items)),
            func.lower(Asset.fqn).in_(excluded),
        )
    )
    for vid in set(bad):
        items.pop(vid, None)


def _apply_priors(
    session: Session,
    items: dict[uuid.UUID, RankedItem],
    connection: str | None,
    wid: uuid.UUID,
) -> None:
    if not items:
        return
    vids = list(items)
    item_ids = list({r.item_id for r in items.values()})

    stale = set(
        session.scalars(
            select(ContextEntityLink.version_id)
            .join(Asset, Asset.id == ContextEntityLink.asset_id)
            .where(ContextEntityLink.version_id.in_(vids), Asset.dropped_at.is_not(None))
        )
    )
    votes: dict[uuid.UUID, tuple[float, float]] = {}

    def vote(item_id: uuid.UUID, up: float, down: float) -> None:
        u, d = votes.get(item_id, (0.0, 0.0))
        votes[item_id] = (u + up, d + down)

    # Agent-relayed feedback is attached to the search call it followed.
    for item_id, up, down in session.execute(
        select(
            ContextVersion.item_id,
            func.count().filter(Feedback.rating > 0),
            func.count().filter(Feedback.rating < 0),
        )
        .select_from(ServedContext)
        .join(ContextVersion, ContextVersion.id == ServedContext.version_id)
        .join(Feedback, Feedback.tool_call_id == ServedContext.tool_call_id)
        .where(ContextVersion.item_id.in_(item_ids), Feedback.source == "agent")
        .group_by(ContextVersion.item_id)
    ):
        vote(item_id, float(up), float(down))
    # A person rating a result (result widget) votes on every item served earlier in that
    # session, and counts HUMAN_VOTE times as much.
    for item_id, up, down in session.execute(
        select(
            ContextVersion.item_id,
            func.count(distinct(case((Feedback.rating > 0, Feedback.id)))),
            func.count(distinct(case((Feedback.rating < 0, Feedback.id)))),
        )
        .select_from(ServedContext)
        .join(ToolCall, ToolCall.id == ServedContext.tool_call_id)
        .join(
            Feedback,
            and_(
                Feedback.session_id == ToolCall.session_id,
                Feedback.source == "human",
                or_(Feedback.tool_call_id.is_(None), ToolCall.id <= Feedback.tool_call_id),
            ),
        )
        .join(ContextVersion, ContextVersion.id == ServedContext.version_id)
        .where(ContextVersion.item_id.in_(item_ids))
        .group_by(ContextVersion.item_id)
    ):
        vote(item_id, HUMAN_VOTE * float(up), HUMAN_VOTE * float(down))

    conn_id: uuid.UUID | None = None
    asset_conn: dict[uuid.UUID, uuid.UUID | None] = {}
    if connection:
        conn_id = session.scalars(
            select(Connection.id).where(
                Connection.workspace_id == wid, Connection.name == connection
            )
        ).first()
        asset_ids = [r.scope_id for r in items.values() if r.scope_kind == "asset" and r.scope_id]
        if asset_ids:
            asset_conn = dict(
                session.execute(
                    select(Asset.id, Asset.connection_id).where(Asset.id.in_(asset_ids))
                ).tuples()
            )

    for r in items.values():
        v_up, v_down = votes.get(r.item_id, (0.0, 0.0))
        helpful = (v_up - v_down) / (v_up + v_down + 5)
        boost = (
            1
            + 0.5 * bool(r.meta.get("endorsed"))
            + 0.3 * (r.type == "golden_query")
            + 0.2 * helpful
        )
        fresh = 0.5 if r.version_id in stale else 1.0
        scope = 1.0
        if connection:
            if r.scope_kind == "connection":
                scope = 1.0 if r.scope_id == conn_id else 0.5
            elif r.scope_kind == "asset":
                scope = 1.0 if asset_conn.get(r.scope_id) in (conn_id, None) else 0.5  # type: ignore[arg-type]
            else:
                scope = 0.8
        r.score = r.rrf * boost * fresh * scope


@dataclass
class SearchHit:
    key: str
    type: str
    seq: int
    tag: str
    score: float
    sources: dict[str, int]
    snippet: str
    version_id: uuid.UUID


def search_context(
    session: Session,
    query: str,
    *,
    types: list[str] | None = None,
    k: int = 10,
    embedder: EmbeddingProvider | None = None,
    release_id: uuid.UUID | None = None,
    connection: str | None = None,
) -> list[SearchHit]:
    """Ranked items (hybrid retrieval + priors) for ``query``, with a text snippet each."""
    _, ranked = rank_items(
        session,
        query,
        embedder or make_embedder(),
        release_id=release_id,
        types=types,
        connection=connection,
        limit=k,
    )
    texts = chunk_texts(session, ranked)
    return [
        SearchHit(
            key=r.key,
            type=r.type,
            seq=r.seq,
            tag=r.tag,
            score=r.score,
            sources=r.sources,
            snippet=texts[r.version_id][:400],
            version_id=r.version_id,
        )
        for r in ranked
    ]


def chunk_texts(session: Session, ranked: list[RankedItem]) -> dict[uuid.UUID, str]:
    """Text to show for each item: its best-matching chunk, or the whole item."""
    if not ranked:
        return {}
    by_version: dict[uuid.UUID, list[tuple[int, int, str]]] = {}
    for cid, vid, ord_, txt in session.execute(
        select(ContextChunk.id, ContextChunk.version_id, ContextChunk.ord, ContextChunk.text)
        .where(ContextChunk.version_id.in_([r.version_id for r in ranked]))
        .order_by(ContextChunk.version_id, ContextChunk.ord)
    ):
        by_version.setdefault(vid, []).append((cid, ord_, txt))
    out: dict[uuid.UUID, str] = {}
    for r in ranked:
        chunks = by_version.get(r.version_id, [])
        best = [t for cid, _, t in chunks if cid == r.best_chunk_id]
        text = best[0] if best and len(chunks) > 1 else "\n\n".join(t for _, _, t in chunks)
        out[r.version_id] = f"{text}\n\n{r.folded}" if r.folded else text
    return out
