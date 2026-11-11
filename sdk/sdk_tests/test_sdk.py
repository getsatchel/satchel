from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from pydantic import ValidationError

from satchel_adapter_sdk import (
    IR_VERSION,
    MdlBuilder,
    SourceAsset,
    SourceFiles,
    SourceQuery,
    SourceSemanticModel,
    SourceSnapshot,
    is_compatible,
    normalise_fqn,
    validate_mdl,
)
from satchel_adapter_sdk.testing import (
    ConformanceError,
    assert_conformant,
    check_golden,
    check_no_network,
    check_no_server_imports,
)
from sdk_tests.toy import ToyAdapter, ToyConfig, adapter

FILES = SourceFiles(
    {"export": json.dumps({"tables": [{"name": "orders", "doc": "Orders", "columns": ["id"]}]}).encode()}
)
CONFIG = ToyConfig(export="export.json")


def test_normalise_fqn() -> None:
    assert normalise_fqn('"JAFFLE"."Analytics"."Orders"') == "jaffle.analytics.orders"
    assert normalise_fqn("`proj.ds.tbl`") == "proj.ds.tbl"
    assert normalise_fqn("[dbo].[Orders]") == "dbo.orders"
    assert normalise_fqn("Schema.Table") == "schema.table"


def test_ir_version_policy() -> None:
    assert is_compatible(IR_VERSION)
    major = IR_VERSION.split(".")[0]
    assert is_compatible(f"{major}.99.0")
    assert not is_compatible(f"{int(major) + 1}.0.0")
    assert not is_compatible("garbage")
    with pytest.raises(ValidationError):
        SourceSnapshot(kind="x", fingerprint="f", ir_version="nope")


def test_ir_is_strict_and_validated() -> None:
    with pytest.raises(ValidationError):
        SourceAsset(key="k", fqn="a.b", desription="typo")  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="exactly one"):
        SourceQuery(key="q", question="?", sql="select 1", metric_query={"metrics": ["x"]})
    with pytest.raises(ValidationError, match="invalid MDL"):
        SourceSemanticModel(key="s", name="s", mdl={"models": [], "cubes": [{"name": "c", "baseObject": "x"}]})


def test_mdl_builder_and_validation() -> None:
    b = MdlBuilder(data_source="duckdb")
    b.model("orders", schema="a", table="orders", columns={"amount": "integer"})
    b.model("customers", ref_sql="select 1 as id", columns={"id": "integer"})
    b.relationship("r", ("orders", "customers"), "MANY_TO_ONE", "orders.id = customers.id")
    b.cube("sales", base="orders", measures=[("revenue", "SUM(amount)", "BIGINT", "Revenue")])
    mdl = b.build()
    assert validate_mdl(mdl) == []
    assert mdl["cubes"][0]["measures"][0]["description"] == "Revenue"

    bad = MdlBuilder()
    bad.model("m", columns={"a": "int"})  # neither table nor refSql
    with pytest.raises(ValueError, match="tableReference or refSql"):
        bad.build()
    assert any("unknown baseObject" in e for e in validate_mdl({"models": [], "cubes": [
        {"name": "c", "baseObject": "nope", "measures": [{"name": "m", "expression": "1", "type": "int"}]}]}))
    dup = {"models": [{"name": "m", "tableReference": {"table": "t"}}] * 2}
    assert any("duplicate model" in e for e in validate_mdl(dup))


def test_source_files() -> None:
    f = SourceFiles({"a": b"1", "b": b"2"})
    assert f.names() == ["a", "b"] and f.has("a") and not f.has("c")
    assert f.fingerprint() == SourceFiles({"b": b"2", "a": b"1"}).fingerprint()
    assert f.fingerprint(["a"]) != f.fingerprint(["b"])
    with pytest.raises(FileNotFoundError):
        f.read("c")


def test_multi_file_inputs() -> None:
    from satchel_adapter_sdk import AdapterInfo, FileInput

    f = SourceFiles({"models/b/y.yml": b"2", "models/a.yml": b"1", "other": b"3", "modelsx/z": b"4"})
    assert f.group("models") == {"a.yml": b"1", "b/y.yml": b"2"}  # sorted, prefix is a folder
    assert list(f.group("models")) == ["a.yml", "b/y.yml"] and f.has_group("models")
    assert not f.has_group("nothing") and f.group("other") == {"other": b"3"}
    assert f.fingerprint(f.names()) == f.fingerprint()  # fingerprints cover every file
    fi = FileInput(name="models", multiple=True, extensions=(".yml",))
    assert fi.multiple and fi.extensions == (".yml",) and not FileInput(name="x").multiple
    info = AdapterInfo(kind="opt", display_name="Opt", version="1.0.0",
                       capabilities=frozenset({"assets"}), config_model=ToyConfig,
                       requires=("some_optional_module",), extras=("opt",))
    assert info.requires == ("some_optional_module",) and AdapterInfo.model_fields["requires"].default == ()


def test_toy_adapter_is_conformant(tmp_path: Path) -> None:
    golden = tmp_path / "golden" / "snapshot.json"
    with pytest.raises(AssertionError, match="was created"):
        assert_conformant(adapter, CONFIG, FILES, golden=golden, update_golden=False)
    snap = assert_conformant(adapter, CONFIG, FILES, golden=golden, update_golden=False)
    assert snap.summary()["assets"] == 1 and snap.summary()["metrics"] == 1
    # a changed snapshot fails the golden comparison until --update-golden
    other = SourceFiles({"export": json.dumps({"tables": [{"name": "orders", "doc": "Changed"}]}).encode()})
    with pytest.raises(ConformanceError, match="differs from"):
        assert_conformant(adapter, CONFIG, other, golden=golden, update_golden=False)
    assert_conformant(adapter, CONFIG, other, golden=golden, update_golden=True)
    assert "Changed" in golden.read_text()


def test_conformance_catches_nondeterminism_and_network() -> None:
    class Flaky(ToyAdapter):
        def load(self, config: ToyConfig, files: SourceFiles) -> SourceSnapshot:
            snap = super().load(config, files)
            snap.warnings.clear()
            from satchel_adapter_sdk import SourceWarning

            snap.warnings.append(SourceWarning(code="r", message=str(random.random())))
            return snap

    with pytest.raises(ConformanceError, match="deterministic"):
        assert_conformant(Flaky(), CONFIG, FILES)

    class Online(ToyAdapter):
        def load(self, config: ToyConfig, files: SourceFiles) -> SourceSnapshot:
            import socket

            socket.getaddrinfo("example.com", 80)
            return super().load(config, files)

    with pytest.raises(AssertionError, match="network"):
        check_no_network(Online(), CONFIG, FILES)

    class Unregistered(ToyAdapter):
        def load(self, config: ToyConfig, files: SourceFiles) -> SourceSnapshot:
            snap = super().load(config, files)
            snap.assets[0].fqn = "Shop.Orders"  # not normalised
            return snap

    with pytest.raises(ConformanceError, match="not normalised"):
        assert_conformant(Unregistered(), CONFIG, FILES)


def test_no_server_imports_check(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    class Sneaky(ToyAdapter):
        def load(self, config: ToyConfig, files: SourceFiles) -> SourceSnapshot:
            sys.modules["sqlalchemy"] = types.ModuleType("sqlalchemy")
            return super().load(config, files)

    monkeypatch.delitem(sys.modules, "sqlalchemy", raising=False)
    try:
        with pytest.raises(AssertionError, match="server-side modules"):
            check_no_server_imports(Sneaky(), CONFIG, FILES)
    finally:
        sys.modules.pop("sqlalchemy", None)


def test_golden_flag_via_pytester(pytester: pytest.Pytester) -> None:
    pytester.makeconftest('pytest_plugins = ["satchel_adapter_sdk.pytest_plugin"]')
    pytester.makepyfile("def test_flag(update_golden):\n    assert update_golden is True\n")
    pytester.runpytest("--update-golden").assert_outcomes(passed=1)
    pytester.runpytest().assert_outcomes(failed=1)


def test_check_golden_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snap = adapter.load(CONFIG, FILES)
    g = tmp_path / "g.json"
    monkeypatch.setenv("SATCHEL_UPDATE_GOLDEN", "1")
    check_golden(snap, g)
    monkeypatch.delenv("SATCHEL_UPDATE_GOLDEN")
    check_golden(snap, g)
