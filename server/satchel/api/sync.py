"""Context repo uploads (design §8.2), used by the GitHub Action (``action/``).

- ``POST /v1/sync/preview`` (curator): a pull request's context folder becomes a preview release
  (:mod:`satchel.sync.preview`), without changing drafts or what is served. Run the publish gate
  on it with ``POST /v1/previews/{id}/evals``. Such a preview is published by merging the pull
  request, not from the UI.
- ``POST /v1/sync/import`` (admin): the default branch's folder is imported and published, the
  same as ``satchel sync import``. Admin, because the folder's ``satchel.config.yaml`` can change
  connections and the publish gate. Sources declared in it are not synced from an upload.

Both take ``multipart/form-data`` with the file field ``bundle`` (a ``.tar.gz`` of the folder) and
text fields ``ref`` (e.g. ``refs/pull/12/merge@3f2c1a9``) and, for previews, ``note``.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from satchel.api.auth import Auth, Caller
from satchel.api.common import audit
from satchel.evals.gate import load_gate_config
from satchel.store.db import workspace_session
from satchel.store.models import Workspace
from satchel.sync.bundle import MAX_BUNDLE, BundleError, unpacked
from satchel.sync.importer import ImportValidationError, import_context
from satchel.sync.preview import preview_context


class SyncPreviewOut(BaseModel):
    preview_id: uuid.UUID | None = Field(description="None when the folder changes nothing")
    preview_seq: int | None
    base_release_seq: int | None
    created: list[str]
    updated: list[str]
    removed: list[str]
    unchanged: int
    warnings: list[str]
    eval_suites_changed: list[str] = Field(
        description="Suites this folder changes; the gate uses the suites already imported")
    gate_required: list[str] = Field(description="publish.require_evals on the server")


class SyncImportOut(BaseModel):
    release_id: uuid.UUID | None
    release_seq: int | None
    release_created: bool
    created: list[str]
    updated: list[str]
    deprecated: list[str]
    unchanged: int
    eval_suites: list[str]
    skipped_files: list[str]


async def _form(request: Request) -> tuple[bytes, dict[str, str]]:
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(415, "send multipart/form-data with a 'bundle' file")
    form = await request.form()
    bundle: bytes | None = None
    fields: dict[str, str] = {}
    for name, value in form.multi_items():
        if hasattr(value, "read"):
            if name != "bundle":
                raise HTTPException(422, f"unexpected file field {name!r}")
            bundle = await value.read()
            if len(bundle) > MAX_BUNDLE:
                raise HTTPException(413, f"the bundle is larger than {MAX_BUNDLE >> 20} MB")
        else:
            fields[name] = str(value)[:500]
    if bundle is None:
        raise HTTPException(422, "the 'bundle' file field is required")
    return bundle, fields


def _errors(e: ImportValidationError | BundleError) -> HTTPException:
    detail: Any = e.errors if isinstance(e, ImportValidationError) else str(e)
    return HTTPException(422, detail)


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["sync"])
    Cur = Annotated[Caller, Depends(auth.require("curator"))]
    Adm = Annotated[Caller, Depends(auth.require("admin"))]
    eng, emb = auth.ctx.engine, auth.ctx.embedder

    @r.post("/v1/sync/preview", response_model=SyncPreviewOut)
    async def sync_preview(request: Request, who: Cur) -> SyncPreviewOut:
        bundle, fields = await _form(request)
        ref = fields.get("ref", "").strip() or "upload"
        return await run_in_threadpool(_preview, who, bundle, ref, fields.get("note"))

    def _preview(who: Caller, bundle: bytes, ref: str, note: str | None) -> SyncPreviewOut:
        try:
            with unpacked(bundle) as root:
                rep = preview_context(root, who.workspace_id, ref=ref, note=note,
                                      created_by=who.user_id, embedder=emb, engine=eng)
        except (ImportValidationError, BundleError) as e:
            raise _errors(e) from None
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            required = load_gate_config(s, who.workspace_id).require
            if rep.release_id is not None:
                audit(s, who, "sync.preview", {
                    "ref": ref, "preview_id": rep.release_id, "created": len(rep.created),
                    "updated": len(rep.updated), "removed": len(rep.removed)})
        return SyncPreviewOut(
            preview_id=rep.release_id, preview_seq=rep.release_seq,
            base_release_seq=rep.base_release_seq, created=rep.created, updated=rep.updated,
            removed=rep.removed, unchanged=rep.unchanged, warnings=rep.warnings,
            eval_suites_changed=rep.eval_suites_changed, gate_required=required)

    @r.post("/v1/sync/import", response_model=SyncImportOut)
    async def sync_import(request: Request, who: Adm) -> SyncImportOut:
        bundle, fields = await _form(request)
        ref = fields.get("ref", "").strip() or "upload"
        return await run_in_threadpool(_import, who, bundle, ref)

    def _import(who: Caller, bundle: bytes, ref: str) -> SyncImportOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            ws = s.get(Workspace, who.workspace_id)
            assert ws is not None
            slug = ws.slug
        try:
            with unpacked(bundle) as root:
                rep = import_context(root, slug, embedder=emb, engine=eng, sync_sources=False)
        except (ImportValidationError, BundleError) as e:
            raise _errors(e) from None
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            audit(s, who, "sync.import", {"ref": ref, "release_seq": rep.release_seq,
                                          "release_created": rep.release_created,
                                          **rep.counts()})
        return SyncImportOut(
            release_id=rep.release_id, release_seq=rep.release_seq,
            release_created=rep.release_created, created=rep.created, updated=rep.updated,
            deprecated=rep.deprecated, unchanged=len(rep.unchanged), eval_suites=rep.eval_suites,
            skipped_files=rep.skipped_files)

    return r
