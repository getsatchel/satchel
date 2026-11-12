"""Sources (design §8.3): upstream tools whose metadata satchel imports (curator and above).

* ``GET /v1/source-kinds``: the installed adapters (kind, version, maturity, built-in or external,
  config schema).
* ``GET /v1/sources``: configured sources with their last sync.
* ``POST /v1/sources/{id}/sync``: sync now. Optional multipart upload of the adapter's input files
  (for CI: ``curl -F manifest=@target/manifest.json -F catalog=@target/catalog.json ...``);
  an input that may be a folder (Cube ``models``) takes one repeated field per file, named by the
  uploaded file name (``-F models=@model/cubes/orders.yml``); without a body the paths stored
  in the source's config are read on the server.

* ``POST /v1/sources``, ``PUT`` / ``DELETE /v1/sources/{id}`` (admin): manage sources from the UI.
  File inputs there are ``s3://`` / ``gs://`` locations or uploads at sync time; server paths and
  ``*_env`` fields stay CLI-only, and secret fields are sealed (``satchel.sources.admin``).
* ``POST`` / ``DELETE /v1/sources/{id}/webhook`` (admin): generate, rotate or remove the push
  webhook secret (shown once). Deliveries go to ``POST /v1/hooks/sources/{id}``
  (``satchel.api.hooks``). Scheduled syncs (``sync_interval_hours``) run in the worker.

Every sync takes the source's advisory lock (``satchel.sources.automation.sync_lock``); a
manual sync while another one runs is a 409.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from satchel.api.auth import Auth, Caller
from satchel.api.common import audit
from satchel.api.jobs import JobOut, job_of
from satchel.core.crypto import EncryptionUnavailable, Keyring, configured
from satchel.sources import admin as source_admin
from satchel.sources import dbt_build, git
from satchel.sources.admin import SourceAdminError, masked
from satchel.sources.apply import SourceSyncError
from satchel.sources.automation import interval_of, next_sync_at, sync_lock, webhook_url
from satchel.sources.jobs import queue_sync
from satchel.sources.registry import AdapterEntry, discover
from satchel.sources.service import plain_config, record_failure, sync_in_session
from satchel.store.db import workspace_session
from satchel.store.models import Connection, ContextItem, ContextSource

MAX_UPLOAD = 256 * 1024 * 1024


class DbtParseOut(BaseModel):
    available: bool
    message: str


class SourceKindOut(BaseModel):
    kind: str
    display_name: str | None = None
    version: str | None = None
    maturity: str | None = None
    builtin: bool
    package: str
    package_version: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    file_inputs: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(
        default_factory=list,
        description="File inputs a sync cannot run without (a stored location or an upload).")
    folder_inputs: list[str] = Field(
        default_factory=list,
        description="The file inputs that may be a folder of files (a Cube model, a SQLMesh "
        "project); upload them as repeated multipart fields, one per file.",
    )
    config_schema: dict[str, Any] = Field(default_factory=dict)
    available: bool = Field(
        default=True,
        description="False when an optional dependency of the adapter is missing; see "
        "missing and install_hint. Syncing such a source fails with the same hint.",
    )
    missing: list[str] = Field(default_factory=list)
    install_hint: str | None = None
    error: str | None = None
    dbt_parse: DbtParseOut | None = Field(
        None, description="dbt only: whether this server can build manifest.json from a dbt "
        "project in git (dbt_project_dir) with dbt parse.")


class SourceOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    adapter: SourceKindOut | None = None
    connection: str | None = None
    publish: str
    priority: int
    config: dict[str, Any]
    created_at: datetime
    last_synced_at: datetime | None
    last_status: str | None
    last_error: str | None
    last_fingerprint: str | None
    item_counts: dict[str, int] = Field(default_factory=dict)
    last_report: dict[str, Any] = Field(
        default_factory=dict,
        description="The last sync report, plus trigger (manual, cli, schedule, webhook) and, "
        "after an automatic check found no new commit, checked: {at, trigger, commit}.")
    sync_interval_hours: int | None = Field(
        None, description="Scheduled sync every N hours (config key); null = off.")
    webhook_url: str | None = Field(
        None, description="Where to send push events; null when no webhook is set up.")
    next_sync_at: datetime | None = Field(
        None, description="When the schedule syncs next (in the past: due at the next worker "
        "tick); null when there is no schedule. After failures it retries sooner, with backoff.")


class WebhookSecretOut(BaseModel):
    url: str
    secret: str = Field(description="Shown once; only a sealed copy is stored.")
    rotated: bool = Field(description="True when this replaced an existing secret.")


class WebhookRemovedOut(BaseModel):
    removed: bool


class SyncReportOut(BaseModel):
    source: str
    kind: str
    publish: str
    status: str
    counts: dict[str, int]
    created: list[str]
    updated: list[str]
    unchanged: list[str]
    deprecated: list[str]
    held: list[str]
    warnings: list[dict[str, str | None]]
    overlaps: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Tables (asset_doc) and semantic models also described by another source or a "
        "hand-written doc, with the primary: [{type, asset, primary: {source, key}, "
        "also_described_by: [{source, key}], this_source_is_primary}]",
    )
    release_id: uuid.UUID | None
    release_seq: int | None
    release_kind: str | None
    release_created: bool
    gate_status: str | None
    gate_message: str
    signals: dict[str, int]
    assets_created: int
    chunks_embedded: int
    summary: dict[str, int]
    fingerprint: str
    git_commit: str | None = Field(None, description="The commit synced (git sources).")
    dbt_version: str | None = Field(
        None, description="The dbt version that built the manifest (dbt_project_dir sources).")


class SourceCreate(BaseModel):
    kind: str
    name: str
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Adapter fields plus publish / priority / connection and, to read files "
        "from git, git_url / git_ref / git_username / git_token. File inputs: paths in the "
        "repository (with git_url), s3:// or gs:// locations, or empty to upload on sync. "
        "Secret fields (git_token too) are write-only.")


class SourceUpdate(BaseModel):
    config: dict[str, Any] = Field(
        description="Replaces the config. A secret field sent as *** (or left out) is kept.")


class GitTestRequest(BaseModel):
    url: str
    ref: str | None = None
    username: str | None = None
    token: str | None = Field(
        None, description="Write-only. Left out with source_id: the source's stored token.")
    source_id: uuid.UUID | None = None


class GitTestOut(BaseModel):
    ok: bool
    commit: str | None = None
    error: str | None = None


class SourceRemovedOut(BaseModel):
    items_detached: int = Field(description="Items deprecated and detached from the source.")


def kind_out(e: AdapterEntry) -> SourceKindOut:
    if e.adapter is None:
        return SourceKindOut(kind=e.kind, builtin=e.builtin, package=e.package,
                             package_version=e.package_version, available=False, error=e.error)
    i = e.info
    return SourceKindOut(
        kind=e.kind, display_name=i.display_name, version=i.version, maturity=i.maturity,
        builtin=e.builtin, package=e.package, package_version=e.package_version,
        capabilities=sorted(i.capabilities), file_inputs=[f.name for f in i.file_inputs],
        required_inputs=[f.name for f in i.file_inputs if f.required],
        folder_inputs=[f.name for f in i.file_inputs if f.multiple],
        config_schema=i.config_schema(), available=e.available, missing=e.missing,
        install_hint=e.install_hint,
        dbt_parse=_dbt_parse() if e.kind == "dbt" else None,
    )


def _dbt_parse() -> DbtParseOut:
    from satchel.settings import get_settings

    st = dbt_build.status(get_settings().dbt_bin)
    return DbtParseOut(available=st.available, message=st.message)


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["sources"])
    Cur = Annotated[Caller, Depends(auth.require("curator"))]
    Adm = Annotated[Caller, Depends(auth.require("admin"))]
    eng, emb = auth.ctx.engine, auth.ctx.embedder

    def hook_url(src: ContextSource) -> str | None:
        return webhook_url(src, auth.ctx.settings.public_url)

    @r.get("/v1/source-kinds", response_model=list[SourceKindOut])
    def list_kinds(who: Cur) -> list[SourceKindOut]:
        return [kind_out(e) for e in discover().values()]

    @r.get("/v1/sources", response_model=list[SourceOut])
    def list_sources(who: Cur) -> list[SourceOut]:
        kinds = discover()
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            counts: dict[uuid.UUID, dict[str, int]] = {}
            for sid, typ, n in s.execute(
                select(ContextItem.source_id, ContextItem.type, func.count())
                .where(ContextItem.source_id.is_not(None), ContextItem.state != "deprecated")
                .group_by(ContextItem.source_id, ContextItem.type)
            ):
                if sid is not None:
                    counts.setdefault(sid, {})[typ] = n
            conns = {c.id: c.name for c in s.scalars(select(Connection))}
            out = []
            for src in s.scalars(select(ContextSource).order_by(ContextSource.name)):
                entry = kinds.get(src.kind)
                out.append(SourceOut(
                    id=src.id, name=src.name, kind=src.kind,
                    adapter=kind_out(entry) if entry else None,
                    connection=conns.get(src.connection_id) if src.connection_id else None,
                    publish=str(src.config.get("publish") or "auto"),
                    priority=int(src.config.get("priority") or 0), config=masked(src.config),
                    created_at=src.created_at, last_synced_at=src.last_synced_at,
                    last_status=src.last_status, last_error=src.last_error,
                    last_fingerprint=src.last_fingerprint,
                    item_counts=counts.get(src.id, {}), last_report=src.last_report,
                    sync_interval_hours=interval_of(src), webhook_url=hook_url(src),
                    next_sync_at=next_sync_at(src),
                ))
            return out

    @r.post(
        "/v1/sources/{source_id}/sync", response_model=SyncReportOut | JobOut,
        responses={202: {"model": JobOut, "description": "queued (no files and no wait)"}},
    )
    async def sync(source_id: uuid.UUID, request: Request, response: Response, who: Cur,
                   wait: bool = False) -> SyncReportOut | JobOut:
        """Sync the source. With uploaded files, or ``wait=true``, it syncs in the request and
        returns the report. Otherwise it queues a ``source.sync`` job and returns it (202):
        poll ``GET /v1/jobs/{id}``; its ``result.report`` is the report."""
        sent: list[tuple[str, str, bytes]] = []
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
            form = await request.form()
            for name, value in form.multi_items():
                if not hasattr(value, "read"):
                    raise HTTPException(422, f"field {name!r} must be a file")
                data = await value.read()
                if len(data) > MAX_UPLOAD:
                    raise HTTPException(413, f"{name} is larger than {MAX_UPLOAD >> 20} MB")
                sent.append((name, str(getattr(value, "filename", "") or name), data))
        if sent or wait:
            return await run_in_threadpool(_sync, source_id, who, sent)
        response.status_code = 202
        return await run_in_threadpool(_queue, source_id, who)

    def _queue(source_id: uuid.UUID, who: Caller) -> JobOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            src = s.get(ContextSource, source_id)
            if src is None:
                raise HTTPException(404, "no such source")
            name = src.name
        jid, queued = queue_sync(eng, who.workspace_id, source_id, trigger="manual",
                                 actor=who.user_id)
        if queued:
            with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
                audit(s, who, "source.queue_sync", {"source": name, "job_id": jid})
        if auth.ctx.jobs is not None:
            auth.ctx.jobs.wake()
        return job_of(eng, who.workspace_id, jid)

    def _sync(
        source_id: uuid.UUID, who: Caller, sent: list[tuple[str, str, bytes]]
    ) -> SyncReportOut:
        with sync_lock(eng, source_id) as got:
            if not got:
                raise HTTPException(409, "this source is being synced right now (a schedule, "
                                    "a webhook or another user); try again when it finishes")
            return _sync_locked(source_id, who, sent)

    def _sync_locked(
        source_id: uuid.UUID, who: Caller, sent: list[tuple[str, str, bytes]]
    ) -> SyncReportOut:
        error: str | None = None
        try:
            with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
                src = s.get(ContextSource, source_id)
                if src is None:
                    raise HTTPException(404, "no such source")
                entry = discover().get(src.kind)
                inputs = {f.name: f for f in entry.info.file_inputs} if entry and entry.adapter \
                    else {}
                allowed = set(inputs)
                unknown = sorted({n for n, _, _ in sent} - allowed)
                if unknown:
                    raise HTTPException(
                        422, f"unknown file(s) {', '.join(unknown)}; this source accepts: "
                        f"{', '.join(sorted(allowed)) or 'none'}"
                    )
                uploads = {
                    (f"{n}/{fname}" if inputs[n].multiple else n): data for n, fname, data in sent
                }
                try:
                    with s.begin_nested():
                        report = sync_in_session(s, src, embedder=emb, uploads=uploads,
                                                 settings=auth.ctx.settings, trigger="manual")
                except SourceSyncError as e:
                    error = str(e)
                else:
                    audit(s, who, "source.sync", {"source": src.name, "trigger": "manual",
                                                  **report.counts()})
                    return SyncReportOut.model_validate(report.to_json())
        except SourceSyncError as e:  # pragma: no cover - defensive
            error = str(e)
        record_failure(eng, source_id, error or "sync failed")
        raise HTTPException(422, error or "sync failed")

    def keyring() -> Keyring | None:
        st = auth.ctx.settings
        return Keyring.from_settings(st) if configured(st) else None

    def out_of(s: Session, src: ContextSource) -> SourceOut:
        entry = discover().get(src.kind)
        conn = s.get(Connection, src.connection_id) if src.connection_id else None
        return SourceOut(
            id=src.id, name=src.name, kind=src.kind, adapter=kind_out(entry) if entry else None,
            connection=conn.name if conn else None,
            publish=str(src.config.get("publish") or "auto"),
            priority=int(src.config.get("priority") or 0), config=masked(src.config),
            created_at=src.created_at, last_synced_at=src.last_synced_at,
            last_status=src.last_status, last_error=src.last_error,
            last_fingerprint=src.last_fingerprint, item_counts={}, last_report=src.last_report,
            sync_interval_hours=interval_of(src), webhook_url=hook_url(src),
            next_sync_at=next_sync_at(src),
        )

    def guarded(fn: Any) -> Any:
        try:
            return fn()
        except SourceAdminError as e:
            raise HTTPException(422, str(e)) from None
        except EncryptionUnavailable as e:
            raise HTTPException(503, str(e)) from None

    @r.post("/v1/sources", response_model=SourceOut, status_code=201)
    def create_source(who: Adm, req: SourceCreate) -> SourceOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            src = guarded(lambda: source_admin.create(
                s, who.workspace_id, who.user_id, keyring(), kind=req.kind, name=req.name,
                config=req.config))
            s.refresh(src)
            return out_of(s, src)

    @r.put("/v1/sources/{source_id}", response_model=SourceOut)
    def update_source(source_id: uuid.UUID, who: Adm, req: SourceUpdate) -> SourceOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            src = s.get(ContextSource, source_id)
            if src is None:
                raise HTTPException(404, "no such source")
            guarded(lambda: source_admin.update_src(s, src, who.user_id, keyring(),
                                                     config=req.config))
            return out_of(s, src)

    @r.post("/v1/sources/git/test", response_model=GitTestOut)
    async def test_git(who: Adm, req: GitTestRequest) -> GitTestOut:
        """Check a repository URL, ref and token (``git ls-remote``) before or after saving."""
        token = req.token
        if req.source_id is not None and "token" not in req.model_fields_set:
            with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
                src = s.get(ContextSource, req.source_id)
                if src is None:
                    raise HTTPException(404, "no such source")
                try:
                    token = plain_config(src, auth.ctx.settings).get("git_token")
                except SourceSyncError as e:
                    return GitTestOut(ok=False, error=str(e))
        spec = git.GitSpec(url=req.url, ref=req.ref, username=req.username, token=token)
        try:
            commit = await run_in_threadpool(git.resolve, spec)
        except git.GitSourceError as e:
            return GitTestOut(ok=False, error=str(e))
        return GitTestOut(ok=True, commit=commit)

    @r.post("/v1/sources/{source_id}/webhook", response_model=WebhookSecretOut)
    def set_webhook(source_id: uuid.UUID, who: Adm) -> WebhookSecretOut:
        """Generate the push-webhook secret, or rotate it (the old one stops working)."""
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            src = s.get(ContextSource, source_id)
            if src is None:
                raise HTTPException(404, "no such source")
            secret, rotated = guarded(lambda: source_admin.set_webhook(s, src, who.user_id,
                                                                       keyring()))
            return WebhookSecretOut(url=hook_url(src) or "", secret=secret, rotated=rotated)

    @r.delete("/v1/sources/{source_id}/webhook", response_model=WebhookRemovedOut)
    def remove_webhook(source_id: uuid.UUID, who: Adm) -> WebhookRemovedOut:
        """Remove the push webhook; deliveries are refused from then on."""
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            src = s.get(ContextSource, source_id)
            if src is None:
                raise HTTPException(404, "no such source")
            return WebhookRemovedOut(removed=source_admin.remove_webhook(s, src, who.user_id))

    @r.delete("/v1/sources/{source_id}", response_model=SourceRemovedOut)
    def delete_source(source_id: uuid.UUID, who: Adm) -> SourceRemovedOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            src = s.get(ContextSource, source_id)
            if src is None:
                raise HTTPException(404, "no such source")
            return SourceRemovedOut(items_detached=source_admin.remove(s, src, who.user_id))

    return r
