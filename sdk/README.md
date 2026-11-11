# satchel-adapter-sdk

The small public contract for Satchel **source adapters**: packages that read an upstream tool's
metadata (dbt, Cube, SQLMesh, ...) and describe it in Satchel's canonical IR. The Satchel server turns
that IR into ordinary context items, keeps them in sync, and never lets a sync overwrite what
analysts added on top.

This distribution depends on `pydantic` and `pyyaml` only. It has no web framework, ORM or
database driver; an adapter must not need them either.

```python
from satchel_adapter_sdk import AdapterInfo, SourceAdapter, SourceFiles, SourceSnapshot
```

## What you write

An adapter is an object with an `info: AdapterInfo` manifest and a pure `load` method:

```python
class MyAdapter:
    info = AdapterInfo(kind="mytool", display_name="My tool", version="0.1.0",
                       maturity="experimental", capabilities=frozenset({"assets"}),
                       config_model=MyConfig, file_inputs=(FileInput(name="export", required=True),))

    def load(self, config: MyConfig, files: SourceFiles) -> SourceSnapshot: ...

adapter = MyAdapter()
```

Register it under the `satchel.sources` entry-point group in your `pyproject.toml`:

```toml
[project.entry-points."satchel.sources"]
mytool = "satchel_source_mytool:adapter"
```

`pip install satchel-source-mytool` is then enough; `satchel source list` shows it as *external*.
Start from [`templates/adapter/`](../templates/adapter) and read
[`docs/adapters/writing-an-adapter.md`](../docs/adapters/writing-an-adapter.md).

### Rules

* **Pure.** `load(config, files)` is deterministic and does no database access. It does no network
  access either, unless you declare the `network` capability. The host reads the input files
  (named by `AdapterInfo.file_inputs`, a path in the config or an upload) and hands you bytes.
* **No server imports.** Never import `satchel.*`, SQLAlchemy, a database driver or FastAPI. Built-in
  adapters are held to the same rule by a test.
* **Folders and optional dependencies.** `FileInput(multiple=True, extensions=...)` lets the path be a
  folder (read it with `SourceFiles.group(name)`); `AdapterInfo.requires` names the optional modules
  an adapter imports lazily, so the host can list it as unavailable with an install hint.
* **Normalised names.** Emit `schema.table` fqns in lower case (`normalise_fqn`). Put the database
  in `SourceAsset.database`.
* **Warnings, not failures.** A construct you cannot map goes into `SourceSnapshot.warnings`;
  one bad node must not fail the sync.
* **Trust.** Installed adapters run in the Satchel process as trusted code. Install only adapters you
  would run on the server.

## IR versioning

`satchel_adapter_sdk.IR_VERSION` is a semantic version carried by every snapshot.

| Change                                              | Bump  |
|-----------------------------------------------------|-------|
| New optional field, new optional model, new capability | minor |
| Removed or renamed field, changed meaning, new required field | major |

The server accepts a snapshot whose **major** equals its own SDK's major and refuses others with a
clear message. Within a major, the server ignores nothing it does not understand and requires
nothing new, so an adapter written against `1.0.0` keeps working on every `1.x`.

## Conformance kit

```python
from satchel_adapter_sdk.testing import assert_conformant

def test_conformance(update_golden):
    assert_conformant(adapter, MyConfig(export="..."), files,
                      golden=Path(__file__).parent / "golden" / "snapshot.json",
                      update_golden=update_golden)
```

It checks the manifest, that the snapshot validates and round-trips, the IR version, determinism
(two loads equal), a stable fingerprint, normalised fqns and unique keys, valid MDL, declared
capabilities, no network (unless declared) and no server imports, and compares the snapshot to a
committed golden file. Add `pytest_plugins = ["satchel_adapter_sdk.pytest_plugin"]` to your
`conftest.py` for the `--update-golden` flag (or set `SATCHEL_UPDATE_GOLDEN=1`).

## Wren MDL

If your tool has governed metrics, build a Wren MDL manifest with `MdlBuilder` (models over
warehouse tables plus *cubes* of measures and dimensions) and return it in a
`SourceSemanticModel`. Satchel serves it through `query_metrics`.

## Develop

```bash
cd sdk && uv sync && uv run pytest -q && uv run ruff check . && uv run mypy satchel_adapter_sdk
```

Licence: Apache-2.0.
