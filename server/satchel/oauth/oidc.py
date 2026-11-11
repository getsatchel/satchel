"""Minimal OIDC relying party: discovery, authorization-code exchange, ID-token validation.

Authlib would do this too; this is ~150 lines on httpx + PyJWT and keeps the flow explicit.
satchel always uses PKCE (S256) and a nonce towards the IdP, and trusts only a signature-verified
ID token (issuer, audience, expiry and nonce checked).
"""

from __future__ import annotations

import base64
import hmac
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote_plus, urlencode

import httpx
import jwt

from satchel.settings import Settings

ALLOWED_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"]
DISCOVERY_TTL_S = 3600


class OidcError(Exception):
    """The IdP round-trip failed or returned something we refuse to trust."""


class OidcNotConfigured(OidcError):
    pass


@dataclass(frozen=True)
class Identity:
    subject: str
    email: str | None
    email_verified: bool
    name: str | None


class OidcProvider(Protocol):
    def authorize_url(
        self, *, state: str, nonce: str, code_challenge: str, redirect_uri: str
    ) -> str: ...

    def exchange(
        self, *, code: str, code_verifier: str, nonce: str, redirect_uri: str
    ) -> Identity: ...


def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.lower() == "true")


class HttpOidcProvider:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        if not (settings.oidc_issuer and settings.oidc_client_id):
            raise OidcNotConfigured("SATCHEL_OIDC_ISSUER and SATCHEL_OIDC_CLIENT_ID are required")
        self.issuer = settings.oidc_issuer.rstrip("/")
        self.client_id = settings.oidc_client_id
        self.client_secret = settings.oidc_client_secret
        self.scopes = settings.oidc_scopes
        self._transport = transport
        self._doc: dict[str, Any] | None = None
        self._doc_at = 0.0
        self._jwks: dict[str, Any] | None = None

    def _http(self) -> httpx.Client:
        return httpx.Client(timeout=10.0, transport=self._transport, follow_redirects=False)

    def _discovery(self) -> dict[str, Any]:
        if self._doc is not None and time.monotonic() - self._doc_at < DISCOVERY_TTL_S:
            return self._doc
        try:
            with self._http() as c:
                r = c.get(f"{self.issuer}/.well-known/openid-configuration")
                r.raise_for_status()
                doc = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise OidcError(f"OIDC discovery failed: {e}") from e
        if not isinstance(doc, dict) or str(doc.get("issuer", "")).rstrip("/") != self.issuer:
            raise OidcError("OIDC discovery issuer does not match SATCHEL_OIDC_ISSUER")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not doc.get(k):
                raise OidcError(f"OIDC discovery document lacks {k}")
        self._doc, self._doc_at = doc, time.monotonic()
        return doc

    def authorize_url(
        self, *, state: str, nonce: str, code_challenge: str, redirect_uri: str
    ) -> str:
        doc = self._discovery()
        q = urlencode({
            "response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
            "scope": self.scopes, "state": state, "nonce": nonce,
            "code_challenge": code_challenge, "code_challenge_method": "S256",
        })
        sep = "&" if "?" in doc["authorization_endpoint"] else "?"
        return f"{doc['authorization_endpoint']}{sep}{q}"

    def _signing_key(self, c: httpx.Client, doc: dict[str, Any], kid: str | None) -> Any:
        for attempt in (0, 1):
            if self._jwks is None or attempt == 1:
                r = c.get(doc["jwks_uri"])
                r.raise_for_status()
                self._jwks = r.json()
            keys = (self._jwks or {}).get("keys", [])
            match = [k for k in keys if kid is None or k.get("kid") == kid]
            if match:
                return jwt.PyJWK(match[0]).key
        raise OidcError("no matching signing key in the IdP JWKS")

    def exchange(
        self, *, code: str, code_verifier: str, nonce: str, redirect_uri: str
    ) -> Identity:
        doc = self._discovery()
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "code_verifier": code_verifier}
        headers = {"Accept": "application/json"}
        if self.client_secret:
            methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
            if "client_secret_basic" in methods:
                headers["Authorization"] = "Basic " + _b64(
                    f"{quote_plus(self.client_id)}:{quote_plus(self.client_secret)}")
            else:
                data.update(client_id=self.client_id, client_secret=self.client_secret)
        else:
            data["client_id"] = self.client_id
        try:
            with self._http() as c:
                r = c.post(doc["token_endpoint"], data=data, headers=headers)
                if r.status_code != 200:
                    raise OidcError(f"IdP token endpoint returned {r.status_code}")
                tok = r.json()
                id_token = tok.get("id_token")
                if not isinstance(id_token, str):
                    raise OidcError("IdP response has no id_token")
                claims = self._validate(c, doc, id_token, nonce)
                email = claims.get("email")
                verified = _truthy(claims.get("email_verified"))
                if not email and tok.get("access_token") and doc.get("userinfo_endpoint"):
                    ui = c.get(doc["userinfo_endpoint"],
                               headers={"Authorization": f"Bearer {tok['access_token']}"})
                    if ui.status_code == 200:
                        info = ui.json()
                        if info.get("sub") == claims["sub"]:
                            email = info.get("email")
                            verified = _truthy(info.get("email_verified"))
        except (httpx.HTTPError, ValueError) as e:
            raise OidcError(f"IdP request failed: {e}") from e
        name = claims.get("name") or claims.get("preferred_username")
        return Identity(
            str(claims["sub"]),
            email if isinstance(email, str) and email else None,
            verified,
            name if isinstance(name, str) else None,
        )

    def _validate(
        self, c: httpx.Client, doc: dict[str, Any], id_token: str, nonce: str
    ) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
            alg = header.get("alg")
            if alg not in ALLOWED_ALGS:
                raise OidcError(f"unsupported ID token algorithm: {alg}")
            key = self._signing_key(c, doc, header.get("kid"))
            claims: dict[str, Any] = jwt.decode(
                id_token, key, algorithms=[alg], audience=self.client_id,
                issuer=str(doc["issuer"]),
                options={"require": ["exp", "iss", "aud", "sub"]}, leeway=30,
            )
        except jwt.PyJWTError as e:
            raise OidcError(f"invalid ID token: {e}") from e
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.client_id:
            raise OidcError("invalid ID token: azp does not match client_id")
        got = claims.get("nonce")
        if not isinstance(got, str) or not hmac.compare_digest(got, nonce):
            raise OidcError("invalid ID token: nonce mismatch")
        return claims


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def make_provider(settings: Settings) -> OidcProvider | None:
    """The configured IdP client, or None when OIDC is not configured."""
    if not (settings.oidc_issuer and settings.oidc_client_id):
        return None
    return HttpOidcProvider(settings)
