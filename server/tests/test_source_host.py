"""Host-side support for adapters added in M7b: folder inputs (``FileInput.multiple``) and
adapters that need an optional dependency (listed as unavailable with an install hint)."""

from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict
from satchel_adapter_sdk import AdapterInfo, FileInput, SourceFiles, SourceSnapshot
from typer.testing import CliRunner

from satchel.cli.main import app as cli
from satchel.sources.config import SourceConfigError, read_files, validate_config
from satchel.sources.registry import AdapterEntry, discover, get_entry


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: str | None = None
    note: str | None = None


class _Folder:
    info = AdapterInfo(
        kind="folder", display_name="Folder", version="0.1.0",
        capabilities=frozenset({"assets"}), config_model=_Cfg,
        file_inputs=(
            FileInput(name="models", required=True, multiple=True, extensions=(".yml", ".yaml")),
            FileInput(name="note"),
        ),
    )

    def load(self, config: _Cfg, files: SourceFiles) -> SourceSnapshot:
        return SourceSnapshot(kind="folder", fingerprint=files.fingerprint())


class _Optional:
    info = AdapterInfo(
        kind="needs_dep", display_name="Needs a dependency", version="2.0.0",
        capabilities=frozenset({"assets"}), config_model=_Cfg, extras=("needs_dep",),
        requires=("satchel_surely_not_installed_xyz",),
    )

    def load(self, config: _Cfg, files: SourceFiles) -> SourceSnapshot:  # pragma: no cover
        raise AssertionError("must not be called when the dependency is missing")


FOLDER, OPTIONAL = _Folder(), _Optional()


def _entry(adapter: _Folder) -> AdapterEntry:
    return AdapterEntry("folder", False, "satchel-source-folder", "0.1.0", adapter)


def test_folder_inputs_are_read_recursively_and_filtered(tmp_path: Path) -> None:
    root = tmp_path / "model"
    (root / "cubes" / "nested").mkdir(parents=True)
    (root / "views").mkdir()
    (root / ".hidden").mkdir()
    (root / "node_modules" / "pkg").mkdir(parents=True)
    for rel in ("cubes/orders.yml", "cubes/nested/b.yaml", "views/v.yml", ".hidden/x.yml",
                "node_modules/pkg/y.yml", "cubes/readme.md", "cubes/.dot.yml"):
        (root / rel).write_text(rel)
    entry = _entry(FOLDER)
    _, cfg = validate_config(entry, {"models": str(root)})
    files = read_files(entry, cfg)
    assert files.names() == ["models/cubes/nested/b.yaml", "models/cubes/orders.yml",
                             "models/views/v.yml"]
    assert files.group("models") == {
        "cubes/nested/b.yaml": b"cubes/nested/b.yaml", "cubes/orders.yml": b"cubes/orders.yml",
        "views/v.yml": b"views/v.yml"}
    # a single file is accepted too, named by its file name
    single = read_files(entry, validate_config(entry, {"models": str(root / "views" / "v.yml")})[1])
    assert single.group("models") == {"v.yml": b"views/v.yml"}
    # relative paths resolve against base_dir
    rel = read_files(entry, validate_config(entry, {"models": "model"})[1], base_dir=tmp_path)
    assert rel.names() == files.names()
    # uploads win and use the same names; a plain upload is stored under its input name
    up = read_files(entry, validate_config(entry, {"models": str(root)})[1],
                    uploads={"models/a.yml": b"A", "models/sub/b.yml": b"B"})
    assert up.names() == ["models/a.yml", "models/sub/b.yml"]
    # fingerprints see every file
    assert files.fingerprint() != rel.fingerprint(["models/views/v.yml"])
    # a missing or empty required folder is a clear error
    with pytest.raises(SourceConfigError, match="cannot read models"):
        read_files(entry, validate_config(entry, {"models": str(tmp_path / "nope")})[1])
    (tmp_path / "empty").mkdir()
    with pytest.raises(SourceConfigError, match="missing required"):
        read_files(entry, validate_config(entry, {"models": str(tmp_path / "empty")})[1])
    with pytest.raises(SourceConfigError, match="models is required"):
        read_files(entry, validate_config(entry, {})[1])


def test_unavailable_adapter_is_listed_with_an_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eps = [EntryPoint("needs_dep", f"{__name__}:OPTIONAL", "satchel.sources"),
           EntryPoint("folder", f"{__name__}:FOLDER", "satchel.sources")]
    monkeypatch.setattr("satchel.sources.registry.entry_points", lambda group: eps)
    found = discover()
    e = found["needs_dep"]
    assert e.adapter is OPTIONAL and not e.available
    assert e.missing == ["satchel_surely_not_installed_xyz"]
    assert e.install_hint is not None and "needs_dep" in e.install_hint
    assert found["folder"].available and found["folder"].install_hint is None
    with pytest.raises(LookupError, match="unavailable.*satchel_surely_not_installed_xyz.*pip"):
        get_entry("needs_dep")
    assert get_entry("needs_dep", require_available=False).info.version == "2.0.0"

    out = CliRunner().invoke(cli, ["source", "kinds"])
    assert "needs_dep" in out.output and "unavailable" in out.output and "pip install" in out.output
    info = CliRunner().invoke(cli, ["source", "info", "needs_dep"])
    assert info.exit_code == 0 and "UNAVAILABLE" in info.output
    bad = CliRunner().invoke(cli, ["source", "validate", "needs_dep"])
    assert bad.exit_code == 1 and "unavailable" in bad.output
