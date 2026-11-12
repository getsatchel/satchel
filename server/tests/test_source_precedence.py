"""Multi-source precedence (M7b): one primary doc per table, chosen by ``priority`` (lower wins,
hand-written beats a source at a tie); non-primary docs stay as provenance but are not served,
their distinct facts are folded into the primary; the catalog, item detail, signals and the
sync report agree."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
import yaml
from sqlalchemy import select

from satchel.data.semantic import PublishedModel, merge_manifests, shadowed_models
from satchel.retrieval import build_context_pack, search_context
from satchel.signals.extractors import _Catalog, _load_docs
from satchel.sources.precedence import (
    DocGroup,
    SourceInfo,
    fold_facts,
    folded_text,
    group_docs,
)
from satchel.sources.service import add_source
from satchel.store.db import admin_session, workspace_session
from satchel.store.models import Asset, ContextItem, ContextVersion
from tests.test_sources_sync import EMB, FIX, ORDERS, Env, env  # noqa: F401

SECOND_ORDERS = "sources/second/assets/analytics.orders.yaml"


def k(key: str) -> str:
    return quote(key, safe="/")


# ---- ranking rules (no database) -------------------------------------------------------------


def _item(key: str, source_id: uuid.UUID | None, managed_by: str, scope: uuid.UUID) -> ContextItem:
    return ContextItem(
        id=uuid.uuid4(), key=key, type="asset_doc", scope_kind="asset", scope_id=scope,
        source_id=source_id, managed_by=managed_by,
    )


def test_lower_priority_number_wins_then_handwritten_then_name() -> None:
    a, b, scope = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sources = {a: SourceInfo("dbt", "dbt", 2), b: SourceInfo("cube", "cube", 1)}
    rows: list[tuple[ContextItem, ContextVersion | None]] = [
        (_item("sources/dbt/orders", a, "source", scope), None),
        (_item("sources/cube/orders", b, "source", scope), None),
    ]
    g = group_docs(rows, sources)[scope]
    assert [m.ref.source for m in g.members] == ["cube", "dbt"] and g.overlapping

    # a hand-written doc counts as priority 0: it beats both here...
    hand = _item("assets/orders.yaml", None, "git", scope)
    g = group_docs([*rows, (hand, None)], sources)[scope]
    assert g.primary.ref.label == "git" and g.primary.ref.handwritten
    # ...loses to a source with a lower number, and wins a tie against a source
    sources[b] = SourceInfo("cube", "cube", -1)
    assert group_docs([*rows, (hand, None)], sources)[scope].primary.ref.source == "cube"
    sources[b] = SourceInfo("cube", "cube", 0)
    assert group_docs([*rows, (hand, None)], sources)[scope].primary.ref.label == "git"
    # equal priorities between sources: the name decides, deterministically
    sources[a] = SourceInfo("dbt", "dbt", 0)
    assert [m.ref.source for m in group_docs(rows, sources)[scope].members] == ["cube", "dbt"]
    assert [m.ref.source for m in group_docs(rows[::-1], sources)[scope].members] == [
        "cube", "dbt"]
    # items without a (live) source count as hand-written, e.g. after a source was removed
    gone = _item("sources/old/orders", uuid.uuid4(), "ui", scope)
    assert group_docs([(gone, None)], sources)[scope].primary.ref.handwritten


def _version(body: dict[str, Any]) -> ContextVersion:
    return ContextVersion(body=yaml.safe_dump(body), meta={})


def test_fold_adds_only_what_the_primary_lacks() -> None:
    scope = uuid.uuid4()
    a, b = uuid.uuid4(), uuid.uuid4()
    prim = _version({
        "asset": "analytics.orders", "description": "Orders.", "caveats": ["Exclude test"],
        "columns": [{"name": "status", "description": "State", "enum_values": ["a"]},
                    {"name": "amount"}],
    })
    other = _version({
        "asset": "analytics.orders", "description": "Other description.", "owner": "bi",
        "caveats": ["exclude test", "Net of tax"], "joins": ["customer_id -> x.customers"],
        "columns": [{"name": "status", "description": "other state", "enum_values": ["b"]},
                    {"name": "amount", "description": "In cents", "unit": "cents"},
                    {"name": "margin", "description": "Gross margin"}],
    })
    sources = {a: SourceInfo("dbt", "dbt", 1), b: SourceInfo("cube", "cube", 2)}
    rows: list[tuple[ContextItem, ContextVersion | None]] = [
        (_item("sources/dbt/o", a, "source", scope), prim),
        (_item("sources/cube/o", b, "source", scope), other),
    ]
    g = group_docs(rows, sources)[scope]
    lines = fold_facts(g)
    text = "\n".join(lines)
    assert "[cube] owner: bi" in text and "[cube] caveat: Net of tax" in text
    assert "[cube] join: customer_id -> x.customers" in text
    assert "column amount: In cents; unit: cents" in text and "column margin: Gross margin" in text
    # the primary wins conflicts: its description, its caveat, its status column are not echoed
    assert "Other description" not in text and "Exclude test" not in text.replace("exclude", "")
    assert "column status" not in text
    assert folded_text(g).startswith("Also described by cube (sources/cube/o)")
    assert fold_facts(DocGroup(members=g.members[:1])) == []


def test_overlapping_semantic_models_serve_the_best_one_whole() -> None:
    def pm(key: str, rank: tuple[int, int, str, str], hand: bool, cube: str) -> PublishedModel:
        mdl = {"models": [{"name": "orders", "tableReference": {"table": "orders"}, "columns": []}],
               "cubes": [{"name": cube, "baseObject": "orders",
                          "measures": [{"name": "n", "expression": "COUNT(*)", "type": "BIGINT"}]}]}
        return PublishedModel(uuid.uuid4(), key, 1, mdl, rank=rank, handwritten=hand)

    best = pm("sources/cube/semantic/a", (1, 1, "cube", "x"), False, "orders")
    worse = pm("sources/dbt/semantic/b", (2, 1, "dbt", "y"), False, "orders_dbt")
    other = pm("semantic/core.json", (0, 0, "", "z"), True, "core_cube")
    other.mdl["models"][0]["name"] = "core_orders"
    other.mdl["cubes"][0]["baseObject"] = "core_orders"
    skipped = shadowed_models([other, best, worse])
    assert list(skipped) == ["sources/dbt/semantic/b"] and "orders" in skipped[worse.key]
    merged = merge_manifests([other, best, worse])
    assert [c["name"] for c in merged["cubes"]] == ["core_cube", "orders"]
    # two hand-written models that collide are still an error
    h2 = pm("semantic/b.json", (0, 0, "", "zz"), True, "core_cube")
    h2.mdl["models"][0]["name"] = "core_orders"
    h2.mdl["cubes"][0]["baseObject"] = "core_orders"
    with pytest.raises(Exception, match="duplicate"):
        merge_manifests([other, h2])


# ---- end to end on Postgres: two dbt-shaped sources describe analytics.orders ------------------


def _second_source(env: Env, tmp: Path, **cfg: Any) -> None:
    """A second source over the same table that knows something the first does not."""
    d = tmp / "dbt2"
    d.mkdir(exist_ok=True)
    for n in ("manifest", "semantic_manifest", "catalog"):
        shutil.copy(FIX / f"{n}.json", d / f"{n}.json")
    m = json.loads((d / "manifest.json").read_text())
    node = m["nodes"]["model.jaffle_shop.orders"]
    node["description"] = "Second tool's description of orders."
    node["columns"]["loyalty_points"] = {
        "name": "loyalty_points", "description": "Loyalty points earned on the order",
        "meta": {}, "tags": [], "data_type": "integer"}
    node["columns"]["status"]["description"] = "A different status description."
    (d / "manifest.json").write_text(json.dumps(m))
    paths = {n: str(d / f"{n}.json") for n in ("manifest", "semantic_manifest", "catalog")}
    add_source(env.slug, "dbt", "second", {**paths, "connection": "warehouse", **cfg},
               engine=env.engine)


def _orders_node(env: Env) -> dict[str, Any]:
    """analytics.orders from the asset tree (database > schema > table)."""
    todo: list[dict[str, Any]] = env.client.get(
        "/v1/assets?connection=warehouse", headers=env.h("curator")).json()["assets"]
    while todo:
        n = todo.pop()
        if n["fqn"] == "analytics.orders":
            return n
        todo += n["children"]
    raise AssertionError("analytics.orders is not in the catalog")


def _orders_asset(env: Env) -> uuid.UUID:
    with admin_session(engine=env.engine) as s:
        return s.scalars(select(Asset).where(Asset.workspace_id == env.ws_id,
                                              Asset.fqn == "analytics.orders")).one().id


def test_two_sources_one_served_doc_deterministic_primary_and_catalog(
    env: Env, tmp_path: Path
) -> None:
    env.add("jaffle", priority=1)
    _second_source(env, tmp_path, priority=2)
    r1 = env.sync("jaffle")
    assert r1.overlaps == []  # nothing to overlap yet
    r2 = env.sync("second")
    (ov,) = [o for o in r2.overlaps if o["type"] == "asset_doc" and o["asset"] == "analytics.orders"]
    assert ov["primary"] == {"source": "jaffle", "key": ORDERS}
    assert ov["also_described_by"] == [{"source": "second", "key": SECOND_ORDERS}]
    assert ov["this_source_is_primary"] is False
    assert any(w["code"] == "also_described_by" and "'jaffle'" in str(w["message"])
               for w in r2.warnings)
    assert [o["asset"] for o in r2.to_json()["overlaps"] if o["type"] == "asset_doc"] == sorted(
        o["asset"] for o in r2.overlaps if o["type"] == "asset_doc")  # deterministic order
    # the semantic models of both define the same names: the second is shadowed and reported
    assert any(o["type"] == "semantic_model" and o["shadowed"] for o in r2.overlaps)
    assert any(w["code"] == "semantic_shadowed" for w in r2.warnings)

    # retrieval: a question that only the *second* doc can answer still serves one doc, the primary
    with workspace_session(env.ws_id, engine=env.engine) as s:
        hits = search_context(s, "loyalty points earned on analytics.orders", k=20, embedder=EMB,
                              types=["asset_doc"])
        orders = [h for h in hits if h.key in (ORDERS, SECOND_ORDERS)]
        assert [h.key for h in orders] == [ORDERS]
        pack = build_context_pack(s, "orders loyalty points", embedder=EMB,
                                  budget_tokens=4000)
        keys = [e.key for e in pack.entries]
        assert ORDERS in keys and SECOND_ORDERS not in keys
        text = [e.text for e in pack.entries if e.key == ORDERS][0]
        assert text.count("Also described by second") == 1
        assert "column loyalty_points: Loyalty points earned on the order" in text
        assert "A different status description" not in text  # the primary wins conflicts

    # catalog: the primary is the doc of the asset; the other is provenance
    c, h = env.client, env.h("curator")
    node = _orders_node(env)
    assert node["doc_item_key"] == ORDERS
    assert node["primary"] == {"source": "jaffle", "key": ORDERS, "primary": True}
    assert node["also_described_by"] == [{"source": "second", "key": SECOND_ORDERS,
                                          "primary": False}]
    detail = c.get(f"/v1/assets/{_orders_asset(env)}", headers=h).json()
    assert detail["primary"]["source"] == "jaffle" and len(detail["also_described_by"]) == 1
    # item detail: primary flag and the others, each flagged
    d1 = c.get(f"/v1/items/{k(ORDERS)}", headers=h).json()
    assert d1["primary"] is True and d1["also_described_by"] == [
        {"source": "second", "key": SECOND_ORDERS, "primary": False}]
    d2 = c.get(f"/v1/items/{k(SECOND_ORDERS)}", headers=h).json()
    assert d2["primary"] is False and d2["also_described_by"] == [
        {"source": "jaffle", "key": ORDERS, "primary": True}]
    # items that are not asset docs carry no precedence fields
    sem = next(i["key"] for i in c.get("/v1/items?type=semantic_model", headers=h).json()["items"]
               if "/jaffle/" in i["key"])
    assert c.get(f"/v1/items/{k(sem)}", headers=h).json()["primary"] is None

    # the sync report is also served by the sources API
    srcs = {x["name"]: x for x in c.get("/v1/sources", headers=h).json()}
    assert "analytics.orders" in [o.get("asset") for o in srcs["second"]["last_report"]["overlaps"]]

    # signals use the primary doc (loyalty_points is not in it: an undocumented column)
    with workspace_session(env.ws_id, engine=env.engine) as s:
        cat = _Catalog(s, None)
        docs = _load_docs(s, cat)
        asset = _orders_asset(env)
        assert docs.docs[asset][0].key == ORDERS
        assert "loyalty_points" not in {col["name"] for col in docs.docs[asset][1]["columns"]}

    # flipping the priorities flips the primary at once, without a new sync
    env.add("jaffle", priority=5)
    node = _orders_node(env)
    assert node["doc_item_key"] == SECOND_ORDERS and node["primary"]["source"] == "second"
    with workspace_session(env.ws_id, engine=env.engine) as s:
        hits = search_context(s, "orders status", k=20, embedder=EMB, types=["asset_doc"])
        assert SECOND_ORDERS in [x.key for x in hits] and ORDERS not in [x.key for x in hits]
    # equal priorities: name decides (jaffle < second), so the order never depends on sync order
    env.add("jaffle", priority=2)
    node = _orders_node(env)
    assert node["doc_item_key"] == ORDERS

    # query_metrics: the shadowed semantic model is skipped, the other is served whole
    with workspace_session(env.ws_id, engine=env.engine) as s:
        from satchel.data.semantic import load_published

        models = load_published(s)
        assert len(models) == 2 and len(shadowed_models(models)) == 1
        assert [cb["name"] for cb in merge_manifests(models)["cubes"]] == ["orders"]


def test_a_single_source_is_unchanged(env: Env) -> None:
    env.sync()
    with workspace_session(env.ws_id, engine=env.engine) as s:
        hits = search_context(s, "orders status", k=20, embedder=EMB, types=["asset_doc"])
        assert ORDERS in [x.key for x in hits]
        assert "Also described by" not in " ".join(h.snippet for h in hits)
    assert env.sync().overlaps == []
    node = _orders_node(env)
    assert node["also_described_by"] == [] and node["primary"]["key"] == ORDERS


# ---- dbt and Cube both describe analytics.orders --------------------------------------------------

CUBE_ORDERS = "sources/cube/assets/analytics.orders.yaml"


def _cube_source(env: Env, **cfg: Any) -> None:
    from tests.helpers import EXAMPLE

    add_source(env.slug, "cube", "cube",
               {"models": str(EXAMPLE / "cube" / "model"), "connection": "warehouse", **cfg},
               engine=env.engine)


def test_dbt_and_cube_describe_orders_one_served_doc_and_a_correct_catalog(env: Env) -> None:
    import tempfile

    import duckdb

    from satchel.data.semantic import build_cube_query, compile_query, load_published
    from tests.helpers import build_jaffle_db

    env.add("jaffle", priority=1)
    _cube_source(env, priority=2)
    r1 = env.sync("jaffle")
    r2 = env.sync("cube")
    assert r2.counts()["created"] == 4  # 3 asset docs + 1 semantic model
    (ov,) = [o for o in r2.overlaps if o["type"] == "asset_doc" and o["asset"] == "analytics.orders"]
    assert ov["primary"] == {"source": "jaffle", "key": ORDERS}
    assert ov["also_described_by"] == [{"source": "cube", "key": CUBE_ORDERS}]
    assert ov["this_source_is_primary"] is False and r1.overlaps == []
    assert {o["asset"] for o in r2.overlaps if o["type"] == "asset_doc"} == {
        "analytics.orders", "analytics.customers", "analytics.payments"}
    assert any(o["type"] == "semantic_model" and o["shadowed"] for o in r2.overlaps)

    # the primary is dbt; Cube's distinct facts (its segment caveat) are folded into it
    with workspace_session(env.ws_id, engine=env.engine) as s:
        pack = build_context_pack(s, "which orders count towards revenue on analytics.orders",
                                  embedder=EMB, budget_tokens=6000)
        keys = [e.key for e in pack.entries if e.type == "asset_doc"]
        assert ORDERS in keys and CUBE_ORDERS not in keys
        assert len(keys) == len(set(keys))
        text = next(e.text for e in pack.entries if e.key == ORDERS)
        assert "Also described by cube" in text and "[cube] caveat: Cube segment valid_orders" in text
        assert text.count("Also described by") == 1
    c, h = env.client, env.h("curator")
    node = _orders_node(env)
    assert node["doc_item_key"] == ORDERS and node["primary"]["source"] == "jaffle"
    assert node["also_described_by"] == [{"source": "cube", "key": CUBE_ORDERS, "primary": False}]
    d = c.get(f"/v1/items/{k(CUBE_ORDERS)}", headers=h).json()
    assert d["primary"] is False and d["also_described_by"][0] == {
        "source": "jaffle", "key": ORDERS, "primary": True}
    assert d["managed_by"] == "source" and d["meta"]["source"]["kind"] == "cube"

    # lower number wins: Cube at 0 takes over at once; dbt's duplicate semantic model is shadowed
    _cube_source(env, priority=0)
    node = _orders_node(env)
    assert node["doc_item_key"] == CUBE_ORDERS and node["primary"]["source"] == "cube"
    assert [a["source"] for a in node["also_described_by"]] == ["jaffle"]
    with workspace_session(env.ws_id, engine=env.engine) as s:
        models = load_published(s)
        assert [m.key for m in models][0].startswith("sources/cube/")
        assert list(shadowed_models(models)) == [m.key for m in models if "/jaffle/" in m.key]
        mdl = merge_manifests(models)
        hits = search_context(s, "orders status", k=20, embedder=EMB, types=["asset_doc"])
        assert CUBE_ORDERS in [x.key for x in hits] and ORDERS not in [x.key for x in hits]
    # query_metrics serves the Cube-derived MDL: March 2025 revenue 1716.15 over 25 orders
    with tempfile.TemporaryDirectory() as tmp:
        con = duckdb.connect(str(build_jaffle_db(Path(tmp) / "j.duckdb")), read_only=True)
        march = [{"dimension": "order_date", "operator": "gte", "value": "2025-03-01"},
                 {"dimension": "order_date", "operator": "lt", "value": "2025-04-01"}]
        q = build_cube_query(mdl, ["revenue", "order_count"], [], march, [], None)
        rev, n = con.execute(compile_query(mdl, q, "duckdb").sql).fetchall()[0]
        assert (round(rev, 2), n) == (1716.15, 25)
        con.close()
    # priorities decide what is served, never what a sync writes: a re-sync is a no-op
    again = env.sync("jaffle")
    assert not again.created and not again.updated and not again.release_created


def test_cube_folder_upload_and_sync_report_over_rest(env: Env) -> None:
    from tests.helpers import EXAMPLE

    _cube_source(env)
    c, h = env.client, env.h("curator")
    kinds = {x["kind"]: x for x in c.get("/v1/source-kinds", headers=h).json()}
    cube = kinds["cube"]
    assert cube["available"] and cube["install_hint"] is None and cube["folder_inputs"] == ["models"]
    assert "network" in cube["capabilities"] and "models" in cube["file_inputs"]
    (src,) = [x for x in c.get("/v1/sources", headers=h).json() if x["kind"] == "cube"]
    r = c.post(f"/v1/sources/{src['id']}/sync", headers=h)
    assert r.status_code == 200 and r.json()["counts"]["created"] == 4, r.text
    # CI style: upload the model files (one repeated field per file, named by their path inside
    # the model folder); uploads win over the stored path
    model = EXAMPLE / "cube" / "model"
    same = [("models", (p.relative_to(model).as_posix(), p.read_bytes(), "text/yaml"))
            for p in sorted(model.rglob("*.yml"))]
    r1 = c.post(f"/v1/sources/{src['id']}/sync", headers=h, files=same)
    assert r1.status_code == 200 and r1.json()["fingerprint"] == r.json()["fingerprint"]
    assert r1.json()["counts"]["created"] == 0 and not r1.json()["release_created"]
    uploads = [(n, (fn, data.replace(b"Customer first name.", b"Uploaded."), t))
               for n, (fn, data, t) in same]
    r2 = c.post(f"/v1/sources/{src['id']}/sync", headers=h, files=uploads)
    assert r2.status_code == 200, r2.text
    assert "sources/cube/assets/analytics.customers.yaml" in r2.json()["updated"]
    assert c.post(f"/v1/sources/{src['id']}/sync", headers=h,
                  files=[("nope", ("a.yml", b"x"))]).status_code == 422
    # a token-like key in a source config is masked in the API
    _cube_source(env, meta_token="s3cret", meta_url="http://cube.invalid")
    (src,) = [x for x in c.get("/v1/sources", headers=h).json() if x["kind"] == "cube"]
    assert src["config"]["meta_token"] == "***" and src["config"]["meta_url"].startswith("http")


def test_sqlmesh_sync_creates_linked_asset_docs_and_overlaps_with_dbt(env: Env) -> None:
    from tests.helpers import EXAMPLE

    SQLMESH_EXAMPLE = EXAMPLE / "sqlmesh"
    env.add("jaffle", priority=1)
    add_source(env.slug, "sqlmesh", "mesh", {"project": str(SQLMESH_EXAMPLE), "connection": "warehouse",
                                             "priority": 2}, engine=env.engine)
    env.sync("jaffle")
    r = env.sync("mesh")
    assert r.counts()["created"] == 4 and r.kind == "sqlmesh" and r.release_kind == "published"
    assert r.assets_created >= 1  # analytics.monthly_revenue is not in the crawl: created
    over = {o["asset"]: o for o in r.overlaps if o["type"] == "asset_doc"}
    assert set(over) == {"analytics.orders", "analytics.customers", "analytics.payments"}
    assert over["analytics.orders"]["primary"]["source"] == "jaffle"  # priority 1 beats 2
    with admin_session(engine=env.engine) as s:
        keys = set(s.scalars(select(ContextItem.key).where(
            ContextItem.workspace_id == env.ws_id, ContextItem.key.like("sources/mesh/%"))))
        assert keys == {f"sources/mesh/assets/analytics.{t}.yaml"
                        for t in ("orders", "customers", "payments", "monthly_revenue")}
        rows = list(s.scalars(select(Asset).where(
            Asset.workspace_id == env.ws_id, Asset.fqn == "analytics.orders")))
        assert len(rows) == 1  # linked to the crawler's row, not duplicated
    assert not env.sync("mesh").changed()  # an identical second sync is a no-op
    # the folder is read from the stored path; uploads (CI) name each file
    c, h = env.client, env.h("curator")
    (src,) = [x for x in c.get("/v1/sources", headers=h).json() if x["kind"] == "sqlmesh"]
    up = [("project", (p.relative_to(SQLMESH_EXAMPLE).as_posix(), p.read_bytes(), "text/plain"))
          for p in sorted(SQLMESH_EXAMPLE.rglob("*")) if p.suffix in (".sql", ".py", ".yaml", ".yml")]
    r2 = c.post(f"/v1/sources/{src['id']}/sync", headers=h, files=up)
    # the uploaded names (paths inside the project) are kept: same inputs, same fingerprint
    assert r2.status_code == 200 and r2.json()["fingerprint"] == r.fingerprint
    assert r2.json()["counts"]["created"] == 0 and not r2.json()["release_created"]
