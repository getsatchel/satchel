#!/usr/bin/env python3
"""End-to-end OAuth 2.1 smoke test against a running satchel server and the compose Dex IdP.

    docker compose -f deploy/compose/docker-compose.yml --profile auth up -d
    # migrate + import + run the server with OIDC pointed at Dex (see docs/integrations/claude.md)
    uv run --project server python scripts/oauth_smoke.py [--base http://localhost:8080]

Flow: /mcp 401 -> protected-resource + AS metadata -> dynamic registration -> /oauth/authorize
-> log in to Dex by form post -> consent -> token (PKCE) -> /mcp tools/list -> refresh
(rotation) -> reuse detection -> revoke. Exits non-zero on the first failure.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import re
import secrets
import sys
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

REDIRECT = "http://127.0.0.1:53682/callback"  # never actually listened on; we read the redirect
EMAIL, PASSWORD = "analyst@satchel.test", "password"


def step(msg: str) -> None:
    print(f"\n== {msg}")


def check(cond: bool, msg: str) -> None:
    print(f"   {'ok ' if cond else 'FAIL'} {msg}")
    if not cond:
        sys.exit(1)


def follow(c: httpx.Client, r: httpx.Response, stop_prefix: str) -> httpx.Response:
    """Follow redirects by hand until the next hop is the client's redirect_uri."""
    for _ in range(20):
        if r.status_code not in (301, 302, 303, 307, 308):
            return r
        loc = urljoin(str(r.url), r.headers["location"])
        if loc.startswith(stop_prefix):
            return r
        r = c.get(loc)
    raise SystemExit("too many redirects")


def field(page: str, name: str) -> str:
    m = re.search(rf"name={name} value='([^']+)'", page)
    check(m is not None, f"consent form has {name}")
    assert m
    return html.unescape(m.group(1))


def mcp_list(c: httpx.Client, base: str, access: str) -> httpx.Response:
    return c.post(f"{base}/mcp", headers={
        "Authorization": f"Bearer {access}", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})


def grant(c: httpx.Client, base: str, md: dict[str, object], prm: dict[str, object],
          client_id: str, workspace: str, label: str) -> dict[str, str]:
    """authorize -> Dex login -> consent -> code -> token. Returns the token response."""
    step(f"{label} authorize -> Dex login -> consent -> code -> token (PKCE)")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    state = secrets.token_urlsafe(8)
    r = c.get(str(md["authorization_endpoint"]), params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge.decode(), "code_challenge_method": "S256",
        "state": state, "scope": "mcp", "resource": str(prm["resource"]), "workspace": workspace})
    check(r.status_code == 302, f"redirected to IdP: {r.headers.get('location', '')[:60]}...")
    r = follow(c, r, REDIRECT)  # lands on Dex's login form
    check(r.status_code == 200 and "<form" in r.text, f"Dex login form at {str(r.url)[:60]}")
    action = re.search(r'<form[^>]+action="([^"]+)"', r.text)
    assert action
    login_url = urljoin(str(r.url), html.unescape(action.group(1)))
    r = c.post(login_url, data={"login": EMAIL, "password": PASSWORD})
    r = follow(c, r, REDIRECT)  # Dex -> /auth/callback -> /oauth/consent
    check(r.status_code == 200 and "Authorize" in r.text, f"consent page at {str(r.url)[:60]}")
    check("oauth_smoke.py" in r.text, "consent page names the client")
    r = c.post(f"{base}/oauth/consent", data={
        "flow": field(r.text, "flow"), "csrf": field(r.text, "csrf"), "decision": "approve"})
    check(r.status_code == 302 and r.headers["location"].startswith(REDIRECT), "redirect to client")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    check(q["state"] == [state], "state round-trips")
    check(q["iss"] == [str(md["issuer"])], "iss (RFC 9207) present")
    code = q["code"][0]

    bad = c.post(str(md["token_endpoint"]), data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": REDIRECT, "code_verifier": secrets.token_urlsafe(48)})
    check(bad.status_code == 400 and bad.json()["error"] == "invalid_grant",
          "wrong verifier refused")
    r = c.post(str(md["token_endpoint"]), data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": REDIRECT, "code_verifier": verifier, "resource": str(prm["resource"])})
    check(r.status_code == 200, f"tokens issued (expires_in={r.json().get('expires_in')})")
    return dict(r.json())



def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--workspace", default="jaffle")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    c = httpx.Client(timeout=20.0, follow_redirects=False)

    step("1. unauthenticated /mcp -> 401 with resource_metadata")
    r = c.post(f"{base}/mcp", json={})
    check(r.status_code == 401, f"401 (got {r.status_code})")
    m = re.search(r'resource_metadata="([^"]+)"', r.headers.get("www-authenticate", ""))
    check(m is not None, f"WWW-Authenticate: {r.headers.get('www-authenticate')}")
    assert m

    step("2. discovery")
    prm = c.get(m.group(1)).json()
    check(prm["resource"].endswith("/mcp") and prm["authorization_servers"], f"PRM {prm}")
    iss = prm["authorization_servers"][0].rstrip("/")
    md = c.get(f"{iss}/.well-known/oauth-authorization-server").json()
    check(md["issuer"] == iss, "AS issuer matches")
    check(md["code_challenge_methods_supported"] == ["S256"], "S256 only")
    check("registration_endpoint" in md, "DCR advertised")

    step("3. dynamic client registration (public client)")
    reg = c.post(md["registration_endpoint"], json={
        "client_name": "oauth_smoke.py", "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    check(reg.status_code == 201, f"registered {reg.json().get('client_id')}")
    client_id = reg.json()["client_id"]

    t1 = grant(c, base, md, prm, client_id, a.workspace, "4./5.")

    step("6. call /mcp tools/list with the access token")
    r = mcp_list(c, base, t1["access_token"])
    check(r.status_code == 200 and "start_analysis" in r.text, "tools/list returned satchel tools")

    step("7. refresh (rotation) and reuse detection")
    r = c.post(md["token_endpoint"], data={
        "grant_type": "refresh_token", "refresh_token": t1["refresh_token"],
        "client_id": client_id})
    check(r.status_code == 200, "refreshed")
    t2 = r.json()
    check(t2["refresh_token"] != t1["refresh_token"], "refresh token rotated")
    check(mcp_list(c, base, t2["access_token"]).status_code == 200, "new access token works")
    r = c.post(md["token_endpoint"], data={
        "grant_type": "refresh_token", "refresh_token": t1["refresh_token"],
        "client_id": client_id})
    check(r.status_code == 400, "reusing the rotated refresh token is refused")
    check(mcp_list(c, base, t2["access_token"]).status_code == 401,
          "...and revoked the whole token family")

    step("8. revocation (RFC 7009) on a fresh grant")
    t3 = grant(c, base, md, prm, client_id, a.workspace, "8.")
    check(mcp_list(c, base, t3["access_token"]).status_code == 200, "fresh access token works")
    r = c.post(md["revocation_endpoint"], data={"token": t3["access_token"],
                                                "client_id": client_id})
    check(r.status_code == 200, "revoke endpoint answers 200")
    check(mcp_list(c, base, t3["access_token"]).status_code == 401, "revoked access token -> 401")
    r = c.post(md["token_endpoint"], data={
        "grant_type": "refresh_token", "refresh_token": t3["refresh_token"],
        "client_id": client_id})
    check(r.status_code == 400, "refresh token of a revoked grant is rejected")
    step("9. human login: /auth/login -> Dex -> session cookie -> REST")
    r = c.get(f"{base}/auth/login", params={"workspace": a.workspace, "next": "/v1/items"})
    check(r.status_code == 302, "redirected to IdP")
    r = follow(c, r, f"{base}/v1/items")
    action = re.search(r'<form[^>]+action="([^"]+)"', r.text)
    check(action is not None, "Dex login form")
    assert action
    r = c.post(urljoin(str(r.url), html.unescape(action.group(1))),
               data={"login": EMAIL, "password": PASSWORD})
    r = follow(c, r, f"{base}/v1/items")
    check(r.status_code == 302 and r.headers["location"] == "/v1/items", "callback -> next")
    me = c.get(f"{base}/auth/me").json()
    check(me["email"] == EMAIL and me["workspace"] == a.workspace, f"/auth/me {me['email']}")
    items = c.get(f"{base}/v1/items")
    check(items.status_code == 200 and len(items.json()) > 0,
          f"REST with cookie: {len(items.json())} items")
    check(c.post(f"{base}/v1/search", json={"query": "revenue"}).status_code == 403,
          "cookie write without CSRF token refused")
    ok = c.post(f"{base}/v1/search", json={"query": "revenue", "k": 1},
                headers={"X-CSRF-Token": me["csrf_token"]})
    check(ok.status_code == 200, "cookie write with CSRF token accepted")
    print("\nALL OK")


if __name__ == "__main__":
    main()
