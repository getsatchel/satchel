"""Release building shared by the git importer and the curator publish/rollback paths (§8.1).

A release is an immutable mapping ``item -> version``. Publishing creates ``published seq+1``
from the previous release plus changed versions; rollback re-publishes an older mapping as a
new release. Previews (``kind='preview'``) are numbered separately.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from satchel.store.models import ContextItem, ContextVersion, Release, ReleaseItem


def release_items_stmt(
    release_id: uuid.UUID | None = None,
) -> Select[ContextItem, ContextVersion]:
    """Select ``(ContextItem, ContextVersion)`` for ``release_id``, or the latest published
    versions when ``release_id`` is ``None``."""
    if release_id is None:
        return (
            select(ContextItem, ContextVersion)
            .join(ContextVersion, ContextVersion.id == ContextItem.published_version_id)
            .where(ContextItem.state == "published")
        )
    return (
        select(ContextItem, ContextVersion)
        .join(ReleaseItem, ReleaseItem.item_id == ContextItem.id)
        .join(ContextVersion, ContextVersion.id == ReleaseItem.version_id)
        .where(ReleaseItem.release_id == release_id)
    )


def latest_published(s: Session, workspace_id: uuid.UUID) -> Release | None:
    return s.scalars(
        select(Release)
        .where(Release.workspace_id == workspace_id, Release.kind == "published")
        .order_by(Release.seq.desc())
    ).first()


def release_map(s: Session, release_id: uuid.UUID | None) -> dict[uuid.UUID, uuid.UUID]:
    """``item_id -> version_id`` for a release (empty for ``None``)."""
    if release_id is None:
        return {}
    return {
        ri.item_id: ri.version_id
        for ri in s.scalars(select(ReleaseItem).where(ReleaseItem.release_id == release_id))
    }


def next_seq(s: Session, workspace_id: uuid.UUID, kind: str) -> int:
    cur = s.scalar(
        select(func.max(Release.seq)).where(
            Release.workspace_id == workspace_id, Release.kind == kind
        )
    )
    return (cur or 0) + 1


def create_release(
    s: Session,
    workspace_id: uuid.UUID,
    *,
    kind: str,
    mapping: dict[uuid.UUID, uuid.UUID],
    parent_id: uuid.UUID | None,
    note: str | None,
    created_by: uuid.UUID | None = None,
) -> Release:
    """Insert a release and its ``release_item`` rows. The single place releases are built."""
    rel = Release(
        workspace_id=workspace_id,
        seq=next_seq(s, workspace_id, kind),
        kind=kind,
        note=note,
        created_by=created_by,
        parent_release_id=parent_id,
    )
    s.add(rel)
    s.flush()
    for item_id, ver_id in mapping.items():
        s.add(ReleaseItem(release_id=rel.id, item_id=item_id, version_id=ver_id))
    s.flush()
    return rel
