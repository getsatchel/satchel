"""Catalog: connections and the asset tree with documentation / endorsement state."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import ColumnElement, exists, func, or_, select
from sqlalchemy.orm import Session

from satchel.api.auth import Auth, Caller
from satchel.api.common import escape_like
from satchel.api.connections import secret_source
from satchel.api.schemas import (
    AssetDetail,
    AssetLinkedItem,
    AssetNode,
    AssetPage,
    ConnectionOut,
    DocRefOut,
)
from satchel.core.item_types import yaml_item_dict
from satchel.sources.precedence import DocRef, group_by_key, load_sources
from satchel.store.db import workspace_session
from satchel.store.models import (
    Asset,
    Connection,
    ContextEntityLink,
    ContextItem,
    ContextVersion,
)


class _Docs:
    """Documentation and endorsement lookups from the *published* context.

    When several sources (or a hand-written doc) describe one asset, the primary doc is used
    (see ``satchel.sources.precedence``) and the others are listed as ``also_described_by``."""

    def __init__(self, s: Session) -> None:
        self.doc: dict[uuid.UUID, tuple[str, bool]] = {}
        self.refs: dict[uuid.UUID, tuple[DocRef, list[DocRef]]] = {}
        groups = group_by_key(
            ((asset_id, item, ver) for asset_id, item, ver in s.execute(
                select(ContextEntityLink.asset_id, ContextItem, ContextVersion)
                .join(ContextVersion, ContextVersion.id == ContextEntityLink.version_id)
                .join(ContextItem, ContextItem.published_version_id == ContextVersion.id)
                .where(
                    ContextItem.type == "asset_doc",
                    ContextEntityLink.relation == "defines",
                    ContextItem.state != "deprecated",
                )
            )),
            load_sources(s),
        )
        for asset_id, g in groups.items():
            prim = g.primary
            self.doc[asset_id] = (
                prim.item.key, bool(prim.version.meta.get("endorsed")) if prim.version else False
            )
            self.refs[asset_id] = (prim.ref, [m.ref for m in g.others])
        self.endorse: dict[str, tuple[bool, bool]] = {}
        for meta, body in s.execute(
            select(ContextVersion.meta, ContextVersion.body)
            .join(ContextItem, ContextItem.published_version_id == ContextVersion.id)
            .where(ContextItem.type == "endorsement", ContextItem.state != "deprecated")
        ):
            d = yaml_item_dict("endorsement", body, meta)
            if d.get("asset"):
                self.endorse[str(d["asset"]).lower()] = (
                    bool(d.get("endorsed")), bool(d.get("excluded_from_ai")),
                )

    def node(self, a: Asset, conn_name: str | None) -> AssetNode:
        doc = self.doc.get(a.id)
        e_flag, excluded = self.endorse.get(a.fqn.lower(), (False, False))
        prim, others = self.refs.get(a.id, (None, []))
        return AssetNode(
            id=a.id, connection=conn_name, kind=a.kind, fqn=a.fqn, name=a.fqn.split(".")[-1],
            data_type=a.data_type, dropped=a.dropped_at is not None, has_doc=doc is not None,
            doc_item_key=doc[0] if doc else None, endorsed=bool(doc and doc[1]) or e_flag,
            excluded_from_ai=excluded,
            primary=DocRefOut(source=prim.label, key=prim.key, primary=True) if prim else None,
            also_described_by=[DocRefOut(source=o.label, key=o.key) for o in others],
        )


def _tree(s: Session, roots: list[Asset], conn_names: dict[uuid.UUID, str]) -> list[AssetNode]:
    docs = _Docs(s)
    nodes: dict[uuid.UUID, AssetNode] = {}
    level = roots
    out = [docs.node(a, conn_names.get(a.connection_id) if a.connection_id else None)
           for a in roots]
    for a, n in zip(roots, out, strict=True):
        nodes[a.id] = n
    for _ in range(5):  # database > schema > table > column
        if not level:
            break
        kids = list(
            s.scalars(
                select(Asset).where(Asset.parent_id.in_([a.id for a in level])).order_by(Asset.fqn)
            )
        )
        for k in kids:
            node = docs.node(k, conn_names.get(k.connection_id) if k.connection_id else None)
            nodes[k.id] = node
            if k.parent_id in nodes:
                nodes[k.parent_id].children.append(node)
        level = kids
    return out


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["catalog"])
    Any_ = Annotated[Caller, Depends(auth.require("explorer"))]
    eng = auth.ctx.engine

    @r.get("/v1/connections", response_model=list[ConnectionOut])
    def list_connections(who: Any_) -> list[ConnectionOut]:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            n = (
                select(func.count())
                .where(Asset.connection_id == Connection.id, Asset.dropped_at.is_(None))
                .scalar_subquery()
            )
            return [
                ConnectionOut(
                    id=c.id, name=c.name, kind=c.kind, default_for_ai=c.default_for_ai,
                    has_secret=c.secret_ref is not None, asset_count=cnt,
                    secret=secret_source(c.secret_ref),
                    config=dict(c.config or {}) if who.has_role("admin") else None,
                )
                for c, cnt in s.execute(select(Connection, n).order_by(Connection.name))
            ]

    @r.get("/v1/assets", response_model=AssetPage)
    def list_assets(
        who: Any_,
        connection: Annotated[str | None, Query(description="Connection name")] = None,
        q: Annotated[str | None, Query(description="Substring of an asset name")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> AssetPage:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            conn_names = {c.id: c.name for c in s.scalars(select(Connection))}
            conds: list[ColumnElement[bool]] = [Asset.parent_id.is_(None)]
            if connection:
                cid = next((i for i, n in conn_names.items() if n == connection), None)
                if cid is None:
                    raise HTTPException(404, f"no such connection: {connection}")
                conds.append(Asset.connection_id == cid)
            if q and q.strip():
                pat = f"%{escape_like(q.strip())}%"
                child = Asset.__table__.alias("child")
                conds.append(
                    or_(
                        Asset.fqn.ilike(pat, escape="\\"),
                        exists().where(child.c.parent_id == Asset.id,
                                       child.c.fqn.ilike(pat, escape="\\")),
                    )
                )
            total = s.scalar(select(func.count()).select_from(Asset).where(*conds)) or 0
            roots = list(
                s.scalars(
                    select(Asset).where(*conds).order_by(Asset.fqn).limit(limit).offset(offset)
                )
            )
            return AssetPage(total=total, limit=limit, offset=offset,
                             assets=_tree(s, roots, conn_names))

    @r.get("/v1/assets/{asset_id}", response_model=AssetDetail)
    def get_asset(asset_id: uuid.UUID, who: Any_) -> AssetDetail:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            a = s.get(Asset, asset_id)
            if a is None:
                raise HTTPException(404, "no such asset")
            conn_names = {c.id: c.name for c in s.scalars(select(Connection))}
            node = _tree(s, [a], conn_names)[0]
            linked = s.execute(
                select(ContextItem.key, ContextItem.type, ContextItem.state,
                       ContextEntityLink.relation)
                .join(ContextVersion, ContextVersion.item_id == ContextItem.id)
                .join(ContextEntityLink, ContextEntityLink.version_id == ContextVersion.id)
                .where(
                    ContextEntityLink.asset_id == asset_id,
                    or_(
                        ContextVersion.id == ContextItem.published_version_id,
                        ContextVersion.id == ContextItem.current_version_id,
                    ),
                )
                .distinct()
                .order_by(ContextItem.key, ContextEntityLink.relation)
            )
            return AssetDetail(
                **node.model_dump(), stats=a.stats, first_seen_at=a.first_seen_at,
                last_seen_at=a.last_seen_at,
                linked_items=[AssetLinkedItem(key=k, type=t, state=st, relation=rel)
                              for k, t, st, rel in linked],
            )

    return r
