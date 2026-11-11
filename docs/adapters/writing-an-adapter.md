# Writing a source adapter

A **source adapter** reads the artifacts of a tool your team already uses (dbt, Cube, SQLMesh, a BI tool) and describes them in Satchel's canonical IR. The Satchel server does everything else: it turns the IR into context items, versions them, keeps analyst notes safe across syncs, publishes releases and feeds the curation loop. You only write the reading.

You need Python 3.12 and `uv`. No Postgres, no running Satchel.

## 1. Start from the template

```bash
cp -r templates/adapter ../satchel-source-mytool && cd ../satchel-source-mytool
# rename: the folder satchel_source_example, the project name, the entry-point name and info.kind
uv sync
uv run pytest --update-golden        # writes tests/golden/snapshot.json from the toy fixture
uv run pytest
```

The template is a toy adapter that reads a JSON list of tables. Open `satchel_source_example/__init__.py`; it is about 60 lines.

## 2. The three pieces

**`info`: the manifest.** `kind` (lower case, equals the entry-point name), `display_name`, `version`, `maturity` (`experimental`, `beta`, `stable`), `capabilities` (`assets`, `semantic`, `queries`, `glossary`, `lineage`, `network`), a `config_model` and `file_inputs`.

**`config_model`: a pydantic model.** Its JSON schema generates the CLI flags (`satchel source add mytool NAME --export PATH`) and `satchel source info mytool`. Field names that match a `FileInput` hold file paths. Use `extra="forbid"` so typos fail.

**`file_inputs`: the files you read.** `FileInput(name="export", required=True)` is one file; `files.read("export")` returns its bytes. A tool whose model is a folder of files (a Cube data model, a SQLMesh project) declares `FileInput(name="models", multiple=True, extensions=(".yml", ".yaml"))`: the path may be a folder, the host reads it recursively (skipping dot folders, `node_modules`, `target`, ...) and `files.group("models")` returns `{relative path: bytes}`. CI uploads use one repeated multipart field per file.

**`load(config, files) -> SourceSnapshot`: pure.** `files` holds the input files as bytes (the host read them from the configured paths, or from a CI upload). Return a `SourceSnapshot`:

| IR | becomes |
|---|---|
| `SourceAsset` (fqn, kind table/view, description, `columns`, `grain`, `relationships`, `tags`, `owner`, `endorsed`, `endorsement_hint`, `caveats`, `used_by`, `depends_on`, `source_meta`) | an `asset_doc` linked to the warehouse asset |
| `SourceSemanticModel` (a Wren MDL manifest, built with `MdlBuilder`) | a `semantic_model` item, so `query_metrics` works |
| `SourceQuery` (a question and `sql` or `metric_query`) | a `golden_query` |
| `SourceTerm` | a `glossary_term` |
| `SourceWarning` | a line in the sync report |

Rules the conformance kit enforces:

* **Deterministic.** Sort everything. No timestamps or random ids in the snapshot.
* **Pure.** No database, no server imports (`satchel.*`, SQLAlchemy, FastAPI). No network unless you declare the `network` capability, and then only when the user asked for it with a flag (the Cube adapter reads `/cubejs-api/v1/meta` only when `--meta-url` is set; inject the fetch function so tests use a recorded response). A remote call that fails should raise, not silently import less.
* **Normalised names.** `normalise_fqn` gives `schema.table` in lower case; put the database in `SourceAsset.database`. Keys are unique within the snapshot.
* **Never fail the whole import for one bad node.** Skip it and add a `SourceWarning`.
* **`endorsed` only when the upstream author explicitly said so.** A heuristic goes in `endorsement_hint`; Satchel turns it into a suggestion a human reviews, never an endorsement.
* **Fingerprint** the inputs (`files.fingerprint([...])`): equal fingerprints mean nothing changed.
* **Optional dependencies.** If your adapter needs a package that not everyone has, list its import name in `AdapterInfo.requires` and the pip extra in `extras`, and import it inside `load()`. When it is missing, `satchel source kinds` and `GET /v1/source-kinds` show the adapter as *unavailable* with an install hint instead of failing, and a sync says the same.
* **Several sources describe one table.** You do not resolve that. Satchel keeps each source's doc and serves the primary one (`priority`, lower wins); see [`docs/integrations/dbt.md`](../integrations/dbt.md#multiple-sources-for-one-table). Emit the facts your tool knows and let the host pick.

If your tool has governed metrics, build MDL with the SDK helper, not by hand:

```python
b = MdlBuilder(data_source="duckdb")
b.model("orders", schema="analytics", table="orders", columns={"amount": "integer"})
b.cube("sales", base="orders", measures=[("revenue", "SUM(amount)", "DOUBLE", "Order revenue")])
SourceSemanticModel(key="mytool.sales", name="sales", mdl=b.build(), queryable_metrics=["revenue"])
```

Anything you cannot express as a queryable measure (windows, conversions, joins you cannot model) should go into `SourceSemanticModel.notes` with the reason; Satchel shows those to agents as "not queryable".

## 3. Test with the conformance kit

```python
from satchel_adapter_sdk.testing import assert_conformant

def test_conformance(update_golden):
    assert_conformant(adapter, MyConfig(), files, golden=GOLDEN, update_golden=update_golden)
```

It checks the manifest, a valid JSON round trip, the IR version, determinism (two loads equal), a stable fingerprint, normalised fqns, unique keys, valid MDL, declared capabilities, no network and no server imports, and compares the snapshot to a committed golden file. Use a **real** artifact from your tool as the fixture, trimmed. The dbt adapter's tests (`server/tests/test_source_dbt.py`) are the reference; the Cube (`test_source_cube.py`) and SQLMesh (`test_source_sqlmesh.py`) adapters show folder inputs, an optional network input and a static reader of a project folder.

Add tests for the mapping rules and for malformed input (it must produce warnings, not exceptions).

## 4. Try it against Satchel (still no database)

```bash
uv pip install -e .                       # into the environment that has satchel installed
satchel source kinds                         # your adapter shows as "external"
satchel source validate mytool --export tests/fixtures/export.json          # prints the summary
satchel source validate mytool --export tests/fixtures/export.json --json   # the full snapshot
```

With a Satchel server: `satchel source add mytool main --export /path/to/export.json`, `satchel source sync main`.

## 5. Publish

Name the package `satchel-source-<tool>` (the full rule, and the built-in kinds you must not reuse, are in [Naming and listing](naming.md)), register the entry point in `pyproject.toml`:

```toml
[project.entry-points."satchel.sources"]
mytool = "satchel_source_mytool:adapter"
```

and release it to PyPI. `pip install satchel-source-mytool` next to Satchel is enough; it is discovered identically to the built-in dbt, Cube and SQLMesh adapters (which register through the same group). Open an issue using the *Integration request* template, and see [Getting listed](naming.md#getting-listed) to have it added to the [community adapters](wanted.md#community-adapters) list. Set `maturity` according to [Maturity tiers](maturity.md). Looking for something to build? See [Wanted integrations](wanted.md).

**Trust.** Installed adapters run inside the Satchel process as trusted code. Read an adapter before installing it on a server.

## IR versions

The SDK exposes `IR_VERSION` (semver). The server accepts a snapshot with the same major version. New optional fields are minor bumps; removals and changes of meaning are major. The full policy, including what counts as breaking and which SDK version to depend on, is in [Adapter compatibility](compatibility.md).
