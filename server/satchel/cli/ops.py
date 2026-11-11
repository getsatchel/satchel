"""Database operations shared by ``init`` / ``status`` / ``doctor`` / ``connect``."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, func, select

from satchel import audit
from satchel.mcp.auth import create_token, hash_token, revoke_token, verify_token
from satchel.store.db import admin_session
from satchel.store.models import (
    ApiToken,
    AppUser,
    Connection,
    ContextChunk,
    ContextItem,
    ContextVersion,
    Release,
    Workspace,
)


def ensure_workspace_admin(
    engine: Engine, slug: str, email: str, *, name: str | None = None
) -> tuple[uuid.UUID, uuid.UUID, list[str]]:
    """Create the workspace and an admin user if missing; promote the user to admin.

    Returns (workspace id, user id, what-changed notes). Idempotent."""
    notes: list[str] = []
    with admin_session(engine=engine) as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == slug)).first()
        if ws is None:
            ws = Workspace(slug=slug, name=name or slug)
            s.add(ws)
            s.flush()
            audit.record(s, ws.id, None, "workspace.create", {"slug": slug})
            notes.append(f"created workspace '{slug}'")
        user = s.scalars(
            select(AppUser).where(AppUser.workspace_id == ws.id, AppUser.email == email)
        ).first()
        if user is None:
            user = AppUser(workspace_id=ws.id, email=email, display_name=email, role="admin")
            s.add(user)
            s.flush()
            audit.record(s, ws.id, None, "user.create",
                         {"user": user.id, "email": email, "role": "admin", "via": "cli"})
            notes.append(f"created admin user {email}")
        elif user.role != "admin":
            audit.record(s, ws.id, None, "user.role_change",
                         {"user": user.id, "email": email, "from": user.role, "to": "admin"})
            user.role = "admin"
            notes.append(f"promoted {email} to admin")
        return ws.id, user.id, notes


def mint_token(
    engine: Engine, slug: str, email: str, name: str, *, role: str = "admin"
) -> str:
    """A fresh PAT called ``name``; earlier live tokens with that name are revoked first so
    reconnecting does not pile up credentials."""
    revoke_token(workspace_slug=slug, name=name, engine=engine)
    raw, _ = create_token(slug, email, name, role=role, engine=engine)
    return raw


@dataclass(frozen=True)
class TokenInfo:
    valid: bool
    reason: str  # "" when valid
    workspace: str | None = None
    email: str | None = None
    role: str | None = None
    name: str | None = None
    expires_at: datetime | None = None
    user_id: uuid.UUID | None = None


def token_info(engine: Engine, raw: str) -> TokenInfo:
    """Why a token is or is not usable (verify_token just says None)."""
    with admin_session(engine=engine) as s:
        row = s.execute(
            select(ApiToken, Workspace.slug, AppUser.email, AppUser.role, AppUser.id)
            .join(Workspace, Workspace.id == ApiToken.workspace_id)
            .join(AppUser, AppUser.id == ApiToken.user_id)
            .where(ApiToken.hash == hash_token(raw))
        ).first()
        if row is None:
            return TokenInfo(False, "unknown token")
        tok, slug, email, role, user_id = row
        name, expires_at, revoked_at = tok.name, tok.expires_at, tok.revoked_at

    def info(valid: bool, reason: str) -> TokenInfo:
        return TokenInfo(valid, reason, slug, email, role, name, expires_at, user_id)

    if revoked_at is not None:
        return info(False, "revoked")
    if expires_at and expires_at <= datetime.now(UTC):
        return info(False, "expired")
    if verify_token(raw, engine=engine) is None:
        return info(False, "not accepted (wrong kind or scope)")
    return info(True, "")


@dataclass(frozen=True)
class WorkspaceSummary:
    exists: bool
    items: int = 0
    releases: int = 0
    latest_release: int | None = None
    connections: tuple[tuple[str, str], ...] = ()  # (name, kind)
    embedding_models: tuple[str, ...] = ()


def workspace_summary(engine: Engine, slug: str) -> WorkspaceSummary:
    with admin_session(engine=engine) as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == slug)).first()
        if ws is None:
            return WorkspaceSummary(False)
        items = s.scalar(
            select(func.count()).select_from(ContextItem).where(ContextItem.workspace_id == ws.id)
        ) or 0
        rel = s.execute(
            select(func.count(), func.max(Release.seq)).where(
                Release.workspace_id == ws.id, Release.kind == "published"
            )
        ).one()
        conns = s.execute(
            select(Connection.name, Connection.kind)
            .where(Connection.workspace_id == ws.id)
            .order_by(Connection.name)
        ).all()
        models = s.scalars(
            select(ContextChunk.embedding_model)
            .join(ContextVersion, ContextVersion.id == ContextChunk.version_id)
            .join(ContextItem, ContextItem.id == ContextVersion.item_id)
            .where(ContextItem.workspace_id == ws.id, ContextChunk.embedding_model.is_not(None))
            .distinct()
        ).all()
    return WorkspaceSummary(
        True, int(items), int(rel[0]), rel[1],
        tuple((c[0], c[1]) for c in conns), tuple(sorted(str(m) for m in models)),
    )
