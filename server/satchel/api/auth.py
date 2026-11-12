"""REST authentication + role enforcement (design §9).

Auth, first match wins: bearer PAT / OAuth access token; the ``satchel_session`` cookie (writes
must echo ``X-CSRF-Token``); and only with ``SATCHEL_DEV_AUTH=1`` the unverified
``X-Satchel-Workspace`` / ``X-Satchel-User`` headers. ``require(role)`` builds a dependency that
also enforces a minimum role.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import Engine, select

from satchel.jobs.runner import JobRunner
from satchel.llm.chat import ChatProvider
from satchel.llm.embeddings import EmbeddingProvider
from satchel.mcp.auth import ROLE_RANK, authenticate_bearer
from satchel.oauth import sessions
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import AppUser, Workspace


@dataclass
class ApiContext:
    engine: Engine
    embedder: EmbeddingProvider
    settings: Settings
    chat: ChatProvider | None = None  # overrides the configured chat provider (tests)
    jobs: JobRunner | None = None  # this process's job consumers (satchel.jobs)


class Caller(BaseModel):
    workspace_id: uuid.UUID
    user_id: uuid.UUID | None
    role: str | None = None
    csrf: str | None = None  # only for cookie sessions
    auth: str = "dev"  # bearer | cookie | dev

    @property
    def effective_role(self) -> str:
        """Dev-header calls without a user act as the lowest role."""
        return self.role if self.role in ROLE_RANK else "explorer"

    def has_role(self, minimum: str) -> bool:
        return ROLE_RANK[self.effective_role] >= ROLE_RANK[minimum]


def _unauth(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": 'Bearer realm="satchel"'})


class Auth:
    def __init__(self, ctx: ApiContext) -> None:
        self.ctx = ctx

    def caller(
        self,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_satchel_workspace: Annotated[str | None, Header()] = None,
        x_satchel_user: Annotated[str | None, Header()] = None,
    ) -> Caller:
        eng, st = self.ctx.engine, self.ctx.settings
        if authorization:
            p = authenticate_bearer(authorization, engine=eng, settings=st)
            if p is None:
                raise _unauth("invalid, expired or revoked token")
            return Caller(
                workspace_id=p.workspace_id, user_id=p.user_id, role=p.role, auth="bearer"
            )
        cookie = request.cookies.get(sessions.COOKIE)
        if cookie:
            info = sessions.load_session(cookie, engine=eng)
            if info is None:
                raise _unauth("session expired")
            if request.method not in sessions.SAFE_METHODS and not sessions.csrf_ok(
                info, request.headers.get(sessions.CSRF_HEADER)
            ):
                raise HTTPException(403, "missing or invalid X-CSRF-Token")
            return Caller(
                workspace_id=info.workspace_id, user_id=info.user_id, role=info.role,
                csrf=info.csrf, auth="cookie",
            )
        if not st.dev_auth:
            raise _unauth("authentication required")
        # DEV-ONLY header auth (SATCHEL_DEV_AUTH=1): unverified. Without headers (e.g. a UI build
        # without VITE_DEV_AUTH) it acts as the first admin of the configured workspace
        # (SATCHEL_WORKSPACE / config file), so `SATCHEL_DEV_AUTH=1 satchel up` works with any UI
        # build.
        slug = x_satchel_workspace or st.workspace
        if not slug:
            raise HTTPException(401, "dev auth: send X-Satchel-Workspace or set SATCHEL_WORKSPACE")
        with admin_session(engine=eng) as s:
            ws = s.scalars(select(Workspace).where(Workspace.slug == slug)).first()
            if ws is None:
                raise HTTPException(404, f"unknown workspace: {slug}")
            if not x_satchel_user and not x_satchel_workspace:
                admin = s.scalars(
                    select(AppUser)
                    .where(AppUser.workspace_id == ws.id, AppUser.role == "admin")
                    .order_by(AppUser.created_at)
                ).first()
                if admin is not None:
                    return Caller(workspace_id=ws.id, user_id=admin.id, role="admin", auth="dev")
            user_id: uuid.UUID | None = None
            role: str | None = None
            if x_satchel_user:
                try:
                    user_id = uuid.UUID(x_satchel_user)
                except ValueError:
                    raise HTTPException(400, "X-Satchel-User must be a UUID") from None
                u = s.scalars(
                    select(AppUser).where(AppUser.id == user_id, AppUser.workspace_id == ws.id)
                ).first()
                if u is None:
                    raise HTTPException(403, "user is not a member of this workspace")
                role = u.role
            return Caller(workspace_id=ws.id, user_id=user_id, role=role, auth="dev")

    def require(self, minimum: str) -> Callable[..., Caller]:
        """Dependency: an authenticated caller with at least ``minimum`` role."""

        def dep(who: Annotated[Caller, Depends(self.caller)]) -> Caller:
            if not who.has_role(minimum):
                raise HTTPException(
                    403, f"requires the {minimum} role; your role is {who.effective_role}"
                )
            return who

        return dep
