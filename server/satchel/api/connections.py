"""Warehouse connections, managed from Settings → Connections.

* ``GET /v1/connection-kinds`` (curator): the form for each kind (fields, secret, availability).
* ``POST /v1/connections``, ``PATCH`` / ``DELETE /v1/connections/{id}`` (admin). The secret
  (password, URL, private key or service-account key) is write-only and sealed with
  SATCHEL_ENCRYPTION_KEY; ``"secret": null`` removes it. ``passphrase`` decrypts a new private
  key once and is never stored. Removing a connection marks its assets dropped (history is
  kept) and is refused while a source uses it.
* ``POST /v1/connections/test`` (admin): open it and run ``select 1``, before or after saving.
* ``POST /v1/connections/{id}/crawl`` (curator): refresh the catalog from the warehouse.

``GET /v1/connections`` lives in the catalog router.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from satchel.api.auth import Auth, Caller
from satchel.core.crypto import EncryptionUnavailable, Keyring, configured
from satchel.data import connection_admin as ca
from satchel.data.connections import ConnectionError_
from satchel.data.crawl import crawl_connection
from satchel.store.db import workspace_session
from satchel.store.models import Connection, Workspace


class KindFieldOut(BaseModel):
    name: str
    label: str
    type: Literal["text", "number", "path"]
    required: bool
    placeholder: str | None = None
    help: str | None = None
    default: Any = None
    min: int = Field(1, description="Number fields: the smallest value.")
    max: int = Field(65535, description="Number fields: the largest value.")


class ConnectionKindOut(BaseModel):
    kind: str
    label: str
    available: bool
    fields: list[KindFieldOut]
    secret_label: str | None = Field(None, description="Null when the kind takes no secret.")
    secret_help: str | None = None
    secret_multiline: bool = Field(
        False, description="The secret is a multi-line key (PEM / JSON): keep its newlines.")
    passphrase_label: str | None = Field(
        None, description="Set when the secret may be an encrypted key that takes a passphrase.")
    note: str | None = None


_PASSPHRASE = "Write-only: decrypts the private key given in secret; never stored."


class ConnectionCreate(BaseModel):
    name: str
    kind: str
    config: dict[str, Any] = Field(default_factory=dict)
    secret: str | None = Field(None, description="Write-only.")
    passphrase: str | None = Field(None, description=_PASSPHRASE)
    default_for_ai: bool | None = None


class ConnectionPatch(BaseModel):
    name: str | None = None
    config: dict[str, Any] | None = None
    secret: str | None = Field(None, description="Write-only; null removes the secret.")
    passphrase: str | None = Field(None, description=_PASSPHRASE)
    default_for_ai: bool | None = None


class ConnectionTestRequest(BaseModel):
    connection_id: uuid.UUID | None = Field(
        None, description="Start from a saved connection; fields given here override it.")
    kind: str | None = None
    config: dict[str, Any] | None = None
    secret: str | None = None
    passphrase: str | None = Field(None, description=_PASSPHRASE)


class ConnectionTestOut(BaseModel):
    ok: bool
    latency_ms: int
    error: str | None = None


class CrawlOut(BaseModel):
    connection: str
    created: int
    updated: int
    dropped: int
    revived: int
    draft_docs: int
    seen: int


class ConnectionSavedOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    config: dict[str, Any]
    secret: Literal["stored", "env"] | None
    default_for_ai: bool


def saved_out(c: Connection) -> ConnectionSavedOut:
    return ConnectionSavedOut(id=c.id, name=c.name, kind=c.kind, config=dict(c.config or {}),
                              secret=secret_source(c.secret_ref),
                              default_for_ai=c.default_for_ai)


def secret_source(ref: str | None) -> Literal["stored", "env"] | None:
    if not ref:
        return None
    return "env" if ref.startswith("env:") else "stored"


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["connections"])
    Cur = Annotated[Caller, Depends(auth.require("curator"))]
    Adm = Annotated[Caller, Depends(auth.require("admin"))]
    eng, st = auth.ctx.engine, auth.ctx.settings

    def keyring() -> Keyring | None:
        return Keyring.from_settings(st) if configured(st) else None

    def get_conn(s: Session, cid: uuid.UUID) -> Connection:
        c = s.get(Connection, cid)
        if c is None:
            raise HTTPException(404, "no such connection")
        return c

    def guard(fn: Any) -> Any:
        try:
            return fn()
        except ca.ConnectionAdminError as e:
            raise HTTPException(422, str(e)) from None
        except EncryptionUnavailable as e:
            raise HTTPException(503, str(e)) from None

    @r.get("/v1/connection-kinds", response_model=list[ConnectionKindOut])
    def list_connection_kinds(who: Cur) -> list[ConnectionKindOut]:
        return [
            ConnectionKindOut(
                kind=k.kind, label=k.label, available=k.available,
                fields=[KindFieldOut(**f.__dict__) for f in k.fields],
                secret_label=k.secret_label, secret_help=k.secret_help,
                secret_multiline=k.secret_multiline, passphrase_label=k.passphrase_label,
                note=k.note,
            )
            for k in ca.kinds()
        ]

    @r.post("/v1/connections", response_model=ConnectionSavedOut, status_code=201)
    def create_connection(who: Adm, req: ConnectionCreate) -> ConnectionSavedOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            c = guard(lambda: ca.create(
                s, who.workspace_id, who.user_id, keyring(), name=req.name, kind=req.kind,
                config=req.config, secret=req.secret, passphrase=req.passphrase,
                default_for_ai=req.default_for_ai))
            return saved_out(c)

    @r.patch("/v1/connections/{connection_id}", response_model=ConnectionSavedOut)
    def update_connection(
        connection_id: uuid.UUID, who: Adm, req: ConnectionPatch,
    ) -> ConnectionSavedOut:
        kw = {f: getattr(req, f) for f in req.model_fields_set}
        for f in ("name", "config", "default_for_ai", "passphrase"):
            if f in kw and kw[f] is None:
                kw.pop(f)
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            c = get_conn(s, connection_id)
            guard(lambda: ca.update_conn(s, c, who.user_id, keyring(), **kw))
            return saved_out(c)

    @r.delete("/v1/connections/{connection_id}", status_code=204)
    def delete_connection(connection_id: uuid.UUID, who: Adm) -> Response:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            c = get_conn(s, connection_id)
            try:
                ca.remove(s, c, who.user_id)
            except ca.ConnectionAdminError as e:
                raise HTTPException(409, str(e)) from None
        return Response(status_code=204)

    @r.post("/v1/connections/test", response_model=ConnectionTestOut)
    async def test_connection(who: Adm, req: ConnectionTestRequest) -> ConnectionTestOut:
        given = req.model_fields_set
        kind, config, secret = req.kind, req.config or {}, req.secret
        if req.connection_id is not None:
            with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
                c = get_conn(s, req.connection_id)
                kind = c.kind
                if "config" not in given:
                    config = dict(c.config or {})
                if "secret" not in given:
                    try:
                        secret = ca.stored_secret(c, st)
                    except ConnectionError_ as e:
                        return ConnectionTestOut(ok=False, latency_ms=0, error=str(e))
        if not kind:
            raise HTTPException(422, "kind is required without connection_id")
        res = await run_in_threadpool(ca.test, kind, config, secret, req.passphrase)
        return ConnectionTestOut(ok=res.ok, latency_ms=res.latency_ms, error=res.error)

    @r.post("/v1/connections/{connection_id}/crawl", response_model=CrawlOut)
    async def crawl(connection_id: uuid.UUID, who: Cur) -> CrawlOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            c = get_conn(s, connection_id)
            name = c.name
            ws = s.get(Workspace, who.workspace_id)
            assert ws is not None
            slug = ws.slug
        try:
            rep = await run_in_threadpool(crawl_connection, slug, name, engine=eng, settings=st)
        except ConnectionError_ as e:
            raise HTTPException(409, str(e)) from None
        return CrawlOut(**rep.__dict__)

    return r
