"""OAuth client registry: RFC 7591 dynamic registration and Client ID Metadata Documents.

Only *public* clients exist (``token_endpoint_auth_method = none``, PKCE S256 mandatory).
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import Engine, select

from satchel.oauth.common import SUPPORTED_SCOPES, is_public_ip, random_token, redirect_uri_error
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import OAuthClient

log = logging.getLogger("satchel.oauth")

MAX_REDIRECT_URIS = 10
MAX_NAME = 200
CIMD_MAX_BYTES = 64 * 1024
CIMD_REFRESH_AFTER = timedelta(hours=1)
GRANTS = ("authorization_code", "refresh_token")

# fetch(url) -> parsed JSON document. Injected in tests; SSRF-guarded by default.
CimdFetcher = Callable[[str], dict[str, Any]]


class ClientMetadataError(ValueError):
    """Invalid registration / metadata (maps to ``invalid_client_metadata``)."""


@dataclass(frozen=True)
class ClientInfo:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    kind: str


def _info(row: OAuthClient) -> ClientInfo:
    return ClientInfo(row.client_id, row.client_name, tuple(row.redirect_uris),
                      row.registration_kind)


def validate_metadata(meta: dict[str, Any]) -> tuple[str, list[str], list[str], str | None]:
    """Common validation of RFC 7591 / CIMD fields. Returns (name, redirect_uris, grants, scope)."""
    uris = meta.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        raise ClientMetadataError("redirect_uris must be a non-empty array of strings")
    if len(uris) > MAX_REDIRECT_URIS:
        raise ClientMetadataError(f"at most {MAX_REDIRECT_URIS} redirect_uris")
    for u in uris:
        if (err := redirect_uri_error(u)) is not None:
            raise ClientMetadataError(f"{err}: {u[:100]}")
    method = meta.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise ClientMetadataError("only public clients are supported "
                                  "(token_endpoint_auth_method must be 'none')")
    grants = meta.get("grant_types") or ["authorization_code"]
    if not isinstance(grants, list) or not set(grants) <= set(GRANTS):
        raise ClientMetadataError(f"grant_types must be a subset of {list(GRANTS)}")
    if "authorization_code" not in grants:
        raise ClientMetadataError("grant_types must include authorization_code")
    rtypes = meta.get("response_types") or ["code"]
    if rtypes != ["code"]:
        raise ClientMetadataError("response_types must be ['code']")
    name = meta.get("client_name")
    if name is not None and not isinstance(name, str):
        raise ClientMetadataError("client_name must be a string")
    name = (name or "Unnamed client").strip()[:MAX_NAME] or "Unnamed client"
    scope = meta.get("scope")
    if scope is not None and not isinstance(scope, str):
        raise ClientMetadataError("scope must be a string")
    return name, list(uris), list(grants), scope


def register_client(meta: dict[str, Any], *, engine: Engine | None = None) -> dict[str, Any]:
    """RFC 7591 registration. Returns the registration response document."""
    name, uris, grants, scope = validate_metadata(meta)
    client_id = "lc_" + random_token(18)
    with admin_session(engine=engine) as s:
        s.add(OAuthClient(
            client_id=client_id, client_name=name, redirect_uris=uris, grant_types=grants,
            token_endpoint_auth_method="none", scope=scope, registration_kind="dcr",
        ))
    # oauth_client is global (no workspace), so it cannot be written to the per-workspace audit
    # trail; the workspace-scoped record is `oauth.consent.*` when a user approves the client.
    log.info("oauth client registered: %s (%s)", client_id, name)
    out: dict[str, Any] = {
        "client_id": client_id,
        "client_id_issued_at": int(datetime.now(UTC).timestamp()),
        "client_name": name,
        "redirect_uris": uris,
        "grant_types": grants,
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scope:
        out["scope"] = scope
    return out


def is_cimd_client_id(client_id: str) -> bool:
    return client_id.startswith("https://")


def _valid_cimd_url(url: str) -> bool:
    p = urlsplit(url)
    segs = p.path.split("/")
    return (p.scheme == "https" and bool(p.hostname) and p.path not in ("", "/")
            and not p.fragment and p.username is None and p.password is None
            and "." not in segs and ".." not in segs)


def ssrf_guarded_fetch(url: str, *, allow_private: bool = False) -> dict[str, Any]:
    """GET a JSON document from a public https host.

    Resolves the host once, rejects non-public addresses, then connects to that exact IP (with
    SNI/Host of the original name) so DNS rebinding cannot swap it. No redirects, 5s timeout,
    64 KiB cap.
    """
    p = urlsplit(url)
    host = p.hostname or ""
    port = p.port or 443
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise ClientMetadataError(f"cannot resolve client metadata host: {e}") from e
    addrs = [str(i[4][0]) for i in infos]
    if not addrs or (not allow_private and not all(is_public_ip(a) for a in addrs)):
        raise ClientMetadataError("client metadata host resolves to a non-public address")
    ip = addrs[0]
    ip_host = f"[{ip}]" if ":" in ip else ip
    target = f"https://{ip_host}:{port}{p.path or '/'}" + (f"?{p.query}" if p.query else "")
    try:
        with httpx.Client(timeout=5.0, follow_redirects=False) as c, c.stream(
            "GET", target, headers={"Host": p.netloc, "Accept": "application/json"},
            extensions={"sni_hostname": host},
        ) as r:
            if r.status_code != 200:
                raise ClientMetadataError(f"client metadata fetch returned {r.status_code}")
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > CIMD_MAX_BYTES:
                    raise ClientMetadataError("client metadata document too large")
    except httpx.HTTPError as e:
        raise ClientMetadataError(f"client metadata fetch failed: {e}") from e
    try:
        doc = json.loads(body)
    except ValueError as e:
        raise ClientMetadataError("client metadata is not valid JSON") from e
    if not isinstance(doc, dict):
        raise ClientMetadataError("client metadata must be a JSON object")
    return doc


def _fetch_cimd(
    client_id: str, settings: Settings, engine: Engine | None, fetcher: CimdFetcher | None
) -> ClientInfo | None:
    if not settings.oauth_cimd_enabled or not _valid_cimd_url(client_id):
        return None
    with admin_session(engine=engine) as s:
        row = s.scalars(select(OAuthClient).where(OAuthClient.client_id == client_id)).first()
        if row is not None and row.fetched_at is not None and (
            datetime.now(UTC) - row.fetched_at < CIMD_REFRESH_AFTER
        ):
            return _info(row)
    fetch = fetcher or (lambda u: ssrf_guarded_fetch(
        u, allow_private=settings.oauth_cimd_allow_private_hosts))
    try:
        doc = fetch(client_id)
        if doc.get("client_id") != client_id:
            raise ClientMetadataError("metadata client_id does not match its URL")
        name, uris, grants, scope = validate_metadata(doc)
    except ClientMetadataError:
        return None
    with admin_session(engine=engine) as s:
        row = s.scalars(select(OAuthClient).where(OAuthClient.client_id == client_id)).first()
        if row is None:
            row = OAuthClient(client_id=client_id, registration_kind="cimd",
                              token_endpoint_auth_method="none")
            s.add(row)
        row.client_name, row.redirect_uris, row.grant_types, row.scope = name, uris, grants, scope
        row.fetched_at = datetime.now(UTC)
        s.flush()
        return _info(row)


def get_client(
    client_id: str, settings: Settings, *, engine: Engine | None = None,
    fetcher: CimdFetcher | None = None,
) -> ClientInfo | None:
    """Look up a registered client, resolving (and caching) CIMD client_ids on demand."""
    if is_cimd_client_id(client_id):
        return _fetch_cimd(client_id, settings, engine, fetcher)
    with admin_session(engine=engine) as s:
        row = s.scalars(select(OAuthClient).where(OAuthClient.client_id == client_id)).first()
        return _info(row) if row is not None and row.registration_kind == "dcr" else None


def normalize_scopes(requested: str | None) -> list[str]:
    """Intersect the request with what we support; default to ``mcp`` (unknown values such as
    ``offline_access`` are dropped rather than rejected)."""
    asked = (requested or "").split()
    granted = [s for s in dict.fromkeys(asked) if s in SUPPORTED_SCOPES]
    return granted or ["mcp"]
