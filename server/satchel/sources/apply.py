"""Map a :class:`~satchel_adapter_sdk.SourceSnapshot` onto context items (design §8.3).

One shared function for every adapter. It is idempotent: an item version is created only when its
content hash changes, an identical second sync creates no versions and no release. It reuses the
git importer's helpers (content hash, chunking, entity links, release creation).

Field ownership: each item version keeps a ``source`` block (replaced on every sync) and an
``overlay`` block (analyst additions, carried into each new version). If a sync removes something
the overlay refers to, an ``overlay_out_of_sync`` hygiene signal is emitted.

Overlaps: when another source or a hand-written doc describes the same table, the sync report
lists it (``overlaps``) with the primary chosen by ``priority`` (lower wins), see
``satchel.sources.precedence``.

Publishing follows the source's ``publish`` mode: ``auto`` publishes source changes as a new
release like a git import on the default branch (unless an eval gate is configured and not
passing, then a preview is left for a human); ``preview`` only ever creates a preview release.
Overlays are never created by a sync, and an analyst's unpublished overlay draft is never
published by one (the item is "held").
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any

from satchel_adapter_sdk import (
    SourceAsset,
    SourceQuery,
    SourceSnapshot,
    is_compatible,
    normalise_fqn,
)
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from satchel.curation.hygiene import HygieneReport, emit_finding
from satchel.curation.releases import create_release, latest_published, next_seq, release_map
from satchel.data.semantic import PublishedModel, SemanticError, parse_mdl, shadowed_models
from satchel.evals.gate import evaluate_gate, load_gate_config
from satchel.llm.embeddings import EmbeddingProvider
from satchel.sources import items as it
from satchel.sources.precedence import (
    SourceInfo,
    describe,
    doc_ref,
    group_by_key,
    load_sources,
)
from satchel.store.models import (
    Asset,
    Connection,
    ContextChunk,
    ContextItem,
    ContextSource,
    ContextVersion,
    Release,
)
from satchel.sync.embedding_sync import reembed_stale
from satchel.sync.importer import AssetIndex, content_hash, sync_links
from satchel.sync.parse import ParsedItem, slugify


class SourceSyncError(Exception):
    """A sync that cannot proceed (bad config, unreadable input, incompatible IR)."""


@dataclass
class SyncReport:
    source: str
    kind: str
    publish: str = "auto"
    status: str = "ok"
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deprecated: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    warnings: list[dict[str, str | None]] = field(default_factory=list)
    overlaps: list[dict[str, Any]] = field(default_factory=list)
    release_id: uuid.UUID | None = None
    release_seq: int | None = None
    release_kind: str | None = None
    release_created: bool = False
    gate_status: str | None = None
    gate_message: str = ""
    signals: dict[str, int] = field(default_factory=dict)
    assets_created: int = 0
    chunks_embedded: int = 0
    summary: dict[str, int] = field(default_factory=dict)
    fingerprint: str = ""
    # The commit synced, for sources read from git (satchel.sources.git).
    git_commit: str | None = None
    # The dbt version that built the manifest (dbt sources built with dbt parse).
    dbt_version: str | None = None

    def counts(self) -> dict[str, int]:
        return {
            "created": len(self.created), "updated": len(self.updated),
            "unchanged": len(self.unchanged), "deprecated": len(self.deprecated),
            "held": len(self.held),
        }

    def changed(self) -> bool:
        return bool(self.created or self.updated or self.deprecated)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["release_id"] = str(self.release_id) if self.release_id else None
        d["counts"] = self.counts()
        return d


@dataclass
class _Desired:
    key: str
    build: Callable[[dict[str, Any]], it.ItemContent]
    asset: SourceAsset | None = None


def _desired(src: ContextSource, snap: SourceSnapshot, conn: Connection | None) -> list[_Desired]:
    name, kind = src.name, src.kind
    prio = int(src.config.get("priority") or 0)
    out: list[_Desired] = []
    for a in snap.assets:
        out.append(_Desired(
            f"sources/{name}/assets/{a.fqn}.yaml",
            partial(it.asset_content, name, kind, prio, a), a,
        ))
    for m in snap.semantic_models:
        out.append(_Desired(
            f"sources/{name}/semantic/{slugify(m.name)}.mdl.json",
            partial(it.semantic_content, name, kind, prio, m),
        ))
    for q in snap.queries:
        out.append(_Desired(
            f"sources/{name}/queries/{slugify(q.key)}.yaml",
            partial(_query, name, kind, prio, q, conn.name if conn else None),
        ))
    for t in snap.terms:
        out.append(_Desired(
            f"sources/{name}/glossary/{slugify(t.key)}.yaml",
            partial(it.term_content, name, kind, prio, t),
        ))
    return out


def _query(
    name: str, kind: str, prio: int, q: SourceQuery, connection: str | None, overlay: dict[str, Any]
) -> it.ItemContent:
    return it.query_content(name, kind, prio, q, overlay, connection)


class _Assets:
    """Find or create the warehouse ``asset`` rows a source describes (design §11)."""

    def __init__(self, s: Session, ws_id: uuid.UUID, conn: Connection | None) -> None:
        self.s, self.ws_id = s, ws_id
        self.cid = conn.id if conn else None
        self.by = {
            (a.connection_id, a.fqn.lower()): a
            for a in s.scalars(select(Asset).where(Asset.workspace_id == ws_id))
        }
        self.created = 0

    def table(self, a: SourceAsset) -> Asset:
        cands = [normalise_fqn(f"{a.database}.{a.fqn}")] if a.database else []
        cands.append(a.fqn.lower())
        row = next((self.by[(self.cid, c)] for c in cands if (self.cid, c) in self.by), None)
        if row is None:
            row = self._new(a.fqn, "table", None, None)
        for col in a.columns:
            ckey = (self.cid, f"{row.fqn}.{col.name}".lower())
            existing = self.by.get(ckey)
            if existing is None:
                self._new(f"{row.fqn}.{col.name}", "column", row, col.data_type)
            elif existing.data_type is None and col.data_type:
                existing.data_type = col.data_type
        return row

    def _new(self, fqn: str, kind: str, parent: Asset | None, dtype: str | None) -> Asset:
        row = Asset(workspace_id=self.ws_id, connection_id=self.cid, kind=kind, fqn=fqn,
                    parent_id=parent.id if parent else None, data_type=dtype)
        self.s.add(row)
        self.s.flush()
        self.by[(self.cid, fqn.lower())] = row
        self.created += 1
        return row


def apply_snapshot(
    s: Session, src: ContextSource, snap: SourceSnapshot, *, embedder: EmbeddingProvider,
    now: datetime | None = None,
) -> SyncReport:
    """Apply ``snap`` to ``src``'s items. ``s`` is an open transaction (admin or workspace)."""
    now = now or datetime.now(UTC)
    if not is_compatible(snap.ir_version):
        raise SourceSyncError(
            f"adapter {src.kind!r} produced IR {snap.ir_version}, which this satchel cannot read"
        )
    ws_id = src.workspace_id
    publish = str(src.config.get("publish") or "auto")
    report = SyncReport(
        src.name, src.kind, publish=publish, summary=snap.summary(),
        fingerprint=snap.fingerprint,
        warnings=[w.model_dump() for w in snap.warnings],
    )
    conn = s.get(Connection, src.connection_id) if src.connection_id else None
    desired = _desired(src, snap, conn)
    keys = [d.key for d in desired]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise SourceSyncError(f"the adapter produced colliding item keys: {', '.join(dup)}")

    items = {
        i.key: i
        for i in s.scalars(select(ContextItem).where(ContextItem.workspace_id == ws_id))
    }
    for k in keys:
        other = items.get(k)
        if other is not None and other.source_id != src.id:
            raise SourceSyncError(
                f"item key {k!r} already exists and is not managed by source {src.name!r}"
            )

    assets = _Assets(s, ws_id, conn)
    table_for: dict[str, Asset] = {}
    for d in desired:
        if d.asset is not None:
            table_for[d.key] = assets.table(d.asset)
    report.assets_created = assets.created
    index = AssetIndex(list(s.scalars(select(Asset).where(Asset.workspace_id == ws_id))))
    conns = {conn.name: conn} if conn else {}

    new_versions: list[tuple[ContextVersion, it.ItemContent]] = []
    current: list[tuple[ContextItem, uuid.UUID, it.ItemContent]] = []
    held: set[uuid.UUID] = set()
    findings: list[tuple[ContextItem, it.ItemContent]] = []

    for d in desired:
        item = items.get(d.key)
        cur = s.get(ContextVersion, item.current_version_id) if item and item.current_version_id \
            else None
        overlay = dict((cur.meta or {}).get("overlay") or {}) if cur else {}
        c = d.build(overlay)
        chash = content_hash(c.body, c.meta)
        scope_id = _scope_id(c, d.key, table_for, conn)
        prov = {"source": "source", "ref": src.name, "kind": src.kind, "key": c.source_key,
                "fingerprint": snap.fingerprint}
        if item is None:
            item = ContextItem(
                workspace_id=ws_id, type=c.type, key=c.key, scope_kind=c.scope_kind,
                scope_id=scope_id, state="draft", managed_by="source", source_id=src.id,
                source_key=c.source_key,
            )
            s.add(item)
            s.flush()
            items[c.key] = item
            ver = _new_version(s, item, 1, c, chash, prov)
            new_versions.append((ver, c))
            item.current_version_id = ver.id
            report.created.append(c.key)
            current.append((item, ver.id, c))
        else:
            item.scope_kind, item.scope_id, item.type = c.scope_kind, scope_id, c.type
            item.source_key = c.source_key
            revived = item.state == "deprecated"
            if cur is not None and item.published_version_id:
                pub = s.get(ContextVersion, item.published_version_id)
                if (
                    pub is not None and pub.id != cur.id
                    and (pub.meta or {}).get("overlay") != (cur.meta or {}).get("overlay")
                ):
                    held.add(item.id)  # an analyst's unpublished overlay draft: not ours to publish
                    report.held.append(c.key)
            if cur is None or cur.content_hash != chash:
                ver = _new_version(s, item, (cur.seq if cur else 0) + 1, c, chash, prov)
                new_versions.append((ver, c))
                item.current_version_id = ver.id
                report.updated.append(c.key)
                vid = ver.id
            else:
                vid = cur.id
                (report.updated if revived else report.unchanged).append(c.key)
            if revived:
                item.state = "published" if item.published_version_id else "draft"
            current.append((item, vid, c))
        findings.append((item, c))

    texts = [t for _, c in new_versions for t in c.chunks]
    vectors = embedder.embed(texts) if texts else []
    vi = 0
    for ver, c in new_versions:
        for ord_, text in enumerate(c.chunks):
            s.add(ContextChunk(version_id=ver.id, ord=ord_, text=text, embedding=vectors[vi],
                               embedding_model=embedder.model_id))
            vi += 1
    report.chunks_embedded = vi
    s.flush()
    for _item, vid, c in current:
        parsed = ParsedItem(
            key=c.key, type=c.type, body=c.body, meta=c.meta, chunk_texts=c.chunks,
            links=c.links, connection=conn.name if conn else None,
        )
        sync_links(s, vid, parsed, index, conns)

    seen = set(keys)
    gone: list[ContextItem] = []
    for key, item in items.items():
        if item.source_id == src.id and key not in seen and item.state != "deprecated":
            item.state = "deprecated"
            report.deprecated.append(key)
            gone.append(item)
    s.flush()

    _overlaps(s, src, desired, table_for, report)
    _emit_findings(s, src, findings, gone, report, now)

    if report.changed():
        _release(s, src, conn, report, held, publish)
    report.chunks_embedded += reembed_stale(s, ws_id, embedder)
    return report


def _scope_id(
    c: it.ItemContent, key: str, table_for: dict[str, Asset], conn: Connection | None
) -> uuid.UUID | None:
    if c.scope_kind == "asset":
        return table_for[key].id
    if c.scope_kind == "connection" and conn is not None:
        return conn.id
    return None


def _new_version(
    s: Session, item: ContextItem, seq: int, c: it.ItemContent, chash: str, prov: dict[str, Any]
) -> ContextVersion:
    ver = ContextVersion(item_id=item.id, seq=seq, body=c.body, meta=c.meta, content_hash=chash,
                         provenance=prov)
    s.add(ver)
    s.flush()
    return ver


# ---- hygiene findings ---------------------------------------------------------------------


def _emit_findings(
    s: Session, src: ContextSource, findings: list[tuple[ContextItem, it.ItemContent]],
    gone: list[ContextItem], report: SyncReport, now: datetime,
) -> None:
    hr = HygieneReport()
    for item, c in findings:
        overlay = c.meta.get("overlay") or {}
        block = c.meta.get("source") or {}
        if c.type == "asset_doc" and overlay:
            have = {col["name"].lower() for col in (block.get("asset") or {}).get("columns") or []}
            missing = sorted(k for k in (overlay.get("columns") or {}) if k.lower() not in have)
            if missing:
                emit_finding(
                    s, src.workspace_id, hr, "overlay_out_of_sync",
                    f"{item.key}:{','.join(missing)}",
                    f"The analyst overlay on {item.key} mentions column(s) {', '.join(missing)} "
                    f"that {src.kind} no longer provides for {c.scope_asset}. Move or delete "
                    "those notes.",
                    {"item_key": item.key, "missing_columns": missing, "source": src.name},
                    item_ids=[item.id], now=now,
                )
        asset = block.get("asset") or {}
        if (
            c.type == "asset_doc" and asset.get("endorsement_hint")
            and asset.get("endorsed") is None and overlay.get("endorsed") is None
        ):
            emit_finding(
                s, src.workspace_id, hr, "endorsement_candidate", item.key,
                f"{c.scope_asset} looks endorsable: {asset['endorsement_hint']}. Endorse it if "
                "it is the table analysts should use.",
                {"item_key": item.key, "reason": asset["endorsement_hint"], "source": src.name},
                item_ids=[item.id],
                asset_ids=[item.scope_id] if item.scope_id else [], now=now,
            )
    for item in gone:
        ver = s.get(ContextVersion, item.current_version_id) if item.current_version_id else None
        overlay = (ver.meta or {}).get("overlay") if ver else None
        if overlay:
            emit_finding(
                s, src.workspace_id, hr, "overlay_out_of_sync", f"{item.key}:removed",
                f"{item.key} no longer exists in {src.kind} source {src.name!r}, but analysts "
                "had added notes to it. Review whether they belong elsewhere.",
                {"item_key": item.key, "removed": True, "source": src.name},
                item_ids=[item.id], now=now,
            )
    report.signals = dict(hr.emitted)


def _overlaps(
    s: Session, src: ContextSource, desired: list[_Desired], table_for: dict[str, Asset],
    report: SyncReport,
) -> None:
    """List the tables this sync describes that another source (or a hand-written doc) also
    describes, and which doc is primary. Every doc is kept as provenance; retrieval, the catalog
    and the signals use the primary (``satchel.sources.precedence``). Also lists semantic models
    whose names collide with a better-ranked one, which ``query_metrics`` then skips."""
    sources = load_sources(s)
    fqn_of = {table_for[d.key].id: d.asset.fqn for d in desired if d.key in table_for and d.asset}
    if fqn_of:
        rows = s.scalars(
            select(ContextItem).where(
                ContextItem.workspace_id == src.workspace_id, ContextItem.type == "asset_doc",
                ContextItem.scope_kind == "asset", ContextItem.scope_id.in_(list(fqn_of)),
                ContextItem.state != "deprecated",
                # an unpublished hand-written draft (e.g. a crawler draft) is not served
                or_(ContextItem.state == "published", ContextItem.source_id.is_not(None)),
            )
        )
        groups = group_by_key(((i.scope_id, i, None) for i in rows if i.scope_id), sources)
        for aid, g in sorted(groups.items(), key=lambda kv: fqn_of.get(kv[0], "")):
            if not g.overlapping:
                continue
            fqn = fqn_of[aid]
            prim = g.primary.ref
            report.overlaps.append({
                "type": "asset_doc", "asset": fqn, "primary": describe(prim),
                "also_described_by": [describe(m.ref) for m in g.others],
                "this_source_is_primary": prim.source == src.name,
            })
            others = ", ".join(f"{m.ref.label} (priority {m.ref.priority})" for m in g.others)
            report.warnings.append({
                "code": "also_described_by",
                "message": f"{fqn} is also described by {others}; {prim.label!r} (priority "
                           f"{prim.priority}) is primary and is the doc agents are served",
                "key": fqn,
            })
    _semantic_overlaps(s, src, desired, sources, report)


def _semantic_overlaps(
    s: Session, src: ContextSource, desired: list[_Desired], sources: dict[uuid.UUID, SourceInfo],
    report: SyncReport,
) -> None:
    mine = [d for d in desired if "/semantic/" in d.key]
    if not mine:
        return
    models: list[PublishedModel] = []
    for item in s.scalars(
        select(ContextItem).where(
            ContextItem.workspace_id == src.workspace_id, ContextItem.type == "semantic_model",
            ContextItem.state != "deprecated",
        )
    ):
        ver = s.get(ContextVersion, item.current_version_id) if item.current_version_id else None
        if ver is None or (ver.meta or {}).get("format") != "wren_mdl":
            continue
        try:
            mdl = parse_mdl(ver.body)
        except SemanticError:
            continue
        ref = doc_ref(item, sources)
        models.append(PublishedModel(item.id, item.key, ver.seq, mdl, rank=ref.sort_key(),
                                     handwritten=ref.handwritten))
    models.sort(key=lambda m: m.rank)
    skipped = shadowed_models(models)
    for d in mine:
        reason = skipped.get(d.key)
        if reason is None:
            continue
        msg = (f"semantic model {d.key} {reason}; query_metrics serves the other one and skips "
               "this one. Give the metrics different names, or change the source priorities")
        report.overlaps.append({"type": "semantic_model", "key": d.key, "shadowed": True,
                                "reason": reason})
        report.warnings.append({"code": "semantic_shadowed", "message": msg, "key": d.key})


# ---- publishing ---------------------------------------------------------------------------


def _release(
    s: Session, src: ContextSource, conn: Connection | None, report: SyncReport,
    held: set[uuid.UUID], publish: str,
) -> None:
    ws_id = src.workspace_id
    mine = list(s.scalars(select(ContextItem).where(ContextItem.source_id == src.id)))
    head = latest_published(s, ws_id)
    base = release_map(s, head.id if head else None)
    mapping = dict(base)
    for item in mine:
        if item.id in held:
            continue
        if item.state == "deprecated":
            mapping.pop(item.id, None)
        elif item.current_version_id:
            mapping[item.id] = item.current_version_id
    if head is not None and mapping == base:
        report.release_id, report.release_seq, report.release_kind = head.id, head.seq, "published"
        return
    note = f"source sync {src.name}: " + ", ".join(
        f"{k} {v}" for k, v in report.counts().items() if v
    )
    gate_cfg = load_gate_config(s, ws_id)
    if publish == "auto" and not gate_cfg.require:
        rel = create_release(s, ws_id, kind="published", mapping=mapping,
                             parent_id=head.id if head else None, note=note)
        _point(s, mine, held)
        report.release_id, report.release_seq = rel.id, rel.seq
        report.release_kind, report.release_created = "published", True
        return
    rel = create_release(s, ws_id, kind="preview", mapping=mapping,
                         parent_id=head.id if head else None, note=note)
    report.release_id, report.release_seq = rel.id, rel.seq
    report.release_kind, report.release_created = "preview", True
    if publish == "preview":
        return
    gate = evaluate_gate(s, ws_id, rel)
    report.gate_status = gate.status
    if gate.status in ("none", "pass"):
        _promote(s, rel)
        _point(s, mine, held)
        report.release_seq, report.release_kind = rel.seq, "published"
        return
    report.gate_message = (
        f"the eval gate is {gate.status}: the changes wait in preview #{rel.seq}. Run the "
        "required evals on it, then publish it from the UI"
    )


def _promote(s: Session, rel: Release) -> None:
    rel.seq = next_seq(s, rel.workspace_id, "published")
    rel.kind = "published"
    s.flush()


def _point(s: Session, mine: list[ContextItem], held: set[uuid.UUID]) -> None:
    """Advance published pointers after a release containing the source's items was published."""
    for item in mine:
        if item.id in held:
            continue
        if item.state == "deprecated":
            item.published_version_id = None
        elif item.current_version_id:
            item.published_version_id = item.current_version_id
            if item.state == "draft":
                item.state = "published"
    s.flush()
