"""Re-embed chunks whose embedding model differs from the active provider (design §5.2)."""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from satchel.llm.embeddings import EmbeddingProvider
from satchel.store.models import ContextChunk, ContextItem

_BATCH = 64


def reembed_stale(session: Session, workspace_id: uuid.UUID, embedder: EmbeddingProvider) -> int:
    """Re-embed chunks of current/published versions not embedded with ``embedder``'s model."""
    version_ids: set[uuid.UUID] = set()
    for cur, pub in session.execute(
        select(ContextItem.current_version_id, ContextItem.published_version_id).where(
            ContextItem.workspace_id == workspace_id
        )
    ):
        version_ids.update(v for v in (cur, pub) if v is not None)
    if not version_ids:
        return 0
    chunks = list(
        session.scalars(
            select(ContextChunk)
            .where(
                ContextChunk.version_id.in_(version_ids),
                or_(
                    ContextChunk.embedding_model.is_(None),
                    ContextChunk.embedding_model != embedder.model_id,
                    ContextChunk.embedding.is_(None),
                ),
            )
            .order_by(ContextChunk.id)
        )
    )
    for i in range(0, len(chunks), _BATCH):
        batch = chunks[i : i + _BATCH]
        for c, v in zip(batch, embedder.embed([c.text for c in batch]), strict=True):
            c.embedding, c.embedding_model = v, embedder.model_id
    session.flush()
    return len(chunks)
