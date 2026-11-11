"""/readyz: DB reachable + schema at head; at-head cached, reachability always re-checked."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine

from satchel.api import health
from satchel.api.app import create_app
from satchel.llm.embeddings import HashEmbedder
from satchel.settings import Settings
from satchel.store.schema import SchemaStatus


def _client(engine: Engine) -> TestClient:
    app = create_app(engine, HashEmbedder(), mcp=False, settings=Settings(ui_dir="/nonexistent"))
    return TestClient(app, raise_server_exceptions=False)


def test_ready_at_head_and_healthz_needs_no_db(app_engine: Engine) -> None:
    c = _client(app_engine)
    r = c.get("/readyz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert c.get("/healthz").json() == {"status": "ok"}


def test_not_ready_when_schema_behind(
    app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "schema_status", lambda e: SchemaStatus("behind", "0001", "0009"))
    r = _client(app_engine).get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unavailable" and "needs 0009" in body["reason"]


def test_not_ready_when_db_unreachable() -> None:
    eng = create_engine(
        "postgresql+psycopg://u:p@127.0.0.1:1/x", connect_args={"connect_timeout": 1}
    )
    r = _client(eng).get("/readyz")
    assert r.status_code == 503 and r.json()["reason"] == "database unreachable"


def test_at_head_is_cached_but_reachability_is_not(
    app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = health.schema_status

    def counting(e: Engine) -> SchemaStatus:
        calls.append(1)
        return real(e)

    monkeypatch.setattr(health, "schema_status", counting)
    ready = health.Readiness(app_engine)
    assert ready._check() is None and ready._check() is None and ready._check() is None
    assert len(calls) == 1
    # an expired cache re-checks the revision
    ready._ok_until = 0.0
    assert ready._check() is None and len(calls) == 2
    # a dead database fails even while the at-head result is cached, and drops the cache
    ready._ok_until = 1e12
    ready.engine = create_engine(
        "postgresql+psycopg://u:p@127.0.0.1:1/x", connect_args={"connect_timeout": 1}
    )
    assert ready._check() == "database unreachable" and ready._ok_until == 0.0
