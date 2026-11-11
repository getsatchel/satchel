"""Serve one asset doc per table when several sources describe it (design §8.3, M7b).

Runs after candidate fusion and before exclusions and priors. For every asset that has more than
one published ``asset_doc`` in the release (dbt and Cube, or a source and a hand-written doc):

* non-primary docs are **removed** from the candidates, so the agent is never served duplicates;
* the primary takes their place and keeps the best rank any of them earned (a question that only
  matched the Cube column description still surfaces the one served doc);
* the facts the others add that the primary lacks (a column only Cube describes, a caveat, an
  enum) are **folded** into the primary's served text as an "Also described by" block.

The primary is chosen by :mod:`satchel.sources.precedence`. The non-primary docs are not deleted:
they stay in the catalog and item library as provenance.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from satchel.curation.releases import release_items_stmt
from satchel.sources.precedence import folded_text, group_docs, load_sources
from satchel.store.models import ContextItem

if TYPE_CHECKING:
    from satchel.retrieval.search import RankedItem


def apply_precedence(
    session: Session, rid: uuid.UUID, items: dict[uuid.UUID, RankedItem]
) -> None:
    from satchel.retrieval.search import RankedItem

    hits = [
        r for r in items.values()
        if r.type == "asset_doc" and r.scope_kind == "asset" and r.scope_id is not None
    ]
    if not hits:
        return
    scope_ids = {r.scope_id for r in hits if r.scope_id is not None}
    rows = session.execute(
        release_items_stmt(rid).where(
            ContextItem.type == "asset_doc", ContextItem.scope_kind == "asset",
            ContextItem.scope_id.in_(scope_ids),
        )
    )
    groups = group_docs(((i, v) for i, v in rows), load_sources(session))
    for scope_id, group in groups.items():
        if not group.overlapping:
            continue
        mine = [r for r in hits if r.scope_id == scope_id]
        prim_ver = group.primary.version
        assert prim_ver is not None
        keep = items.get(prim_ver.id)
        best_rrf = max((r.rrf for r in mine), default=0.0)
        merged: dict[str, int] = {}
        for r in mine:
            for name, rank in r.sources.items():
                merged[name] = min(rank, merged.get(name, rank))
            if r.version_id != prim_ver.id:
                items.pop(r.version_id, None)
        if keep is None:
            p = group.primary
            keep = RankedItem(
                version_id=prim_ver.id, item_id=p.item.id, key=p.item.key, type=p.item.type,
                seq=prim_ver.seq, scope_kind=p.item.scope_kind, scope_id=p.item.scope_id,
                meta=prim_ver.meta,
            )
            items[prim_ver.id] = keep
        keep.rrf = max(keep.rrf, best_rrf)
        keep.sources = merged
        keep.folded = folded_text(group)
