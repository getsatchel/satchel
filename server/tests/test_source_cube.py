"""The Cube source adapter, built as if third-party: conformance kit on the jaffle-shop Cube model,
the mapping rules, tolerance for unsupported/malformed input (JS, Jinja, runtime-only SQL), the
optional /meta input (a saved file, and the network path with an injected fetch) and
``query_metrics`` over the Cube-derived MDL."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import duckdb
import pytest
from satchel_adapter_sdk import SourceFiles, SourceSnapshot
from satchel_adapter_sdk.testing import _no_network, assert_conformant
from typer.testing import CliRunner

from satchel.data.semantic import build_cube_query, compile_query
from satchel.sources.adapters.cube import CubeAdapter, CubeConfig, adapter
from satchel.sources.adapters.cube.meta import http_fetch, meta_endpoint
from satchel.sources.registry import discover
from tests.helpers import EXAMPLE, build_jaffle_db

FIX = Path(__file__).parent / "fixtures" / "cube"
MODEL = EXAMPLE / "cube" / "model"
GOLDEN = FIX / "golden" / "snapshot.json"


def tree(root: Path, **extra: bytes) -> SourceFiles:
    data = {f"models/{p.relative_to(root).as_posix()}": p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in (".yml", ".yaml", ".js")}
    data.update({f"models/{k}": v for k, v in extra.items()})
    return SourceFiles(data)


def files(**override: bytes | None) -> SourceFiles:
    f = tree(MODEL)
    data = {n: f.read(n) for n in f.names()}
    for k, v in override.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    return SourceFiles(data)


def load(f: SourceFiles | None = None, **cfg: Any) -> SourceSnapshot:
    return adapter.load(CubeConfig(**cfg), f or files())


def asset(snap: SourceSnapshot, fqn: str) -> Any:
    return next(a for a in snap.assets if a.fqn == fqn)


def codes(snap: SourceSnapshot) -> set[str]:
    return {w.code for w in snap.warnings}


def test_conformance_kit(update_golden: bool) -> None:
    snap = assert_conformant(adapter, CubeConfig(), files(), golden=GOLDEN,
                             update_golden=update_golden)
    assert snap.summary()["assets"] == 3 and snap.summary()["metrics"] == 7


def test_cubes_become_assets_with_columns_joins_segments_and_views() -> None:
    snap = load()
    orders = asset(snap, "analytics.orders")
    assert orders.key == "cube.orders" and orders.kind == "table"
    assert orders.owner == "data-team@jaffle.example" and orders.endorsed is True
    assert orders.grain == ["order_id"] and orders.description.startswith("One row per customer")
    cols = {c.name: c for c in orders.columns}
    assert sorted(cols) == ["amount", "customer_id", "order_date", "order_id", "status"]
    assert cols["order_id"].unique and cols["order_id"].meta["cube_type"] == "number"
    assert cols["status"].enum_values == ["placed", "shipped", "completed", "return_pending",
                                          "returned"]  # meta.satchel.enum_values
    assert cols["amount"].description.startswith("Order total including GST")
    assert [(r.column, r.to_asset, r.to_column) for r in orders.relationships] == [
        ("customer_id", "analytics.customers", "customer_id")]
    assert any("Cube segment valid_orders" in c and "NOT IN ('returned', 'return_pending')" in c
               for c in orders.caveats)  # a segment is a caveat with its filter
    assert any("refund" in c for c in orders.caveats)  # meta.satchel.caveats
    assert orders.used_by == ["Cube view orders_view"]
    assert "orders.revenue (sum)" in orders.source_meta["measures"]
    customers = asset(snap, "analytics.customers")
    assert {c.name: c.pii for c in customers.columns}["email"] is True
    # a column a measure aggregates (no dimension) is still listed, with where it is used
    assert "Cube measure lifetime_orders" in {c.name: c for c in customers.columns}[
        "number_of_orders"].description
    payments = asset(snap, "analytics.payments")
    assert payments.relationships[0].to_asset == "analytics.orders"
    assert {c.name: c for c in payments.columns}["payment_method"].enum_values[0] == "credit_card"
    assert [w.code for w in snap.warnings] == ["types_guessed"]


def test_semantic_model_is_one_mdl_with_inlined_filters_and_measures() -> None:
    (sem,) = load().semantic_models
    assert sem.name == "cube" and sem.queryable_metrics == [
        "average_order_value", "collected", "customer_count", "lifetime_orders", "order_count",
        "payment_count", "revenue"]
    cubes = {c["name"]: c for c in sem.mdl["cubes"]}
    assert set(cubes) == {"customers", "orders", "payments"}
    measures = {m["name"]: m for m in cubes["orders"]["measures"]}
    assert measures["revenue"]["expression"] == (
        "SUM(CASE WHEN (status NOT IN ('returned', 'return_pending') AND customer_id <> 0) "
        "THEN amount / 100.0 END)")
    assert measures["order_count"]["type"] == "BIGINT"
    assert "NULLIF(CAST((COUNT(CASE WHEN" in measures["average_order_value"]["expression"]
    assert [d["name"] for d in cubes["orders"]["timeDimensions"]] == ["order_date"]
    assert {m["name"] for m in sem.mdl["models"]} == {"customers", "orders", "payments"}
    orders_model = next(m for m in sem.mdl["models"] if m["name"] == "orders")
    assert orders_model["tableReference"] == {"schema": "analytics", "table": "orders"}
    assert orders_model["primaryKey"] == "order_id"


def test_query_metrics_over_the_cube_derived_mdl_matches_dbt() -> None:
    """March 2025 revenue 1716.15 over 25 orders: the same numbers as the dbt-derived and the
    hand-written MDL (metric names revenue, order_count, average_order_value match too)."""
    mdl = load().semantic_models[0].mdl
    with tempfile.TemporaryDirectory() as d:
        db = build_jaffle_db(Path(d) / "j.duckdb")
        con = duckdb.connect(str(db), read_only=True)
        march = [{"dimension": "order_date", "operator": "gte", "value": "2025-03-01"},
                 {"dimension": "order_date", "operator": "lt", "value": "2025-04-01"}]
        q = build_cube_query(mdl, ["revenue", "order_count", "average_order_value"], [], march,
                             [], None)
        rev, n, aov = con.execute(compile_query(mdl, q, "duckdb").sql).fetchall()[0]
        assert (round(rev, 2), n) == (1716.15, 25) and aov == pytest.approx(rev / n)
        q = build_cube_query(mdl, ["revenue"], ["order_date:month"], [], [], None)
        assert len(con.execute(compile_query(mdl, q, "duckdb").sql).fetchall()) > 10
        # a categorical dimension and another cube of the same MDL
        q = build_cube_query(mdl, ["order_count"], ["status"], [], [], None)
        by_status = dict(con.execute(compile_query(mdl, q, "duckdb").sql).fetchall())
        assert by_status and sum(by_status.values()) > 0
        q = build_cube_query(mdl, ["collected"], ["payment_method"], [], [], None)
        assert con.execute(compile_query(mdl, q, "duckdb").sql).fetchall()
        q = build_cube_query(mdl, ["customer_count"], [], [], [], None)
        assert con.execute(compile_query(mdl, q, "duckdb").sql).fetchall()[0][0] > 0
        con.close()


# ---- unsupported and malformed input ------------------------------------------------------------


def edge() -> SourceFiles:
    return tree(FIX / "edge")


def test_edge_model_extends_merges_tables_and_reports_what_cannot_be_queried() -> None:
    snap = load(edge())
    c = codes(snap)
    assert {"js_model_skipped", "invalid_yaml", "invalid_node", "cube_no_table",
            "dimension_not_queryable", "metric_not_queryable", "join_unparsed",
            "types_guessed"} <= c
    # base_orders + shipped_orders (extends) + completed_orders read analytics.orders: one asset
    orders = asset(snap, "analytics.orders")
    assert sorted(orders.source_meta["cubes"]) == ["base_orders", "completed_orders",
                                                   "shipped_orders"]
    assert orders.key == "cube.base_orders" and orders.grain == ["order_id"]
    assert any("Cube completed_orders only covers rows where status = 'completed'" in c
               for c in orders.caveats)
    assert any("Cube segment big" in c and "amount > 10000" in c for c in orders.caveats)
    assert "analytics.refunds" not in {a.fqn for a in snap.assets}  # the .js model is skipped
    # sql cubes: a joining query has no table; the JS file, Jinja and bad nodes are warned
    assert {a.fqn for a in snap.assets} == {"analytics.customers", "analytics.orders",
                                            "analytics.payments"}
    msgs = "\n".join(w.message for w in snap.warnings)
    assert "legacy.js" in msgs and "broken.yml" in msgs and "not_valid" in msgs
    assert "dynamic" in msgs  # the dynamic sql_table

    (sem,) = snap.semantic_models
    cubes = {x["name"]: x for x in sem.mdl["cubes"]}
    assert "base_orders" not in cubes  # public: false stays out of the MDL
    shipped = {m["name"]: m["expression"] for m in cubes["shipped_orders"]["measures"]}
    assert shipped["shipped_count"] == "COUNT(CASE WHEN (status = 'shipped') THEN 1 END)"
    assert shipped["shipped_amount"] == "SUM(CASE WHEN (status = 'shipped') THEN amount END)"
    assert shipped["distinct_customers"] == "COUNT(DISTINCT customer_id)"
    assert shipped["mean_amount"] == "AVG(amount)" and shipped["smallest"] == "MIN(amount)"
    # a number measure inlines the measures it references (inherited ones too)
    assert shipped["shipped_share"] == (
        "(COUNT(CASE WHEN (status = 'shipped') THEN 1 END)) * 1.0 / NULLIF((COUNT(*)), 0)")
    assert [d["name"] for d in cubes["shipped_orders"]["dimensions"]] == ["order_id", "status"]
    # a sql cube becomes a refSql model; the sql_table ones a table reference
    models = {m["name"]: m for m in sem.mdl["models"]}
    assert models["order_lines"]["refSql"].startswith("SELECT o.order_id, p.amount FROM")
    assert "tableReference" in models["shipped_orders"]
    # the unsupported members are notes (and warnings), never queryable
    notes = "\n".join(sem.notes)
    for needle in ("tricky.running", "tricky.share_of_other", "tricky.median_amount",
                   "tricky.region", "tricky.owner_name", "tricky.place", "tricky.latest"):
        assert needle in notes, needle
    assert "region" not in {d["name"] for d in cubes["tricky"].get("dimensions", [])}
    assert [m["name"] for m in cubes["tricky"]["measures"]] == ["payments"]
    assert "dynamic" not in cubes  # a dynamic sql_table is skipped
    assert "order_count" in sem.queryable_metrics and "shipped_count" in sem.queryable_metrics


def test_nothing_queryable_gives_no_semantic_model_and_bad_input_never_raises() -> None:
    f = SourceFiles({"models/a.yml": b"cubes:\n  - name: c\n    sql_table: s.t\n"})
    snap = adapter.load(CubeConfig(), f)
    assert snap.semantic_models == [] and [a.fqn for a in snap.assets] == ["s.t"]
    junk = SourceFiles({"models/x.yml": b"- just\n- a list\n", "models/y.yml": b"\xff\xfe",
                        "models/z.yml": b"", "models/w.yml": b"cubes: [not-a-mapping]"})
    snap = adapter.load(CubeConfig(), junk)
    assert snap.assets == [] and {"invalid_yaml", "invalid_node"} <= codes(snap)
    with pytest.raises(ValueError, match="models"):
        adapter.load(CubeConfig(), SourceFiles({}))
    # extends: unknown and circular bases are warnings
    cyc = (b"cubes:\n  - name: a\n    extends: b\n    sql_table: s.a\n"
           b"  - name: b\n    extends: a\n    sql_table: s.b\n"
           b"  - name: c\n    extends: nope\n    sql_table: s.c\n")
    snap = adapter.load(CubeConfig(), SourceFiles({"models/c.yml": cyc}))
    assert [w.code for w in snap.warnings].count("extends") == 2  # b in the a<->b cycle, c
    # default_schema qualifies a bare sql_table; three-part names keep the database
    bare = SourceFiles({"models/a.yml": b"cubes:\n  - name: a\n    sql_table: orders\n"
                                        b"  - name: b\n    sql_table: db.sch.t\n"})
    snap = adapter.load(CubeConfig(default_schema="analytics"), bare)
    assert sorted((a.fqn, a.database) for a in snap.assets) == [
        ("analytics.orders", None), ("sch.t", "db")]
    # JSON-pointing camelCase spellings are accepted
    camel = SourceFiles({"models/a.yml": (
        b"cubes:\n  - name: a\n    sqlTable: s.a\n    dimensions:\n      - name: id\n"
        b"        sql: id\n        type: number\n        primaryKey: true\n")})
    assert adapter.load(CubeConfig(), camel).assets[0].grain == ["id"]


def test_two_cubes_with_the_same_measure_name_are_qualified() -> None:
    f = SourceFiles({"models/a.yml": (
        b"cubes:\n"
        b"  - name: a\n    sql_table: s.a\n    measures:\n      - name: n\n        type: count\n"
        b"  - name: b\n    sql_table: s.b\n    measures:\n      - name: n\n        type: count\n"
    )})
    (sem,) = adapter.load(CubeConfig(), f).semantic_models
    assert sem.queryable_metrics == ["a.n", "b.n"]


# ---- /meta: a saved response, and the network path with an injected fetch ---------------------


def test_meta_file_enriches_descriptions_and_imports_js_only_cubes_as_terms() -> None:
    meta = (FIX / "meta.json").read_bytes()
    stripped = (MODEL / "cubes" / "payments.yml").read_text().replace(
        "description: Number of payments of any outcome.\n", "")
    base = files(**{"models/cubes/payments.yml": stripped.encode()})
    without = load(base)
    with_meta = adapter.load(CubeConfig(), SourceFiles(
        {n: base.read(n) for n in base.names()} | {"meta": meta}))
    cube_of = lambda snap: {c["name"]: c for c in snap.semantic_models[0].mdl["cubes"]}  # noqa: E731
    pay = lambda snap: {m["name"]: m for m in cube_of(snap)["payments"]["measures"]}  # noqa: E731
    assert "description" not in pay(without)["payment_count"]
    assert pay(with_meta)["payment_count"]["description"] == "Number of payments of any outcome."
    # the YAML wins where both describe a member
    assert pay(with_meta)["collected"]["description"] == "Payments that succeeded, in AUD dollars."
    # a cube only /meta knows (a JavaScript model) becomes glossary terms, with a warning
    assert {t.term for t in with_meta.terms} == {
        "legacy_refunds", "legacy_refunds.refunded_total", "legacy_refunds.reason"}
    assert "cube_meta_only" in codes(with_meta) and "cube_meta_only" not in codes(without)
    assert not any(a.fqn.startswith("legacy") for a in with_meta.assets)
    assert with_meta.fingerprint != without.fingerprint
    # views in /meta repeat their cubes' members and add nothing
    assert not any("orders_view" in t.term for t in with_meta.terms)
    assert "unreadable_input" in codes(adapter.load(CubeConfig(), SourceFiles(
        {n: base.read(n) for n in base.names()} | {"meta": b"{nope"})))
    assert "unreadable_input" in codes(adapter.load(CubeConfig(), SourceFiles(
        {n: base.read(n) for n in base.names()} | {"meta": b'{"cubes": 5}'})))
    only_meta = adapter.load(CubeConfig(), SourceFiles({"meta": meta}))
    assert only_meta.assets == [] and only_meta.terms


def test_meta_url_uses_the_injected_fetch_with_token_and_fails_loudly() -> None:
    calls: list[tuple[str, str | None]] = []
    meta = (FIX / "meta.json").read_bytes()

    def fetch(url: str, token: str | None) -> bytes:
        calls.append((url, token))
        return meta

    a = CubeAdapter(fetch=fetch)
    snap = a.load(CubeConfig(meta_url="http://cube.local:4000", meta_token="s3cret"), files())
    assert calls == [("http://cube.local:4000", "s3cret")]
    assert "cube_meta_only" in {w.code for w in snap.warnings}
    again = a.load(CubeConfig(meta_url="http://cube.local:4000", meta_token="s3cret"), files())
    assert again.canonical_json() == snap.canonical_json()  # deterministic given the response
    assert snap.fingerprint != load().fingerprint
    # the token can come from the environment; a missing variable is an error
    import os

    os.environ["CUBE_TEST_TOKEN"] = "from-env"
    try:
        a.load(CubeConfig(meta_url="http://c", meta_token_env="CUBE_TEST_TOKEN"), files())
    finally:
        del os.environ["CUBE_TEST_TOKEN"]
    assert calls[-1] == ("http://c", "from-env")
    with pytest.raises(ValueError, match="CUBE_TEST_TOKEN"):
        a.load(CubeConfig(meta_url="http://c", meta_token_env="CUBE_TEST_TOKEN"), files())

    def boom(url: str, token: str | None) -> bytes:
        raise ValueError(f"cannot reach Cube /meta at {url}: refused")

    with pytest.raises(ValueError, match="cannot reach Cube"):
        CubeAdapter(fetch=boom).load(CubeConfig(meta_url="http://c"), files())
    # the real fetch: endpoint normalisation, scheme check and a network error is reported
    assert meta_endpoint("http://h:4000/") == "http://h:4000/cubejs-api/v1/meta"
    assert meta_endpoint("https://h/cubejs-api/v1") == "https://h/cubejs-api/v1/meta"
    assert meta_endpoint("https://h/cubejs-api/v1/meta") == "https://h/cubejs-api/v1/meta"
    with pytest.raises(ValueError, match="http"):
        http_fetch("file:///etc/passwd", None)
    with _no_network(), pytest.raises(AssertionError, match="network"):
        http_fetch("http://localhost:1", None)


def test_the_adapter_does_not_touch_the_network_unless_meta_url_is_set() -> None:
    assert "network" in adapter.info.capabilities  # declared, because meta_url may use it
    with _no_network():
        load()
        load(edge())
        adapter.load(CubeConfig(), SourceFiles({"models/a.yml": b"cubes: []", "meta": b"{}"}))


def test_registry_and_cli() -> None:
    entries = discover()
    assert entries["cube"].builtin and entries["cube"].info.maturity == "beta"
    assert entries["cube"].available
    runner = CliRunner()
    from satchel.cli.main import app as cli

    out = runner.invoke(cli, ["source", "validate", "cube", "--models", str(MODEL)])
    assert out.exit_code == 0, out.output
    assert "analytics.orders" in out.output and "revenue" in out.output
    js = runner.invoke(cli, ["source", "validate", "cube", "--models", str(MODEL), "--json"])
    assert json.loads(js.output)["kind"] == "cube"
    assert runner.invoke(cli, ["source", "validate", "cube"]).exit_code == 1  # nothing to read
    info = runner.invoke(cli, ["source", "info", "cube"])
    assert "--models (file or folder)" in info.output and "--meta-url" in info.output
    assert runner.invoke(cli, ["source", "validate", "cube", "--models", str(MODEL),
                               "--meta-url", "ftp://x"]).exit_code == 1
