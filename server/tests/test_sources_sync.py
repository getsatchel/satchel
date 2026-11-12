"""Source sync end to end on Postgres: dbt import onto items, idempotence, field ownership
(source block vs analyst overlay), overlay_out_of_sync, publish modes and the eval gate, the
REST/CLI surfaces and ``sources:`` in a context repo."""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from typer.testing import CliRunner

from satchel.api.app import create_app
from satchel.data.connections import add_connection
from satchel.data.crawl import crawl_connection
from satchel.data.semantic import load_published, merge_manifests
from satchel.llm.embeddings import HashEmbedder
from satchel.settings import Settings
from satchel.sources.apply import SourceSyncError
from satchel.sources.service import add_source, remove_source, sync_source
from satchel.store.db import admin_session, workspace_session
from satchel.store.models import (
    AppUser,
    Asset,
    ContextChunk,
    ContextEntityLink,
    ContextItem,
    ContextSource,
    ContextVersion,
    Release,
    Signal,
    Workspace,
)
from satchel.sync import import_context
from satchel.sync.importer import get_or_create_workspace
from tests.helpers import build_jaffle_db, copy_example, unique_slug

EMB = HashEmbedder()
FIX = Path(__file__).parent / "fixtures" / "dbt"
ORDERS = "sources/jaffle/assets/analytics.orders.yaml"
ROLES = ["admin", "curator", "analyst"]


def k(key: str) -> str:
    return quote(key, safe="/")


@dataclass
class Env:
    engine: Engine
    slug: str
    ws_id: uuid.UUID
    dir: Path
    client: TestClient
    users: dict[str, uuid.UUID]

    def h(self, role: str = "curator") -> dict[str, str]:
        return {"X-Satchel-Workspace": self.slug, "X-Satchel-User": str(self.users[role])}

    def manifest(self) -> dict[str, Any]:
        return json.loads((self.dir / "manifest.json").read_text())  # type: ignore[no-any-return]

    def write(self, name: str, data: dict[str, Any]) -> None:
        (self.dir / f"{name}.json").write_text(json.dumps(data))

    def sync(self, name: str = "jaffle") -> Any:
        return sync_source(self.slug, name, embedder=EMB, engine=self.engine)

    def add(self, name: str = "jaffle", **cfg: Any) -> None:
        paths = {n: str(self.dir / f"{n}.json") for n in ("manifest", "semantic_manifest", "catalog")}
        add_source(self.slug, "dbt", name, {**paths, "connection": "warehouse", **cfg},
                   engine=self.engine)

    def item(self, key: str = ORDERS) -> ContextItem:
        with admin_session(engine=self.engine) as s:
            it = s.scalars(select(ContextItem).where(
                ContextItem.workspace_id == self.ws_id, ContextItem.key == key)).one()
            s.expunge(it)
            return it

    def versions(self, key: str = ORDERS) -> list[ContextVersion]:
        with admin_session(engine=self.engine) as s:
            item = s.scalars(select(ContextItem).where(
                ContextItem.workspace_id == self.ws_id, ContextItem.key == key)).one()
            vs = list(s.scalars(select(ContextVersion).where(ContextVersion.item_id == item.id)
                                .order_by(ContextVersion.seq)))
            s.expunge_all()
            return vs

    def releases(self, kind: str) -> list[Release]:
        with admin_session(engine=self.engine) as s:
            rows = list(s.scalars(select(Release).where(
                Release.workspace_id == self.ws_id, Release.kind == kind).order_by(Release.seq)))
            s.expunge_all()
            return rows

    def signals(self, kind: str) -> list[Signal]:
        with admin_session(engine=self.engine) as s:
            rows = list(s.scalars(select(Signal).where(
                Signal.workspace_id == self.ws_id, Signal.kind == kind)))
            s.expunge_all()
            return rows


@pytest.fixture
def env(app_engine: Engine, tmp_path: Path) -> Env:
    slug = unique_slug("src")
    with admin_session(engine=app_engine) as s:
        ws = get_or_create_workspace(s, slug)
        ws_id = ws.id
    db = build_jaffle_db(tmp_path / "jaffle.duckdb")
    add_connection(slug, "warehouse", "duckdb", {"path": str(db)}, engine=app_engine)
    crawl_connection(slug, "warehouse", engine=app_engine)  # the crawler's asset rows
    d = tmp_path / "dbt"
    d.mkdir()
    for n in ("manifest", "semantic_manifest", "catalog"):
        shutil.copy(FIX / f"{n}.json", d / f"{n}.json")
    users = {r: uuid.uuid4() for r in ROLES}
    with admin_session(engine=app_engine) as s:
        for r, uid in users.items():
            s.add(AppUser(id=uid, workspace_id=ws_id, role=r, email=f"{r}@{slug}.test"))
    client = TestClient(create_app(app_engine, EMB, mcp=False, settings=Settings(dev_auth=True)))
    e = Env(app_engine, slug, ws_id, d, client, users)
    e.add()
    return e


def test_first_sync_creates_linked_items_and_second_is_a_noop(env: Env) -> None:
    r1 = env.sync()
    assert r1.counts() == {"created": 9, "updated": 0, "unchanged": 0, "deprecated": 0, "held": 0}
    assert r1.release_kind == "published" and r1.release_seq == 1 and r1.release_created
    item = env.item()
    assert item.managed_by == "source" and item.state == "published"
    assert item.source_key == "model.jaffle_shop.orders" and item.source_id is not None
    (v,) = env.versions()
    doc = yaml.safe_load(v.body)
    assert doc["asset"] == "analytics.orders" and doc["endorsed"] is True
    status = next(c for c in doc["columns"] if c["name"] == "status")
    assert status["enum_values"][0] == "placed" and status["data_type"] == "varchar"
    assert doc["grain"] == "order_id" and "Used by" not in v.body and doc["used_by"]
    assert v.meta["source"]["kind"] == "dbt" and v.meta["overlay"] == {}
    assert v.provenance["source"] == "source" and v.provenance["ref"] == "jaffle"

    with admin_session(engine=env.engine) as s:
        # linked to the crawler's asset rows; none duplicated
        rows = list(s.scalars(select(Asset).where(
            Asset.workspace_id == env.ws_id, Asset.fqn == "analytics.orders")))
        assert len(rows) == 1 and item.scope_id == rows[0].id and rows[0].kind == "table"
        links = {(lk.asset_id, lk.relation) for lk in s.scalars(
            select(ContextEntityLink).where(ContextEntityLink.version_id == v.id))}
        assert (rows[0].id, "defines") in links and len(links) > 3
        n_versions = s.scalar(select(func.count()).select_from(ContextVersion))
        n_chunks = s.scalar(select(func.count()).select_from(ContextChunk))
        src = s.scalars(select(ContextSource).where(ContextSource.workspace_id == env.ws_id)).one()
        assert src.last_status == "ok" and src.last_fingerprint and src.last_report["counts"]

    r2 = env.sync()
    assert r2.counts() == {"created": 0, "updated": 0, "unchanged": 9, "deprecated": 0, "held": 0}
    assert not r2.release_created and r2.chunks_embedded == 0
    with admin_session(engine=env.engine) as s:
        assert s.scalar(select(func.count()).select_from(ContextVersion)) == n_versions
        assert s.scalar(select(func.count()).select_from(ContextChunk)) == n_chunks
    assert len(env.releases("published")) == 1

    # the dbt-derived MDL is a normal published semantic model
    with workspace_session(env.ws_id, engine=env.engine) as s:
        models = load_published(s)
        assert len(models) == 1 and models[0].key.startswith("sources/jaffle/semantic/")
        assert [c["name"] for c in merge_manifests(models)["cubes"]] == ["orders"]


def test_changed_description_makes_a_new_version_and_release(env: Env) -> None:
    env.sync()
    m = env.manifest()
    m["nodes"]["model.jaffle_shop.orders"]["description"] = "Orders, now with a new description."
    env.write("manifest", m)
    r = env.sync()
    assert r.updated == [ORDERS] and r.counts()["unchanged"] == 8
    assert r.release_created and r.release_seq == 2
    vs = env.versions()
    assert [v.seq for v in vs] == [1, 2] and "new description" in vs[1].body
    assert env.item().published_version_id == vs[1].id


def test_overlay_survives_resync_and_out_of_sync_signal(env: Env) -> None:
    env.sync()
    c = env.client
    ov = {"notes": "Finance closes the books on day 3.", "caveats": ["Check GST before comparing"],
          "columns": {"amount": {"notes": "Includes shipping."}}}
    r = c.put(f"/v1/items/{k(ORDERS)}/overlay", json={"overlay": ov}, headers=env.h("curator"))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["has_changes"] and d["meta"]["overlay"]["notes"].startswith("Finance")
    assert "Analyst notes: Finance closes" in d["body"] and "Includes shipping." in d["body"]
    # the source description is always shown
    assert d["meta"]["source"]["asset"]["description"] in yaml.safe_load(d["body"])["description"]
    # publish the overlay through the normal preview -> publish flow
    pv = c.post("/v1/previews", json={"keys": [ORDERS]}, headers=env.h("curator"))
    assert pv.status_code == 201, pv.text
    assert c.post(f"/v1/previews/{pv.json()['id']}/publish", headers=env.h("curator")).status_code == 200
    # an identical overlay is not a new version
    again = c.put(f"/v1/items/{k(ORDERS)}/overlay", json={"overlay": ov}, headers=env.h("curator"))
    assert [v["seq"] for v in again.json()["versions"]] == [2, 1]

    m = env.manifest()
    m["nodes"]["model.jaffle_shop.orders"]["description"] = "Upstream rewrote this."
    env.write("manifest", m)
    r2 = env.sync()
    assert r2.updated == [ORDERS] and not r2.held
    v3 = env.versions()[-1]
    assert v3.seq == 3 and v3.meta["overlay"] == d["meta"]["overlay"]
    assert "Upstream rewrote this." in v3.body and "Finance closes the books" in v3.body
    assert env.item().published_version_id == v3.id
    assert env.signals("overlay_out_of_sync") == []

    # upstream drops the column the overlay documents -> hygiene signal, overlay kept
    m = env.manifest()
    m["nodes"]["model.jaffle_shop.orders"]["columns"].pop("amount")
    env.write("manifest", m)
    cat = json.loads((env.dir / "catalog.json").read_text())
    cat["nodes"]["model.jaffle_shop.orders"]["columns"].pop("amount")
    env.write("catalog", cat)
    r3 = env.sync()
    assert r3.signals.get("overlay_out_of_sync") == 1
    (sig,) = env.signals("overlay_out_of_sync")
    assert sig.source == "hygiene" and sig.payload["missing_columns"] == ["amount"]
    assert env.item().id in sig.item_ids
    v4 = env.versions()[-1]
    assert "amount" in v4.meta["overlay"]["columns"] and "Includes shipping" not in v4.body
    assert env.sync().signals == {}  # nothing new, signal not repeated

    # the finding flows into curation like other hygiene findings
    from satchel.curation.config import CurationConfig
    from satchel.curation.gaps import KIND_LABEL, detect_gaps
    from satchel.store.models import Suggestion

    assert "overlay_out_of_sync" in KIND_LABEL
    with admin_session(engine=env.engine) as s:
        rep = detect_gaps(s, env.ws_id, CurationConfig())
        assert len(rep.created) == 1
        sug = s.get(Suggestion, rep.created[0])
        assert sug is not None and sug.source == "hygiene" and "Overlay out of sync" in sug.title


def test_unpublished_overlay_draft_is_held_not_published_by_a_sync(env: Env) -> None:
    env.sync()
    env.client.put(f"/v1/items/{k(ORDERS)}/overlay", json={"overlay": {"notes": "draft only"}},
                   headers=env.h("curator"))
    published_before = env.item().published_version_id
    m = env.manifest()
    m["nodes"]["model.jaffle_shop.orders"]["description"] = "Changed upstream."
    env.write("manifest", m)
    r = env.sync()
    assert r.held == [ORDERS] and r.updated == [ORDERS]
    it = env.item()
    assert it.published_version_id == published_before and it.current_version_id != it.published_version_id
    assert env.versions()[-1].meta["overlay"] == {"notes": "draft only"}


def test_source_block_is_read_only_overlay_is_editable(env: Env) -> None:
    env.sync()
    c = env.client
    cur = c.get(f"/v1/items/{k(ORDERS)}", headers=env.h("curator")).json()
    r = c.put(f"/v1/items/{k(ORDERS)}", json={"body": cur["body"] + "\n# x", "meta": cur["meta"]},
              headers=env.h("curator"))
    assert r.status_code == 409 and "upstream source" in r.json()["detail"]
    assert c.post(f"/v1/items/{k(ORDERS)}/deprecate", headers=env.h("curator")).status_code == 409
    ok = c.put(f"/v1/items/{k(ORDERS)}/overlay", json={"overlay": {"notes": "n"}},
               headers=env.h("curator"))
    assert ok.status_code == 200 and ok.json()["managed_by"] == "source"
    bad = c.put(f"/v1/items/{k(ORDERS)}/overlay", json={"overlay": {"description_x": "n"}},
                headers=env.h("curator"))
    assert bad.status_code == 422
    assert c.put(f"/v1/items/{k(ORDERS)}/overlay", json={"overlay": {}},
                 headers=env.h("analyst")).status_code == 403
    # not a source item
    assert c.put("/v1/items/workspace.md/overlay", json={"overlay": {}},
                 headers=env.h("curator")).status_code in (404, 409)
    # notes on the semantic model and a golden query are supported too
    sem = next(i["key"] for i in c.get("/v1/items?type=semantic_model", headers=env.h()).json()["items"])
    assert c.put(f"/v1/items/{k(sem)}/overlay", json={"overlay": {"notes": "Use revenue"}},
                 headers=env.h()).status_code == 200
    items = c.get("/v1/items?managed_by=source", headers=env.h()).json()["items"]
    assert len(items) == 9 and all(i["source_key"] for i in items)


def test_removed_model_is_deprecated_and_leaves_the_release(env: Env) -> None:
    env.sync()
    m = env.manifest()
    m["nodes"]["model.jaffle_shop.payments"]["config"]["enabled"] = False
    env.write("manifest", m)
    r = env.sync()
    key = "sources/jaffle/assets/analytics.payments.yaml"
    assert r.deprecated == [key] and r.release_seq == 2
    it = env.item(key)
    assert it.state == "deprecated" and it.published_version_id is None


def test_preview_mode_creates_only_a_preview_release(env: Env) -> None:
    env.add(publish="preview")
    r = env.sync()
    assert r.release_kind == "preview" and r.release_created and r.counts()["created"] == 9
    assert env.releases("published") == [] and len(env.releases("preview")) == 1
    it = env.item()
    assert it.state == "draft" and it.published_version_id is None and it.current_version_id
    changes = env.client.get("/v1/changes", headers=env.h()).json()["changes"]
    assert len([c for c in changes if c["managed_by"] == "source"]) == 9
    r2 = env.sync()  # identical: no new versions, no new preview
    assert r2.counts()["unchanged"] == 9 and not r2.release_created
    assert len(env.releases("preview")) == 1
    # a human publishes the preview from the UI flow
    pid = env.releases("preview")[0].id
    assert env.client.post(f"/v1/previews/{pid}/publish", headers=env.h()).status_code == 200
    assert env.item().state == "published"


def test_eval_gate_is_respected_for_auto_publish(env: Env) -> None:
    with admin_session(engine=env.engine) as s:
        ws = s.get(Workspace, env.ws_id)
        assert ws is not None
        ws.settings = {**ws.settings, "publish": {"require_evals": ["retrieval"]}}
    r = env.sync()
    assert r.release_kind == "preview" and r.gate_status == "pending"
    assert "eval gate" in r.gate_message
    assert env.releases("published") == [] and env.item().published_version_id is None


def test_sync_failure_is_recorded_and_other_state_untouched(env: Env) -> None:
    env.sync()
    (env.dir / "manifest.json").write_text("{broken")
    with pytest.raises(SourceSyncError, match="not valid JSON"):
        env.sync()
    r = env.client.get("/v1/sources", headers=env.h("curator"))
    (src,) = r.json()
    assert src["last_status"] == "error" and "not valid JSON" in src["last_error"]
    assert env.item().state == "published"
    (env.dir / "manifest.json").unlink()
    with pytest.raises(SourceSyncError, match="cannot read manifest"):
        env.sync()


def test_rest_list_and_sync_with_upload(env: Env) -> None:
    c = env.client
    kinds = c.get("/v1/source-kinds", headers=env.h("curator")).json()
    dbt = next(x for x in kinds if x["kind"] == "dbt")
    assert dbt["builtin"] and dbt["maturity"] == "beta" and "manifest" in dbt["file_inputs"]
    (src,) = c.get("/v1/sources", headers=env.h("curator")).json()
    assert src["adapter"]["version"] and src["connection"] == "warehouse"
    assert src["last_synced_at"] is None and src["item_counts"] == {}
    sid = src["id"]
    r = c.post(f"/v1/sources/{sid}/sync?wait=true", headers=env.h("curator"))
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["created"] == 9 and r.json()["release_kind"] == "published"
    # CI style: upload the artifacts (a changed manifest) instead of reading server paths
    m = env.manifest()
    m["nodes"]["model.jaffle_shop.customers"]["description"] = "Uploaded description."
    r2 = c.post(f"/v1/sources/{sid}/sync?wait=true", headers=env.h("curator"),
                files={"manifest": ("manifest.json", json.dumps(m), "application/json")})
    assert r2.status_code == 200 and r2.json()["updated"] == [
        "sources/jaffle/assets/analytics.customers.yaml"]
    assert c.post(f"/v1/sources/{sid}/sync?wait=true", headers=env.h("curator"),
                  files={"bogus": ("x.json", "{}")}).status_code == 422
    bad = c.post(f"/v1/sources/{sid}/sync?wait=true", headers=env.h("curator"),
                 files={"manifest": ("manifest.json", "{nope")})
    assert bad.status_code == 422 and "not valid JSON" in bad.json()["detail"]
    (src,) = c.get("/v1/sources", headers=env.h("curator")).json()
    assert src["last_status"] == "error" and src["item_counts"]["asset_doc"] == 7
    assert c.post(f"/v1/sources/{uuid.uuid4()}/sync?wait=true", headers=env.h("curator")).status_code == 404
    assert c.post(f"/v1/sources/{sid}/sync?wait=true", headers=env.h("analyst")).status_code == 403


def test_two_sources_describing_one_table_warn_with_priority(env: Env) -> None:
    env.sync()
    env.add("second", priority=5)
    r = env.sync("second")
    msgs = [w["message"] for w in r.warnings if w["code"] == "also_described_by"]
    assert msgs and "'jaffle' (priority 0) is primary" in msgs[0]  # lower number wins (M7b)


def test_remove_source_deprecates_and_detaches(env: Env) -> None:
    env.sync()
    assert remove_source(env.slug, "jaffle", engine=env.engine) == 9
    it = env.item()
    assert it.state == "deprecated" and it.source_id is None
    with pytest.raises(LookupError):
        env.sync()


def test_sources_are_workspace_isolated(env: Env, app_engine: Engine) -> None:
    other = unique_slug("other")
    with admin_session(engine=app_engine) as s:
        other_id = get_or_create_workspace(s, other).id
    with workspace_session(other_id, engine=app_engine) as s:
        assert list(s.scalars(select(ContextSource))) == []
    with workspace_session(env.ws_id, engine=app_engine) as s:
        assert len(list(s.scalars(select(ContextSource)))) == 1


def test_context_repo_sources_and_legacy_alias(app_engine: Engine, tmp_path: Path) -> None:
    ctx = copy_example(tmp_path)
    dbt = ctx / "dbt"
    dbt.mkdir()
    for n in ("manifest", "semantic_manifest", "catalog"):
        shutil.copy(FIX / f"{n}.json", dbt / f"{n}.json")
    cfg_path = ctx / "satchel.config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["sources"] = [{
        "kind": "dbt", "name": "jaffle", "manifest": "dbt/manifest.json",
        "semantic_manifest": "dbt/semantic_manifest.json", "catalog": "dbt/catalog.json",
        "connection": "warehouse", "publish": "auto", "priority": 3,
    }]
    cfg_path.write_text(yaml.safe_dump(cfg))
    slug = unique_slug("repo")
    r = import_context(ctx, slug, embedder=EMB, engine=app_engine)
    assert r.source_errors == [] and len(r.sources) == 1
    assert r.sources[0].counts()["created"] == 9
    r2 = import_context(ctx, slug, embedder=EMB, engine=app_engine)
    assert r2.sources[0].counts()["unchanged"] == 9 and not r2.sources[0].release_created
    with admin_session(engine=app_engine) as s:
        src = s.scalars(select(ContextSource).where(ContextSource.workspace_id == r.workspace_id)).one()
        assert src.config["priority"] == 3 and src.connection_id is not None

    # the older import.dbt_manifest key still works, as a source named "dbt"
    cfg.pop("sources")
    cfg["import"] = {"dbt_manifest": "dbt/manifest.json"}
    cfg_path.write_text(yaml.safe_dump(cfg))
    r3 = import_context(ctx, unique_slug("legacy"), embedder=EMB, engine=app_engine)
    assert r3.source_errors == [] and r3.sources[0].source == "dbt"
    assert r3.sources[0].counts()["created"] == 9  # metrics fall back to manifest.json

    # a missing artifact is reported, not fatal for the files import
    cfg["import"] = {"dbt_manifest": "dbt/missing.json"}
    cfg_path.write_text(yaml.safe_dump(cfg))
    r4 = import_context(ctx, unique_slug("bad"), embedder=EMB, engine=app_engine)
    assert r4.source_errors and "missing.json" in r4.source_errors[0]


def test_cli_validate_works_without_a_database(tmp_path: Path) -> None:
    from satchel.cli.main import app as cli

    runner = CliRunner()
    out = runner.invoke(cli, ["source", "validate", "dbt", "--manifest", str(FIX / "manifest.json"),
                              "--catalog", str(FIX / "catalog.json")])
    assert out.exit_code == 0, out.output
    assert "assets" in out.output and "analytics.orders" in out.output
    snap = tmp_path / "snap.json"
    js = runner.invoke(cli, ["source", "validate", "dbt", "--manifest", str(FIX / "manifest.json"),
                             "--json", "--out", str(snap)])
    assert js.exit_code == 0 and json.loads(js.output)["kind"] == "dbt"
    assert json.loads(snap.read_text())["ir_version"] == "1.0.0"
    assert runner.invoke(cli, ["source", "validate", "dbt"]).exit_code == 1  # manifest required
    bad = runner.invoke(cli, ["source", "validate", "dbt", "--nope", "x"])
    assert bad.exit_code != 0
    kinds = runner.invoke(cli, ["source", "kinds"])
    assert "dbt" in kinds.output and "beta" in kinds.output and "built-in" in kinds.output
    info = runner.invoke(cli, ["source", "info", "dbt"])
    assert "--manifest" in info.output and "--semantic-manifest" in info.output


def test_cli_add_list_sync_remove(env: Env, app_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from satchel.cli.main import app as cli
    from satchel.store import db as dbmod

    # point the CLI's engine at the test database (app role, so RLS applies)
    monkeypatch.setattr(dbmod, "get_engine", lambda: app_engine)
    monkeypatch.setattr("satchel.store.db.get_engine", lambda: app_engine)
    worker = dbmod.admin_engine_for(app_engine)  # admin_session() with no engine: satchel_worker
    monkeypatch.setattr(dbmod, "get_admin_engine", lambda: worker)
    runner = CliRunner()
    base = ["-w", env.slug]
    res = runner.invoke(cli, ["source", "add", "dbt", "cli-src", *base,
                              "--manifest", str(env.dir / "manifest.json"),
                              "--catalog", str(env.dir / "catalog.json"),
                              "--connection", "warehouse", "--publish", "preview",
                              "--priority", "2"])
    assert res.exit_code == 0, res.output
    with admin_session(engine=app_engine) as s:
        src = s.scalars(select(ContextSource).where(ContextSource.name == "cli-src")).one()
        assert src.config["publish"] == "preview" and src.config["priority"] == 2
    lst = runner.invoke(cli, ["source", "list", *base])
    assert lst.exit_code == 0 and "cli-src" in lst.output and "installed adapters" in lst.output
    assert runner.invoke(cli, ["source", "add", "dbt", "x", *base, "--publish", "sometimes"]).exit_code == 1
    assert runner.invoke(cli, ["source", "add", "nope", "x", *base]).exit_code == 1


def test_heuristic_endorsement_is_only_a_suggestion(env: Env) -> None:
    m = env.manifest()
    n = m["nodes"]["model.jaffle_shop.customers"]
    n["access"] = "public"
    n["contract"] = {"enforced": True}
    env.write("manifest", m)
    r = env.sync()
    assert r.signals.get("endorsement_candidate") == 1
    (sig,) = env.signals("endorsement_candidate")
    assert sig.source == "hygiene" and "access: public" in sig.payload["reason"]
    key = "sources/jaffle/assets/analytics.customers.yaml"
    assert yaml.safe_load(env.versions(key)[-1].body)["endorsed"] is False  # never auto-applied
    # an explicit analyst endorsement through the overlay silences the suggestion on later syncs
    env.client.put(f"/v1/items/{k(key)}/overlay", json={"overlay": {"endorsed": True}},
                   headers=env.h("curator"))
    assert yaml.safe_load(env.versions(key)[-1].body)["endorsed"] is True
