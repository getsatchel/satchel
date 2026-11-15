"""Settings → Members (``satchel.api.members``) and what deactivation refuses."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select

from satchel.api import members as members_api
from satchel.mcp.auth import create_token, verify_token
from satchel.oauth import sessions
from satchel.oauth.directory import AccountResult, cognito_pool, directory_for
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import AppUser, Workspace
from tests.test_settings_admin import Env, make_env, st


def members(env: Env, role: str = "admin") -> dict[str, dict[str, Any]]:
    r = env.client.get("/v1/members", headers=env.h(role))
    assert r.status_code == 200, r.text
    return {m["email"]: m for m in r.json()["members"]}


def patch(env: Env, email: str, body: dict[str, Any], role: str = "admin") -> Any:
    mid = members(env, role)[email]["id"]
    return env.client.patch(f"/v1/members/{mid}", json=body, headers=env.h(role))


def test_list_and_invite(app_engine: Engine, tmp_path: Path) -> None:
    env = make_env(app_engine, tmp_path)
    admin = f"admin@{env.slug}.test"
    body = env.client.get("/v1/members", headers=env.h()).json()
    assert body["sign_in"] == {"provision": "reject", "allowed_email_domains": []}
    assert body["oidc_configured"] is False
    m = members(env)
    assert set(m) == {admin, f"curator@{env.slug}.test", f"explorer@{env.slug}.test"}
    assert m[admin]["is_you"] and m[admin]["role"] == "admin"
    # pre-created, never used: shown as an invite
    assert {x["status"] for x in m.values()} == {"invited"}

    assert env.client.get("/v1/members", headers=env.h("curator")).status_code == 403
    r = env.post("/v1/members", {"emails": ["  New@Example.com", "new@example.com", admin],
                                 "role": "curator"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert [(x["email"], x["role"], x["status"]) for x in out["invited"]] == [
        ("new@example.com", "curator", "invited")]
    assert out["existing"] == [admin]
    assert env.post("/v1/members", {"emails": ["not-an-email"]}).status_code == 422
    assert [a.target["email"] for a in env.actions("user.invite")] == ["new@example.com"]

    # an unused invite can be cancelled; a member who has signed in cannot
    nid = members(env)["new@example.com"]["id"]
    assert env.client.delete(f"/v1/members/{nid}", headers=env.h()).status_code == 204
    assert "new@example.com" not in members(env)
    create_token(env.slug, f"explorer@{env.slug}.test", "t", engine=app_engine)
    raw, _ = create_token(env.slug, f"curator@{env.slug}.test", "laptop", engine=app_engine)
    assert verify_token(raw, engine=app_engine) is not None  # marks the token used
    cur = members(env)[f"curator@{env.slug}.test"]
    assert cur["status"] == "active" and cur["last_active_at"] is not None
    assert env.client.delete(f"/v1/members/{cur['id']}", headers=env.h()).status_code == 409


def test_roles_guards_and_deactivation(app_engine: Engine, tmp_path: Path) -> None:
    env = make_env(app_engine, tmp_path)
    admin, cur = f"admin@{env.slug}.test", f"curator@{env.slug}.test"
    # you can't change yourself, and the last admin stays an admin
    assert patch(env, admin, {"role": "curator"}).status_code == 409
    assert patch(env, admin, {"active": False}).status_code == 409
    r = patch(env, cur, {"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert [(a.target["from"], a.target["to"]) for a in env.actions("user.role_change")] == [
        ("curator", "admin")]
    # the curator, now an admin, demotes the original admin; then cannot demote themselves
    assert patch(env, admin, {"role": "analyst"}, role="curator").status_code == 200
    assert patch(env, cur, {"role": "analyst"}, role="curator").status_code == 409

    raw, _ = create_token(env.slug, f"explorer@{env.slug}.test", "t", engine=app_engine)
    with admin_session(engine=app_engine) as s:
        uid = s.scalars(select(AppUser.id).where(
            AppUser.email == f"explorer@{env.slug}.test")).one()
    web, _csrf = sessions.create_session(app_engine, env.ws_id, uid, st())
    assert sessions.load_session(web, engine=app_engine) is not None

    r = patch(env, f"explorer@{env.slug}.test", {"active": False}, role="curator")
    assert r.status_code == 200 and r.json()["status"] == "deactivated"
    assert verify_token(raw, engine=app_engine) is None
    assert sessions.load_session(web, engine=app_engine) is None
    assert env.client.get("/v1/me", headers=env.h("explorer")).status_code == 401
    assert [a.action for a in env.actions("user.disable")] == ["user.disable"]

    r = patch(env, f"explorer@{env.slug}.test", {"active": True}, role="curator")
    assert r.json()["status"] == "active"
    assert verify_token(raw, engine=app_engine) is not None


def test_sign_in_policy(app_engine: Engine, tmp_path: Path) -> None:
    env = make_env(app_engine, tmp_path)
    r = env.client.put("/v1/members/sign-in", headers=env.h(), json={
        "provision": "explorer", "allowed_email_domains": ["@Example.com", "example.com"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"provision": "explorer", "allowed_email_domains": ["example.com"]}
    with admin_session(engine=app_engine) as s:
        ws = s.get(Workspace, env.ws_id)
        assert ws is not None
        assert ws.settings["auth"] == {"provision": "explorer",
                                       "allowed_email_domains": ["example.com"]}
    bad = {"provision": "explorer", "allowed_email_domains": ["not a domain"]}
    assert env.client.put("/v1/members/sign-in", headers=env.h(), json=bad).status_code == 422
    assert env.client.put("/v1/members/sign-in", headers=env.h("curator"),
                          json=bad).status_code == 403


def test_cognito_pool_from_issuer() -> None:
    iss = "https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_JR8x2sMV8"
    assert cognito_pool(iss) == ("ap-southeast-2", "ap-southeast-2_JR8x2sMV8")
    assert cognito_pool("https://accounts.google.com") is None
    assert cognito_pool(None) is None
    on = Settings(oidc_issuer=iss, oidc_create_users=True)
    assert directory_for(on) is not None
    assert directory_for(Settings(oidc_issuer=iss)) is None  # off unless asked for
    assert directory_for(Settings(oidc_issuer="https://x.okta.com", oidc_create_users=True)) is None


class FakeDirectory:
    name = "Fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def create_user(self, email: str) -> AccountResult:
        self.calls.append(email)
        if email.startswith("broken"):
            return AccountResult(email, "failed", "AccessDenied")
        return AccountResult(email, "exists" if email.startswith("old") else "created")


def test_invite_creates_provider_accounts(app_engine: Engine, tmp_path: Path,
                                          monkeypatch: Any) -> None:
    env = make_env(app_engine, tmp_path)
    fake = FakeDirectory()
    monkeypatch.setattr(members_api, "directory_for", lambda _s: fake)
    assert env.client.get("/v1/members", headers=env.h()).json()["creates_accounts"] is True
    admin = f"admin@{env.slug}.test"
    r = env.post("/v1/members", {"emails": ["a@example.com", "old@example.com",
                                            "broken@example.com"]})
    assert r.status_code == 201, r.text
    acc = {a["email"]: a for a in r.json()["accounts"]}
    assert acc["a@example.com"]["status"] == "created"
    assert acc["old@example.com"]["status"] == "exists"
    assert acc["broken@example.com"] == {"email": "broken@example.com", "status": "failed",
                                         "detail": "AccessDenied"}
    # a failure never undoes the invite, and re-inviting retries it; active members are skipped
    assert "broken@example.com" in members(env)
    create_token(env.slug, admin, "t", engine=app_engine)
    raw, _ = create_token(env.slug, f"curator@{env.slug}.test", "t", engine=app_engine)
    verify_token(raw, engine=app_engine)
    fake.calls.clear()
    r = env.post("/v1/members", {"emails": ["broken@example.com", f"curator@{env.slug}.test"]})
    assert r.json()["invited"] == [] and fake.calls == ["broken@example.com"]
    assert len(env.actions("user.idp_account")) == 4
