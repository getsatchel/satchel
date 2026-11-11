"""Library: browse items, versions and diffs (any authenticated role)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from satchel.api.auth import Auth, Caller
from satchel.api.common import (
    escape_like,
    get_item,
    get_version,
    make_diff,
    summaries,
)
from satchel.api.schemas import (
    DiffOut,
    DocRefOut,
    ItemDetail,
    ItemPage,
    VersionOut,
    VersionSummary,
)
from satchel.sources.precedence import doc_group_of
from satchel.store.db import workspace_session
from satchel.store.models import ContextItem, ContextVersion


def version_summaries(
    versions: list[ContextVersion], item: ContextItem
) -> list[VersionSummary]:
    return [
        VersionSummary(
            seq=v.seq,
            content_hash=v.content_hash,
            author_user_id=v.author_user_id,
            provenance=v.provenance,
            created_at=v.created_at,
            is_current=v.id == item.current_version_id,
            is_published=v.id == item.published_version_id,
        )
        for v in versions
    ]


def item_detail(s: Session, item: ContextItem) -> ItemDetail:
    base = summaries(s, [item])[0]
    versions = list(
        s.scalars(
            select(ContextVersion)
            .where(ContextVersion.item_id == item.id)
            .order_by(ContextVersion.seq.desc())
        )
    )
    cur = next((v for v in versions if v.id == item.current_version_id), None)
    group = doc_group_of(s, item)
    return ItemDetail(
        **base.model_dump(),
        primary=group.primary.item.id == item.id if group else None,
        also_described_by=[
            DocRefOut(source=m.ref.label, key=m.ref.key, primary=m is group.primary)
            for m in group.members if m.item.id != item.id
        ] if group else [],
        body=cur.body if cur else None,
        meta=cur.meta if cur else {},
        content_hash=cur.content_hash if cur else None,
        versions=version_summaries(versions, item),
    )


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["library"])
    Any_ = Annotated[Caller, Depends(auth.require("explorer"))]
    eng = auth.ctx.engine

    @r.get("/v1/items", response_model=ItemPage)
    def list_items(
        who: Any_,
        type: Annotated[str | None, Query()] = None,
        state: Annotated[str | None, Query()] = None,
        scope: Annotated[str | None, Query(description="scope_kind")] = None,
        managed_by: Annotated[str | None, Query(pattern="^(ui|git|source)$")] = None,
        q: Annotated[str | None, Query(description="Text search over key and body")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ItemPage:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            conds = []
            if type:
                conds.append(ContextItem.type == type)
            if state:
                conds.append(ContextItem.state == state)
            if scope:
                conds.append(ContextItem.scope_kind == scope)
            if managed_by:
                conds.append(ContextItem.managed_by == managed_by)
            if q and q.strip():
                pat = f"%{escape_like(q.strip())}%"
                body_hit = exists().where(
                    ContextVersion.id == ContextItem.current_version_id,
                    ContextVersion.body.ilike(pat, escape="\\"),
                )
                conds.append(or_(ContextItem.key.ilike(pat, escape="\\"), body_hit))
            total = s.scalar(select(func.count()).select_from(ContextItem).where(*conds)) or 0
            items = list(
                s.scalars(
                    select(ContextItem)
                    .where(*conds)
                    .order_by(ContextItem.key)
                    .limit(limit)
                    .offset(offset)
                )
            )
            return ItemPage(
                total=total, limit=limit, offset=offset, items=summaries(s, items)
            )

    # Specific /v1/items/{key}/... routes must be registered before the greedy {key:path}.
    @r.get("/v1/items/{key:path}/versions/{seq}", response_model=VersionOut)
    def get_item_version(key: str, seq: int, who: Any_) -> VersionOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            item = get_item(s, key)
            v = get_version(s, item, seq)
            return VersionOut(
                **version_summaries([v], item)[0].model_dump(),
                key=item.key, body=v.body, meta=v.meta,
            )

    @r.get("/v1/items/{key:path}/diff", response_model=DiffOut)
    def diff_item(
        key: str,
        who: Any_,
        from_seq: Annotated[
            int | None, Query(alias="from", description="default: published version")
        ] = None,
        to_seq: Annotated[
            int | None, Query(alias="to", description="default: current version")
        ] = None,
    ) -> DiffOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            item = get_item(s, key)
            a = (
                get_version(s, item, from_seq)
                if from_seq is not None
                else (s.get(ContextVersion, item.published_version_id)
                      if item.published_version_id else None)
            )
            b = (
                get_version(s, item, to_seq)
                if to_seq is not None
                else (s.get(ContextVersion, item.current_version_id)
                      if item.current_version_id else None)
            )
            if a is None and b is None:
                raise HTTPException(404, f"{key} has no versions to compare")
            return make_diff(item.key, a, b)

    @r.get("/v1/items/{key:path}", response_model=ItemDetail)
    def get_item_detail(key: str, who: Any_) -> ItemDetail:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            return item_detail(s, get_item(s, key))

    return r
