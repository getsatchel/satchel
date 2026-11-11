"""LLM connections (Settings → Models): the model providers curation and evals call.

* ``GET /v1/llm-connections`` (curator): the deployment provider (``SATCHEL_CHAT_*``, read-only),
  the workspace's connections and which one is in effect. Secrets are never returned: a key shows
  as a hint (``…abcd``), extra headers by name only.
* ``POST /v1/llm-connections``, ``PATCH`` / ``DELETE /v1/llm-connections/{id}`` (admin).
  ``PATCH`` leaves out what it does not change; ``"api_key": null`` clears the key.
* ``POST /v1/llm-connections/test`` (admin): one tiny call, before or after saving. With
  ``connection_id`` the stored secrets are used unless the body replaces them.

Writing a secret needs ``SATCHEL_ENCRYPTION_KEY`` (503 otherwise). Every write is audited
(``llm_connection.*``) with field names, never values.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from satchel.api.auth import Auth, Caller
from satchel.core.crypto import (
    DecryptionError,
    EncryptionUnavailable,
    Keyring,
    configured,
)
from satchel.llm.chat import DEFAULT_ANTHROPIC_MODEL
from satchel.llm.connections import (
    ChatTarget,
    LlmConnectionError,
    create_connection,
    delete_connection,
    deployment_target,
    target_of,
    test_target,
    update_connection,
    validate_base_url,
    validate_headers,
)
from satchel.store.db import workspace_session
from satchel.store.models import LlmConnection

ProviderName = Literal["anthropic", "openai_compat"]


class LlmConnectionOut(BaseModel):
    id: uuid.UUID
    name: str
    provider: ProviderName
    base_url: str | None
    default_model: str | None
    has_api_key: bool
    api_key_hint: str | None = Field(None, description="Last four characters, e.g. '…abcd'.")
    extra_header_names: list[str]
    is_default: bool
    created_at: datetime
    updated_at: datetime


class DeploymentLlmOut(BaseModel):
    provider: ProviderName
    model: str | None
    base_url: str | None
    has_api_key: bool


class LlmConnectionsOut(BaseModel):
    encryption_configured: bool = Field(
        description="False when SATCHEL_ENCRYPTION_KEY is unset: connections with an API key "
        "cannot be added until it is."
    )
    deployment: DeploymentLlmOut | None = Field(
        None, description="SATCHEL_CHAT_* when set; it then wins over workspace connections."
    )
    connections: list[LlmConnectionOut]
    effective: str | None = Field(
        None, description="'deployment', the default connection's name, or null (no LLM)."
    )


class LlmConnectionCreate(BaseModel):
    name: str
    provider: ProviderName
    base_url: str | None = None
    default_model: str | None = None
    api_key: str | None = Field(None, description="Write-only.")
    extra_headers: dict[str, str] | None = Field(None, description="Write-only values.")
    is_default: bool | None = Field(
        None, description="Make this the workspace default (the first connection always is)."
    )


class LlmConnectionPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    default_model: str | None = None
    api_key: str | None = Field(None, description="Write-only; null clears the stored key.")
    extra_headers: dict[str, str] | None = Field(None, description="Replaces the whole map.")
    is_default: bool | None = None


class LlmTestRequest(BaseModel):
    connection_id: uuid.UUID | None = Field(
        None, description="Start from a saved connection; fields given here override it."
    )
    deployment: bool = Field(False, description="Test the SATCHEL_CHAT_* provider.")
    provider: ProviderName | None = None
    base_url: str | None = None
    default_model: str | None = None
    api_key: str | None = None
    extra_headers: dict[str, str] | None = None
    model: str | None = Field(None, description="Test this model instead of the default.")


class LlmTestOut(BaseModel):
    ok: bool
    model_id: str | None
    latency_ms: int | None
    error: str | None = None


def out(row: LlmConnection) -> LlmConnectionOut:
    return LlmConnectionOut(
        id=row.id, name=row.name, provider=row.provider,
        base_url=row.base_url, default_model=row.default_model,
        has_api_key=row.api_key_enc is not None, api_key_hint=row.api_key_hint,
        extra_header_names=list(row.extra_header_names or []), is_default=row.is_default,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["llm"])
    Cur = Annotated[Caller, Depends(auth.require("curator"))]
    Adm = Annotated[Caller, Depends(auth.require("admin"))]
    eng, st = auth.ctx.engine, auth.ctx.settings

    def keyring_or_none() -> Keyring | None:
        return Keyring.from_settings(st) if configured(st) else None

    def get_row(s: Session, cid: uuid.UUID) -> LlmConnection:
        row = s.get(LlmConnection, cid)
        if row is None:
            raise HTTPException(404, "no such LLM connection")
        return row

    @r.get("/v1/llm-connections", response_model=LlmConnectionsOut)
    def list_llm_connections(who: Cur) -> LlmConnectionsOut:
        dep = deployment_target(st)
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            rows = list(s.scalars(select(LlmConnection).order_by(LlmConnection.name)))
            conns = [out(x) for x in rows]
        default = next((c.name for c in conns if c.is_default), None)
        return LlmConnectionsOut(
            encryption_configured=configured(st),
            deployment=DeploymentLlmOut(
                provider=dep.provider,
                model=dep.model or (DEFAULT_ANTHROPIC_MODEL if dep.provider == "anthropic"
                                    else None),
                base_url=dep.base_url, has_api_key=bool(dep.api_key),
            ) if dep else None,
            connections=conns,
            effective="deployment" if dep else default,
        )

    @r.post("/v1/llm-connections", response_model=LlmConnectionOut, status_code=201)
    def create_llm_connection(who: Adm, req: LlmConnectionCreate) -> LlmConnectionOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            try:
                row = create_connection(
                    s, who.workspace_id, who.user_id, keyring_or_none(),
                    name=req.name, provider=req.provider, base_url=req.base_url,
                    default_model=req.default_model, api_key=req.api_key,
                    extra_headers=req.extra_headers, is_default=req.is_default,
                )
            except LlmConnectionError as e:
                raise HTTPException(422, str(e)) from None
            except EncryptionUnavailable as e:
                raise HTTPException(503, str(e)) from None
            return out(row)

    @r.patch("/v1/llm-connections/{connection_id}", response_model=LlmConnectionOut)
    def update_llm_connection(
        connection_id: uuid.UUID, who: Adm, req: LlmConnectionPatch,
    ) -> LlmConnectionOut:
        given = req.model_fields_set
        if "name" in given and req.name is None:
            raise HTTPException(422, "name cannot be null")
        kw = {f: getattr(req, f) for f in given}
        if "is_default" in kw and kw["is_default"] is None:
            kw.pop("is_default")
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            row = get_row(s, connection_id)
            try:
                update_connection(s, row, who.user_id, keyring_or_none(), **kw)
            except LlmConnectionError as e:
                raise HTTPException(422, str(e)) from None
            except EncryptionUnavailable as e:
                raise HTTPException(503, str(e)) from None
            return out(row)

    @r.delete("/v1/llm-connections/{connection_id}", status_code=204)
    def delete_llm_connection(connection_id: uuid.UUID, who: Adm) -> Response:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            delete_connection(s, get_row(s, connection_id), who.user_id)
        return Response(status_code=204)

    @r.post("/v1/llm-connections/test", response_model=LlmTestOut)
    async def test_llm_connection(who: Adm, req: LlmTestRequest) -> LlmTestOut:
        given = req.model_fields_set
        if req.deployment:
            base = deployment_target(st)
            if base is None:
                raise HTTPException(404, "SATCHEL_CHAT_PROVIDER is not set")
        elif req.connection_id is not None:
            with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
                row = get_row(s, req.connection_id)
                try:
                    base = target_of(row, keyring_or_none())
                except (LlmConnectionError, DecryptionError) as e:
                    raise HTTPException(409, str(e)) from None
        else:
            if req.provider is None:
                raise HTTPException(422, "provider is required without connection_id")
            base = ChatTarget(name="unsaved", provider=req.provider, base_url=None, model=None,
                              api_key=None)
        try:
            target = ChatTarget(
                name=base.name, managed_by=base.managed_by, connection_id=base.connection_id,
                provider=req.provider if req.provider and not req.deployment else base.provider,
                base_url=validate_base_url(req.base_url) if "base_url" in given
                and not req.deployment else base.base_url,
                model=(req.default_model or None) if "default_model" in given
                and not req.deployment else base.model,
                api_key=req.api_key if "api_key" in given and not req.deployment
                else base.api_key,
                extra_headers=validate_headers(req.extra_headers) if "extra_headers" in given
                and not req.deployment else base.extra_headers,
            )
        except LlmConnectionError as e:
            raise HTTPException(422, str(e)) from None
        res = await run_in_threadpool(test_target, target, st, req.model or None)
        return LlmTestOut(ok=res.ok, model_id=res.model_id, latency_ms=res.latency_ms,
                          error=res.error)

    return r
