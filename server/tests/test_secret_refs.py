"""Secret manager references: parsing, the allow list, caching, and every secret field that
accepts one (warehouse connections, model API keys, source tokens), plus SATCHEL_SECRETS=references.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine

import satchel.llm.connections as lc
from satchel.api.app import create_app
from satchel.core import secret_refs as sr
from satchel.core.crypto import generate_key
from satchel.llm.chat import AnthropicChat
from satchel.llm.embeddings import HashEmbedder
from satchel.secrets import is_sealed
from satchel.settings import Settings
from tests.test_git_sources import repo  # noqa: F401 - fixture
from tests.test_settings_admin import Env, make_env, pg_config

ARN = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:satchel/"
ALLOW = f"aws-sm:{ARN}*,gcp-sm:projects/acme/secrets/satchel-*"


def st(**kw: Any) -> Settings:
    base: dict[str, Any] = {"dev_auth": True, "encryption_key": SecretStr(generate_key()),
                            "secret_ref_allow": ALLOW}
    return Settings(**(base | kw))


class Vault:
    """Stands in for the secret managers: values by reference, and every read recorded."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.reads: list[str] = []

    def __call__(self, r: sr.Ref) -> str:
        plain = f"{r.scheme}:{r.target}"
        self.reads.append(plain)
        if plain not in self.values:
            raise sr.SecretRefError(f"{r.scheme}: ResourceNotFoundException reading {r.target}")
        return self.values[plain]


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> Iterator[Vault]:
    v = Vault()
    monkeypatch.setitem(sr.FETCHERS, "aws-sm", v)
    monkeypatch.setitem(sr.FETCHERS, "gcp-sm", v)
    sr.clear_cache()
    yield v
    sr.clear_cache()


def client(app_engine: Engine, env: Env, settings: Settings) -> TestClient:
    return TestClient(create_app(app_engine, HashEmbedder(), mcp=False, settings=settings))


# --- parsing and policy -----------------------------------------------------------------------


@pytest.mark.parametrize("ref", [
    "aws-sm:satchel/pg", f"aws-sm:{ARN}pg-AbCd#password", "env:PG_PASSWORD",
    "gcp-sm:projects/acme/secrets/satchel-pg", "gcp-sm:projects/acme/secrets/s/versions/3#k",
])
def test_parse_accepts(ref: str) -> None:
    assert str(sr.parse(ref)) == ref


@pytest.mark.parametrize("ref", [
    "vault:x", "aws-sm:", "aws-sm:has space", "gcp-sm:acme/s", "gcp-sm:projects/a/secrets",
    "gcp-sm:projects/a/secrets/s/v/1", "password",
])
def test_parse_refuses(ref: str) -> None:
    with pytest.raises(sr.SecretRefError):
        sr.parse(ref)


def test_api_refs_need_the_allow_list() -> None:
    assert sr.check_api_ref(f"aws-sm:{ARN}pg#password", st()).key == "password"
    assert sr.check_api_ref("gcp-sm:projects/acme/secrets/satchel-pg", st())
    for ref, msg in [
        (f"aws-sm:{ARN.replace('satchel/', 'other/')}pg", "outside"),
        ("aws-sm:satchel/pg", "outside"),
        ("gcp-sm:projects/acme/secrets/billing", "outside"),
        ("env:SATCHEL_ENCRYPTION_KEY", "can't be set here"),
    ]:
        with pytest.raises(sr.SecretRefError, match=msg):
            sr.check_api_ref(ref, st())
    with pytest.raises(sr.SecretRefError, match="off"):
        sr.check_api_ref(f"aws-sm:{ARN}pg", st(secret_ref_allow=""))


def test_resolve_caches_picks_a_key_and_reads_fresh(vault: Vault) -> None:
    ref = f"aws-sm:{ARN}pg"
    vault.values[ref] = json.dumps({"password": "one", "user": "u"})
    assert sr.resolve(ref + "#password", st()) == "one"
    assert sr.resolve(ref + "#user", st()) == "u"
    assert len(vault.reads) == 1  # one read per secret, whatever the key
    vault.values[ref] = json.dumps({"password": "two"})
    assert sr.resolve(ref + "#password", st()) == "one"
    assert sr.resolve(ref + "#password", st(), fresh=True) == "two"
    assert sr.resolve(ref + "#password", st(secret_cache_s=0)) == "two"
    with pytest.raises(sr.SecretRefError, match="no field 'nope'"):
        sr.resolve(ref + "#nope", st())
    vault.values[ref] = "not json"
    with pytest.raises(sr.SecretRefError, match="JSON object"):
        sr.resolve(ref + "#password", st(), fresh=True)
    with pytest.raises(sr.SecretRefError, match="ResourceNotFound"):
        sr.resolve(f"aws-sm:{ARN}missing", st())


def test_aws_fetcher_uses_the_region_in_the_arn(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    seen: dict[str, Any] = {}

    class Client:
        def get_secret_value(self, SecretId: str) -> dict[str, str]:  # noqa: N803 - boto3
            seen["id"] = SecretId
            return {"SecretString": "s3cret"}

    def fake_client(service: str, region_name: str | None = None) -> Client:
        seen["service"], seen["region"] = service, region_name
        return Client()

    monkeypatch.setattr(boto3, "client", fake_client)
    assert sr.FETCHERS["aws-sm"](sr.parse(f"aws-sm:{ARN}pg")) == "s3cret"
    assert seen == {"service": "secretsmanager", "region": "eu-west-1", "id": f"{ARN}pg"}


# --- warehouse connections --------------------------------------------------------------------


def test_postgres_password_from_a_secret_manager(app_engine: Engine, tmp_path: Any,
                                                 pg_url: str, vault: Vault) -> None:
    from tests.conftest import APP_PW

    env = make_env(app_engine, tmp_path)
    c = client(app_engine, env, st())
    ref = f"aws-sm:{ARN}pg"
    vault.values[ref] = json.dumps({"password": APP_PW})
    cfg = pg_config(pg_url) | {"user": "satchel_app"}
    t = c.post("/v1/connections/test", headers=env.h(),
               json={"kind": "postgres", "config": cfg, "secret": ref + "#password"})
    assert t.json()["ok"], t.text
    r = c.post("/v1/connections", headers=env.h(), json={
        "name": "pg", "kind": "postgres", "config": cfg, "secret": ref + "#password"})
    assert r.status_code == 201, r.text
    assert r.json()["secret"] == "reference" and r.json()["secret_ref"] == ref + "#password"
    assert env.conn("pg").secret_ref == ref + "#password"  # the reference, not the value
    assert APP_PW not in json.dumps(env.actions("connection.add")[0].target)
    cid = r.json()["id"]
    reads = len(vault.reads)
    assert c.post("/v1/connections/test", headers=env.h(),
                  json={"connection_id": cid}).json()["ok"]
    assert len(vault.reads) == reads + 1  # Test reads it fresh
    crawl = c.post(f"/v1/connections/{cid}/crawl?wait=true", headers=env.h())
    assert crawl.status_code == 200, crawl.text
    listed = c.get("/v1/connections", headers=env.h()).json()
    assert listed[0]["secret"] == "reference" and listed[0]["secret_ref"] == ref + "#password"
    assert c.get("/v1/connections", headers=env.h("curator")).json()[0]["secret_ref"] is None

    vault.values[ref] = json.dumps({"password": "rotated-wrong"})
    bad = c.post("/v1/connections/test", headers=env.h(), json={"connection_id": cid}).json()
    assert not bad["ok"] and "rotated-wrong" not in json.dumps(bad)


def test_connection_refs_outside_the_allow_list_are_refused(app_engine: Engine,
                                                            tmp_path: Any) -> None:
    env = make_env(app_engine, tmp_path)
    c = client(app_engine, env, st())
    body = {"name": "pg", "kind": "postgres",
            "config": {"host": "h", "database": "d", "user": "u"}}
    for secret, msg in [("aws-sm:billing/root", "outside"), ("env:SATCHEL_ENCRYPTION_KEY",
                                                              None)]:
        r = c.post("/v1/connections", headers=env.h(), json=body | {"secret": secret})
        if msg:
            assert r.status_code == 422 and msg in r.json()["detail"], r.text
        else:  # env: is not a reference over the API: it is stored as a literal password
            assert r.status_code == 201 and r.json()["secret"] == "stored", r.text
            c.delete(f"/v1/connections/{r.json()['id']}", headers=env.h())
    t = c.post("/v1/connections/test", headers=env.h(), json={
        "kind": "postgres", "config": body["config"], "secret": "aws-sm:billing/root"}).json()
    assert not t["ok"] and "outside" in t["error"]


def test_references_mode_stores_no_secret_values(app_engine: Engine, tmp_path: Any,
                                                 vault: Vault) -> None:
    env = make_env(app_engine, tmp_path)
    c = client(app_engine, env, st(secrets="references"))
    assert c.get("/v1/secret-policy", headers=env.h()).json() == {
        "mode": "references", "allow": ALLOW.split(",")}
    assert c.get("/v1/secret-policy", headers=env.h("curator")).status_code == 403
    body = {"name": "pg", "kind": "postgres",
            "config": {"host": "h", "database": "d", "user": "u"}}
    r = c.post("/v1/connections", headers=env.h(), json=body | {"secret": "hunter2"})
    assert r.status_code == 422 and "SATCHEL_SECRETS=references" in r.json()["detail"]
    r = c.post("/v1/connections", headers=env.h(), json=body | {"secret": f"aws-sm:{ARN}pg"})
    assert r.status_code == 201, r.text
    # No secret at all is fine (DuckDB, BigQuery through workload identity).
    r = c.post("/v1/connections", headers=env.h(), json={
        "name": "bq", "kind": "bigquery", "config": {"project": "acme-analytics"}})
    assert r.status_code in (201, 422) and "SATCHEL_SECRETS" not in r.text

    r = c.post("/v1/llm-connections", headers=env.h(), json={
        "name": "claude", "provider": "anthropic", "api_key": "sk-ant-0000000000000000"})
    assert r.status_code == 422 and "SATCHEL_SECRETS=references" in r.json()["detail"]
    r = c.post("/v1/llm-connections", headers=env.h(), json={
        "name": "gw", "provider": "openai_compat", "default_model": "m",
        "base_url": "http://gateway.internal/v1", "extra_headers": {"X-Key": "v"}})
    assert r.status_code == 422 and "header values" in r.json()["detail"]
    r = c.post("/v1/sources", headers=env.h(), json={"kind": "sqlmesh", "name": "g", "config": {
        "git_url": "https://github.com/acme/analytics.git", "project": ".",
        "git_token": "tok-123456"}})
    assert r.status_code == 422 and "SATCHEL_SECRETS=references" in r.json()["detail"], r.text


# --- model API keys ---------------------------------------------------------------------------


def test_model_api_key_from_a_secret_manager(app_engine: Engine, tmp_path: Any, vault: Vault,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(200, json={"content": [
            {"type": "tool_use", "id": "t", "name": "respond", "input": {"ok": True}}]})

    def factory(*a: Any, **kw: Any) -> AnthropicChat:
        return AnthropicChat(*a, client=httpx.Client(transport=httpx.MockTransport(handler)),
                             **kw)

    monkeypatch.setattr(lc, "AnthropicChat", factory)
    env = make_env(app_engine, tmp_path)
    settings = st()
    c = client(app_engine, env, settings)
    ref = "gcp-sm:projects/acme/secrets/satchel-anthropic"
    vault.values[ref] = "sk-ant-from-the-vault-000"
    t = c.post("/v1/llm-connections/test", headers=env.h(),
               json={"provider": "anthropic", "api_key": ref}).json()
    assert t["ok"] and sent[-1].headers["x-api-key"] == "sk-ant-from-the-vault-000", t
    r = c.post("/v1/llm-connections", headers=env.h(), json={
        "name": "claude", "provider": "anthropic", "api_key": ref})
    assert r.status_code == 201, r.text
    assert r.json()["has_api_key"] and r.json()["api_key_ref"] == ref
    assert r.json()["api_key_hint"] is None
    chat = lc.chat_for_workspace(env.ws_id, settings, app_engine)
    assert chat is not None
    t = c.post("/v1/llm-connections/test", headers=env.h(),
               json={"connection_id": r.json()["id"]}).json()
    assert t["ok"] and sent[-1].headers["x-api-key"] == "sk-ant-from-the-vault-000"
    # Replacing it with a typed key drops the reference.
    r = c.patch(f"/v1/llm-connections/{r.json()['id']}", headers=env.h(),
                json={"api_key": "sk-ant-typed-0000000000"})
    assert r.json()["api_key_ref"] is None and r.json()["api_key_hint"]


def test_deployment_chat_key_may_be_a_reference(vault: Vault) -> None:
    ref = f"aws-sm:{ARN}anthropic"
    vault.values[ref] = "sk-ant-deploy"
    t = lc.deployment_target(st(chat_provider="anthropic", chat_api_key=ref))
    assert t is not None and t.api_key == "sk-ant-deploy"
    t = lc.deployment_target(st(chat_provider="anthropic", chat_api_key=f"aws-sm:{ARN}nope"))
    assert t is not None and t.api_key is None  # logged, not raised


# --- source tokens ----------------------------------------------------------------------------


def test_git_token_from_a_secret_manager(app_engine: Engine, tmp_path: Any, vault: Vault,
                                         repo: tuple[str, str]) -> None:  # noqa: F811
    url, head = repo
    env = make_env(app_engine, tmp_path)
    c = client(app_engine, env, st())
    ref = f"aws-sm:{ARN}github#token"
    vault.values[f"aws-sm:{ARN}github"] = json.dumps({"token": "ghp_from_the_vault"})
    r = c.post("/v1/sources", headers=env.h(), json={"kind": "sqlmesh", "name": "g", "config": {
        "git_url": url, "git_ref": "main", "project": "analytics/sqlmesh", "git_token": ref}})
    assert r.status_code == 201, r.text
    assert r.json()["config"]["git_token"] == ref
    stored = env.src("g").config["git_token"]
    assert stored == ref and not is_sealed(stored)
    sync = c.post(f"/v1/sources/{r.json()['id']}/sync?wait=true", headers=env.h("curator"))
    assert sync.status_code == 200 and sync.json()["git_commit"] == head, sync.text
    assert f"aws-sm:{ARN}github" in vault.reads
    t = c.post("/v1/sources/git/test", headers=env.h(),
               json={"url": url, "ref": "main", "token": "aws-sm:billing/x"}).json()
    assert not t["ok"] and "outside" in t["error"]
