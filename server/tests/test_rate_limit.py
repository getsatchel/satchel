"""Rate limits on sign-in, OAuth and /mcp (satchel.api.rate_limit)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from satchel.api.app import create_app
from satchel.api.rate_limit import TokenBuckets, group_of
from satchel.llm.embeddings import HashEmbedder
from satchel.settings import Settings


def test_groups() -> None:
    assert group_of("/mcp") == group_of("/mcp/") == "mcp"
    for p in ("/oauth/token", "/oauth/register", "/oauth/authorize", "/auth/login/start",
              "/auth/callback"):
        assert group_of(p) == "auth", p
    for p in ("/v1/me", "/auth/login", "/healthz", "/.well-known/oauth-authorization-server",
              "/mcpx", "/library"):
        assert group_of(p) is None, p


def test_token_bucket_refills_evenly() -> None:
    now = [0.0]
    b = TokenBuckets(60, clock=lambda: now[0])
    assert all(b.take("a") == 0 for _ in range(60))  # a burst up to the limit
    assert b.take("a") == 1.0  # then one a second
    assert b.take("b") == 0  # keys are separate
    now[0] += 1.0
    assert b.take("a") == 0 and b.take("a") > 0


def test_middleware_limits_auth_by_ip_and_mcp_by_token(app_engine: Engine) -> None:
    st = Settings(dev_auth=True, rate_limit_auth_per_min=3, rate_limit_mcp_per_min=2)
    c = TestClient(create_app(app_engine, HashEmbedder(), settings=st))
    codes = [c.post("/oauth/register", json={}).status_code for _ in range(4)]
    assert codes[:3] != [429] * 3 and codes[3] == 429, codes
    r = c.post("/oauth/register", json={})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert "Too many requests" in r.json()["detail"]
    assert r.headers["x-content-type-options"] == "nosniff"  # 429s get the security headers too
    assert c.get("/healthz").status_code == 200  # other routes are not limited

    def mcp(token: str) -> int:
        return c.post("/mcp", json={}, headers={"authorization": f"Bearer {token}"}).status_code

    assert [mcp("a"), mcp("a"), mcp("a")][2] == 429
    assert mcp("b") != 429  # another token has its own budget
    assert c.options("/mcp").status_code != 429  # CORS preflight is never limited


def test_zero_turns_a_group_off(app_engine: Engine) -> None:
    st = Settings(dev_auth=True, rate_limit_auth_per_min=0)
    c = TestClient(create_app(app_engine, HashEmbedder(), settings=st))
    assert 429 not in {c.post("/oauth/register", json={}).status_code for _ in range(40)}
