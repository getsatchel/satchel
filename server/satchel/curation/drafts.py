"""Draft versions made through the API (workbench, accepted proposals): validation, chunking
and embedding, so a draft is retrievable from a preview release. Entity links are not
computed for drafts (they are for git imports); asset-scoped retrieval of a draft therefore
relies on text matching until it is published via a git import."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from satchel.core.item_types import ItemValidationError, normalized_meta, validate_item
from satchel.core.render import render_yaml_item
from satchel.llm.embeddings import EmbeddingProvider
from satchel.store.models import ContextChunk, ContextItem, ContextVersion
from satchel.sync.chunking import chunk_markdown
from satchel.sync.importer import content_hash

__all__ = [
    "ItemValidationError",
    "add_version",
    "chunk_texts_for",
    "content_hash",
    "ensure_chunks",
    "normalized_meta",
    "validate_item",
]


def chunk_texts_for(item_type: str, body: str, meta: dict[str, Any]) -> list[str]:
    """Chunk texts for a validated (type, body, meta), mirroring ``sync.parse``."""
    model = validate_item(item_type, body, meta)
    if isinstance(meta.get("source"), dict):  # source-managed: rendered from source + overlay
        from satchel.sources.items import chunks_from_meta

        return chunks_from_meta(item_type, body, meta)
    if item_type == "guide":
        name = getattr(model, "name", "")
        desc = getattr(model, "description", "")
        return chunk_markdown(body, f"Guide: {name}. {desc}")
    if item_type == "caveat":
        return chunk_markdown(body, "Caveat")
    if item_type in {"workspace_rule", "user_memory"}:
        return chunk_markdown(body, None)
    if item_type == "semantic_model":
        if meta.get("format") == "wren_mdl":
            from satchel.data.semantic import SemanticError, parse_mdl, summarize

            try:
                return [summarize(parse_mdl(body))]
            except SemanticError as e:
                raise ItemValidationError(str(e)) from None
        return [body[:2000]]
    return [render_yaml_item(item_type, model)]


def ensure_chunks(
    s: Session, version: ContextVersion, item_type: str, embedder: EmbeddingProvider
) -> int:
    """Create (and embed) chunks for ``version`` when it has none. Returns chunks added."""
    have = s.scalar(
        select(func.count()).select_from(ContextChunk).where(ContextChunk.version_id == version.id)
    )
    if have:
        return 0
    texts = chunk_texts_for(item_type, version.body, version.meta)
    vectors = embedder.embed(texts) if texts else []
    for ord_, (text, vec) in enumerate(zip(texts, vectors, strict=True)):
        s.add(
            ContextChunk(
                version_id=version.id, ord=ord_, text=text, embedding=vec,
                embedding_model=embedder.model_id,
            )
        )
    s.flush()
    return len(texts)


def add_version(
    s: Session,
    item: ContextItem,
    body: str,
    meta: dict[str, Any],
    *,
    author: uuid.UUID | None,
    provenance: dict[str, Any],
    embedder: EmbeddingProvider,
) -> ContextVersion:
    """Validate and store a new version (seq = max + 1) and make it the item's current one."""
    enriched_meta = normalized_meta(item.type, body, meta)
    seq = (
        s.scalar(select(func.max(ContextVersion.seq)).where(ContextVersion.item_id == item.id))
        or 0
    ) + 1
    ver = ContextVersion(
        item_id=item.id,
        seq=seq,
        body=body,
        meta=enriched_meta,
        content_hash=content_hash(body, enriched_meta),
        author_user_id=author,
        provenance=provenance,
    )
    s.add(ver)
    s.flush()
    ensure_chunks(s, ver, item.type, embedder)
    item.current_version_id = ver.id
    item.updated_at = func.now()
    s.flush()
    return ver
