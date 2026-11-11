"""Idempotent import of a §4 context repository (design §4, §8.2).

Items are keyed by file path (``context_item.key``; ``path#slug`` for several items in one
file). A new ``context_version`` is created only when the content hash changes. Files that
disappear are deprecated. Every import that changes anything publishes a new release
(previous release + changed versions).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from satchel.curation.releases import create_release, latest_published, release_map
from satchel.llm.embeddings import EmbeddingProvider, make_embedder
from satchel.store.db import admin_session
from satchel.store.models import (
    Asset,
    Connection,
    ContextChunk,
    ContextEntityLink,
    ContextItem,
    ContextVersion,
    Domain,
    Workspace,
)
from satchel.sync.embedding_sync import reembed_stale
from satchel.sync.parse import ParsedItem, parse_repo


class ImportValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class ImportReport:
    workspace_id: uuid.UUID
    workspace_slug: str
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deprecated: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    release_id: uuid.UUID | None = None
    release_seq: int | None = None
    release_created: bool = False
    chunks_embedded: int = 0
    eval_suites: list[str] = field(default_factory=list)
    sources: list[Any] = field(default_factory=list)  # satchel.sources.apply.SyncReport
    source_errors: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "created": len(self.created),
            "updated": len(self.updated),
            "unchanged": len(self.unchanged),
            "deprecated": len(self.deprecated),
        }


def content_hash(body: str, meta: dict[str, Any]) -> str:
    payload = json.dumps({"body": body, "meta": meta}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def get_or_create_workspace(
    session: Session, slug: str, name: str | None = None
) -> Workspace:
    ws = session.scalars(select(Workspace).where(Workspace.slug == slug)).first()
    if ws is None:
        ws = Workspace(slug=slug, name=name or slug)
        session.add(ws)
        session.flush()
    return ws


class _AssetIndex:
    """Resolve names written in the repo (``schema.table``, ``table``) to asset ids."""

    def __init__(self, assets: list[Asset]) -> None:
        self._by_fqn: dict[str, list[Asset]] = {}
        for a in assets:
            self._by_fqn.setdefault(a.fqn.lower(), []).append(a)
        self._assets = assets

    def resolve(self, name: str, connection_id: uuid.UUID | None = None) -> uuid.UUID | None:
        n = name.lower()
        cands = list(self._by_fqn.get(n, []))
        if not cands:
            cands = [a for a in self._assets if a.fqn.lower().endswith("." + n)]
        if not cands:
            return None
        if connection_id is not None:
            same = [a for a in cands if a.connection_id == connection_id]
            if same:
                cands = same
        ids = {a.id for a in cands}
        return cands[0].id if len(ids) == 1 else None

    def mentioned_tables(self, text: str) -> list[uuid.UUID]:
        out: list[uuid.UUID] = []
        for a in self._assets:
            if a.kind != "table":
                continue
            if re.search(rf"(?<![\w.]){re.escape(a.fqn)}(?!\w)", text, re.IGNORECASE):
                out.append(a.id)
        return out


def _upsert_connections(
    s: Session, ws_id: uuid.UUID, config: dict[str, Any], needed: set[str]
) -> dict[str, Connection]:
    existing = {
        c.name: c for c in s.scalars(select(Connection).where(Connection.workspace_id == ws_id))
    }
    declared = config.get("connections") or {}
    for name in sorted(set(declared) | needed):
        spec = dict(declared.get(name) or {})
        kind = str(spec.pop("kind", "unknown"))
        conn = existing.get(name)
        if conn is None:
            conn = Connection(workspace_id=ws_id, kind=kind, name=name, config=spec)
            s.add(conn)
            existing[name] = conn
        else:
            # The repo only declares a connection; operators own its real settings (path,
            # secret_ref set via `satchel connection add`), so never overwrite existing keys.
            conn.kind = kind
            conn.config = {**spec, **conn.config}
    s.flush()
    return existing


def _upsert_assets(
    s: Session, ws_id: uuid.UUID, items: list[ParsedItem], conns: dict[str, Connection]
) -> None:
    existing = {
        (a.connection_id, a.fqn): a
        for a in s.scalars(select(Asset).where(Asset.workspace_id == ws_id))
    }

    def upsert(conn_id: uuid.UUID | None, fqn: str, kind: str, parent: Asset | None) -> Asset:
        a = existing.get((conn_id, fqn))
        if a is None:
            a = Asset(
                workspace_id=ws_id,
                connection_id=conn_id,
                kind=kind,
                fqn=fqn,
                parent_id=parent.id if parent else None,
            )
            s.add(a)
            s.flush()
            existing[(conn_id, fqn)] = a
        else:
            a.dropped_at = None
        return a

    for it in items:
        if it.type != "asset_doc" or not it.asset_table:
            continue
        conn = conns.get(it.connection) if it.connection else None
        cid = conn.id if conn else None
        table = upsert(cid, it.asset_table, "table", None)
        for col in it.asset_columns:
            upsert(cid, f"{it.asset_table}.{col}", "column", table)
    s.flush()


def _sync_links(
    s: Session,
    version_id: uuid.UUID,
    it: ParsedItem,
    index: _AssetIndex,
    conns: dict[str, Connection],
) -> None:
    conn = conns.get(it.connection) if it.connection else None
    cid = conn.id if conn else None
    desired: set[tuple[uuid.UUID, str]] = set()
    for name, rel in it.links:
        aid = index.resolve(name, cid)
        if aid is not None:
            desired.add((aid, rel))
    if it.scan_text:
        for aid in index.mentioned_tables(it.scan_text):
            desired.add((aid, "mentions"))
    existing = {
        (link.asset_id, link.relation): link
        for link in s.scalars(
            select(ContextEntityLink).where(ContextEntityLink.version_id == version_id)
        )
    }
    for k, link in existing.items():
        if k not in desired:
            s.delete(link)
    for aid, rel in desired - set(existing):
        s.add(ContextEntityLink(version_id=version_id, asset_id=aid, relation=rel))


def import_context(
    path: str | Path,
    workspace_slug: str,
    *,
    embedder: EmbeddingProvider | None = None,
    engine: Engine | None = None,
) -> ImportReport:
    root = Path(path).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"context path is not a directory: {root}")
    repo = parse_repo(root)
    if repo.errors:
        raise ImportValidationError(repo.errors)
    emb = embedder or make_embedder()

    with admin_session(engine=engine) as s:
        ws = get_or_create_workspace(s, workspace_slug, str(repo.config.get("name") or ""))
        report = ImportReport(ws.id, ws.slug, skipped_files=repo.skipped)
        publish_cfg = repo.config.get("publish")
        if isinstance(publish_cfg, dict):  # e.g. require_evals: [core, retrieval]
            ws.settings = {**(ws.settings or {}), "publish": publish_cfg}

        needed_conns = {i.connection for i in repo.items if i.connection}
        conns = _upsert_connections(s, ws.id, repo.config, {c for c in needed_conns if c})
        domains = {
            d.name: d for d in s.scalars(select(Domain).where(Domain.workspace_id == ws.id))
        }
        for it in repo.items:
            if it.scope_kind == "domain" and it.scope_ref and it.scope_ref not in domains:
                d = Domain(workspace_id=ws.id, name=it.scope_ref)
                s.add(d)
                domains[it.scope_ref] = d
        _upsert_assets(s, ws.id, repo.items, conns)
        s.flush()
        index = _AssetIndex(list(s.scalars(select(Asset).where(Asset.workspace_id == ws.id))))

        existing_items = {
            i.key: i
            for i in s.scalars(select(ContextItem).where(ContextItem.workspace_id == ws.id))
        }
        # (item, version) pairs that need chunks, and (item, version, parsed) for link sync.
        new_versions: list[tuple[ContextVersion, ParsedItem]] = []
        current: list[tuple[ContextItem, uuid.UUID, ParsedItem]] = []

        for it in repo.items:
            scope_id = _scope_id(it, ws.id, conns, domains, index)
            chash = content_hash(it.body, it.meta)
            item = existing_items.get(it.key)
            if item is None:
                item = ContextItem(
                    workspace_id=ws.id,
                    type=it.type,
                    key=it.key,
                    scope_kind=it.scope_kind,
                    scope_id=scope_id,
                    state="published",
                    managed_by="git",
                )
                s.add(item)
                s.flush()
                ver = _new_version(s, item, 1, it, chash)
                new_versions.append((ver, it))
                item.current_version_id = item.published_version_id = ver.id
                report.created.append(it.key)
                current.append((item, ver.id, it))
                continue

            item.scope_kind, item.scope_id, item.type = it.scope_kind, scope_id, it.type
            item.managed_by = "git"
            cvid = item.current_version_id
            cur = s.get(ContextVersion, cvid) if cvid else None
            if cur is None or cur.content_hash != chash:
                seq = (cur.seq if cur else 0) + 1
                ver = _new_version(s, item, seq, it, chash)
                new_versions.append((ver, it))
                item.current_version_id = item.published_version_id = ver.id
                item.state = "published"
                report.updated.append(it.key)
                vid = ver.id
            elif item.state != "published" or item.published_version_id != cur.id:
                item.state, item.published_version_id = "published", cur.id
                report.updated.append(it.key)
                vid = cur.id
            else:
                report.unchanged.append(it.key)
                vid = cur.id
            current.append((item, vid, it))

        # Chunks + embeddings for new versions, embedded in one batch.
        texts = [t for _, it in new_versions for t in it.chunk_texts]
        vectors = emb.embed(texts) if texts else []
        vi = 0
        for ver, it in new_versions:
            for ord_, text in enumerate(it.chunk_texts):
                s.add(
                    ContextChunk(
                        version_id=ver.id,
                        ord=ord_,
                        text=text,
                        embedding=vectors[vi],
                        embedding_model=emb.model_id,
                    )
                )
                vi += 1
        report.chunks_embedded = vi
        s.flush()

        for _, vid, it in current:
            _sync_links(s, vid, it, index, conns)

        # Removed files -> deprecated.
        seen = {it.key for it in repo.items}
        for key, item in existing_items.items():
            if key not in seen and item.managed_by == "git" and item.state != "deprecated":
                item.state = "deprecated"
                item.published_version_id = None
                report.deprecated.append(key)
        s.flush()

        _publish_release(s, ws.id, current, existing_items, report)
        report.chunks_embedded += reembed_stale(s, ws.id, emb)
        from satchel.evals.store import import_repo_suites

        report.eval_suites, suite_errors = import_repo_suites(s, ws.id, root, repo.config)
        if suite_errors:
            raise ImportValidationError(suite_errors)
    from satchel.sources.repo import sync_repo_sources, validate_declared

    problems = validate_declared(repo.config)
    if problems:
        raise ImportValidationError(problems)
    report.sources, report.source_errors = sync_repo_sources(
        repo.config, root, workspace_slug, embedder=emb, engine=engine
    )
    return report


def _new_version(
    s: Session, item: ContextItem, seq: int, it: ParsedItem, chash: str
) -> ContextVersion:
    ver = ContextVersion(
        item_id=item.id,
        seq=seq,
        body=it.body,
        meta=it.meta,
        content_hash=chash,
        provenance={"source": "git", "ref": it.key.split("#")[0]},
    )
    s.add(ver)
    s.flush()
    return ver


def _scope_id(
    it: ParsedItem,
    ws_id: uuid.UUID,
    conns: dict[str, Connection],
    domains: dict[str, Domain],
    index: _AssetIndex,
) -> uuid.UUID | None:
    if it.scope_kind == "domain" and it.scope_ref:
        return domains[it.scope_ref].id
    if it.scope_kind == "connection" and it.scope_ref:
        return conns[it.scope_ref].id
    if it.scope_kind == "asset" and it.scope_ref:
        conn = conns.get(it.connection) if it.connection else None
        return index.resolve(it.scope_ref, conn.id if conn else None)
    return None


def _publish_release(
    s: Session,
    ws_id: uuid.UUID,
    current: list[tuple[ContextItem, uuid.UUID, ParsedItem]],
    existing_items: dict[str, ContextItem],
    report: ImportReport,
) -> None:
    prev = latest_published(s, ws_id)
    prev_map = release_map(s, prev.id if prev else None)
    new_map = dict(prev_map)
    for item, vid, _ in current:
        new_map[item.id] = vid
    for key in report.deprecated:
        new_map.pop(existing_items[key].id, None)

    if prev is not None and new_map == prev_map:
        report.release_id, report.release_seq = prev.id, prev.seq
        return
    rel = create_release(
        s,
        ws_id,
        kind="published",
        mapping=new_map,
        parent_id=prev.id if prev else None,
        note="git import: " + ", ".join(f"{k} {v}" for k, v in report.counts().items() if v),
    )
    report.release_id, report.release_seq, report.release_created = rel.id, rel.seq, True


# Shared with the source-adapter sync (satchel.sources.apply).
AssetIndex = _AssetIndex
sync_links = _sync_links
