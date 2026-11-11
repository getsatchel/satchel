"""The SQLMesh source adapter (a static reader: no sqlmesh import, nothing executed): conformance
kit on the jaffle-shop project and an edge-case project, the MODEL scanner and value parser, the
mapping rules (grain, audits, kinds, lineage, references, Python models) and tolerance for macros
and broken files."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from satchel_adapter_sdk import SourceFiles, SourceSnapshot
from satchel_adapter_sdk.testing import assert_conformant
from typer.testing import CliRunner

from satchel.sources.adapters.sqlmesh import SqlmeshConfig, adapter
from satchel.sources.adapters.sqlmesh import parse as P
from satchel.sources.config import SourceConfigError, read_files, validate_config
from satchel.sources.registry import discover, get_entry
from tests.helpers import EXAMPLE

FIX = Path(__file__).parent / "fixtures" / "sqlmesh"
EDGE = FIX / "project"
JAFFLE = EXAMPLE / "sqlmesh"
EXTS = (".sql", ".py", ".yaml", ".yml")


def tree(root: Path, drop: tuple[str, ...] = (), **extra: bytes) -> SourceFiles:
    data = {f"project/{p.relative_to(root).as_posix()}": p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix in EXTS and not p.name.startswith(".")
            and "logs" not in p.parts and not any(d in p.as_posix() for d in drop)}
    data.update({f"project/{k}": v for k, v in extra.items()})
    return SourceFiles(data)


def load(root: Path = JAFFLE, **cfg: Any) -> SourceSnapshot:
    return adapter.load(SqlmeshConfig(**cfg), tree(root))


def asset(snap: SourceSnapshot, fqn: str) -> Any:
    return next(a for a in snap.assets if a.fqn == fqn)


def codes(snap: SourceSnapshot) -> dict[str, int]:
    out: dict[str, int] = {}
    for w in snap.warnings:
        out[w.code] = out.get(w.code, 0) + 1
    return out


def test_conformance_kit_on_both_projects(update_golden: bool) -> None:
    snap = assert_conformant(adapter, SqlmeshConfig(), tree(JAFFLE),
                             golden=FIX / "golden" / "jaffle.json", update_golden=update_golden)
    assert snap.summary()["assets"] == 4 and snap.summary()["warnings"] == 0
    edge = assert_conformant(adapter, SqlmeshConfig(), tree(EDGE),
                             golden=FIX / "golden" / "edge.json", update_golden=update_golden)
    assert edge.summary()["assets"] == 5


def test_jaffle_models_columns_audits_grain_and_lineage() -> None:
    snap = load()
    orders = asset(snap, "analytics.orders")
    assert orders.key == "model.analytics.orders" and orders.kind == "table"
    assert orders.owner == "data-team@jaffle.example" and orders.tags == ["core", "finance"]
    assert orders.grain == ["order_id"] and orders.description.startswith("One row per customer")
    cols = {c.name: c for c in orders.columns}
    assert [c.name for c in orders.columns] == sorted(cols)  # deterministic order
    assert cols["amount"].data_type == "int" and cols["amount"].description.startswith("Order total")
    assert cols["order_id"].unique and cols["order_id"].not_null  # unique_values + not_null
    assert cols["status"].not_null and not cols["amount"].not_null
    assert cols["status"].enum_values == ["placed", "shipped", "completed", "return_pending",
                                          "returned"]  # accepted_values
    # references (customer_id): a column named like another model's grain is a join hint
    assert [(r.column, r.to_asset, r.to_column) for r in orders.relationships] == [
        ("customer_id", "analytics.customers", "customer_id")]
    assert asset(snap, "analytics.payments").relationships[0].to_asset == "analytics.orders"
    assert orders.source_meta["kind"] == "FULL" and "unique_values" in orders.source_meta["audits"]
    # a VIEW with no declared columns: names come from the query; lineage from FROM/JOIN
    mr = asset(snap, "analytics.monthly_revenue")
    assert mr.kind == "view" and mr.grain == ["month"]  # unique_combination_of_columns
    assert [c.name for c in mr.columns] == ["month", "order_count", "revenue"]
    assert mr.depends_on == ["model.analytics.customers", "model.analytics.orders"]
    assert orders.depends_on == []  # raw.orders is not a model
    cust = asset(snap, "analytics.customers")
    assert cust.grain == ["customer_id"] and "customer's first order" in {
        c.name: c for c in cust.columns}["first_order_date"].description  # '' unescaped
    payments = asset(snap, "analytics.payments")
    assert {c.name: c for c in payments.columns}["status"].enum_values == [
        "succeeded", "failed", "refunded"]  # two accepted_values audits, one per column
    assert {c.name: c for c in payments.columns}["payment_method"].enum_values[0] == "credit_card"
    assert payments.owner is None


def test_edge_project_kinds_defaults_macros_python_and_broken_files() -> None:
    snap = load(EDGE)
    assert sorted(a.fqn for a in snap.assets) == [
        "sales.daily", "sales.dim_customer", "sales.enriched", "sales.orders", "sales.regions"]
    # EMBEDDED (inlined) and `enabled false` models are not assets
    assert {"sales.inlined", "sales.old"}.isdisjoint(a.fqn for a in snap.assets)
    # a 3-part name keeps the database; the incremental kind, cron, start and time column are kept
    orders = asset(snap, "sales.orders")
    assert orders.key == "model.warehouse.sales.orders" and orders.database == "warehouse"
    assert orders.source_meta["kind"] == "INCREMENTAL_BY_TIME_RANGE"
    assert orders.source_meta["time_column"] == "order_ts" and orders.source_meta["cron"] == "@daily"
    assert orders.source_meta["start"] == "2024-01-01" and orders.source_meta["dialect"] == "snowflake"
    # comments, a ')' and '--' inside strings and a trailing comment do not break the scanner
    assert orders.description == (
        'Order lines (one per item). Joined to dim_customer; a ")" and -- are fine here.')
    assert orders.tags == ["pii (some)", "sales"]
    # grains ((a, b), (a)): the first; the filtered accepted_values audit is ignored
    assert orders.grain == ["order_id", "line_no"]
    cols = {c.name: c for c in orders.columns}
    assert sorted(cols) == ["channel", "customer_name", "line_no", "order_id", "order_ts"]
    assert cols["order_id"].not_null  # not_null_non_blocking counts
    assert cols["channel"].enum_values == ["web", "store", "phone"]
    assert cols["channel"].description == "Where it was bought."
    # columns come from the query when none are declared (CTE and aliases resolved), lineage from
    # joins and depends_on
    assert orders.depends_on == ["model.warehouse.sales.dim_customer"]
    # declared types incl. parameterised ones; `NOT NULL` is a fact, not part of the type
    dim = asset(snap, "sales.dim_customer")
    dcols = {c.name: c for c in dim.columns}
    assert dcols["price"].data_type == "decimal(10, 2)" and dcols["created"].data_type == (
        "timestamp with time zone")
    assert dcols["name"].data_type == "text" and dcols["name"].not_null
    assert dim.grain == ["id"] and dcols["id"].unique and dcols["id"].not_null
    assert dim.owner == "default-owner@example.com"  # model_defaults.owner
    # a Python model is read with ast, never executed
    enr = asset(snap, "sales.enriched")
    assert enr.source_meta["python"] is True and enr.owner == "ds@example.com"
    assert enr.grain == ["order_id"] and enr.tags == ["ml"]
    ecols = {c.name: c for c in enr.columns}
    assert ecols["score"].data_type == "double" and ecols["score"].description.startswith("Model")
    assert enr.source_meta["kind"] == "FULL"
    # macros: the query is not parsed (a warning), lineage still read from FROM/JOIN
    daily = asset(snap, "sales.daily")
    assert daily.kind == "view" and daily.columns == []
    assert daily.depends_on == ["model.warehouse.sales.orders"]
    # a seed has declared columns; of two files defining one model the first (by path) wins
    regions = asset(snap, "sales.regions")
    assert regions.source_meta["kind"] == "SEED" and [c.name for c in regions.columns] == [
        "code", "label"]
    assert codes(snap) == {"invalid_model": 2, "query_unparsed": 1, "duplicate_model": 1}
    msgs = "\n".join(w.message for w in snap.warnings)
    assert "broken.sql" in msgs and "no_name.sql" in msgs and "z_dup.sql" in msgs
    # audits/ and macros/ are not models
    assert not any("my_audit" in a.key or "helpers" in a.key for a in snap.assets)


def test_scanner_and_value_parser() -> None:
    sql = """
    -- leading comment
    MODEL (
      name a."B".c,
      kind INCREMENTAL_BY_UNIQUE_KEY (unique_key (id, tenant), lookback 3),
      tags ['x', "y"], flag true,
      description 'it''s -- not a comment, and has ) inside',
      column_descriptions (a = 'one', "b c" = 'two'),
      audits (not_null(columns := (a)), accepted_values(column := s, is_in := ('p', 'q')), bare_audit)
    );
    SELECT 1 AS a
    """
    block = P.parse_model(sql)
    p = block.props
    assert p["name"] == 'a."B".c' and p["flag"] is True
    assert p["description"] == "it's -- not a comment, and has ) inside"
    assert p["tags"] == ["x", "y"]
    assert p["column_descriptions"] == {"a": "one", "b c": "two"}
    kind = p["kind"]
    assert isinstance(kind, P.Call) and kind.name == "INCREMENTAL_BY_UNIQUE_KEY"
    assert kind.kwargs == {"unique_key": ["id", "tenant"], "lookback": 3}
    audits = p["audits"]
    assert [a.name if isinstance(a, P.Call) else a for a in audits] == [
        "not_null", "accepted_values", "bare_audit"]
    assert audits[0].kwargs == {"columns": ["a"]} and audits[1].kwargs["is_in"] == ["p", "q"]
    assert block.query == "SELECT 1 AS a"
    assert P.parse_columns("(a INT, b DECIMAL(3, 1) NOT NULL, `c d` TEXT)") == [
        ("a", "INT", False), ("b", "DECIMAL(3, 1)", True), ("c d", "TEXT", False)]
    assert P.has_model_block("MODEL (name x);") and not P.has_model_block("SELECT 1")
    assert not P.has_model_block("-- MODEL (name x)\nSELECT 1")
    with pytest.raises(P.ParseError):
        P.parse_model("MODEL (name x")
    with pytest.raises(P.ParseError):
        P.python_models("def (")
    assert P.python_models("@model('a.b', owner='o', columns={'x': 'int'})\ndef f(): pass\n") == [
        {"name": "a.b", "owner": "o", "columns": {"x": "int"}}]
    # python code is never executed
    assert P.python_models("raise SystemExit\n@model('a.b')\ndef f(): pass\n")[0]["name"] == "a.b"


def test_empty_and_unreadable_projects_warn_not_fail() -> None:
    snap = adapter.load(SqlmeshConfig(), SourceFiles({"project/x.sql": b"SELECT 1"}))
    assert snap.assets == [] and codes(snap) == {"no_models": 1}
    bad = SourceFiles({"project/config.yaml": b"a: [unclosed", "project/m.sql": b"\xff\xfe",
                       "project/ok.sql": b"MODEL (name s.t);\nSELECT 1 AS x"})
    snap = adapter.load(SqlmeshConfig(dialect="duckdb"), bad)
    assert [a.fqn for a in snap.assets] == ["s.t"] and "invalid_config" in codes(snap)
    assert asset(snap, "s.t").source_meta["dialect"] == "duckdb"  # the config dialect applies
    entry = get_entry("sqlmesh")  # the project folder is a required input
    with pytest.raises(SourceConfigError, match="project is required"):
        read_files(entry, validate_config(entry, {})[1])


def test_the_adapter_never_imports_sqlmesh_or_runs_project_code() -> None:
    assert "sqlmesh" not in sys.modules  # not even installed in the satchel environment
    assert adapter.info.requires == () and "network" not in adapter.info.capabilities
    load(EDGE)  # the Python model raises if executed; the macro file is never imported
    assert "sqlmesh" not in sys.modules


def test_registry_and_cli() -> None:
    e = discover()["sqlmesh"]
    assert e.builtin and e.available and e.info.maturity == "beta"
    from satchel.cli.main import app as cli

    runner = CliRunner()
    out = runner.invoke(cli, ["source", "validate", "sqlmesh", "--project", str(JAFFLE)])
    assert out.exit_code == 0, out.output
    assert "analytics.orders" in out.output and "assets           4" in out.output
    assert json.loads(runner.invoke(cli, ["source", "validate", "sqlmesh", "--project",
                                          str(JAFFLE), "--json"]).output)["kind"] == "sqlmesh"
    assert runner.invoke(cli, ["source", "validate", "sqlmesh"]).exit_code == 1
    assert "--project (file or folder)" in runner.invoke(cli, ["source", "info", "sqlmesh"]).output
    kinds = runner.invoke(cli, ["source", "kinds"]).output
    assert "sqlmesh" in kinds and "unavailable" not in kinds
