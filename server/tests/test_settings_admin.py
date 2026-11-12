"""Connections and sources managed from the UI: forms, sealed secrets, REST-only restrictions,
object-store inputs, removal semantics, rotation."""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, func, select, update
from sqlalchemy.engine import make_url

from satchel.api.app import create_app
from satchel.core.crypto import Keyring, generate_key
from satchel.data.objstore import register_bucket
from satchel.llm.embeddings import HashEmbedder
from satchel.secrets import connection_aad, rotate_secrets, source_aad
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import (
    AppUser,
    Asset,
    AuditEvent,
    Connection,
    ContextItem,
    ContextSource,
)
from satchel.sync.importer import get_or_create_workspace
from tests.helpers import MemoryBucket, build_jaffle_db, unique_slug

KEY = generate_key()
DBT = Path(__file__).parent / "fixtures" / "dbt"
PW = "pg-password-0123456789"


def st(key: str | None = KEY) -> Settings:
    return Settings(dev_auth=True, encryption_key=SecretStr(key) if key else None)


@dataclass
class Env:
    engine: Engine
    slug: str
    ws_id: uuid.UUID
    users: dict[str, uuid.UUID]
    client: TestClient
    tmp: Path

    def h(self, role: str = "admin") -> dict[str, str]:
        return {"X-Satchel-Workspace": self.slug, "X-Satchel-User": str(self.users[role])}

    def post(self, path: str, body: Any, role: str = "admin") -> Any:
        return self.client.post(path, json=body, headers=self.h(role))

    def conn(self, name: str) -> Connection:
        with admin_session(engine=self.engine) as s:
            c = s.scalars(select(Connection).where(Connection.workspace_id == self.ws_id,
                                                   Connection.name == name)).one()
            s.expunge(c)
            return c

    def src(self, name: str) -> ContextSource:
        with admin_session(engine=self.engine) as s:
            x = s.scalars(select(ContextSource).where(ContextSource.workspace_id == self.ws_id,
                                                      ContextSource.name == name)).one()
            s.expunge(x)
            return x

    def drain(self) -> int:
        """Run the queued jobs now (retries of this workspace's included), as a web server's
        job threads would."""
        from satchel.jobs.runner import WEB_KINDS, JobRunner, Runtime
        from satchel.store.models import Job

        with admin_session(engine=self.engine) as s:
            s.execute(update(Job).where(Job.workspace_id == self.ws_id, Job.status == "queued")
                      .values(run_after=func.now()))
        rt = Runtime(self.engine, st(), HashEmbedder())
        return JobRunner(rt, kinds=WEB_KINDS, threads=0).drain()

    def actions(self, prefix: str) -> list[AuditEvent]:
        with admin_session(engine=self.engine) as s:
            rows = list(s.scalars(select(AuditEvent).where(
                AuditEvent.workspace_id == self.ws_id, AuditEvent.action.like(f"{prefix}%"))
                .order_by(AuditEvent.id)))
            s.expunge_all()
            return rows


def make_env(app_engine: Engine, tmp_path: Path) -> Env:
    tmp_path.mkdir(parents=True, exist_ok=True)
    slug = unique_slug("adm")
    users = {r: uuid.uuid4() for r in ("admin", "curator", "explorer")}
    with admin_session(engine=app_engine) as s:
        ws_id = get_or_create_workspace(s, slug).id
        for r, uid in users.items():
            s.add(AppUser(id=uid, workspace_id=ws_id, role=r, email=f"{r}@{slug}.test"))
    client = TestClient(create_app(app_engine, HashEmbedder(), mcp=False, settings=st()))
    return Env(app_engine, slug, ws_id, users, client, tmp_path)


@pytest.fixture
def env(app_engine: Engine, tmp_path: Path) -> Env:
    return make_env(app_engine, tmp_path)


@pytest.fixture
def bucket() -> Iterator[MemoryBucket]:
    b = MemoryBucket()
    register_bucket("s3", "artifacts", b)
    yield b
    register_bucket("s3", "artifacts", None)


def duckdb_conn(env: Env, name: str = "jaffle") -> Any:
    db = build_jaffle_db(env.tmp / f"{name}.duckdb")
    return env.post("/v1/connections", {"name": name, "kind": "duckdb",
                                        "config": {"path": str(db)}})


def pg_config(pg_url: str) -> dict[str, Any]:
    u = make_url(pg_url)
    return {"host": u.host, "port": u.port, "database": u.database, "user": u.username}


# --- connections --------------------------------------------------------------------------------


def test_kinds(env: Env) -> None:
    kinds = {k["kind"]: k for k in env.client.get("/v1/connection-kinds",
                                                  headers=env.h("curator")).json()}
    assert kinds["postgres"]["available"] and kinds["postgres"]["secret_label"] == "Password"
    assert [f["name"] for f in kinds["postgres"]["fields"]] == [
        "host", "port", "database", "user", "role"]
    assert kinds["duckdb"]["secret_label"] is None and not kinds["postgres"]["secret_multiline"]
    # The warehouse extras are installed in the dev environment (tests/test_connectors_*.py).
    assert kinds["snowflake"]["available"] and kinds["snowflake"]["secret_multiline"]
    assert kinds["snowflake"]["passphrase_label"] and kinds["bigquery"]["available"]


def test_create_test_crawl_duckdb(env: Env) -> None:
    r = duckdb_conn(env)
    assert r.status_code == 201, r.text
    assert r.json()["default_for_ai"] and r.json()["secret"] is None  # first one
    cid = r.json()["id"]
    t = env.post("/v1/connections/test", {"connection_id": cid})
    assert t.json()["ok"], t.text
    c = env.post(f"/v1/connections/{cid}/crawl?wait=true", {}, role="curator")
    assert c.status_code == 200 and c.json()["created"] > 0
    listed = env.client.get("/v1/connections", headers=env.h()).json()
    assert listed[0]["asset_count"] > 0 and listed[0]["config"]["path"].endswith(".duckdb")
    assert env.client.get("/v1/connections", headers=env.h("explorer")).json()[0]["config"] is None


def test_crawl_is_queued_and_its_report_kept_on_the_job(env: Env) -> None:
    cid = duckdb_conn(env).json()["id"]
    q = env.post(f"/v1/connections/{cid}/crawl", {}, role="curator")
    assert q.status_code == 202 and q.json()["kind"] == "connection.crawl", q.text
    job_id = q.json()["id"]
    assert q.json()["status"] == "queued" and q.json()["result"] is None
    again = env.post(f"/v1/connections/{cid}/crawl", {}, role="curator")
    assert again.status_code == 202 and again.json()["id"] == job_id  # one at a time
    assert env.drain() >= 1
    done = env.client.get(f"/v1/jobs/{job_id}", headers=env.h("curator")).json()
    assert done["status"] == "done" and done["result"]["created"] > 0
    assert done["result"]["connection"] == "jaffle"
    assert env.client.get(f"/v1/jobs/{job_id}", headers=env.h("explorer")).status_code == 403
    assert env.client.get("/v1/jobs/999999999", headers=env.h()).status_code == 404


def test_a_crawl_of_a_deleted_connection_fails_at_once(env: Env) -> None:
    from satchel.data.jobs import queue_crawl

    jid, _ = queue_crawl(env.engine, env.ws_id, uuid.uuid4())
    env.drain()
    j = env.client.get(f"/v1/jobs/{jid}", headers=env.h()).json()
    assert j["status"] == "failed" and j["attempts"] == 1 and "deleted" in j["error"]


def test_jobs_of_another_workspace_are_not_found(env: Env, app_engine: Engine,
                                                 tmp_path: Path) -> None:
    cid = duckdb_conn(env).json()["id"]
    jid = env.post(f"/v1/connections/{cid}/crawl", {}).json()["id"]
    other = make_env(app_engine, tmp_path / "other")  # a second workspace
    assert other.client.get(f"/v1/jobs/{jid}", headers=other.h()).status_code == 404


def test_postgres_secret_is_sealed_and_used(env: Env, pg_url: str) -> None:
    from tests.conftest import APP_PW

    cfg = pg_config(pg_url) | {"user": "satchel_app"}
    bad = env.post("/v1/connections/test", {"kind": "postgres", "config": cfg, "secret": PW})
    assert not bad.json()["ok"] and PW not in bad.text
    r = env.post("/v1/connections", {"name": "pg", "kind": "postgres", "config": cfg,
                                     "secret": APP_PW})
    assert r.status_code == 201 and r.json()["secret"] == "stored" and "secret_ref" not in r.text
    row = env.conn("pg")
    assert row.secret_ref and row.secret_ref.startswith("enc:v1:")
    kr = Keyring.from_settings(st())
    assert kr.decrypt(row.secret_ref, connection_aad(env.ws_id, row.id)) == APP_PW
    assert env.post("/v1/connections/test", {"connection_id": str(row.id)}).json()["ok"]
    crawl = env.post(f"/v1/connections/{row.id}/crawl?wait=true", {})
    assert crawl.status_code == 200, crawl.text
    (ev,) = env.actions("connection.add")
    assert ev.target["secret"] == "stored" and "enc:v1" not in json.dumps(ev.target)


def test_validation_and_roles(env: Env) -> None:
    for body, msg in [
        ({"name": "x", "kind": "snowflake"}, "Account is required"),
        ({"name": "x", "kind": "postgres", "config": {"host": "h"}}, "Database is required"),
        ({"name": "x", "kind": "postgres", "config": {"host": "h", "database": "d",
                                                     "user": "u", "port": "abc"}}, "number"),
        ({"name": "x", "kind": "duckdb", "config": {"path": "/x", "extra": 1}}, "unknown field"),
        ({"name": "bad name!", "kind": "duckdb", "config": {"path": "/x"}}, "name"),
    ]:
        r = env.post("/v1/connections", body)
        assert r.status_code == 422 and msg in r.json()["detail"], (body, r.text)
    assert env.post("/v1/connections", {"name": "x", "kind": "duckdb",
                                        "config": {"path": "/x"}}, role="curator"
                    ).status_code == 403


def test_without_key_secret_is_refused(env: Env, app_engine: Engine) -> None:
    client = TestClient(create_app(app_engine, HashEmbedder(), mcp=False, settings=st(None)))
    r = client.post("/v1/connections", headers=env.h(), json={
        "name": "pg", "kind": "postgres", "secret": PW,
        "config": {"host": "h", "database": "d", "user": "u"}})
    assert r.status_code == 503 and "SATCHEL_ENCRYPTION_KEY" in r.json()["detail"]


def test_patch_and_remove(env: Env, pg_url: str) -> None:
    duckdb_conn(env)
    cid = env.conn("jaffle").id
    env.post(f"/v1/connections/{cid}/crawl?wait=true", {})
    r = env.post("/v1/connections", {"name": "pg", "kind": "postgres",
                                     "config": pg_config(pg_url), "secret": PW,
                                     "default_for_ai": True})
    assert r.json()["default_for_ai"] and not env.conn("jaffle").default_for_ai
    pid = r.json()["id"]
    p = env.client.patch(f"/v1/connections/{pid}", headers=env.h(),
                         json={"name": "warehouse", "secret": None})
    assert p.status_code == 200 and p.json()["name"] == "warehouse" and p.json()["secret"] is None

    # A source using the connection blocks removal.
    env.post("/v1/sources", {"kind": "dbt", "name": "proj", "config": {"connection": "jaffle"}})
    d = env.client.delete(f"/v1/connections/{cid}", headers=env.h())
    assert d.status_code == 409 and "proj" in d.json()["detail"]
    env.client.delete(f"/v1/sources/{env.src('proj').id}", headers=env.h())
    assert env.client.delete(f"/v1/connections/{cid}", headers=env.h()).status_code == 204
    with admin_session(engine=env.engine) as s:
        dropped = [a.dropped_at is not None for a in s.scalars(select(Asset).where(
            Asset.workspace_id == env.ws_id, Asset.connection_id.is_(None)))]
    assets = dropped
    assert assets and all(dropped)
    (ev,) = env.actions("connection.remove")
    assert ev.target["assets_dropped"] == len(assets)


# --- sources ------------------------------------------------------------------------------------


def test_source_from_object_store(env: Env, bucket: MemoryBucket) -> None:
    duckdb_conn(env)
    env.client.post(f"/v1/connections/{env.conn('jaffle').id}/crawl?wait=true", headers=env.h())
    for n in ("manifest", "semantic_manifest", "catalog"):
        bucket.put(f"dbt/target/{n}.json", (DBT / f"{n}.json").read_bytes())
    r = env.post("/v1/sources", {"kind": "dbt", "name": "jaffle", "config": {
        "connection": "jaffle", "manifest": "s3://artifacts/dbt/target/manifest.json",
        "semantic_manifest": "s3://artifacts/dbt/target/semantic_manifest.json",
        "catalog": "s3://artifacts/dbt/target/catalog.json", "publish": "preview"}})
    assert r.status_code == 201, r.text
    assert r.json()["connection"] == "jaffle" and r.json()["publish"] == "preview"
    sync = env.client.post(f"/v1/sources/{r.json()['id']}/sync?wait=true", headers=env.h("curator"))
    assert sync.status_code == 200, sync.text
    assert sync.json()["counts"]["created"] > 0

    missing = env.client.put(f"/v1/sources/{r.json()['id']}", headers=env.h(), json={
        "config": {"manifest": "s3://artifacts/nope/manifest.json"}})
    assert missing.status_code == 200
    bad = env.client.post(f"/v1/sources/{r.json()['id']}/sync?wait=true", headers=env.h())
    assert bad.status_code == 422 and "no object at S3" in bad.json()["detail"]


def test_source_upload_on_sync(env: Env) -> None:
    r = env.post("/v1/sources", {"kind": "dbt", "name": "up", "config": {}})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    with (DBT / "manifest.json").open("rb") as f:
        sync = env.client.post(f"/v1/sources/{sid}/sync?wait=true", headers=env.h(),
                               files={"manifest": ("manifest.json", f)})
    assert sync.status_code == 200, sync.text


@pytest.mark.parametrize(("config", "msg"), [
    ({"manifest": "/etc/passwd"}, "s3:// or gs://"),
    ({"manifest": "target/manifest.json"}, "s3:// or gs://"),
])
def test_rest_refuses_server_paths(env: Env, config: dict[str, Any], msg: str) -> None:
    r = env.post("/v1/sources", {"kind": "dbt", "name": "x", "config": config})
    assert r.status_code == 422 and msg in r.json()["detail"], r.text


def test_cube_secret_env_and_url_rules(env: Env) -> None:
    base = {"kind": "cube", "name": "cube", "config": {"project": "shop"}}
    env_ref = env.post("/v1/sources", base | {"config": {"project": "shop",
                                                          "meta_token_env": "SATCHEL_TOKEN"}})
    assert env_ref.status_code == 422 and "CLI" in env_ref.json()["detail"]
    meta = env.post("/v1/sources", base | {"config": {
        "project": "shop", "meta_url": "http://169.254.169.254/latest"}})
    assert meta.status_code == 422 and "meta_url" in meta.json()["detail"]

    r = env.post("/v1/sources", base | {"config": {
        "project": "shop", "meta_url": "https://cube.internal", "meta_token": "cube-secret-xyz"}})
    assert r.status_code == 201, r.text
    assert r.json()["config"]["meta_token"] == "***" and "cube-secret-xyz" not in r.text
    src = env.src("cube")
    sealed = src.config["meta_token"]
    assert sealed.startswith("enc:v1:")
    kr = Keyring.from_settings(st())
    assert kr.decrypt(sealed, source_aad(env.ws_id, src.id, "meta_token")) == "cube-secret-xyz"
    listed = env.client.get("/v1/sources", headers=env.h("curator")).text
    assert "cube-secret-xyz" not in listed and "enc:v1" not in listed

    # *** keeps the stored value; "" removes it.
    keep = env.client.put(f"/v1/sources/{src.id}", headers=env.h(), json={"config": {
        "project": "shop2", "meta_url": "https://cube.internal", "meta_token": "***"}})
    assert keep.status_code == 200, keep.text
    assert env.src("cube").config["meta_token"] == sealed
    assert env.src("cube").config["project"] == "shop2"
    gone = env.client.put(f"/v1/sources/{src.id}", headers=env.h(), json={"config": {
        "project": "shop2", "meta_token": ""}})
    assert gone.status_code == 200 and "meta_token" not in env.src("cube").config
    upd = env.actions("source.update")
    assert "cube-secret" not in json.dumps([e.target for e in upd])


def test_source_remove_detaches_items(env: Env) -> None:
    sid = env.post("/v1/sources", {"kind": "dbt", "name": "d", "config": {}}).json()["id"]
    with (DBT / "manifest.json").open("rb") as f:
        env.client.post(f"/v1/sources/{sid}/sync?wait=true", headers=env.h(),
                        files={"manifest": ("manifest.json", f)})
    assert env.client.delete(f"/v1/sources/{sid}", headers=env.h("curator")).status_code == 403
    r = env.client.delete(f"/v1/sources/{sid}", headers=env.h())
    assert r.status_code == 200 and r.json()["items_detached"] > 0
    with admin_session(engine=env.engine) as s:
        items = [(i.source_id, i.state) for i in s.scalars(select(ContextItem).where(
            ContextItem.workspace_id == env.ws_id, ContextItem.source_key.is_not(None)))]
    assert items and all(sid is None and state == "deprecated" for sid, state in items)


def test_cli_server_path_survives_ui_edit(env: Env, tmp_path: Path) -> None:
    """A source added with the CLI keeps its server path when the UI saves it unchanged."""
    from satchel.sources.service import add_source

    d = tmp_path / "dbt"
    shutil.copytree(DBT, d)
    add_source(env.slug, "dbt", "cli", {"manifest": str(d / "manifest.json")},
               engine=env.engine)
    src = env.src("cli")
    r = env.client.put(f"/v1/sources/{src.id}", headers=env.h(), json={"config": {
        "manifest": str(d / "manifest.json"), "publish": "preview"}})
    assert r.status_code == 200, r.text
    changed = env.client.put(f"/v1/sources/{src.id}", headers=env.h(), json={"config": {
        "manifest": "/etc/other.json"}})
    assert changed.status_code == 422


def test_rotation_covers_connections_and_sources(env: Env, pg_url: str) -> None:
    env.post("/v1/connections", {"name": "pg", "kind": "postgres", "config": pg_config(pg_url),
                                 "secret": PW})
    env.post("/v1/sources", {"kind": "cube", "name": "c", "config": {
        "project": "p", "meta_token": "tok-123456"}})
    new = generate_key()
    rotated = Settings(encryption_key=SecretStr(new), encryption_key_previous=SecretStr(KEY))
    rep = rotate_secrets(rotated, env.engine, workspace_id=env.ws_id)
    assert (rep.rotated, rep.failed) == (2, [])
    kr = Keyring.from_settings(Settings(encryption_key=SecretStr(new)))
    conn, src = env.conn("pg"), env.src("c")
    assert kr.decrypt(conn.secret_ref or "", connection_aad(env.ws_id, conn.id)) == PW
    assert kr.decrypt(src.config["meta_token"], source_aad(env.ws_id, src.id,
                                                           "meta_token")) == "tok-123456"
