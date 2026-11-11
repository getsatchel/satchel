"""`satchel serve api` serves the built curator UI at / with an SPA fallback that never shadows
the API surface."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from satchel.api.app import create_app
from satchel.llm.embeddings import HashEmbedder
from satchel.settings import Settings


def _client(engine: Engine, ui_dir: Path) -> TestClient:
    st = Settings(ui_dir=str(ui_dir), dev_auth=False)
    app = create_app(engine, HashEmbedder(), settings=st)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>satchel</title><div id=root></div>")
    (d / "assets" / "app.js").write_text("console.log(1)")
    (d / "favicon.svg").write_text("<svg/>")
    return d


def test_serves_index_assets_and_spa_fallback(app_engine: Engine, dist: Path) -> None:
    c = _client(app_engine, dist)
    r = c.get("/")
    assert r.status_code == 200 and "<div id=root>" in r.text
    # client-side routes (deep links) fall back to index.html
    for path in ("/library", "/library/item/guides/x.md", "/sessions/123"):
        r = c.get(path)
        assert r.status_code == 200 and "<div id=root>" in r.text, path
    assert c.get("/assets/app.js").text == "console.log(1)"
    assert c.get("/favicon.svg").text == "<svg/>"
    # a missing asset falls back to index.html (SPA), traversal does not escape the dist dir
    assert "<div id=root>" in c.get("/assets/missing.js").text
    r = c.get("/..%2f..%2fetc/passwd")
    assert "root:" not in r.text


def test_does_not_shadow_api_routes(app_engine: Engine, dist: Path) -> None:
    c = _client(app_engine, dist)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/openapi.json").json()["info"]["title"] == "satchel"
    # API paths keep their own behaviour (401 unauthenticated, JSON 404 for unknown)
    r = c.get("/v1/me")
    assert r.status_code == 401 and r.headers["content-type"].startswith("application/json")
    r = c.get("/v1/nope")
    assert r.status_code == 404 and "text/html" not in r.headers["content-type"]
    r = c.get("/auth/me")
    assert "<div id=root>" not in r.text
    r = c.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200 and "issuer" in r.json()
    r = c.get("/.well-known/does-not-exist")
    assert r.status_code == 404 and "<div id=root>" not in r.text
    r = c.get("/oauth/unknown")
    assert r.status_code == 404 and "<div id=root>" not in r.text
    r = c.post("/mcp", json={})
    assert r.status_code == 401
    r = c.get("/mcp")
    assert "<div id=root>" not in r.text


def test_no_ui_when_not_built(app_engine: Engine, tmp_path: Path) -> None:
    c = _client(app_engine, tmp_path / "empty")
    r = c.get("/")
    assert r.status_code == 200 and "no built curator UI" in r.text  # guidance, not a bare 404
    assert c.get("/healthz").status_code == 200


def test_ui_dir_resolution_falls_back_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from satchel.api import app as app_mod

    monkeypatch.setattr(app_mod, "_PACKAGED_UI_DIR", tmp_path / "none-a")
    monkeypatch.setattr(app_mod, "_DEFAULT_UI_DIR", tmp_path / "none-b")
    assert app_mod._resolve_ui_dir(None) is None
    (tmp_path / "ui" / "dist").mkdir(parents=True)
    (tmp_path / "ui" / "dist" / "index.html").write_text("<html></html>")
    monkeypatch.chdir(tmp_path)
    assert app_mod._resolve_ui_dir(None) == tmp_path / "ui" / "dist"
    assert app_mod._resolve_ui_dir("~/x") == Path("~/x").expanduser()
