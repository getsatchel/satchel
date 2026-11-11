import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from satchel.api.app import create_app
from satchel.llm.embeddings import HashEmbedder
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import AppUser, Workspace
from satchel.sync import import_context
from tests.helpers import copy_example, unique_slug


@pytest.fixture(scope="module")
def client_ctx(
    app_engine: Engine, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[TestClient, str, uuid.UUID]]:
    emb = HashEmbedder()
    slug = unique_slug("api")
    r = import_context(copy_example(tmp_path_factory.mktemp("api")), slug, embedder=emb,
                       engine=app_engine)
    uid = uuid.uuid4()
    with admin_session(engine=app_engine) as s:
        s.add(AppUser(id=uid, workspace_id=r.workspace_id, role="analyst"))
    st = Settings(dev_auth=True, workspace=None)
    yield TestClient(create_app(app_engine, emb, settings=st)), slug, uid


def test_health_and_auth(client_ctx: tuple[TestClient, str, uuid.UUID]) -> None:
    c, slug, uid = client_ctx
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/v1/items").status_code == 401
    assert c.get("/v1/items", headers={"X-Satchel-Workspace": "nope"}).status_code == 404
    h = {"X-Satchel-Workspace": slug, "X-Satchel-User": str(uuid.uuid4())}
    assert c.get("/v1/items", headers=h).status_code == 403


def test_items_endpoints(client_ctx: tuple[TestClient, str, uuid.UUID]) -> None:
    c, slug, uid = client_ctx
    h = {"X-Satchel-Workspace": slug, "X-Satchel-User": str(uid)}
    page = c.get("/v1/items", params={"limit": 500}, headers=h).json()
    items = page["items"]
    assert page["total"] == len(items) == 24
    assert {i["key"] for i in items} >= {"workspace.md", "queries/monthly-revenue.yaml"}
    g = c.get("/v1/items", params={"type": "golden_query"}, headers=h).json()["items"]
    assert len(g) == 5
    d = c.get("/v1/items/glossary/business-terms.yaml%23revenue", headers=h)
    assert d.status_code == 200
    assert d.json()["type"] == "glossary_term" and "AUD cents" in d.json()["body"]
    assert d.json()["meta"]["term"] == "revenue"
    assert c.get("/v1/items/missing.md", headers=h).status_code == 404


def test_search_and_pack(client_ctx: tuple[TestClient, str, uuid.UUID]) -> None:
    c, slug, uid = client_ctx
    h = {"X-Satchel-Workspace": slug}
    r = c.post("/v1/search", json={"query": "revenue last month", "k": 3}, headers=h)
    assert r.status_code == 200
    keys = [x["key"] for x in r.json()]
    assert "glossary/business-terms.yaml#revenue" in keys
    assert c.post("/v1/search", json={"query": ""}, headers=h).status_code == 422
    p = c.post("/v1/context-pack", json={"question": "refund rate", "budget_tokens": 1500},
               headers=h).json()
    assert p["used_tokens"] <= 1500
    assert p["entries"][0]["key"] == "workspace.md"
    assert "[ctx:queries/refund-rate.yaml@v1]" in p["text"]


def test_workspace_row_exists(
    app_engine: Engine, client_ctx: tuple[TestClient, str, uuid.UUID]
) -> None:
    _, slug, _ = client_ctx
    with admin_session(engine=app_engine) as s:
        assert s.query(Workspace).filter_by(slug=slug).count() == 1
