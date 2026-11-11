"""Shared helpers: hashing, public URLs, resource (audience) URIs, redirect-URI rules."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import secrets
from urllib.parse import urlsplit

from satchel.settings import Settings

SUPPORTED_SCOPES = ("mcp",)
DEFAULT_SCOPE = "mcp"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
ACCESS_PREFIX = "satchel_at_"
REFRESH_PREFIX = "satchel_rt_"
_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def valid_verifier(verifier: str) -> bool:
    return bool(_VERIFIER_RE.match(verifier))


def valid_challenge(challenge: str) -> bool:
    # S256 challenge = base64url(sha256) = exactly 43 chars.
    return bool(re.fullmatch(r"[A-Za-z0-9\-_]{43}", challenge))


def issuer(settings: Settings) -> str:
    return settings.public_url.rstrip("/")


def mcp_resource(settings: Settings) -> str:
    return issuer(settings) + "/mcp"


def normalize_resource(uri: str) -> str:
    """Canonical form for comparing resource URIs: lowercase scheme/host, no trailing slash."""
    parts = urlsplit(uri.strip())
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme.lower()}://{netloc}{parts.path.rstrip('/')}"


def allowed_resources(settings: Settings) -> frozenset[str]:
    """Audiences this server issues and accepts (RFC 8707): exactly its MCP resource URI, the
    ``resource`` of ``/.well-known/oauth-protected-resource/mcp``. Clients that omit
    ``resource`` are bound to it by default."""
    return frozenset({normalize_resource(mcp_resource(settings))})


def cookie_secure(settings: Settings) -> bool:
    return settings.public_url.lower().startswith("https://")


def redirect_uri_error(uri: str) -> str | None:
    """Why ``uri`` is not an acceptable redirect URI, or None. https, or http on loopback."""
    if not uri or len(uri) > 2000 or any(c in uri for c in " \t\r\n\\"):
        return "malformed redirect_uri"
    parts = urlsplit(uri)
    if parts.fragment or "#" in uri:
        return "redirect_uri must not contain a fragment"
    if parts.username is not None or parts.password is not None:
        return "redirect_uri must not contain credentials"
    host = (parts.hostname or "").lower()
    if not host:
        return "redirect_uri must be absolute"
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and host in LOOPBACK_HOSTS:
        return None
    return "redirect_uri must be https, or http on a loopback address"


def is_public_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def safe_next(url: str | None) -> str:
    """Only same-origin relative paths survive; everything else becomes '/' (open redirect)."""
    if not url or not url.startswith("/") or url.startswith(("//", "/\\")):
        return "/"
    if any(ord(c) < 0x20 for c in url) or "\\" in url:
        return "/"
    return url
