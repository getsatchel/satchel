"""Browser sessions for the human UI. The cookie holds a random secret (only its sha256 is
stored server-side, so sessions are revocable); writes authenticated by the cookie must also
echo the per-session CSRF token in ``X-CSRF-Token``."""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select

from satchel.oauth.common import random_token, sha256_hex
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import AppUser, WebSession

COOKIE = "satchel_session"
COOKIE_PREFIX = "satchel_ws_"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class SessionInfo:
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    csrf: str


def create_session(
    engine: Engine | None, workspace_id: uuid.UUID, user_id: uuid.UUID, settings: Settings
) -> tuple[str, str]:
    """Create a session. Returns (cookie value, csrf token)."""
    raw = COOKIE_PREFIX + random_token(32)
    csrf = random_token(24)
    with admin_session(engine=engine) as s:
        s.add(WebSession(
            workspace_id=workspace_id, user_id=user_id, hash=sha256_hex(raw), csrf=csrf,
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.session_ttl_s),
        ))
    return raw, csrf


def load_session(raw: str | None, *, engine: Engine | None = None) -> SessionInfo | None:
    if not raw or not raw.startswith(COOKIE_PREFIX):
        return None
    now = datetime.now(UTC)
    with admin_session(engine=engine) as s:
        row = s.execute(
            select(WebSession, AppUser.role)
            .join(AppUser, AppUser.id == WebSession.user_id)
            .where(WebSession.hash == sha256_hex(raw))
        ).first()
        if row is None:
            return None
        ws, role = row
        if ws.revoked_at is not None or ws.expires_at <= now:
            return None
        if ws.last_seen_at is None or now - ws.last_seen_at > timedelta(seconds=60):
            ws.last_seen_at = now
        return SessionInfo(ws.workspace_id, ws.user_id, role, ws.csrf)


def csrf_ok(info: SessionInfo, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(info.csrf, supplied or "")


def revoke_session(raw: str | None, *, engine: Engine | None = None) -> None:
    if not raw:
        return
    with admin_session(engine=engine) as s:
        row = s.scalars(select(WebSession).where(WebSession.hash == sha256_hex(raw))).first()
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
