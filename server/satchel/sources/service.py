"""Run a source sync end to end: load the adapter, apply the snapshot, record the outcome."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from satchel_adapter_sdk import SourceFiles, SourceSnapshot, is_compatible
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from satchel import audit
from satchel.core.crypto import DecryptionError, Keyring
from satchel.llm.embeddings import EmbeddingProvider, make_embedder
from satchel.settings import Settings
from satchel.sources import dbt_build, git
from satchel.sources.apply import SourceSyncError, SyncReport, apply_snapshot
from satchel.sources.config import (
    WEBHOOK_KEY,
    SourceConfigError,
    check_automatable,
    config_with_defaults,
    read_files,
    validate_config,
)
from satchel.sources.registry import AdapterEntry, get_entry
from satchel.store.db import admin_session
from satchel.store.models import Connection, ContextSource, Workspace


def plain_config(src: ContextSource, settings: Settings | None = None) -> dict[str, Any]:
    """The config with secret fields decrypted (sealed when set from the UI)."""
    from satchel.core.crypto import configured
    from satchel.secrets import unseal_source_config
    from satchel.settings import get_settings

    st = settings or get_settings()
    try:
        return unseal_source_config(src, Keyring.from_settings(st) if configured(st) else None)
    except (ValueError, DecryptionError) as e:
        raise SourceSyncError(f"source {src.name!r}: {e}") from None


@dataclass
class Loaded:
    snapshot: SourceSnapshot
    git_commit: str | None = None
    dbt_version: str | None = None  # set when the manifest was built with dbt parse


def load_snapshot(
    src: ContextSource, uploads: dict[str, bytes] | None = None, base_dir: Path | None = None,
    settings: Settings | None = None,
) -> Loaded:
    """Run the source's adapter (pure; no database access). A source with ``git_url`` reads its
    file inputs from a fresh checkout of the repository."""
    try:
        entry = get_entry(src.kind)
        plain = plain_config(src, settings)
        _, cfg = validate_config(entry, plain)
        spec = git.spec_from_config(plain)
        if spec is None:
            return Loaded(_run_adapter(src, entry, cfg,
                                       read_files(entry, cfg, uploads, base_dir)))
        uploaded = set(uploads or {})
        paths = [str(getattr(cfg, f.name)) for f in entry.info.file_inputs
                 if getattr(cfg, f.name, None)
                 and f.name not in uploaded
                 and not any(k.startswith(f"{f.name}/") for k in uploaded)]
        # dbt from a project folder: build the manifest unless one was uploaded for this sync
        build = None if "manifest" in uploaded else dbt_build.options_from_config(src.kind, plain)
        if build is not None:
            paths.append(build.project_dir)
        with git.checkout(spec, paths) as co:
            built = None
            if build is not None:
                st = settings or _settings()
                built = dbt_build.build(co.root, build, dbt_bin=st.dbt_bin,
                                        timeout=st.dbt_timeout_s)
                uploads = {**built.files, **(uploads or {})}
            files = read_files(entry, cfg, uploads, repo_root=co.root)
            return Loaded(_run_adapter(src, entry, cfg, files), co.commit,
                          built.dbt_version if built else None)
    except (LookupError, SourceConfigError, git.GitSourceError, dbt_build.DbtBuildError) as e:
        raise SourceSyncError(str(e)) from None


def _settings() -> Settings:
    from satchel.settings import get_settings

    return get_settings()


def _run_adapter(src: ContextSource, entry: AdapterEntry, cfg: BaseModel,
                 files: SourceFiles) -> SourceSnapshot:
    assert entry.adapter is not None
    try:
        snap = entry.adapter.load(cfg, files)
    except Exception as e:  # noqa: BLE001 - adapters are third-party code
        raise SourceSyncError(f"the {src.kind} adapter failed: {type(e).__name__}: {e}") from None
    if not is_compatible(snap.ir_version):
        raise SourceSyncError(
            f"the {src.kind} adapter produced IR {snap.ir_version}; this satchel reads IR major "
            "version 1. Upgrade the adapter or satchel."
        )
    return snap


def input_key(src: ContextSource) -> str:
    """What a sync read, apart from the commit: the non-secret config and the adapter version.
    An automatic sync of a git source is skipped when this and the commit are unchanged."""
    from satchel.secrets import is_secret_key

    cfg = {k: v for k, v in (src.config or {}).items()
           if not is_secret_key(k) and k != "sync_interval_hours"}
    try:
        entry = get_entry(src.kind)
        version = f"{entry.package}=={entry.package_version}:{entry.info.version}"
    except LookupError:
        version = "?"
    raw = json.dumps({"kind": src.kind, "config": cfg, "adapter": version}, sort_keys=True,
                     default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def sync_in_session(
    s: Session, src: ContextSource, *, embedder: EmbeddingProvider,
    uploads: dict[str, bytes] | None = None, base_dir: Path | None = None,
    settings: Settings | None = None, trigger: str = "manual",
) -> SyncReport:
    """``trigger`` (manual, upload, cli, schedule, webhook) is kept in ``last_report``."""
    loaded = load_snapshot(src, uploads, base_dir, settings)
    snap = loaded.snapshot
    report = apply_snapshot(s, src, snap, embedder=embedder)
    report.git_commit = loaded.git_commit
    report.dbt_version = loaded.dbt_version
    src.last_synced_at = datetime.now(UTC)
    src.last_status, src.last_error = "ok", None
    src.last_fingerprint = snap.fingerprint
    # input_key is only meaningful when the files came from the stored locations.
    src.last_report = {**report.to_json(), "trigger": trigger,
                       "input_key": None if uploads else input_key(src)}
    s.flush()
    return report


def record_failure(
    engine: Engine | None, source_id: uuid.UUID, message: str
) -> None:
    with admin_session(engine=engine) as s:
        src = s.get(ContextSource, source_id)
        if src is not None:
            src.last_synced_at = datetime.now(UTC)
            src.last_status, src.last_error = "error", message[:2000]
            # Consecutive failures, for the schedule's retry backoff (satchel.sources.automation).
            # A successful sync replaces last_report, which resets it.
            rep = dict(src.last_report or {})
            rep["failures"] = int(rep.get("failures") or 0) + 1
            src.last_report = rep


def sync_source(
    workspace_slug: str, name: str, *, embedder: EmbeddingProvider | None = None,
    engine: Engine | None = None, uploads: dict[str, bytes] | None = None,
    base_dir: Path | None = None,
) -> SyncReport:
    """Sync one source by name with an admin session (satchel_worker) (CLI and repo import).
    Holds the source's sync lock, so it never runs beside a scheduled or webhook sync."""
    from satchel.sources.automation import sync_lock

    emb = embedder or make_embedder()
    with admin_session(engine=engine) as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == workspace_slug)).first()
        if ws is None:
            raise LookupError(f"unknown workspace: {workspace_slug}")
        found = s.scalars(select(ContextSource.id).where(
            ContextSource.workspace_id == ws.id, ContextSource.name == name)).first()
    if found is None:
        raise LookupError(f"unknown source: {name}")
    with sync_lock(engine, found) as got:
        if not got:
            raise SourceSyncError(f"source {name!r} is being synced by another process; "
                                  "try again when it finishes")
        return _sync_source(workspace_slug, name, emb, engine, uploads, base_dir)


def _sync_source(
    workspace_slug: str, name: str, emb: EmbeddingProvider, engine: Engine | None,
    uploads: dict[str, bytes] | None, base_dir: Path | None,
) -> SyncReport:
    from satchel.sources.jobs import record_sync  # jobs imports automation, which imports us
    with admin_session(engine=engine) as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == workspace_slug)).first()
        if ws is None:
            raise LookupError(f"unknown workspace: {workspace_slug}")
        src = s.scalars(
            select(ContextSource).where(
                ContextSource.workspace_id == ws.id, ContextSource.name == name
            )
        ).first()
        if src is None:
            raise LookupError(f"unknown source: {name}")
        src_id, ws_id, started = src.id, src.workspace_id, datetime.now(UTC)
        report: SyncReport | None = None
        try:
            with s.begin_nested():
                report = sync_in_session(s, src, embedder=emb, uploads=uploads,
                                         base_dir=base_dir, trigger="cli")
        except SourceSyncError as e:
            err = str(e)
    if report is not None:  # committed with the session above
        record_sync(engine, ws_id, src_id, trigger="cli", started_at=started, report=report,
                    source=name)
        return report
    record_failure(engine, src_id, err)
    record_sync(engine, ws_id, src_id, trigger="cli", started_at=started, error=err,
                source=name)
    raise SourceSyncError(err)


def add_source(
    workspace_slug: str, kind: str, name: str, config: dict[str, Any], *,
    engine: Engine | None = None,
) -> ContextSource:
    """Create or update a source. The config is validated against the adapter's schema."""
    entry = get_entry(kind)
    stored = config_with_defaults(entry, config)
    if stored.get("git_token"):
        raise SourceConfigError("git_token is stored encrypted: set it on the Sources page "
                                "(Edit → Git repository → Access token)")
    if stored.get("git_url"):
        try:
            stored["git_url"] = git.validate_url(str(stored["git_url"]))
            if stored.get("git_ref"):
                stored["git_ref"] = git.validate_ref(str(stored["git_ref"]))
        except git.GitSourceError as e:
            raise SourceConfigError(str(e)) from None
    if stored.get("sync_interval_hours"):
        check_automatable(entry, stored, "--sync-interval-hours (scheduled sync)")
    with admin_session(engine=engine) as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == workspace_slug)).first()
        if ws is None:
            raise LookupError(f"unknown workspace: {workspace_slug}")
        conn_id = None
        conn_name = stored.get("connection")
        if conn_name:
            conn = s.scalars(
                select(Connection).where(
                    Connection.workspace_id == ws.id, Connection.name == conn_name
                )
            ).first()
            if conn is None:
                raise LookupError(f"unknown connection: {conn_name}")
            conn_id = conn.id
        src = s.scalars(
            select(ContextSource).where(
                ContextSource.workspace_id == ws.id, ContextSource.name == name
            )
        ).first()
        if src is None:
            src = ContextSource(workspace_id=ws.id, kind=kind, name=name, config=stored,
                                connection_id=conn_id)
            s.add(src)
            created = True
        else:
            if src.kind != kind:
                raise ValueError(f"source {name!r} exists with kind {src.kind!r}")
            if (src.config or {}).get(WEBHOOK_KEY):  # set on the Sources page; keep it
                check_automatable(entry, stored, "the source's push webhook")
                stored[WEBHOOK_KEY] = src.config[WEBHOOK_KEY]
            src.config, src.connection_id = stored, conn_id
            created = False
        s.flush()
        audit.record(s, ws.id, None, "source.add" if created else "source.update",
                     {"source": name, "kind": kind, "connection": conn_name})
        s.expunge(src)
        return src


def remove_source(
    workspace_slug: str, name: str, *, engine: Engine | None = None
) -> int:
    """Remove a source. Its items are deprecated (published ones leave the next release) and
    detached from the source, so nothing is deleted. Returns how many items were detached."""
    from satchel.store.models import ContextItem

    with admin_session(engine=engine) as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == workspace_slug)).first()
        if ws is None:
            raise LookupError(f"unknown workspace: {workspace_slug}")
        src = s.scalars(
            select(ContextSource).where(
                ContextSource.workspace_id == ws.id, ContextSource.name == name
            )
        ).first()
        if src is None:
            raise LookupError(f"unknown source: {name}")
        n = 0
        for item in s.scalars(select(ContextItem).where(ContextItem.source_id == src.id)):
            item.state = "deprecated"
            item.managed_by = "ui"
            item.source_id = None
            n += 1
        s.flush()
        audit.record(s, ws.id, None, "source.remove",
                     {"source": name, "kind": src.kind, "items_detached": n})
        s.delete(src)
        return n
