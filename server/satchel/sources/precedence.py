"""Which asset doc is *primary* when several describe one table (design §8.3, M7b).

Several sources (dbt, Cube, ...) and hand-written git/ui docs may describe the same asset. Every
doc stays as an item (provenance, overlays, audit), but exactly one is primary:

* ``sources[].priority`` decides, **lower number wins** (1 beats 2);
* a hand-written doc (``source_id is None``: git or ui) counts as priority
  :data:`HANDWRITTEN_PRIORITY`, and hand-written beats a source at equal priority;
* then the source name, then the item key, so the choice is deterministic.

The primary is computed when read, never stored: changing a priority takes effect immediately and
no sync has to run. Readers of "the doc of this asset" (retrieval, ``describe_table``, the
catalog, the signal extractors) all go through :func:`group_docs`.

This module is pure apart from :func:`load_sources`; it holds the ranking and the
:func:`fold_facts` rule used to merge the non-primary docs' distinct facts into the primary at
retrieval time.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from satchel.store.models import ContextItem, ContextSource, ContextVersion

HANDWRITTEN_PRIORITY = 0


@dataclass(frozen=True)
class SourceInfo:
    name: str
    kind: str
    priority: int


@dataclass(frozen=True)
class DocRef:
    """Identity and rank of one asset doc."""

    item_id: uuid.UUID
    key: str
    source: str | None  # source name; None for hand-written
    kind: str | None  # adapter kind (dbt, cube, ...)
    managed_by: str
    priority: int

    @property
    def handwritten(self) -> bool:
        return self.source is None

    @property
    def label(self) -> str:
        """What the API calls the origin: the source name, else ``git`` / ``ui``."""
        return self.source or self.managed_by

    def sort_key(self) -> tuple[int, int, str, str]:
        return (self.priority, 0 if self.handwritten else 1, self.source or "", self.key)


@dataclass
class Member:
    ref: DocRef
    item: ContextItem
    version: ContextVersion | None


@dataclass
class DocGroup:
    """The docs of one asset, best first."""

    members: list[Member] = field(default_factory=list)

    @property
    def primary(self) -> Member:
        return self.members[0]

    @property
    def others(self) -> list[Member]:
        return self.members[1:]

    @property
    def overlapping(self) -> bool:
        return len(self.members) > 1


def load_sources(s: Session) -> dict[uuid.UUID, SourceInfo]:
    """Name, kind and priority of every source visible to the session."""
    return {
        src.id: SourceInfo(src.name, src.kind, int(src.config.get("priority") or 0))
        for src in s.scalars(select(ContextSource))
    }


def doc_ref(item: ContextItem, sources: dict[uuid.UUID, SourceInfo]) -> DocRef:
    info = sources.get(item.source_id) if item.source_id else None
    if info is None:  # hand-written, or a source that was removed (its items were detached)
        return DocRef(item.id, item.key, None, None, item.managed_by, HANDWRITTEN_PRIORITY)
    return DocRef(item.id, item.key, info.name, info.kind, item.managed_by, info.priority)


def group_by_key(
    rows: Iterable[tuple[uuid.UUID, ContextItem, ContextVersion | None]],
    sources: dict[uuid.UUID, SourceInfo],
) -> dict[uuid.UUID, DocGroup]:
    """Group ``(key, item, version)`` rows by ``key`` (an asset id), best first."""
    groups: dict[uuid.UUID, DocGroup] = {}
    for key, item, ver in rows:
        group = groups.setdefault(key, DocGroup())
        if all(m.item.id != item.id for m in group.members):
            group.members.append(Member(doc_ref(item, sources), item, ver))
    for g in groups.values():
        g.members.sort(key=lambda m: m.ref.sort_key())
    return groups


def group_docs(
    rows: Iterable[tuple[ContextItem, ContextVersion | None]],
    sources: dict[uuid.UUID, SourceInfo],
) -> dict[uuid.UUID, DocGroup]:
    """Group asset docs by the asset they describe (``scope_id``), best first.

    Rows that are not asset-scoped ``asset_doc`` items are ignored, so callers can pass a wider
    query. Deprecated items should be filtered by the caller (they differ per use)."""
    return group_by_key(
        (
            (item.scope_id, item, ver)
            for item, ver in rows
            if item.type == "asset_doc" and item.scope_kind == "asset" and item.scope_id
        ),
        sources,
    )


def describe(ref: DocRef) -> dict[str, Any]:
    return {"source": ref.label, "key": ref.key}


def doc_group_of(s: Session, item: ContextItem) -> DocGroup | None:
    """The group an asset doc belongs to (published docs of its asset, plus the item itself when
    it is not published yet). ``None`` for other items and deprecated docs."""
    if item.type != "asset_doc" or item.scope_kind != "asset" or item.scope_id is None:
        return None
    if item.state == "deprecated":
        return None
    pairs: list[tuple[ContextItem, ContextVersion | None]] = [
        (i, v)
        for i, v in s.execute(
            select(ContextItem, ContextVersion)
            .join(ContextVersion, ContextVersion.id == ContextItem.published_version_id)
            .where(
                ContextItem.workspace_id == item.workspace_id,
                ContextItem.type == "asset_doc", ContextItem.scope_kind == "asset",
                ContextItem.scope_id == item.scope_id, ContextItem.state != "deprecated",
            )
        )
    ]
    if all(i.id != item.id for i, _ in pairs):
        pairs.append((item, None))
    return group_docs(pairs, load_sources(s)).get(item.scope_id)


# ---- folding -----------------------------------------------------------------------------


def _doc(version: ContextVersion | None) -> dict[str, Any]:
    if version is None:
        return {}
    try:
        data = yaml.safe_load(version.body)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _column_map(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = doc.get("columns") or []
    pairs = list(raw.items()) if isinstance(raw, dict) else [(c.get("name"), c) for c in raw
                                                             if isinstance(c, dict)]
    return {str(n).lower(): c for n, c in pairs if n and isinstance(c, dict)}


def fold_facts(group: DocGroup) -> list[str]:
    """Facts the non-primary docs add that the primary lacks, as short lines.

    The primary always wins a conflict, so only *missing* things are folded: a description when
    the primary has none, columns the primary does not list, a description or enum for a column
    the primary lists without one, caveats, joins, grain, owner. Each line names its source.
    """
    if not group.overlapping:
        return []
    prim = _doc(group.primary.version)
    prim_cols = _column_map(prim)
    prim_caveats = {str(c).strip().lower() for c in prim.get("caveats") or []}
    prim_joins = {str(j).strip().lower() for j in prim.get("joins") or []}
    lines: list[str] = []
    seen_cols: set[tuple[str, str]] = set()
    for m in group.others:
        doc = _doc(m.version)
        who = m.ref.label
        if not prim.get("description") and doc.get("description"):
            first = str(doc["description"]).strip().split("\n\n")[0]
            lines.append(f"[{who}] description: {first}")
        if not prim.get("grain") and doc.get("grain"):
            lines.append(f"[{who}] grain: {doc['grain']}")
        if not prim.get("owner") and doc.get("owner"):
            lines.append(f"[{who}] owner: {doc['owner']}")
        for cav in doc.get("caveats") or []:
            if str(cav).strip().lower() not in prim_caveats:
                prim_caveats.add(str(cav).strip().lower())
                lines.append(f"[{who}] caveat: {cav}")
        for j in doc.get("joins") or []:
            if str(j).strip().lower() not in prim_joins:
                prim_joins.add(str(j).strip().lower())
                lines.append(f"[{who}] join: {j}")
        for lc, col in _column_map(doc).items():
            have = prim_cols.get(lc) or {}
            bits: list[str] = []
            if col.get("description") and not have.get("description"):
                bits.append(str(col["description"]).strip())
            if col.get("enum_values") and not have.get("enum_values"):
                bits.append("values: " + ", ".join(map(str, col["enum_values"])))
            if col.get("unit") and not have.get("unit"):
                bits.append(f"unit: {col['unit']}")
            if not bits:
                continue
            text = "; ".join(bits)
            if (lc, text) in seen_cols:
                continue
            seen_cols.add((lc, text))
            lines.append(f"[{who}] column {col.get('name', lc)}: {text}")
    return lines


def folded_text(group: DocGroup) -> str:
    """The block appended to the primary's chunk when it is served; empty when nothing to add."""
    lines = fold_facts(group)
    if not lines:
        return ""
    head = ", ".join(f"{m.ref.label} ({m.ref.key})" for m in group.others)
    return f"Also described by {head}; facts the primary lacks:\n" + "\n".join(
        f"- {line}" for line in lines
    )
