"""The dbt source adapter, built as if third-party: conformance kit on real dbt artifacts, then
the mapping rules, tolerance for malformed/unsupported constructs, and the import boundary."""

from __future__ import annotations

import ast
import copy
import json
import sys
import tempfile
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import BaseModel, ConfigDict
from satchel_adapter_sdk import AdapterInfo, SourceFiles, SourceSnapshot
from satchel_adapter_sdk.testing import assert_conformant

from satchel.data.semantic import build_cube_query, compile_query
from satchel.sources.adapters.dbt import DbtConfig, adapter
from satchel.sources.registry import discover, get_entry
from tests.helpers import build_jaffle_db

FIX = Path(__file__).parent / "fixtures" / "dbt"
GOLDEN = FIX / "golden" / "snapshot.json"


def files(**override: Any) -> SourceFiles:
    data = {n: (FIX / f"{n}.json").read_bytes() for n in ("manifest", "semantic_manifest", "catalog")}
    for k, v in override.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v if isinstance(v, bytes) else json.dumps(v).encode()
    return SourceFiles(data)


def manifest() -> dict[str, Any]:
    return json.loads((FIX / "manifest.json").read_text())


def load(**override: Any) -> SourceSnapshot:
    return adapter.load(DbtConfig(), files(**override))


def asset(snap: SourceSnapshot, fqn: str) -> Any:
    return next(a for a in snap.assets if a.fqn == fqn)


def test_conformance_kit(update_golden: bool) -> None:
    snap = assert_conformant(adapter, DbtConfig(), files(), golden=GOLDEN,
                             update_golden=update_golden)
    assert snap.summary()["assets"] == 7 and snap.summary()["metrics"] == 3


def test_models_columns_tests_and_meta() -> None:
    snap = load()
    orders = asset(snap, "analytics.orders")
    assert orders.key == "model.jaffle_shop.orders" and orders.kind == "table"
    assert orders.database == "jaffle" and orders.owner == "data-team@jaffle.example"
    assert orders.endorsed is True and orders.grain == ["order_id"]  # meta.satchel.endorsed
    assert orders.caveats and "refund" in orders.caveats[0]
    cols = {c.name: c for c in orders.columns}
    assert cols["status"].enum_values == ["placed", "shipped", "completed", "return_pending",
                                          "returned"]
    assert cols["order_id"].unique and cols["order_id"].not_null
    assert cols["amount"].data_type == "integer" and cols["amount"].description
    assert [(r.column, r.to_asset, r.to_column) for r in orders.relationships] == [
        ("customer_id", "analytics.customers", "customer_id")]
    assert orders.used_by and "Weekly revenue dashboard" in orders.used_by[0]
    assert orders.depends_on == ["source.jaffle_shop.raw.orders"]
    customers = asset(snap, "analytics.customers")
    assert {c.name: c.pii for c in customers.columns}["email"] is True
    payments = asset(snap, "analytics.payments")
    assert payments.relationships[0].to_asset == "analytics.orders"
    assert {c.name: c for c in payments.columns}["payment_method"].enum_values[0] == "credit_card"
    # source tables are imported when documented (here via the source-level description)
    raw = asset(snap, "raw.orders")
    assert raw.key == "source.jaffle_shop.raw.orders" and raw.description.startswith("The transac")


def test_semantic_layer_and_saved_query() -> None:
    snap = load()
    (sem,) = snap.semantic_models
    assert sem.name == "jaffle_shop" and sem.queryable_metrics == [
        "average_order_value", "order_count", "revenue"]
    (q,) = snap.queries
    assert q.metric_query == {"metrics": ["revenue", "order_count"],
                              "dimensions": ["metric_time:month"]}


def test_metrics_query_matches_hand_written_path() -> None:
    """query_metrics over the dbt-derived MDL: March 2025 revenue 1716.15 over 25 orders (same
    numbers as the hand-written MDL). Metric names are identical to the hand-written model."""
    for catalog in (True, False):
        snap = load(catalog=None if not catalog else files().read("catalog"))
        mdl = snap.semantic_models[0].mdl
        with tempfile.TemporaryDirectory() as d:
            db = build_jaffle_db(Path(d) / "j.duckdb")
            con = duckdb.connect(str(db), read_only=True)
            march = [{"dimension": "order_date", "operator": "gte", "value": "2025-03-01"},
                     {"dimension": "order_date", "operator": "lt", "value": "2025-04-01"}]
            q = build_cube_query(mdl, ["revenue", "order_count", "average_order_value"], [],
                                 march, [], None)
            rev, n, aov = con.execute(compile_query(mdl, q, "duckdb").sql).fetchall()[0]
            assert (round(rev, 2), n) == (1716.15, 25) and aov == pytest.approx(rev / n)
            q = build_cube_query(mdl, ["revenue"], ["metric_time:month"], [], [], None)
            assert len(con.execute(compile_query(mdl, q, "duckdb").sql).fetchall()) > 10
            con.close()
        if not catalog:
            assert any(w.code == "types_guessed" for w in snap.warnings)


def test_semantic_manifest_optional_falls_back_to_manifest() -> None:
    snap = load(semantic_manifest=None)
    assert snap.semantic_models and snap.queries


def test_disabled_and_ephemeral_models_are_skipped() -> None:
    m = manifest()
    m["nodes"]["model.jaffle_shop.payments"]["config"]["enabled"] = False
    m["nodes"]["model.jaffle_shop.customers"]["config"]["materialized"] = "ephemeral"
    snap = load(manifest=m)
    fqns = {a.fqn for a in snap.assets}
    assert "analytics.payments" not in fqns and "analytics.customers" not in fqns
    assert "analytics.orders" in fqns


def test_malformed_and_unsupported_constructs_warn_not_fail() -> None:
    m = manifest()
    m["nodes"]["model.jaffle_shop.payments"]["columns"] = "not a mapping"  # invalid node
    m["nodes"]["model.jaffle_shop.metricflow_time_spine"].pop("schema", None)
    t = next(v for k, v in m["nodes"].items() if "accepted_values_orders" in k)
    t["config"]["where"] = "status <> 'x'"
    rel = next(v for k, v in m["nodes"].items() if k.startswith("test.") and "relationships_orders" in k)
    rel["test_metadata"]["kwargs"]["to"] = "ref('nope')"
    snap = load(manifest=m)
    codes = {w.code for w in snap.warnings}
    assert {"invalid_node", "no_relation", "filtered_test", "unresolved_ref"} <= codes
    assert asset(snap, "analytics.orders")  # the rest still imported
    assert {c.name: c for c in asset(snap, "analytics.orders").columns}["status"].enum_values == []


def test_unsupported_metric_types_are_notes() -> None:
    s = json.loads((FIX / "semantic_manifest.json").read_text())
    base = next(m for m in s["metrics"] if m["name"] == "revenue")
    cumulative = copy.deepcopy(base) | {"name": "running_revenue", "type": "cumulative"}
    conversion = copy.deepcopy(base) | {"name": "signup_conversion", "type": "conversion"}
    offset = copy.deepcopy(base) | {
        "name": "revenue_growth", "type": "derived",
        "type_params": {"expr": "revenue - prior",
                        "metrics": [{"name": "revenue"},
                                    {"name": "revenue", "alias": "prior", "offset_window": "1 month"}]},
    }
    cross = copy.deepcopy(base) | {
        "name": "revenue_us",
        "filter": {"where_filters": [{"where_sql_template": "{{ Dimension('customer__country') }} = 'US'"}]},
    }
    s["metrics"] += [cumulative, conversion, offset, cross]
    snap = load(semantic_manifest=s)
    (sem,) = snap.semantic_models
    assert sem.queryable_metrics == ["average_order_value", "order_count", "revenue"]
    text = "\n".join(sem.notes)
    for name in ("running_revenue", "signup_conversion", "revenue_growth", "revenue_us"):
        assert name in text
    assert sum(w.code == "metric_not_queryable" for w in snap.warnings) == 4


def test_unreadable_optional_inputs_and_old_schema_versions() -> None:
    snap = load(catalog=b"{not json", semantic_manifest=b"nope")
    assert {"unreadable_input"} <= {w.code for w in snap.warnings}
    assert snap.assets and snap.semantic_models  # semantic falls back to the manifest
    m = manifest()
    m["metadata"]["dbt_schema_version"] = "https://schemas.getdbt.com/dbt/manifest/v9.json"
    assert any(w.code == "manifest_version" for w in load(manifest=m).warnings)
    for v in (10, 11):  # older manifests: same tolerant reader
        m["metadata"]["dbt_schema_version"] = f"https://schemas.getdbt.com/dbt/manifest/v{v}.json"
        assert not any(w.code == "manifest_version" for w in load(manifest=m).warnings)
    with pytest.raises(ValueError, match="not valid JSON"):
        load(manifest=b"{broken")
    with pytest.raises(FileNotFoundError):
        adapter.load(DbtConfig(), SourceFiles({}))


def test_registry_lists_builtin_dbt() -> None:
    entries = discover()
    assert entries["dbt"].builtin and entries["dbt"].info.maturity == "beta"
    assert get_entry("dbt").adapter is adapter or get_entry("dbt").info.kind == "dbt"
    with pytest.raises(LookupError, match="unknown source kind"):
        get_entry("nope")


def test_builtin_adapters_import_only_sdk_stdlib_and_declared_deps() -> None:
    """Built-in adapters are written like third-party ones: no `satchel.*` server modules."""
    root = Path(__file__).resolve().parents[1] / "satchel" / "sources" / "adapters"
    stdlib = set(sys.stdlib_module_names)
    checked = 0
    for kind_dir in sorted(p for p in root.iterdir() if p.is_dir() and p.name != "__pycache__"):
        declared = {x.strip() for x in (kind_dir / "deps.txt").read_text().split() if x.strip()}
        for py in kind_dir.rglob("*.py"):
            for node in ast.walk(ast.parse(py.read_text())):
                mods: list[str] = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module]
                for mod in mods:
                    top = mod.split(".")[0]
                    if mod.startswith(f"satchel.sources.adapters.{kind_dir.name}"):
                        continue  # the adapter's own modules
                    assert top in stdlib or top == "satchel_adapter_sdk" or top in declared, (
                        f"{py.relative_to(root)} imports {mod!r}: adapters may import only the "
                        "standard library, satchel_adapter_sdk and packages in deps.txt"
                    )
            checked += 1
    assert checked >= 4


# ---- external adapters are discovered through the same entry-point group ---------------------


class _ExtConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ExternalAdapter:
    info = AdapterInfo(kind="ext", display_name="External", version="0.3.0", maturity="stable",
                       capabilities=frozenset({"assets"}), config_model=_ExtConfig)

    def load(self, config: _ExtConfig, files: SourceFiles) -> SourceSnapshot:
        return SourceSnapshot(kind="ext", fingerprint="f")


EXTERNAL = _ExternalAdapter()


def test_external_adapters_are_discovered_and_broken_ones_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eps = [
        EntryPoint("ext", f"{__name__}:EXTERNAL", "satchel.sources"),
        EntryPoint("broken", "no_such_module_xyz:adapter", "satchel.sources"),
        EntryPoint("wrongkind", f"{__name__}:EXTERNAL", "satchel.sources"),
    ]
    monkeypatch.setattr("satchel.sources.registry.entry_points", lambda group: eps)
    found = discover()
    assert not found["ext"].builtin and found["ext"].info.maturity == "stable"
    assert found["ext"].adapter is EXTERNAL
    assert found["broken"].adapter is None and "no_such_module_xyz" in (found["broken"].error or "")
    assert found["wrongkind"].adapter is None
    assert "entry point name" in (found["wrongkind"].error or "")
    from satchel.sources.registry import _instantiate

    assert isinstance(_instantiate(_ExternalAdapter), _ExternalAdapter)  # a class is instantiated
    assert found["dbt"].builtin  # the built-in fallback keeps dbt even if metadata is stale
    with pytest.raises(LookupError, match="failed to load"):
        get_entry("broken")
