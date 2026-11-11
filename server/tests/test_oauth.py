"""OAuth 2.1 authorization server, OIDC login and the security checklist (design §9, M2.1).

The IdP is faked at the ``OidcProvider`` seam; the real ``HttpOidcProvider`` is exercised
separately against an in-process IdP (``test_oidc_provider_*``).
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import Engine, select, update

from satchel.api.app import create_app
from satchel.llm.embeddings import HashEmbedder
from satchel.mcp.auth import create_token, verify_token
from satchel.oauth.clients import ClientMetadataError, ssrf_guarded_fetch
from satchel.oauth.common import pkce_s256, random_token, safe_next, sha256_hex
from satchel.oauth.oidc import HttpOidcProvider, Identity, OidcError
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import ApiToken, AppUser, AuthFlow, ContextItem, Workspace
from tests.helpers import unique_slug

PUBLIC = "http://localhost:8080"
REDIRECT = "http://localhost:53682/callback"
SFX = uuid.uuid4().hex[:8]
ALICE = f"alice-{SFX}@x.test"


class FakeOidc:
    def __init__(self) -> None:
        self.identity = Identity("sub-alice", ALICE, True, "Alice")
        self.last_state = ""

    def authorize_url(self, *, state: str, nonce: str, code_challenge: str,
                      redirect_uri: str) -> str:
        self.last_state = state
        assert redirect_uri.endswith("/auth/callback")
        return "https://idp.test/authorize?" + urlencode({"state": state})

    def exchange(self, *, code: str, code_verifier: str, nonce: str,
                 redirect_uri: str) -> Identity:
        return self.identity


@dataclass
class Env:
    engine: Engine
    app: Any
    idp: FakeOidc
    wa: str
    wb: str
    wp: str
    wa_id: uuid.UUID
    wb_id: uuid.UUID
    cimd_docs: dict[str, dict[str, Any]]
    settings: Settings

    def make_app(self) -> Any:
        """A fresh app: the MCP session manager can only run once per instance."""
        def fetch(url: str) -> dict[str, Any]:
            if url not in self.cimd_docs:
                raise ClientMetadataError("no such document")
            return self.cimd_docs[url]

        return create_app(self.engine, HashEmbedder(), settings=self.settings, oidc=self.idp,
                          cimd_fetcher=fetch)


@pytest.fixture(scope="module")
def env(app_engine: Engine) -> Iterator[Env]:
    wa, wb, wp = (unique_slug(p) for p in ("oa", "ob", "op"))
    ids = {k: uuid.uuid4() for k in (wa, wb, wp)}
    with admin_session(engine=app_engine) as s:
        for slug in (wa, wb):
            s.add(Workspace(id=ids[slug], slug=slug, name=slug.upper()))
        s.add(Workspace(id=ids[wp], slug=wp, name="Provisioning", settings={
            "auth": {"provision": "explorer", "allowed_email_domains": ["corp.test"]}}))
        s.flush()
        # alice is a member of A and B (same email), so the same identity can reach both.
        s.add_all([
            AppUser(workspace_id=ids[wa], email=ALICE, display_name="Alice", role="analyst"),
            AppUser(workspace_id=ids[wb], email=ALICE, display_name="Alice", role="curator"),
            ContextItem(workspace_id=ids[wa], type="guide", key=f"only-in-a-{SFX}"),
            ContextItem(workspace_id=ids[wb], type="guide", key=f"only-in-b-{SFX}"),
        ])
    idp = FakeOidc()
    settings = Settings(public_url=PUBLIC, dev_auth=False, oauth_cimd_allow_private_hosts=False)
    e = Env(app_engine, None, idp, wa, wb, wp, ids[wa], ids[wb], {}, settings)
    e.app = e.make_app()  # lifespan-less app for extra "other browser" clients
    yield e


@pytest.fixture
def c(env: Env) -> Iterator[TestClient]:
    with TestClient(env.make_app(), base_url=PUBLIC) as client:
        yield client


def register(c: TestClient, redirect: str = REDIRECT, name: str = "Test Client") -> str:
    r = c.post("/oauth/register", json={
        "client_name": name, "redirect_uris": [redirect], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    assert r.status_code == 201, r.text
    return str(r.json()["client_id"])


def _form_field(html: str, name: str) -> str:
    m = re.search(rf"name={name} value='([^']+)'", html)
    assert m, html
    return m.group(1)


@dataclass
class Grant:
    client_id: str
    verifier: str
    code: str
    redirect_uri: str
    state: str


def authorize(c: TestClient, env: Env, client_id: str, *, workspace: str | None,
              approve: bool = True, redirect_uri: str = REDIRECT, resource: str | None = None,
              state: str = "st-123", extra: dict[str, str] | None = None) -> Grant:
    verifier = random_token(48)
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
              "code_challenge": pkce_s256(verifier), "code_challenge_method": "S256",
              "state": state, "scope": "mcp"}
    if workspace:
        params["workspace"] = workspace
    if resource:
        params["resource"] = resource
    params.update(extra or {})
    r = c.get("/oauth/authorize", params=params, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"].startswith("https://idp.test/authorize")
    r = c.get("/auth/callback", params={"code": "idp-code", "state": env.idp.last_state},
              follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    if loc.startswith("/auth/workspace"):  # more than one candidate: picker
        page = c.get(loc)
        assert page.status_code == 200
        assert workspace is None
        raise AssertionError("unexpected picker")
    assert loc.startswith("/oauth/consent?flow="), loc
    page = c.get(loc)
    assert page.status_code == 200, page.text
    form = {"flow": _form_field(page.text, "flow"), "csrf": _form_field(page.text, "csrf"),
            "decision": "approve" if approve else "deny"}
    r = c.post("/oauth/consent", data=form, follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(redirect_uri)
    assert q["state"] == [state] and q["iss"] == [PUBLIC]   # state + RFC 9207 round trip
    if not approve:
        assert q["error"] == ["access_denied"]
        return Grant(client_id, verifier, "", redirect_uri, state)
    return Grant(client_id, verifier, q["code"][0], redirect_uri, state)


def token_req(c: TestClient, g: Grant, **over: str) -> httpx.Response:
    data = {"grant_type": "authorization_code", "code": g.code, "client_id": g.client_id,
            "redirect_uri": g.redirect_uri, "code_verifier": g.verifier}
    data.update(over)
    return c.post("/oauth/token", data=data)


def refresh_req(c: TestClient, client_id: str, rt: str, **over: str) -> httpx.Response:
    data = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": client_id}
    data.update(over)
    return c.post("/oauth/token", data=data)


def bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def full_grant(c: TestClient, env: Env, workspace: str | None = None) -> tuple[str, dict[str, Any]]:
    cid = register(c)
    g = authorize(c, env, cid, workspace=workspace or env.wa)
    r = token_req(c, g)
    assert r.status_code == 200, r.text
    return cid, r.json()


# ---- discovery -----------------------------------------------------------------------------


def test_discovery_documents(c: TestClient) -> None:
    prm = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == f"{PUBLIC}/mcp" and prm["authorization_servers"] == [PUBLIC]
    assert c.get("/.well-known/oauth-protected-resource").json()["resource"] == prm["resource"]
    md = c.get("/.well-known/oauth-authorization-server").json()
    assert md["issuer"] == PUBLIC
    assert md["code_challenge_methods_supported"] == ["S256"]
    assert md["registration_endpoint"] == f"{PUBLIC}/oauth/register"
    assert md["authorization_response_iss_parameter_supported"] is True
    assert md["client_id_metadata_document_supported"] is True
    assert md["token_endpoint_auth_methods_supported"] == ["none"]
    assert c.options("/oauth/token").headers["access-control-allow-origin"] == "*"


def test_mcp_401_advertises_resource_metadata(c: TestClient) -> None:
    r = c.post("/mcp", json={})
    assert r.status_code == 401
    h = r.headers["www-authenticate"]
    assert f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"' in h
    assert "error=" not in h  # no credentials presented -> no error code (RFC 6750)
    r = c.post("/mcp", json={}, headers=bearer("satchel_at_bogus"))
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]


# ---- registration --------------------------------------------------------------------------


def test_register_validation(c: TestClient) -> None:
    ok = c.post("/oauth/register", json={"redirect_uris": ["https://claude.ai/api/mcp/cb"]})
    assert ok.status_code == 201
    body = ok.json()
    assert body["token_endpoint_auth_method"] == "none" and "client_secret" not in body
    assert c.post("/oauth/register", json={"redirect_uris": ["http://[::1]:9/cb"]}
                  ).status_code == 201
    bad: list[dict[str, Any]] = [
        {"redirect_uris": ["http://evil.test/cb"]},            # http, not loopback
        {"redirect_uris": ["http://localhost.evil.test/cb"]},   # loopback look-alike
        {"redirect_uris": ["https://a.test/cb#frag"]},
        {"redirect_uris": ["javascript:alert(1)"]},
        {"redirect_uris": ["https://user:pw@a.test/cb"]},
        {"redirect_uris": []},
        {},
        {"redirect_uris": ["https://a.test/cb"],
         "token_endpoint_auth_method": "client_secret_basic"},
        {"redirect_uris": ["https://a.test/cb"], "grant_types": ["implicit"]},
    ]
    for b in bad:
        r = c.post("/oauth/register", json=b)
        assert r.status_code == 400 and r.json()["error"] == "invalid_client_metadata", b
    assert c.post("/oauth/register", content=b"[]").status_code == 400


# ---- authorize validation ------------------------------------------------------------------


def test_authorize_rejects_unsafe_requests_without_redirecting(c: TestClient) -> None:
    cid = register(c)
    base = {"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
            "code_challenge": pkce_s256("v" * 50), "code_challenge_method": "S256", "state": "s"}
    # Unknown client / non-exact redirect URIs are errors on the AS page, never redirects.
    for over in ({"client_id": "nope"}, {"redirect_uri": REDIRECT + "/x"},
                 {"redirect_uri": "http://localhost:53682/callbac"},
                 {"redirect_uri": "https://evil.test/cb"},
                 {"redirect_uri": "http://localhost:53683/callback"}):
        r = c.get("/oauth/authorize", params={**base, **over}, follow_redirects=False)
        assert r.status_code == 400 and "location" not in r.headers, over


def test_authorize_pkce_and_other_errors_redirect_to_client(c: TestClient) -> None:
    cid = register(c)
    base = {"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
            "code_challenge": pkce_s256("v" * 50), "code_challenge_method": "S256", "state": "xyz"}
    cases = [
        ({"code_challenge_method": "plain"}, "invalid_request"),     # plain rejected
        ({"code_challenge_method": ""}, "invalid_request"),
        ({"code_challenge": ""}, "invalid_request"),                 # PKCE required
        ({"code_challenge": "short"}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"resource": "https://evil.test/mcp"}, "invalid_target"),   # wrong audience
    ]
    for over, err in cases:
        params = {**base, **over}
        if over.get("code_challenge") == "":
            params.pop("code_challenge")
        if over.get("code_challenge_method") == "":
            params.pop("code_challenge_method")
        r = c.get("/oauth/authorize", params=params, follow_redirects=False)
        assert r.status_code == 302, over
        q = parse_qs(urlsplit(r.headers["location"]).query)
        assert r.headers["location"].startswith(REDIRECT)
        assert q["error"] == [err] and q["state"] == ["xyz"] and q["iss"] == [PUBLIC], over


# ---- happy path + token endpoint -----------------------------------------------------------


def test_full_flow_tokens_work_on_rest_and_mcp(c: TestClient, env: Env) -> None:
    cid, tok = full_grant(c, env)
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 3600
    assert tok["access_token"].startswith("satchel_at_")
    assert tok["refresh_token"].startswith("satchel_rt_")
    assert tok["scope"] == "mcp"
    with admin_session(engine=env.engine) as s:
        rows = {t.kind: t for t in s.scalars(
            select(ApiToken).where(ApiToken.client_id == cid))}
        assert rows["oauth_access"].hash == sha256_hex(tok["access_token"]) != tok["access_token"]
        assert rows["oauth_access"].resource == f"{PUBLIC}/mcp"
        assert rows["oauth_access"].workspace_id == env.wa_id
        assert rows["oauth_access"].family_id == rows["oauth_refresh"].family_id
    # REST + MCP accept the access token; the audience is this server.
    page = c.get("/v1/items", headers=bearer(tok["access_token"])).json()
    keys = {i["key"] for i in page["items"]}
    assert keys == {f"only-in-a-{SFX}"}
    r = c.post("/mcp", headers={**bearer(tok["access_token"]),
                                "Accept": "application/json, text/event-stream"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200 and "start_analysis" in r.text
    # A refresh token is never a bearer credential.
    assert c.get("/v1/items", headers=bearer(tok["refresh_token"])).status_code == 401


def test_code_is_single_use_short_lived_and_replay_revokes_family(c: TestClient, env: Env) -> None:
    cid = register(c)
    g = authorize(c, env, cid, workspace=env.wa)
    with admin_session(engine=env.engine) as s:
        f = s.scalars(select(AuthFlow).where(AuthFlow.code_hash == sha256_hex(g.code))).one()
        assert f.code_expires_at is not None
        assert f.code_expires_at - datetime.now(UTC) <= timedelta(seconds=60)
    r = token_req(c, g)
    assert r.status_code == 200
    first = r.json()
    assert c.get("/v1/items", headers=bearer(first["access_token"])).status_code == 200
    again = token_req(c, g)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    # Replaying a used code revokes everything already minted from it.
    assert c.get("/v1/items", headers=bearer(first["access_token"])).status_code == 401
    assert refresh_req(c, cid, first["refresh_token"]).status_code == 400


def test_code_expiry(c: TestClient, env: Env) -> None:
    cid = register(c)
    g = authorize(c, env, cid, workspace=env.wa)
    with admin_session(engine=env.engine) as s:
        s.execute(update(AuthFlow).where(AuthFlow.code_hash == sha256_hex(g.code))
                  .values(code_expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    r = token_req(c, g)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_token_endpoint_pkce_redirect_and_client_binding(c: TestClient, env: Env) -> None:
    cid = register(c)
    other = register(c, name="Other")
    g = authorize(c, env, cid, workspace=env.wa)
    for over in ({"code_verifier": "x" * 50}, {"code_verifier": "short"},
                 {"redirect_uri": REDIRECT + "/other"}, {"client_id": other},
                 {"resource": "https://evil.test/mcp"}):
        r = token_req(c, g, **over)
        assert r.status_code == 400, over
    r = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": g.code,
                                     "client_id": cid, "redirect_uri": REDIRECT})
    assert r.status_code == 400  # no code_verifier at all
    assert token_req(c, g).status_code == 200  # none of the failures burned the code
    assert c.post("/oauth/token", data={"grant_type": "password", "client_id": cid}
                  ).json()["error"] == "unsupported_grant_type"


def test_state_roundtrip_and_deny(c: TestClient, env: Env) -> None:
    cid = register(c)
    authorize(c, env, cid, workspace=env.wa, state="a b&c=d", approve=False)


def test_refresh_rotation_and_reuse_revokes_family(c: TestClient, env: Env) -> None:
    cid, t1 = full_grant(c, env)
    r2 = refresh_req(c, cid, t1["refresh_token"])
    assert r2.status_code == 200
    t2 = r2.json()
    assert t2["refresh_token"] != t1["refresh_token"] and t2["access_token"] != t1["access_token"]
    assert c.get("/v1/items", headers=bearer(t2["access_token"])).status_code == 200
    # Presenting the rotated-out refresh token again is theft: the whole family dies.
    reuse = refresh_req(c, cid, t1["refresh_token"])
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    assert c.get("/v1/items", headers=bearer(t2["access_token"])).status_code == 401
    assert refresh_req(c, cid, t2["refresh_token"]).status_code == 400


def test_refresh_is_bound_to_client_and_expiry(c: TestClient, env: Env) -> None:
    cid, t = full_grant(c, env)
    other = register(c, name="Other")
    assert refresh_req(c, other, t["refresh_token"]).status_code == 400
    assert refresh_req(c, cid, t["refresh_token"], resource="https://evil.test").status_code == 400
    with admin_session(engine=env.engine) as s:
        s.execute(update(ApiToken).where(ApiToken.hash == sha256_hex(t["refresh_token"]))
                  .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert refresh_req(c, cid, t["refresh_token"]).status_code == 400


def test_revocation(c: TestClient, env: Env) -> None:
    cid, t = full_grant(c, env)
    other = register(c, name="Other")
    # Another client cannot revoke it; garbage tokens still get 200 (RFC 7009).
    assert c.post("/oauth/revoke", data={"token": t["access_token"], "client_id": other}
                  ).status_code == 200
    assert c.get("/v1/items", headers=bearer(t["access_token"])).status_code == 200
    assert c.post("/oauth/revoke", data={"token": "garbage", "client_id": cid}).status_code == 200
    assert c.post("/oauth/revoke", data={"token": t["refresh_token"], "client_id": cid,
                                         "token_type_hint": "refresh_token"}).status_code == 200
    assert c.get("/v1/items", headers=bearer(t["access_token"])).status_code == 401
    assert refresh_req(c, cid, t["refresh_token"]).status_code == 400


def test_expired_and_foreign_audience_tokens_rejected(c: TestClient, env: Env) -> None:
    cid, t = full_grant(c, env)
    h = bearer(t["access_token"])
    with admin_session(engine=env.engine) as s:
        row = s.scalars(select(ApiToken).where(ApiToken.hash == sha256_hex(t["access_token"]))
                        ).one()
        row.resource = "https://other-server.test/mcp"
    assert c.get("/v1/items", headers=h).status_code == 401
    assert verify_token(t["access_token"], engine=env.engine, settings=env.settings) is None
    with admin_session(engine=env.engine) as s:
        row = s.scalars(select(ApiToken).where(ApiToken.hash == sha256_hex(t["access_token"]))
                        ).one()
        row.resource = f"{PUBLIC}/mcp"
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert c.get("/v1/items", headers=h).status_code == 401


def test_tokens_do_not_cross_workspaces(c: TestClient, env: Env) -> None:
    # alice belongs to both A and B; each grant is pinned to the workspace chosen at login.
    cid_a, ta = full_grant(c, env, env.wa)
    cid_b, tb = full_grant(c, env, env.wb)
    ka = {i["key"] for i in c.get("/v1/items", headers=bearer(ta["access_token"])).json()["items"]}
    kb = {i["key"] for i in c.get("/v1/items", headers=bearer(tb["access_token"])).json()["items"]}
    assert ka == {f"only-in-a-{SFX}"} and kb == {f"only-in-b-{SFX}"}
    # Spoofing workspace headers does nothing without SATCHEL_DEV_AUTH.
    h = {**bearer(ta["access_token"]), "X-Satchel-Workspace": env.wb}
    assert {i["key"] for i in c.get("/v1/items", headers=h).json()["items"]} == ka
    # A refresh token cannot be used by the other grant's client to land in another workspace.
    assert refresh_req(c, cid_b, ta["refresh_token"]).status_code == 400
    # A grant for an unrelated workspace is refused at login.
    env.idp.identity = Identity("sub-alice", ALICE, True, "Alice")
    cid = register(c)
    verifier = random_token(48)
    c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
        "code_challenge": pkce_s256(verifier), "code_challenge_method": "S256",
        "workspace": env.wp}, follow_redirects=False)
    r = c.get("/auth/callback", params={"code": "x", "state": env.idp.last_state})
    assert r.status_code == 403 and "does not have access" in r.text


def test_pat_still_works_and_dev_headers_are_off(c: TestClient, env: Env) -> None:
    raw, _ = create_token(env.wa, ALICE, "pat", engine=env.engine)
    assert c.get("/v1/items", headers=bearer(raw)).status_code == 200
    assert c.get("/v1/items").status_code == 401
    assert c.get("/v1/items", headers={"X-Satchel-Workspace": env.wa}).status_code == 401


# ---- user mapping / provisioning -----------------------------------------------------------


def _login_only(c: TestClient, env: Env, workspace: str) -> httpx.Response:
    cid = register(c)
    v = random_token(48)
    c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
        "code_challenge": pkce_s256(v), "code_challenge_method": "S256",
        "workspace": workspace}, follow_redirects=False)
    return c.get("/auth/callback", params={"code": "x", "state": env.idp.last_state},
                 follow_redirects=False)


def test_unknown_users_rejected_unless_workspace_provisions(c: TestClient, env: Env) -> None:
    env.idp.identity = Identity("sub-new", f"new-{SFX}@corp.test", True, "New")
    r = _login_only(c, env, env.wa)  # policy: reject (default)
    assert r.status_code == 403
    r = _login_only(c, env, env.wp)  # policy: explorer for corp.test
    assert r.status_code == 302 and r.headers["location"].startswith("/oauth/consent")
    with admin_session(engine=env.engine) as s:
        u = s.scalars(select(AppUser).where(AppUser.external_sub == "sub-new")).one()
        assert u.role == "explorer" and u.email == f"new-{SFX}@corp.test"
    env.idp.identity = Identity("sub-eve", f"eve-{SFX}@evil.test", True, "Eve")
    assert _login_only(c, env, env.wp).status_code == 403          # domain not allowed
    env.idp.identity = Identity("sub-unv", f"unv-{SFX}@corp.test", False, None)
    assert _login_only(c, env, env.wp).status_code == 403          # unverified email


def test_email_link_then_sub_match_and_no_relink(c: TestClient, env: Env) -> None:
    email = f"link-{SFX}@x.test"
    with admin_session(engine=env.engine) as s:
        s.add(AppUser(workspace_id=env.wa_id, email=email, role="analyst"))
    env.idp.identity = Identity("sub-link-1", email, True, "L")
    assert _login_only(c, env, env.wa).status_code == 302   # first login links by email
    with admin_session(engine=env.engine) as s:
        u = s.scalars(select(AppUser).where(AppUser.workspace_id == env.wa_id,
                                            AppUser.email == email)).one()
        assert u.external_sub == "sub-link-1"
    # Same email, different subject (e.g. recycled address): must not take over the account.
    env.idp.identity = Identity("sub-mallory", email.upper(), True, "M")
    assert _login_only(c, env, env.wa).status_code == 403
    # An unverified email never links an existing account.
    env.idp.identity = Identity("sub-unverified", f"x-{SFX}@x.test", False, None)
    assert _login_only(c, env, env.wa).status_code == 403
    env.idp.identity = Identity("sub-link-1", None, False, None)  # by subject alone
    assert _login_only(c, env, env.wa).status_code == 302
    env.idp.identity = Identity("sub-alice", ALICE, True, "Alice")  # restore for other tests


def test_multi_workspace_picker(c: TestClient, env: Env) -> None:
    # Re-link: sub-alice-1 now owns A; use a fresh subject only matched via B's email? Both A and
    # B have alice's email, so a fresh unlinked subject sees both workspaces in the picker.
    email = f"multi-{SFX}@x.test"
    with admin_session(engine=env.engine) as s:
        for wid in (env.wa_id, env.wb_id):
            s.add(AppUser(workspace_id=wid, email=email, role="analyst"))
    env.idp.identity = Identity(f"sub-multi-{SFX}", email, True, "Multi")
    cid = register(c)
    v = random_token(48)
    c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
        "code_challenge": pkce_s256(v), "code_challenge_method": "S256"},
        follow_redirects=False)
    r = c.get("/auth/callback", params={"code": "x", "state": env.idp.last_state},
              follow_redirects=False)
    assert r.headers["location"].startswith("/auth/workspace?flow=")
    page = c.get(r.headers["location"])
    assert env.wa in page.text and env.wb in page.text
    form = {"flow": _form_field(page.text, "flow"), "csrf": "wrong", "workspace": env.wb}
    assert c.post("/auth/workspace", data=form).status_code == 403
    form["csrf"] = _form_field(page.text, "csrf")
    form["workspace"] = env.wp  # not offered to this identity
    assert c.post("/auth/workspace", data=form).status_code == 403
    form["workspace"] = env.wb
    r = c.post("/auth/workspace", data=form, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/oauth/consent")
    env.idp.identity = Identity("sub-alice", ALICE, True, "Alice")


# ---- flow integrity: state, login CSRF, consent CSRF ---------------------------------------


def test_callback_needs_matching_browser_and_state_is_single_use(c: TestClient, env: Env) -> None:
    cid = register(c)
    v = random_token(48)
    params = {"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
              "code_challenge": pkce_s256(v), "code_challenge_method": "S256",
              "workspace": env.wa}
    c.get("/oauth/authorize", params=params, follow_redirects=False)
    state = env.idp.last_state
    attacker = TestClient(env.app, base_url=PUBLIC)  # different browser, no flow cookie
    assert attacker.get("/auth/callback", params={"code": "x", "state": state}).status_code == 403
    assert c.get("/auth/callback", params={"code": "x", "state": "forged"}).status_code == 400
    assert c.get("/auth/callback", params={"code": "x", "state": state},
                 follow_redirects=False).status_code == 302
    assert c.get("/auth/callback", params={"code": "x", "state": state}).status_code == 400


def test_idp_error_redirects_access_denied(c: TestClient, env: Env) -> None:
    cid = register(c)
    c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
        "code_challenge": pkce_s256(random_token(48)), "code_challenge_method": "S256",
        "state": "S9"}, follow_redirects=False)
    r = c.get("/auth/callback", params={"error": "access_denied", "state": env.idp.last_state},
              follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(REDIRECT) and q["error"] == ["access_denied"]
    assert q["state"] == ["S9"]


def test_consent_requires_csrf_token_and_cookie(c: TestClient, env: Env) -> None:
    cid = register(c)
    c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
        "code_challenge": pkce_s256(random_token(48)), "code_challenge_method": "S256",
        "workspace": env.wa}, follow_redirects=False)
    r = c.get("/auth/callback", params={"code": "x", "state": env.idp.last_state},
              follow_redirects=False)
    page = c.get(r.headers["location"])
    assert "Test Client" in page.text and "localhost:53682" in page.text
    flow = _form_field(page.text, "flow")
    bad = c.post("/oauth/consent", data={"flow": flow, "csrf": "nope", "decision": "approve"})
    assert bad.status_code == 403
    other = TestClient(env.app, base_url=PUBLIC)   # other browser: no flow cookie
    assert other.post("/oauth/consent", data={
        "flow": flow, "csrf": _form_field(page.text, "csrf"),
        "decision": "approve"}).status_code == 403


# ---- Client ID Metadata Documents ----------------------------------------------------------


def test_cimd_client(c: TestClient, env: Env) -> None:
    url = "https://app.example.com/oauth/client.json"
    env.cimd_docs[url] = {"client_id": url, "client_name": "CIMD App",
                          "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}
    g = authorize(c, env, url, workspace=env.wa)
    r = token_req(c, g)
    assert r.status_code == 200
    # Mismatched client_id in the document, secrets and bad URLs are all refused.
    bad = "https://app.example.com/oauth/other.json"
    env.cimd_docs[bad] = {"client_id": "https://evil.test/x.json", "redirect_uris": [REDIRECT]}
    sec = "https://app.example.com/oauth/secret.json"
    env.cimd_docs[sec] = {"client_id": sec, "redirect_uris": [REDIRECT],
                          "token_endpoint_auth_method": "client_secret_basic"}
    for cid in (bad, sec, "https://app.example.com/", "https://nowhere.example.com/c.json"):
        r = c.get("/oauth/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
            "code_challenge": pkce_s256("v" * 50), "code_challenge_method": "S256"},
            follow_redirects=False)
        assert r.status_code == 400, cid


def test_cimd_fetch_is_ssrf_guarded() -> None:
    for u in ("https://localhost/c.json", "https://127.0.0.1/c.json", "https://[::1]/c.json",
              "https://169.254.169.254/latest", "https://10.0.0.5/c.json"):
        with pytest.raises(ClientMetadataError):
            ssrf_guarded_fetch(u)


# ---- human login (cookie session) ----------------------------------------------------------


def test_web_login_session_cookie_and_csrf(c: TestClient, env: Env) -> None:
    env.idp.identity = Identity("sub-alice", ALICE, True, "Alice")
    r = c.get("/auth/login", params={"workspace": env.wa, "next": "/v1/items"},
              follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://idp.test/")
    assert c.get("/auth/me").status_code == 401
    r = c.get("/auth/callback", params={"code": "x", "state": env.idp.last_state},
              follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/v1/items"
    sc = "; ".join(r.headers.get_list("set-cookie"))
    assert "satchel_session=" in sc and "HttpOnly" in sc and "samesite=lax" in sc.lower()
    me = c.get("/auth/me").json()
    assert me["workspace"] == env.wa and me["email"] == ALICE and me["csrf_token"]
    assert {i["key"] for i in c.get("/v1/items").json()["items"]} == {f"only-in-a-{SFX}"}
    # Writes need the CSRF header; the cookie alone is not enough.
    body = {"query": "anything", "k": 1}
    assert c.post("/v1/search", json=body).status_code == 403
    assert c.post("/v1/search", json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert c.post("/v1/search", json=body,
                  headers={"X-CSRF-Token": me["csrf_token"]}).status_code == 200
    assert c.post("/auth/logout").status_code == 403
    assert c.post("/auth/logout", headers={"X-CSRF-Token": me["csrf_token"]}).status_code == 200
    assert c.get("/auth/me").status_code == 401


def test_session_cookie_secure_when_https_and_next_is_not_an_open_redirect(env: Env) -> None:
    for bad in ("//evil.test", "https://evil.test", "/\\evil.test", "javascript:x", "", None,
                "/ok\r\nSet-Cookie: x=1"):
        assert safe_next(bad) == "/"
    assert safe_next("/jobs?x=1") == "/jobs?x=1"
    st = Settings(public_url="https://satchel.example.com", dev_auth=False)
    app = create_app(env.engine, HashEmbedder(), settings=st, oidc=env.idp)
    with TestClient(app, base_url="https://satchel.example.com") as c2:
        env.idp.identity = Identity("sub-alice", ALICE, True, "Alice")
        r = c2.get("/auth/login", params={"workspace": env.wa, "next": "//evil.test"},
                   follow_redirects=False)
        assert "secure" in r.headers["set-cookie"].lower()
        iss = c2.get("/.well-known/oauth-authorization-server").json()["issuer"]
        assert iss == "https://satchel.example.com"
        # open redirect attempt through next=: lands on '/'
        cb = c2.get("/auth/callback", params={"code": "x", "state": env.idp.last_state},
                    follow_redirects=False)
        assert cb.headers["location"] == "/"
        assert "secure" in "; ".join(cb.headers.get_list("set-cookie")).lower()


# ---- the real OIDC client against an in-process IdP ----------------------------------------

ISS = "https://idp.example.test"
CLIENT = "satchel-client"


class MiniIdp:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid="k1", alg="RS256", use="sig")
        self.jwks = {"keys": [jwk]}
        self.claims: dict[str, Any] = {}
        self.issuer_in_doc = ISS

    def id_token(self, **over: Any) -> str:
        now = int(time.time())
        claims = {"iss": ISS, "aud": CLIENT, "sub": "u1", "exp": now + 300, "iat": now,
                  "nonce": "N", "email": "u@x.test", "email_verified": True, "name": "U"}
        claims.update(over)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.issuer_in_doc, "authorization_endpoint": f"{ISS}/auth",
                "token_endpoint": f"{ISS}/token", "jwks_uri": f"{ISS}/keys"})
        if p == "/keys":
            return httpx.Response(200, json=self.jwks)
        if p == "/token":
            assert req.headers["authorization"].startswith("Basic ")
            form = parse_qs(req.content.decode())
            assert form["code_verifier"] == ["ver"] and form["grant_type"] == ["authorization_code"]
            return httpx.Response(200, json={"id_token": self.token, "access_token": "at"})
        return httpx.Response(404)

    token = ""


def _provider(idp: MiniIdp) -> HttpOidcProvider:
    st = Settings(oidc_issuer=ISS, oidc_client_id=CLIENT, oidc_client_secret="s3cret")
    return HttpOidcProvider(st, transport=httpx.MockTransport(idp.handler))


def _exchange(p: HttpOidcProvider) -> Identity:
    return p.exchange(code="c", code_verifier="ver", nonce="N", redirect_uri="http://x/cb")


def test_oidc_provider_accepts_valid_id_token() -> None:
    idp = MiniIdp()
    idp.token = idp.id_token()
    p = _provider(idp)
    url = p.authorize_url(state="s", nonce="N", code_challenge="c" * 43,
                          redirect_uri="http://x/cb")
    q = parse_qs(urlsplit(url).query)
    assert q["code_challenge_method"] == ["S256"] and q["nonce"] == ["N"] and q["state"] == ["s"]
    assert _exchange(p) == Identity("u1", "u@x.test", True, "U")


def test_oidc_provider_rejects_bad_id_tokens() -> None:
    idp = MiniIdp()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({"iss": ISS, "aud": CLIENT, "sub": "u", "exp": int(time.time()) + 99,
                         "nonce": "N"}, other, algorithm="RS256", headers={"kid": "k1"})
    hs = jwt.encode({"iss": ISS, "aud": CLIENT, "sub": "u", "exp": int(time.time()) + 99,
                     "nonce": "N"}, "secret" * 8, algorithm="HS256", headers={"kid": "k1"})
    unsigned = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=") + "." + \
        base64.urlsafe_b64encode(json.dumps({"iss": ISS, "aud": CLIENT, "sub": "u",
                                             "exp": int(time.time()) + 99, "nonce": "N"}
                                            ).encode()).decode().rstrip("=") + "."
    for tok in (idp.id_token(nonce="other"), idp.id_token(aud="someone-else"),
                idp.id_token(iss="https://evil.test"), idp.id_token(exp=int(time.time()) - 999),
                forged, hs, unsigned):
        idp.token = tok
        with pytest.raises(OidcError):
            _exchange(_provider(idp))
    bad_doc = MiniIdp()
    bad_doc.issuer_in_doc = "https://attacker.test"
    with pytest.raises(OidcError):
        _provider(bad_doc).authorize_url(state="s", nonce="N", code_challenge="c" * 43,
                                         redirect_uri="http://x/cb")


# ---- RFC 8707 audience binding ---------------------------------------------------------------


def _mcp_list(c: TestClient, token: str) -> httpx.Response:
    headers = {**bearer(token), "Accept": "application/json, text/event-stream"}
    return c.post("/mcp", headers=headers,
                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


def test_resource_must_be_the_mcp_resource_uri(c: TestClient, env: Env) -> None:
    cid = register(c)
    # The REST root is this server but not the MCP resource: invalid_target at authorize...
    r = c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "state": "s",
        "code_challenge": pkce_s256("v" * 50), "code_challenge_method": "S256",
        "resource": PUBLIC}, follow_redirects=False)
    assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["invalid_target"]
    # ...and at the token endpoint, where the code survives the rejected attempt.
    g = authorize(c, env, cid, workspace=env.wa, resource=f"{PUBLIC}/mcp/")  # trailing slash ok
    for bad in ("https://evil.test/mcp", PUBLIC):
        r = token_req(c, g, resource=bad)
        assert r.status_code == 400 and r.json()["error"] == "invalid_target", bad
    ok = token_req(c, g, resource=f"{PUBLIC}/mcp")
    assert ok.status_code == 200
    assert _mcp_list(c, ok.json()["access_token"]).status_code == 200


def test_missing_resource_defaults_to_mcp_and_refresh_keeps_audience(
    c: TestClient, env: Env
) -> None:
    cid, t1 = full_grant(c, env)  # no resource anywhere
    t2 = refresh_req(c, cid, t1["refresh_token"]).json()
    t3 = refresh_req(c, cid, t2["refresh_token"], resource=f"{PUBLIC}/mcp").json()
    with admin_session(engine=env.engine) as s:
        for t in (t1, t2, t3):
            for key in ("access_token", "refresh_token"):
                row = s.scalars(select(ApiToken).where(ApiToken.hash == sha256_hex(t[key]))).one()
                assert row.resource == f"{PUBLIC}/mcp"
    r = refresh_req(c, cid, t3["refresh_token"], resource="https://evil.test/mcp")
    assert r.status_code == 400 and r.json()["error"] == "invalid_target"


def test_mcp_rejects_token_with_other_audience_but_pat_unaffected(c: TestClient, env: Env) -> None:
    _, t = full_grant(c, env)
    assert _mcp_list(c, t["access_token"]).status_code == 200
    for foreign in ("https://other-server.test/mcp", PUBLIC, None):
        with admin_session(engine=env.engine) as s:
            s.scalars(select(ApiToken).where(ApiToken.hash == sha256_hex(t["access_token"]))
                      ).one().resource = foreign
        assert _mcp_list(c, t["access_token"]).status_code == 401, foreign
    pat, _ = create_token(env.wa, ALICE, "pat-audience", engine=env.engine)
    assert _mcp_list(c, pat).status_code == 200


def _audit(env: Env, ws_id: uuid.UUID) -> list[Any]:
    from satchel.store.models import AuditEvent

    with admin_session(engine=env.engine) as s:
        rows = list(s.scalars(select(AuditEvent).where(AuditEvent.workspace_id == ws_id)
                              .order_by(AuditEvent.id)))
        s.expunge_all()
        return rows


def test_login_consent_and_provisioning_are_audited(c: TestClient, env: Env) -> None:
    cid, _tok = full_grant(c, env)
    ev = [e for e in _audit(env, env.wa_id) if e.target.get("client_id") == cid]
    assert [e.action for e in ev] == ["auth.login", "oauth.consent.approve"]
    assert ev[1].target["scope"] == "mcp" and ev[1].actor_user_id is not None
    # A denial is recorded too.
    cid2 = register(c)
    authorize(c, env, cid2, workspace=env.wa, state="s", approve=False)
    assert [e.action for e in _audit(env, env.wa_id) if e.target.get("client_id") == cid2] == [
        "auth.login", "oauth.consent.deny"]
    # Provisioning an unknown IdP user in a workspace that allows it.
    env.idp.identity = Identity("sub-audit", f"audit-{SFX}@corp.test", True, "Audit")
    assert _login_only(c, env, env.wp).status_code == 302
    acts = [(e.action, e.target.get("email")) for e in _audit(env, _wp_id(env))]
    assert ("user.provision", f"audit-{SFX}@corp.test") in acts


def _wp_id(env: Env) -> uuid.UUID:
    with admin_session(engine=env.engine) as s:
        return s.scalars(select(Workspace.id).where(Workspace.slug == env.wp)).one()
