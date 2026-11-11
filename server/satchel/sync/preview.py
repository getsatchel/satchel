"""Pull-request previews of a context repo (design §8.2): build a preview release from a folder
without changing what the workspace serves or drafts.

The preview is the latest published release plus the folder's changes. Only the files are
compared: a file whose content differs from the version in that release gets a version that is
**not** made current, so the workspace's drafts, Changes and Library are untouched. A file new in
the pull request gets an item that has no current version (state ``deprecated``, so the catalog,
curation and source precedence ignore it) and is hidden from the Library. Git-managed items whose
file is gone are left out of the preview. When the pull request merges, :func:`import_context`
reuses these versions.

Nothing else in the folder is applied: connections, ``publish:`` settings, declared sources and
eval suites stay as they are on the server, so the gate measures the preview with the suites
already imported. The report lists eval suites the pull request changes, and tables the docs name
that the server does not know.
"""

from __future__ import annotations

import fnmatch
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from satchel.curation.releases import create_release, latest_published, release_map
from satchel.evals.store import eval_globs, latest_suite
from satchel.evals.suite import SuiteError, parse_suite, spec_hash, suite_spec
from satchel.llm.embeddings import EmbeddingProvider, make_embedder
from satchel.store.db import admin_session
from satchel.store.models import (
    Asset,
    Connection,
    ContextChunk,
    ContextItem,
    ContextVersion,
    Domain,
)
from satchel.sync.importer import (
    AssetIndex,
    ImportValidationError,
    content_hash,
    next_version_seq,
    sync_links,
)
from satchel.sync.parse import ParsedItem, parse_repo


@dataclass
class PreviewReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)
    eval_suites_changed: list[str] = field(default_factory=list)
    release_id: uuid.UUID | None = None
    release_seq: int | None = None
    base_release_seq: int | None = None

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.removed)


def is_preview_only(item: ContextItem) -> bool:
    """An item that only exists in pull-request previews (never imported or drafted)."""
    return item.state == "deprecated" and item.current_version_id is None \
        and item.managed_by == "git"


def preview_context(
    path: str | Path, workspace_id: uuid.UUID, *, ref: str, note: str | None = None,
    created_by: uuid.UUID | None = None, embedder: EmbeddingProvider | None = None,
    engine: Engine | None = None,
) -> PreviewReport:
    root = Path(path).resolve()
    repo = parse_repo(root)
    if repo.errors:
        raise ImportValidationError(repo.errors)
    emb = embedder or make_embedder()
    globs = eval_globs(repo.config)
    report = PreviewReport(warnings=[  # eval suites are not context items
        f"skipped {f}: not a context file" for f in repo.skipped
        if not any(fnmatch.fnmatch(f, g) for g in globs)])
    with admin_session(engine=engine) as s:
        conns = {c.name: c for c in s.scalars(
            select(Connection).where(Connection.workspace_id == workspace_id))}
        domains = {d.name: d for d in s.scalars(
            select(Domain).where(Domain.workspace_id == workspace_id))}
        index = AssetIndex(list(s.scalars(select(Asset).where(Asset.workspace_id == workspace_id))))
        base = latest_published(s, workspace_id)
        report.base_release_seq = base.seq if base else None
        base_map = release_map(s, base.id if base else None)
        mapping = dict(base_map)
        items = {i.key: i for i in s.scalars(
            select(ContextItem).where(ContextItem.workspace_id == workspace_id))}

        fresh: list[tuple[ContextVersion, ParsedItem]] = []
        for it in repo.items:
            chash = content_hash(it.body, it.meta)
            item = items.get(it.key)
            if item is not None and item.managed_by != "git" and not is_preview_only(item):
                report.warnings.append(
                    f"{it.key}: managed by {item.managed_by} on the server; merging takes it over")
            served = s.get(ContextVersion, base_map[item.id]) if (
                item is not None and item.id in base_map) else None
            if served is not None and served.content_hash == chash:
                report.unchanged += 1
                continue
            if item is None:
                item = ContextItem(
                    workspace_id=workspace_id, type=it.type, key=it.key,
                    scope_kind=it.scope_kind,
                    scope_id=_scope_id(it, conns, domains, index, report),
                    state="deprecated", managed_by="git")
                s.add(item)
                s.flush()
                items[it.key] = item
            ver = s.scalars(select(ContextVersion).where(
                ContextVersion.item_id == item.id, ContextVersion.content_hash == chash)
                .order_by(ContextVersion.seq.desc())).first()
            if ver is None:
                ver = ContextVersion(
                    item_id=item.id, seq=next_version_seq(s, item.id), body=it.body,
                    meta=it.meta, content_hash=chash, author_user_id=created_by,
                    provenance={"source": "git", "ref": it.key.split("#")[0], "preview": ref})
                s.add(ver)
                s.flush()
                fresh.append((ver, it))
                sync_links(s, ver.id, it, index, conns)
            mapping[item.id] = ver.id
            (report.updated if served is not None else report.created).append(it.key)
            if it.type == "asset_doc" and it.asset_table and index.resolve(it.asset_table) is None:
                report.warnings.append(f"{it.key}: table {it.asset_table} is not in the catalog")

        seen = {it.key for it in repo.items}
        by_id = {i.id: i for i in items.values()}
        for item_id in list(mapping):
            item = by_id.get(item_id)
            if item is not None and item.managed_by == "git" and item.key not in seen:
                del mapping[item_id]
                report.removed.append(item.key)

        _embed(s, fresh, emb)
        report.eval_suites_changed = _changed_suites(s, workspace_id, root, repo.config)
        if not report.changed:
            return report
        rel = create_release(
            s, workspace_id, kind="preview", mapping=mapping,
            parent_id=base.id if base else None, created_by=created_by, ref=ref,
            note=note or f"pull request preview: {ref}")
        report.release_id, report.release_seq = rel.id, rel.seq
    return report


def _scope_id(
    it: ParsedItem, conns: dict[str, Connection], domains: dict[str, Domain],
    index: AssetIndex, report: PreviewReport,
) -> uuid.UUID | None:
    if it.scope_kind == "domain" and it.scope_ref:
        d = domains.get(it.scope_ref)
        if d is None:
            report.warnings.append(f"{it.key}: domain {it.scope_ref} does not exist yet")
        return d.id if d else None
    if it.scope_kind == "connection" and it.scope_ref:
        c = conns.get(it.scope_ref)
        if c is None:
            report.warnings.append(f"{it.key}: connection {it.scope_ref} does not exist")
        return c.id if c else None
    if it.scope_kind == "asset" and it.scope_ref:
        conn = conns.get(it.connection) if it.connection else None
        return index.resolve(it.scope_ref, conn.id if conn else None)
    return None


def _embed(s: Session, fresh: list[tuple[ContextVersion, ParsedItem]],
           emb: EmbeddingProvider) -> None:
    texts = [t for _, it in fresh for t in it.chunk_texts]
    vectors = emb.embed(texts) if texts else []
    n = 0
    for ver, it in fresh:
        for ord_, text in enumerate(it.chunk_texts):
            s.add(ContextChunk(version_id=ver.id, ord=ord_, text=text, embedding=vectors[n],
                               embedding_model=emb.model_id))
            n += 1
    s.flush()


def _changed_suites(s: Session, workspace_id: uuid.UUID, root: Path,
                    config: dict[str, object]) -> list[str]:
    out: list[str] = []
    globs = eval_globs(dict(config))
    for p in sorted(root.rglob("*.y*ml")):
        rel = p.relative_to(root).as_posix()
        hidden = any(seg.startswith(".") for seg in rel.split("/"))  # as parse_repo
        if hidden or not p.is_file() or not any(fnmatch.fnmatch(rel, g) for g in globs):
            continue
        try:
            suite = parse_suite(yaml.safe_load(p.read_text(encoding="utf-8")))
        except (SuiteError, yaml.YAMLError, UnicodeDecodeError):
            out.append(rel)
            continue
        row = latest_suite(s, workspace_id, suite.id)
        if row is None or row.content_hash != spec_hash(suite_spec(suite)):
            out.append(suite.id)
    return out
