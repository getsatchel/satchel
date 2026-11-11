"""Context packs: always-on rules + ranked candidates, packed to a token budget (§5.1)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from satchel.core.tokens import approx_tokens
from satchel.llm.embeddings import EmbeddingProvider, make_embedder
from satchel.retrieval.search import (
    RankedItem,
    chunk_texts,
    rank_items,
    resolve_release,
    workspace_id_of,
)
from satchel.store.models import (
    ContextItem,
    ContextVersion,
    ReleaseItem,
    ServedContext,
)

# Fixed packing/render order (design §5.1 step 6).
SECTIONS: list[tuple[str, list[str]]] = [
    ("Rules", ["workspace_rule", "user_memory"]),
    ("Caveats", ["caveat"]),
    ("Glossary", ["glossary_term"]),
    ("Golden queries", ["golden_query"]),
    ("Table docs", ["asset_doc"]),
    ("Guides", ["guide"]),
    ("Other", ["semantic_model", "endorsement"]),
]
_TYPE_ORDER = {t: i for i, (_, ts) in enumerate(SECTIONS) for t in ts}
ALWAYS_ON = {"workspace_rule", "user_memory"}
MAX_ITEMS = 25


@dataclass
class PackEntry:
    key: str
    type: str
    seq: int
    version_id: uuid.UUID
    tag: str
    text: str
    tokens: int
    score: float | None
    always_on: bool = False


@dataclass
class ContextPack:
    question: str
    release_id: uuid.UUID | None
    budget_tokens: int
    entries: list[PackEntry] = field(default_factory=list)

    @property
    def used_tokens(self) -> int:
        return sum(e.tokens for e in self.entries)

    @property
    def text(self) -> str:
        out: list[str] = []
        for title, types in SECTIONS:
            section = [e for e in self.entries if e.type in types]
            if section:
                out.append(f"## {title}")
                out.extend(f"{e.tag}\n{e.text}" for e in section)
        return "\n\n".join(out)


def _entry_tokens(tag: str, text: str) -> int:
    return approx_tokens(tag) + approx_tokens(text)


def _always_on(
    session: Session, rid: uuid.UUID, user_id: uuid.UUID | None
) -> list[PackEntry]:
    entries: list[PackEntry] = []
    rows = session.execute(
        select(ContextItem, ContextVersion)
        .join(ReleaseItem, ReleaseItem.item_id == ContextItem.id)
        .join(ContextVersion, ContextVersion.id == ReleaseItem.version_id)
        .where(
            ReleaseItem.release_id == rid,
            ContextItem.type == "workspace_rule",
            ContextItem.scope_kind == "workspace",
        )
        .order_by(ContextItem.key)
    )
    pairs = [(i, v) for i, v in rows]
    if user_id is not None:
        mem = session.execute(
            select(ContextItem, ContextVersion)
            .join(
                ContextVersion,
                ContextVersion.id
                == ContextItem.published_version_id,
            )
            .where(
                ContextItem.type == "user_memory",
                ContextItem.scope_kind == "user",
                ContextItem.scope_id == user_id,
                ContextItem.state != "deprecated",
            )
            .order_by(ContextItem.key)
        )
        pairs.extend((i, v) for i, v in mem)
    for item, ver in pairs:
        tag = f"[ctx:{item.key}@v{ver.seq}]"
        entries.append(
            PackEntry(
                key=item.key,
                type=item.type,
                seq=ver.seq,
                version_id=ver.id,
                tag=tag,
                text=ver.body.strip(),
                tokens=_entry_tokens(tag, ver.body.strip()),
                score=None,
                always_on=True,
            )
        )
    return entries


def always_on_entries(
    session: Session, release_id: uuid.UUID | None, user_id: uuid.UUID | None
) -> list[PackEntry]:
    """Always-on rules (workspace rules + the caller's private memory) for a release."""
    return _always_on(session, release_id, user_id) if release_id else []


def build_context_pack(
    session: Session,
    question: str,
    user_id: uuid.UUID | None,
    *,
    embedder: EmbeddingProvider | None = None,
    release_id: uuid.UUID | None = None,
    budget_tokens: int = 4000,
    connection: str | None = None,
    tool_call_id: int | None = None,
) -> ContextPack:
    """Build the context pack for ``question`` inside an RLS ``workspace_session``.

    Always-on rules (workspace rule + the caller's private memory) are included first and are
    never dropped, even if they alone exceed the budget. Remaining candidates are added
    greedily by score while they fit, then rendered in fixed type order. When
    ``tool_call_id`` is given, a ``served_context`` row is logged per packed version.
    """
    wid = workspace_id_of(session)
    rid, ranked = rank_items(
        session,
        question,
        embedder or make_embedder(),
        release_id=release_id,
        connection=connection,
        limit=60,
    )
    pack = ContextPack(question=question, release_id=rid, budget_tokens=budget_tokens)
    if rid is None:
        return pack
    assert resolve_release(session, wid, rid) == rid

    always = _always_on(session, rid, user_id)
    pack.entries.extend(always)
    remaining = budget_tokens - pack.used_tokens

    candidates: list[RankedItem] = [r for r in ranked if r.type not in ALWAYS_ON]
    texts = chunk_texts(session, candidates)
    picked: list[PackEntry] = []
    for r in candidates:
        if len(picked) >= MAX_ITEMS:
            break
        txt = texts[r.version_id].strip()
        tokens = _entry_tokens(r.tag, txt)
        if tokens > remaining:
            continue
        remaining -= tokens
        picked.append(
            PackEntry(r.key, r.type, r.seq, r.version_id, r.tag, txt, tokens, r.score)
        )
    picked.sort(key=lambda e: (_TYPE_ORDER.get(e.type, 99), -(e.score or 0.0), e.key))
    pack.entries.extend(picked)
    pack.entries.sort(key=lambda e: (_TYPE_ORDER.get(e.type, 99), not e.always_on))

    if tool_call_id is not None:
        for rank, e in enumerate(pack.entries, start=1):
            session.add(
                ServedContext(
                    tool_call_id=tool_call_id, version_id=e.version_id, rank=rank, score=e.score
                )
            )
        session.flush()
    return pack
