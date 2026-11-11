"""Bearer-token authentication (design §9): personal access tokens and OAuth access tokens.

PATs look like ``satchel_pat_<43 url-safe chars>``; OAuth access tokens (issued by
``satchel.oauth``) look like ``satchel_at_...``. Only the sha256 is stored. Verification runs under
``admin_session`` because the workspace is not known until the token is resolved.

``authenticate_bearer`` is the single place the HTTP transports (``/mcp`` and the REST API)
turn an ``Authorization`` header into a ``Principal``. OAuth access tokens must additionally
carry an audience (``resource``) that is this server; refresh tokens are never accepted.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select

from satchel import audit
from satchel.oauth.common import ACCESS_PREFIX, allowed_resources, normalize_resource
from satchel.settings import Settings, get_settings
from satchel.store.db import admin_session
from satchel.store.models import ApiToken, AppUser, Workspace

PREFIX = "satchel_pat_"
DEFAULT_SCOPES = ["mcp"]
LAST_USED_GRANULARITY = timedelta(seconds=60)
ROLE_RANK = {"explorer": 0, "analyst": 1, "curator": 2, "admin": 3}


@dataclass(frozen=True)
class Principal:
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    token_id: uuid.UUID | None = None
    scopes: tuple[str, ...] = ("mcp",)
    kind: str = "pat"
    client_id: str | None = None

    def has_role(self, minimum: str) -> bool:
        return ROLE_RANK.get(self.role, -1) >= ROLE_RANK[minimum]


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_token(
    workspace_slug: str,
    email: str,
    name: str,
    *,
    role: str = "analyst",
    expires_in_days: int | None = None,
    engine: Engine | None = None,
) -> tuple[str, uuid.UUID]:
    """Create (workspace/user if missing) a token. Returns (raw token, token id)."""
    raw = PREFIX + secrets.token_urlsafe(32)
    with admin_session(engine=engine) as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == workspace_slug)).first()
        if ws is None:
            raise LookupError(f"unknown workspace: {workspace_slug}")
        user = s.scalars(
            select(AppUser).where(AppUser.workspace_id == ws.id, AppUser.email == email)
        ).first()
        if user is None:
            user = AppUser(workspace_id=ws.id, email=email, display_name=email, role=role)
            s.add(user)
            s.flush()
            audit.record(s, ws.id, None, "user.create",
                         {"user": user.id, "email": email, "role": role, "via": "token.create"})
        tok = ApiToken(
            workspace_id=ws.id,
            user_id=user.id,
            hash=hash_token(raw),
            name=name,
            scopes=list(DEFAULT_SCOPES),
            expires_at=datetime.now(UTC) + timedelta(days=expires_in_days)
            if expires_in_days
            else None,
        )
        s.add(tok)
        s.flush()
        audit.record(s, ws.id, None, "token.create", {
            "token": tok.id, "name": name, "user": user.id, "email": user.email,
            "role": user.role, "expires_at": tok.expires_at})
        return raw, tok.id


def revoke_token(
    *,
    raw: str | None = None,
    token_id: uuid.UUID | None = None,
    workspace_slug: str | None = None,
    name: str | None = None,
    engine: Engine | None = None,
) -> int:
    """Revoke by raw token, id, or (workspace, name). Returns the number revoked."""
    with admin_session(engine=engine) as s:
        stmt = select(ApiToken).where(ApiToken.revoked_at.is_(None), ApiToken.kind == "pat")
        if raw:
            stmt = stmt.where(ApiToken.hash == hash_token(raw))
        elif token_id:
            stmt = stmt.where(ApiToken.id == token_id)
        elif workspace_slug and name:
            stmt = stmt.join(Workspace, Workspace.id == ApiToken.workspace_id).where(
                Workspace.slug == workspace_slug, ApiToken.name == name
            )
        else:
            raise ValueError("give a token, a token id, or workspace + name")
        toks = list(s.scalars(stmt))
        for t in toks:
            t.revoked_at = datetime.now(UTC)
            audit.record(s, t.workspace_id, None, "token.revoke",
                         {"token": t.id, "name": t.name, "user": t.user_id})
        return len(toks)


def verify_token(
    raw: str, *, engine: Engine | None = None, settings: Settings | None = None
) -> Principal | None:
    """Principal for a valid, unexpired, unrevoked ``mcp``-scoped token, else None.

    Accepts PATs and OAuth access tokens (whose audience must be this server)."""
    if raw.startswith(PREFIX):
        kind = "pat"
    elif raw.startswith(ACCESS_PREFIX):
        kind = "oauth_access"
    else:
        return None
    now = datetime.now(UTC)
    with admin_session(engine=engine) as s:
        row = s.execute(
            select(ApiToken, AppUser.role)
            .join(AppUser, AppUser.id == ApiToken.user_id)
            .where(ApiToken.hash == hash_token(raw))
        ).first()
        if row is None:
            return None
        tok, role = row
        if tok.kind != kind:
            return None
        if tok.revoked_at is not None or (tok.expires_at and tok.expires_at <= now):
            return None
        if "mcp" not in tok.scopes:
            return None
        if kind == "oauth_access" and (
            tok.resource is None
            or normalize_resource(tok.resource) not in allowed_resources(settings or get_settings())
        ):
            return None
        if tok.last_used_at is None or now - tok.last_used_at > LAST_USED_GRANULARITY:
            tok.last_used_at = now
        return Principal(tok.workspace_id, tok.user_id, role, tok.id, tuple(tok.scopes),
                         tok.kind, tok.client_id)


def authenticate_bearer(
    header: str | None, *, engine: Engine | None = None, settings: Settings | None = None
) -> Principal | None:
    """Resolve an ``Authorization`` header value to a Principal (PAT or OAuth access token)."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return verify_token(value.strip(), engine=engine, settings=settings)
