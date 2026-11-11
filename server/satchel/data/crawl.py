"""Schema crawler (design §11): introspect a connection and upsert ``asset`` rows, mark
vanished assets ``dropped_at``, and import DB comments as *draft* ``asset_doc`` items when the
table has no doc yet. Nothing is published."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as OrmSession

from satchel.core.item_types import normalized_meta
from satchel.data.connections import open_connector
from satchel.data.connectors import AssetSpec
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import Asset, Connection, ContextItem, ContextVersion, Workspace
from satchel.sync.importer import content_hash

CATALOG_KINDS = ("database", "schema", "table", "column")


@dataclass
class CrawlReport:
    connection: str
    created: int = 0
    updated: int = 0
    dropped: int = 0
    revived: int = 0
    draft_docs: int = 0
    seen: int = 0


def crawl_specs(s: OrmSession, conn: Connection, specs: list[AssetSpec]) -> CrawlReport:
    now = datetime.now(UTC)
    rep = CrawlReport(conn.name, seen=len(specs))
    existing = {
        a.fqn: a
        for a in s.scalars(
            select(Asset).where(
                Asset.workspace_id == conn.workspace_id,
                Asset.connection_id == conn.id,
                Asset.kind.in_(CATALOG_KINDS),
            )
        )
    }
    by_fqn: dict[str, Asset] = {}
    # specs are ordered parents-first
    for sp in specs:
        a = existing.get(sp.fqn)
        parent = by_fqn.get(sp.parent_fqn) if sp.parent_fqn else None
        if a is None:
            a = Asset(workspace_id=conn.workspace_id, connection_id=conn.id, kind=sp.kind,
                      fqn=sp.fqn, parent_id=parent.id if parent else None,
                      data_type=sp.data_type, last_seen_at=now)
            s.add(a)
            s.flush()
            rep.created += 1
        else:
            if a.dropped_at is not None:
                rep.revived += 1
            changed = (a.data_type != sp.data_type or a.dropped_at is not None
                       or a.parent_id != (parent.id if parent else None))
            a.kind, a.data_type, a.dropped_at, a.last_seen_at = sp.kind, sp.data_type, None, now
            a.parent_id = parent.id if parent else None
            if changed:
                rep.updated += 1
        by_fqn[sp.fqn] = a
    for fqn, a in existing.items():
        if fqn not in by_fqn and a.dropped_at is None:
            a.dropped_at = now
            rep.dropped += 1
    s.flush()
    rep.draft_docs = _draft_docs(s, conn, specs, by_fqn)
    return rep


def _draft_docs(
    s: OrmSession, conn: Connection, specs: list[AssetSpec], by_fqn: dict[str, Asset]
) -> int:
    cols: dict[str, list[AssetSpec]] = {}
    for sp in specs:
        if sp.kind == "column" and sp.comment and sp.parent_fqn:
            cols.setdefault(sp.parent_fqn, []).append(sp)
    n = 0
    for sp in specs:
        if sp.kind != "table" or not (sp.comment or sp.fqn in cols):
            continue
        table = by_fqn[sp.fqn]
        has_doc = s.scalar(
            select(ContextItem.id).where(
                ContextItem.workspace_id == conn.workspace_id,
                ContextItem.type == "asset_doc",
                ContextItem.scope_kind == "asset",
                ContextItem.scope_id == table.id,
            ).limit(1)
        )
        if has_doc is not None:
            continue
        doc: dict[str, object] = {"asset": sp.fqn}
        if sp.comment:
            doc["description"] = sp.comment
        if sp.fqn in cols:
            doc["columns"] = [
                {"name": c.fqn.rsplit(".", 1)[1], "description": c.comment} for c in cols[sp.fqn]
            ]
        body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
        item = ContextItem(
            workspace_id=conn.workspace_id, type="asset_doc",
            key=f"crawl/{conn.name}/{sp.fqn}.yaml", scope_kind="asset", scope_id=table.id,
            state="draft", managed_by="ui",
        )
        s.add(item)
        s.flush()
        enriched_meta = normalized_meta("asset_doc", body, {"asset": sp.fqn, "endorsed": False})
        ver = ContextVersion(
            item_id=item.id, seq=1, body=body,
            meta=enriched_meta,
            content_hash=content_hash(body, enriched_meta),
            provenance={"source": "crawl", "ref": conn.name},
        )
        s.add(ver)
        s.flush()
        item.current_version_id = ver.id  # draft: never published_version_id
        n += 1
    s.flush()
    return n


def crawl_connection(
    workspace_slug: str, name: str, *, engine: Engine | None = None,
    settings: Settings | None = None,
) -> CrawlReport:
    with admin_session(engine=engine) as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == workspace_slug)).first()
        if ws is None:
            raise LookupError(f"unknown workspace: {workspace_slug}")
        conn = s.scalars(
            select(Connection).where(Connection.workspace_id == ws.id, Connection.name == name)
        ).first()
        if conn is None:
            raise LookupError(f"unknown connection: {name}")
        connector = open_connector(conn, settings)
        try:
            specs = connector.introspect()
        finally:
            connector.close()
        return crawl_specs(s, conn, specs)


__all__ = ["CrawlReport", "crawl_connection", "crawl_specs"]
