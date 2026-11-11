"""LLM connections (Settings → Models): encryption at rest, the admin API, resolution for
curation and evals, key rotation."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select

import satchel.llm.connections as lc
from satchel.api.app import create_app
from satchel.core.crypto import DecryptionError, EncryptionUnavailable, Keyring, generate_key
from satchel.evals.providers import resolve_provider
from satchel.evals.suite import ModelSpec
from satchel.llm.chat import AnthropicChat, OpenAICompatChat
from satchel.llm.embeddings import HashEmbedder
from satchel.secrets import rotate_secrets
from satchel.settings import Settings
from satchel.store.db import admin_session, workspace_session
from satchel.store.models import AppUser, AuditEvent, LlmConnection
from satchel.sync.importer import get_or_create_workspace
from tests.helpers import unique_slug

KEY = generate_key()
SECRET = "sk-ant-test-0123456789abcdef"


def st(key: str | None = KEY, **kw: Any) -> Settings:
    return Settings(dev_auth=True, encryption_key=SecretStr(key) if key else None, **kw)


# --- crypto -----------------------------------------------------------------------------------


def test_roundtrip_and_binding() -> None:
    kr = Keyring.from_settings(st())
    tok = kr.encrypt("hello", "row:1")
    assert tok.startswith(f"enc:v1:{kr.current_id}:") and "hello" not in tok
    assert kr.decrypt(tok, "row:1") == "hello"
    assert kr.encrypt("hello", "row:1") != tok  # fresh nonce
    with pytest.raises(DecryptionError, match="tampered or moved"):
        kr.decrypt(tok, "row:2")  # copied to another row
    other = Keyring.from_settings(st(generate_key()))
    with pytest.raises(DecryptionError, match="neither"):
        other.decrypt(tok, "row:1")


def test_previous_key_decrypts_during_rotation() -> None:
    old = generate_key()
    tok = Keyring.from_settings(st(old)).encrypt("v", "a")
    kr = Keyring.from_settings(st(KEY, encryption_key_previous=SecretStr(f" , {old}")))
    assert kr.decrypt(tok, "a") == "v" and not kr.is_current(tok)
    assert kr.is_current(kr.encrypt("v", "a"))


@pytest.mark.parametrize("bad", ["short", "zz" * 32, "ab" * 33])
def test_invalid_key_is_refused(bad: str) -> None:
    with pytest.raises(EncryptionUnavailable, match="64 hex"):
        Keyring.from_settings(st(bad))
    with pytest.raises(EncryptionUnavailable, match="not set"):
        Keyring.from_settings(st(None))


# --- API ------------------------------------------------------------------------------------


@dataclass
class Env:
    engine: Engine
    slug: str
    ws_id: uuid.UUID
    users: dict[str, uuid.UUID]
    make: Callable[[Settings], TestClient]
    client: TestClient

    def h(self, role: str = "admin") -> dict[str, str]:
        return {"X-Satchel-Workspace": self.slug, "X-Satchel-User": str(self.users[role])}

    def row(self, name: str) -> LlmConnection:
        with admin_session(engine=self.engine) as s:
            r = s.scalars(select(LlmConnection).where(
                LlmConnection.workspace_id == self.ws_id, LlmConnection.name == name)).one()
            s.expunge(r)
            return r

    def audits(self) -> list[AuditEvent]:
        with admin_session(engine=self.engine) as s:
            rows = list(s.scalars(select(AuditEvent).where(
                AuditEvent.workspace_id == self.ws_id,
                AuditEvent.action.like("llm_connection.%")).order_by(AuditEvent.id)))
            s.expunge_all()
            return rows


def _workspace(engine: Engine) -> tuple[str, uuid.UUID, dict[str, uuid.UUID]]:
    slug = unique_slug("llm")
    with admin_session(engine=engine) as s:
        ws_id = get_or_create_workspace(s, slug).id
        users = {r: uuid.uuid4() for r in ("admin", "curator", "analyst")}
        for r, uid in users.items():
            s.add(AppUser(id=uid, workspace_id=ws_id, role=r, email=f"{r}@{slug}.test"))
    return slug, ws_id, users


@pytest.fixture
def env(app_engine: Engine) -> Env:
    slug, ws_id, users = _workspace(app_engine)

    def make(settings: Settings) -> TestClient:
        return TestClient(create_app(app_engine, HashEmbedder(), mcp=False, settings=settings))

    return Env(app_engine, slug, ws_id, users, make, make(st()))


def _create(env: Env, **body: Any) -> httpx.Response:
    payload = {"name": "claude", "provider": "anthropic", "api_key": SECRET, **body}
    return env.client.post("/v1/llm-connections", json=payload, headers=env.h())


def test_create_stores_ciphertext_and_never_returns_the_key(env: Env) -> None:
    r = _create(env, extra_headers={"X-Gateway-Key": "gw-secret-value"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["has_api_key"] and body["api_key_hint"] == "…cdef"
    assert body["extra_header_names"] == ["X-Gateway-Key"] and body["is_default"]  # first
    assert SECRET not in r.text and "gw-secret-value" not in r.text

    row = env.row("claude")
    assert row.api_key_enc and SECRET not in row.api_key_enc
    assert row.extra_headers_enc and "gw-secret-value" not in row.extra_headers_enc
    t = lc.target_of(row, Keyring.from_settings(st()))
    assert t.api_key == SECRET and t.extra_headers == {"X-Gateway-Key": "gw-secret-value"}

    listing = env.client.get("/v1/llm-connections", headers=env.h("curator"))
    assert listing.status_code == 200
    data = listing.json()
    assert data["effective"] == "claude" and data["deployment"] is None
    assert data["encryption_configured"] is True
    assert SECRET not in listing.text and "gw-secret-value" not in listing.text

    (ev,) = env.audits()
    assert ev.action == "llm_connection.create" and ev.actor_user_id == env.users["admin"]
    assert ev.target["secrets"] == ["api_key", "extra_headers"]
    assert SECRET not in json.dumps(ev.target)


def test_ciphertext_moved_to_another_row_does_not_decrypt(env: Env) -> None:
    _create(env)
    _create(env, name="other", api_key="sk-ant-other-key-xxxxxxxx")
    a, b = env.row("claude"), env.row("other")
    with admin_session(engine=env.engine) as s:
        s.get(LlmConnection, b.id).api_key_enc = a.api_key_enc  # type: ignore[union-attr]
    with pytest.raises(DecryptionError):
        lc.target_of(env.row("other"), Keyring.from_settings(st()))


def test_roles(env: Env) -> None:
    assert _create(env).status_code == 201
    cid = env.row("claude").id
    for role in ("curator", "analyst"):
        h = env.h(role)
        assert env.client.post("/v1/llm-connections", json={}, headers=h).status_code == 403
        assert env.client.patch(f"/v1/llm-connections/{cid}", json={},
                                headers=h).status_code == 403
        assert env.client.delete(f"/v1/llm-connections/{cid}", headers=h).status_code == 403
        assert env.client.post("/v1/llm-connections/test", json={"connection_id": str(cid)},
                               headers=h).status_code == 403
    assert env.client.get("/v1/llm-connections", headers=env.h("analyst")).status_code == 403


def test_without_encryption_key_secrets_are_refused(env: Env) -> None:
    client = env.make(st(None))
    r = client.post("/v1/llm-connections", headers=env.h(), json={
        "name": "claude", "provider": "anthropic", "api_key": SECRET})
    assert r.status_code == 503 and "SATCHEL_ENCRYPTION_KEY" in r.json()["detail"]
    # A keyless local endpoint needs no key.
    r = client.post("/v1/llm-connections", headers=env.h(), json={
        "name": "ollama", "provider": "openai_compat", "base_url": "http://ollama:11434/v1",
        "default_model": "llama3.3"})
    assert r.status_code == 201, r.text
    assert client.get("/v1/llm-connections",
                      headers=env.h()).json()["encryption_configured"] is False


@pytest.mark.parametrize(("body", "msg"), [
    ({"name": "deployment"}, "reserved"),
    ({"name": "-bad"}, "name"),
    ({"base_url": "http://169.254.169.254/latest"}, "link-local"),
    ({"base_url": "http://metadata.google.internal/"}, "metadata"),
    ({"base_url": "https://user:pw@proxy.example/v1"}, "credentials"),
    ({"base_url": "ftp://x"}, "http"),
    ({"api_key": None}, "needs an API key"),
    ({"provider": "openai_compat"}, "default model"),
    ({"extra_headers": {"Host": "x"}}, "cannot be overridden"),
    ({"extra_headers": {"X-A": "a\r\nb"}}, "invalid value"),
])
def test_validation(env: Env, body: dict[str, Any], msg: str) -> None:
    r = _create(env, **body)
    assert r.status_code == 422 and msg in r.json()["detail"], r.text


def test_duplicate_name(env: Env) -> None:
    assert _create(env).status_code == 201
    r = _create(env)
    assert r.status_code == 422 and "already exists" in r.json()["detail"]


def test_patch_default_and_delete(env: Env) -> None:
    _create(env)
    r = _create(env, name="gpt", provider="openai_compat", default_model="gpt-5",
                api_key="sk-openai-0000000000wxyz", is_default=True)
    assert r.json()["is_default"]
    assert not env.row("claude").is_default

    cid = env.row("claude").id
    old_ct = env.row("claude").api_key_enc
    # Leaving api_key out keeps it.
    r = env.client.patch(f"/v1/llm-connections/{cid}", headers=env.h(),
                         json={"default_model": "claude-haiku-5-5", "name": "claude-fast"})
    assert r.status_code == 200, r.text
    assert r.json()["has_api_key"] and env.row("claude-fast").api_key_enc == old_ct
    # Replacing it re-encrypts; headers are replaced as a whole.
    r = env.client.patch(f"/v1/llm-connections/{cid}", headers=env.h(), json={
        "api_key": "sk-ant-new-key-000000009999", "extra_headers": {"X-Team": "data"}})
    assert r.json()["api_key_hint"] == "…9999" and r.json()["extra_header_names"] == ["X-Team"]
    r = env.client.patch(f"/v1/llm-connections/{cid}", headers=env.h(),
                         json={"extra_headers": {}})
    assert r.json()["extra_header_names"] == [] and env.row("claude-fast").extra_headers_enc is None
    # Clearing the key of an Anthropic connection leaves it unusable: refused.
    r = env.client.patch(f"/v1/llm-connections/{cid}", headers=env.h(), json={"api_key": None})
    assert r.status_code == 422
    r = env.client.patch(f"/v1/llm-connections/{cid}", headers=env.h(), json={"is_default": True})
    assert r.json()["is_default"] and not env.row("gpt").is_default

    assert env.client.delete(f"/v1/llm-connections/{cid}", headers=env.h()).status_code == 204
    data = env.client.get("/v1/llm-connections", headers=env.h()).json()
    assert [c["name"] for c in data["connections"]] == ["gpt"] and data["effective"] is None
    actions = [e.action for e in env.audits()]
    assert actions == ["llm_connection.create"] * 2 + ["llm_connection.update"] * 4 + [
        "llm_connection.delete"]
    upd = [e.target for e in env.audits() if e.action == "llm_connection.update"]
    assert upd[0]["renamed_to"] == "claude-fast" and "api_key" in upd[1]["fields"]
    assert "sk-ant-new-key" not in json.dumps(upd)


def test_workspaces_are_isolated(env: Env) -> None:
    _create(env)
    slug2, ws2, users2 = _workspace(env.engine)
    h2 = {"X-Satchel-Workspace": slug2, "X-Satchel-User": str(users2["admin"])}
    assert env.client.get("/v1/llm-connections", headers=h2).json()["connections"] == []
    cid = env.row("claude").id
    assert env.client.patch(f"/v1/llm-connections/{cid}", json={"default_model": "x"},
                            headers=h2).status_code == 404
    with workspace_session(ws2, engine=env.engine) as s:
        assert s.scalars(select(LlmConnection)).all() == []


def test_deployment_provider_wins(env: Env) -> None:
    _create(env)
    client = env.make(st(chat_provider="openai_compat", chat_model="m",
                         chat_base_url="http://vllm:8000/v1"))
    data = client.get("/v1/llm-connections", headers=env.h()).json()
    assert data["effective"] == "deployment"
    assert data["deployment"] == {"provider": "openai_compat", "model": "m",
                                  "base_url": "http://vllm:8000/v1", "has_api_key": False}


# --- test endpoint ----------------------------------------------------------------------------


class Recorder:
    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": f"bad key {SECRET}"})
        if req.url.path.endswith("/v1/messages"):
            return httpx.Response(200, json={"content": [
                {"type": "tool_use", "id": "t", "name": "respond", "input": {"ok": True}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    for name, cls in (("AnthropicChat", AnthropicChat), ("OpenAICompatChat", OpenAICompatChat)):
        def factory(*a: Any, _cls: Any = cls, **kw: Any) -> Any:
            return _cls(*a, client=httpx.Client(transport=httpx.MockTransport(rec)), **kw)
        monkeypatch.setattr(lc, name, factory)
    return rec


def test_test_saved_connection_uses_stored_secrets(env: Env, recorder: Recorder) -> None:
    _create(env, extra_headers={"X-Gateway-Key": "gw"}, default_model="claude-haiku-5-5")
    cid = str(env.row("claude").id)
    r = env.client.post("/v1/llm-connections/test", headers=env.h(),
                        json={"connection_id": cid})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert r.json()["model_id"] == "anthropic:claude-haiku-5-5"
    req = recorder.requests[-1]
    assert req.headers["x-api-key"] == SECRET and req.headers["x-gateway-key"] == "gw"
    # Override the model and the key without saving.
    r = env.client.post("/v1/llm-connections/test", headers=env.h(), json={
        "connection_id": cid, "model": "claude-opus-5-5", "api_key": "sk-ant-typed-in-form-1"})
    assert r.json()["model_id"] == "anthropic:claude-opus-5-5"
    assert recorder.requests[-1].headers["x-api-key"] == "sk-ant-typed-in-form-1"


def test_test_unsaved_and_errors_are_scrubbed(env: Env, recorder: Recorder) -> None:
    r = env.client.post("/v1/llm-connections/test", headers=env.h(), json={
        "provider": "openai_compat", "base_url": "http://gateway.internal/v1",
        "default_model": "gpt-5", "api_key": SECRET})
    assert r.json()["ok"] and recorder.requests[-1].headers["authorization"] == f"Bearer {SECRET}"
    recorder.status = 401
    r = env.client.post("/v1/llm-connections/test", headers=env.h(), json={
        "provider": "anthropic", "api_key": SECRET})
    body = r.json()
    assert not body["ok"] and "HTTP 401" in body["error"] and SECRET not in body["error"]
    assert len([q for q in recorder.requests if q.url.path.endswith("/v1/messages")]) == 1
    r = env.client.post("/v1/llm-connections/test", headers=env.h(), json={
        "provider": "anthropic", "api_key": SECRET, "base_url": "http://169.254.169.254"})
    assert r.status_code == 422


# --- resolution ---------------------------------------------------------------------------------


def test_resolution_for_curation_and_evals(env: Env, recorder: Recorder) -> None:
    settings = st()
    assert lc.chat_for_workspace(env.ws_id, settings, env.engine) is None
    _create(env, default_model="claude-haiku-5-5")
    _create(env, name="gpt", provider="openai_compat", default_model="gpt-5",
            api_key="sk-openai-0000000000wxyz", is_default=False)
    chat = lc.chat_for_workspace(env.ws_id, settings, env.engine)
    assert chat is not None and chat.model_id == "anthropic:claude-haiku-5-5"

    def ev(spec: ModelSpec | None, override: str | None = None,
           s: Settings = settings) -> str | None:
        p = resolve_provider(spec, override, s, workspace_id=env.ws_id, engine=env.engine)
        return p.model_id if p else None

    assert ev(None) == "anthropic:claude-haiku-5-5"
    assert ev(ModelSpec(provider="openai_compat", name="gpt-5-mini")) == "openai_compat:gpt-5-mini"
    assert ev(None, "openai_compat:gpt-x") == "openai_compat:gpt-x"
    assert ev(None, "claude-opus-5-5") == "anthropic:claude-opus-5-5"
    dep = st(chat_provider="anthropic", chat_api_key="sk-ant-deployment-000000")
    assert ev(None, s=dep) == "anthropic:claude-sonnet-5-5"  # deployment wins
    assert ev(ModelSpec(provider="openai_compat", name="g"), s=dep) == "openai_compat:g"

    # An undecryptable secret degrades to "no LLM", it does not crash curation.
    wrong = st(generate_key())
    assert lc.chat_for_workspace(env.ws_id, wrong, env.engine) is None


def test_rotate(env: Env, app_engine: Engine) -> None:
    old = KEY
    _create(env, extra_headers={"X-A": "a"})
    new = generate_key()
    rotated = st(new, encryption_key_previous=SecretStr(old))
    ws = env.ws_id
    rep = rotate_secrets(rotated, app_engine, dry_run=True, workspace_id=ws)
    assert (rep.rotated, rep.current, rep.failed) == (2, 0, [])
    assert rotate_secrets(rotated, app_engine, workspace_id=ws).rotated == 2
    row = env.row("claude")
    kr = Keyring.from_settings(st(new))
    assert row.api_key_enc and kr.is_current(row.api_key_enc)
    assert lc.target_of(row, kr).api_key == SECRET
    again = rotate_secrets(rotated, app_engine, workspace_id=ws)
    assert (again.rotated, again.current) == (0, 2)
