"""Token endpoint logic: code exchange, refresh rotation with reuse detection, revocation.

Tokens are opaque (``satchel_at_...`` access, ``satchel_rt_...`` refresh) and live in ``api_token``
with only their sha256. Every token minted from one authorization grant shares a ``family_id``;
replaying an authorization code or an already-rotated refresh token revokes the whole family.
"""

from __future__ import annotations

import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session as OrmSession

from satchel.oauth.common import (
    ACCESS_PREFIX,
    REFRESH_PREFIX,
    allowed_resources,
    normalize_resource,
    pkce_s256,
    random_token,
    sha256_hex,
    valid_verifier,
)
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import ApiToken, AuthFlow


class OAuthError(Exception):
    def __init__(self, error: str, description: str = "", status: int = 400) -> None:
        super().__init__(f"{error}: {description}")
        self.error, self.description, self.status = error, description, status


def revoke_family(s: OrmSession, family_id: uuid.UUID) -> int:
    res = s.execute(
        update(ApiToken)
        .where(ApiToken.family_id == family_id, ApiToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    return int(getattr(res, "rowcount", 0) or 0)


def _issue_pair(
    s: OrmSession, *, flow_workspace: uuid.UUID, user_id: uuid.UUID, client_id: str,
    name: str, family_id: uuid.UUID, resource: str, scopes: list[str], settings: Settings,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    access = ACCESS_PREFIX + random_token(32)
    refresh = REFRESH_PREFIX + random_token(32)
    common: dict[str, Any] = dict(
        workspace_id=flow_workspace, user_id=user_id, client_id=client_id, family_id=family_id,
        resource=resource, scopes=list(scopes),
    )
    s.add(ApiToken(hash=sha256_hex(access), name=name, kind="oauth_access",
                   expires_at=now + timedelta(seconds=settings.oauth_access_ttl_s), **common))
    s.add(ApiToken(hash=sha256_hex(refresh), name=name, kind="oauth_refresh",
                   expires_at=now + timedelta(seconds=settings.oauth_refresh_ttl_s), **common))
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": settings.oauth_access_ttl_s,
        "refresh_token": refresh,
        "scope": " ".join(scopes),
    }


def _check_resource(requested: str | None, bound: str | None, settings: Settings) -> None:
    if requested is None:
        return
    n = normalize_resource(requested)
    if n not in allowed_resources(settings) or (bound is not None and n != bound):
        raise OAuthError("invalid_target", "resource does not match the authorized resource")


def exchange_code(
    *, code: str, client_id: str, redirect_uri: str | None, code_verifier: str | None,
    resource: str | None, settings: Settings, engine: Engine | None = None,
) -> dict[str, Any]:
    if not code_verifier or not valid_verifier(code_verifier):
        raise OAuthError("invalid_grant", "missing or malformed code_verifier")
    failure: OAuthError | None = None
    result: dict[str, Any] | None = None
    with admin_session(engine=engine) as s:
        flow = s.scalars(
            select(AuthFlow).where(AuthFlow.code_hash == sha256_hex(code)).with_for_update()
        ).first()
        if flow is None or flow.client_id != client_id:
            failure = OAuthError("invalid_grant", "unknown or invalid authorization code")
        elif flow.code_used_at is not None:
            if flow.family_id is not None:
                revoke_family(s, flow.family_id)
            failure = OAuthError("invalid_grant", "authorization code already used")
        elif (flow.code_expires_at is None or flow.code_expires_at <= datetime.now(UTC)
              or flow.status != "code"):
            failure = OAuthError("invalid_grant", "authorization code expired")
        elif flow.redirect_uri != redirect_uri:
            failure = OAuthError("invalid_grant", "redirect_uri does not match the request")
        elif not hmac.compare_digest(pkce_s256(code_verifier), flow.code_challenge or ""):
            failure = OAuthError("invalid_grant", "PKCE verification failed")
        else:
            try:
                _check_resource(resource, flow.resource, settings)
            except OAuthError as e:
                failure = e
        if failure is None and flow is not None:
            assert flow.workspace_id and flow.user_id and flow.client_id and flow.resource
            family = uuid.uuid4()
            flow.code_used_at = datetime.now(UTC)
            flow.family_id = family
            flow.status = "redeemed"
            result = _issue_pair(
                s, flow_workspace=flow.workspace_id, user_id=flow.user_id,
                client_id=flow.client_id, name=f"oauth:{flow.client_id}"[:200], family_id=family,
                resource=flow.resource, scopes=(flow.scope or "mcp").split(), settings=settings,
            )
    if failure is not None:
        raise failure
    assert result is not None
    return result


def refresh(
    *, refresh_token: str, client_id: str, resource: str | None, settings: Settings,
    engine: Engine | None = None,
) -> dict[str, Any]:
    failure: OAuthError | None = None
    result: dict[str, Any] | None = None
    now = datetime.now(UTC)
    with admin_session(engine=engine) as s:
        tok = s.scalars(
            select(ApiToken).where(
                ApiToken.hash == sha256_hex(refresh_token), ApiToken.kind == "oauth_refresh"
            ).with_for_update()
        ).first()
        if tok is None or tok.client_id != client_id:
            failure = OAuthError("invalid_grant", "unknown refresh token")
        elif tok.rotated_at is not None:
            # An already-rotated token came back: assume theft, kill every token in the grant.
            if tok.family_id is not None:
                revoke_family(s, tok.family_id)
            failure = OAuthError("invalid_grant", "refresh token reuse detected; grant revoked")
        elif tok.revoked_at is not None or (tok.expires_at and tok.expires_at <= now):
            failure = OAuthError("invalid_grant", "refresh token expired or revoked")
        else:
            try:
                _check_resource(resource, tok.resource, settings)
            except OAuthError as e:
                failure = e
        if failure is None and tok is not None:
            assert tok.family_id and tok.resource and tok.client_id
            tok.rotated_at = now
            tok.last_used_at = now
            result = _issue_pair(
                s, flow_workspace=tok.workspace_id, user_id=tok.user_id, client_id=tok.client_id,
                name=tok.name, family_id=tok.family_id,
                resource=tok.resource, scopes=list(tok.scopes), settings=settings,
            )
    if failure is not None:
        raise failure
    assert result is not None
    return result


def revoke(*, token: str, client_id: str | None, engine: Engine | None = None) -> bool:
    """RFC 7009. Revokes the token's whole grant family. Returns whether anything was revoked;
    the endpoint answers 200 either way (never reveals token validity)."""
    with admin_session(engine=engine) as s:
        tok = s.scalars(
            select(ApiToken).where(
                ApiToken.hash == sha256_hex(token),
                ApiToken.kind.in_(("oauth_access", "oauth_refresh")),
            )
        ).first()
        if tok is None or tok.client_id != client_id or tok.family_id is None:
            return False
        return revoke_family(s, tok.family_id) > 0
