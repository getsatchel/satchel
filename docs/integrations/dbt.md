# dbt

Satchel reads the artifacts dbt already writes and turns them into ordinary context items. The adapter itself does not run dbt and has no dbt dependency; a source that reads a dbt project [from a git repository](#from-a-git-repository) can have the server run `dbt parse` to write them. The adapter is built in (`satchel source kinds` shows it as *built-in*, maturity *beta*).

| dbt | becomes in Satchel |
|---|---|
| models, seeds, snapshots (enabled, not ephemeral) and documented source tables | `asset_doc`: description, columns with descriptions and types, owner, tags |
| `accepted_values` test | column `enum_values` |
| `unique` + `not_null` on a column (or dbt's `primary_key`, or `dbt_utils.unique_combination_of_columns`) | the table's grain |
| `relationships` test | join hints ("customer_id -> analytics.customers.customer_id") |
| exposures | "Used by: Weekly revenue dashboard (dashboard) URL" on the models they depend on |
| `meta.owner`, or the model's group owner | owner |
| `meta.satchel.endorsed`, `meta.satchel.caveats`, `meta.satchel.grain`, column `meta.satchel.pii/unit/enum_values` | endorsement, caveats, grain, column facts |
| semantic models, dimensions, entities, measures, simple / ratio / derived metrics | one Wren MDL `semantic_model` item per dbt project, so `query_metrics` works |
| cumulative and conversion metrics, metrics spanning several semantic models, cross-model dimensions | description only, listed as "not queryable through Satchel" with the reason |
| saved queries | `golden_query` with a `metric_query` |

Unsupported or malformed constructs never fail an import. They become warnings in the sync report (`satchel source sync`, the Sources page, `last_report` of `GET /v1/sources`).

## Set up

```bash
cd your-dbt-project
dbt build                      # or: dbt parse
dbt docs generate              # optional: writes catalog.json with exact column types

satchel connection add --name warehouse --kind duckdb --path ./warehouse.duckdb   # once
satchel connection crawl warehouse

satchel source add dbt jaffle \
  --manifest target/manifest.json \
  --semantic-manifest target/semantic_manifest.json \
  --catalog target/catalog.json \
  --connection warehouse \
  --publish auto
satchel source sync jaffle
```

* `manifest.json` is required (schema v10 to v12, dbt 1.6 to 1.12). `semantic_manifest.json` is optional: without it the metrics are read from `manifest.json`. Without `catalog.json`, column types for the metric model are guessed (a warning says so).
* `--connection` names the warehouse connection the models live in. dbt nodes are matched to the crawler's `asset` rows by `schema.table`, so endorsements, entity links and the unendorsed-table signal line up with the crawl. Missing assets are created.
* `satchel source list` shows installed adapters (kind, version, maturity, built-in or external) and this workspace's sources; `satchel source info dbt` lists every option.
* Try it without a database: `satchel source validate dbt --manifest target/manifest.json --catalog target/catalog.json` prints what would be imported (`--json` for the full snapshot).

A runnable example lives in [`examples/jaffle-shop/dbt`](../../examples/jaffle-shop/dbt) (a dbt project over the same data as the Jaffle Shop context).

### In a context repository

```yaml
# satchel.config.yaml
connections:
  warehouse: { kind: duckdb, path: jaffle.duckdb }
sources:
  - kind: dbt
    name: jaffle
    manifest: ../dbt/target/manifest.json        # relative to this folder
    semantic_manifest: ../dbt/target/semantic_manifest.json
    catalog: ../dbt/target/catalog.json
    connection: warehouse
    publish: auto                                 # or: preview
    priority: 1                                   # lower wins when several sources describe a table
```

`satchel sync import` syncs the files first and then every declared source. The older `import: { dbt_manifest: PATH }` key still works as a source named `dbt`.

## Keep it in sync from CI

Upstream changes were already reviewed in your dbt repo, so a sync publishes them like a git import. Push the artifacts after every dbt build:

```bash
dbt build && dbt docs generate
curl -sf -X POST "$SATCHEL_URL/v1/sources/$SOURCE_ID/sync" \
  -H "Authorization: Bearer $SATCHEL_TOKEN" \
  -F manifest=@target/manifest.json \
  -F semantic_manifest=@target/semantic_manifest.json \
  -F catalog=@target/catalog.json
```

If your repository commits `target/` (some teams do, for docs or state comparison), the source can instead read `manifest.json` straight from git on every sync; see [configuration](../deploy/configuration.md#from-a-git-repository). It can then sync on a schedule (**Sync automatically**) or on every push (**Push webhook**); see [Syncing automatically](../deploy/configuration.md#syncing-automatically). A manifest in `s3://` / `gs://` can use the schedule too. A source that only gets uploads can't, because only CI has the files.

Or skip CI entirely and let the server read the dbt project from git (next section).

## From a git repository

A dbt source can read a normal dbt repository; `target/` does not have to be committed. On the Sources page choose **Git repository** (URL, branch / tag / commit, read-only token; see [configuration](../deploy/configuration.md#from-a-git-repository)), then **Build it from the dbt project (dbt parse)**:

| Field (config key) | |
|---|---|
| dbt project folder (`dbt_project_dir`) | the folder with `dbt_project.yml`, `.` for the repository root |
| Target schema (`dbt_target_schema`, required) | `target.schema` of your production target. dbt derives every model's schema from it (and from `generate_schema_name`), and Satchel matches tables to the warehouse by `schema.table`, so give the real one |
| Target name (`dbt_target_name`) | `target.name` during parse, default `prod`, for projects that branch on it |
| Variables (`dbt_vars`) | `--vars` for the parse, a map of names to strings, numbers or booleans. Stored and shown in plain text: names that look like secrets are refused |
| Skip dbt deps (`dbt_skip_deps`) | for projects that commit their packages in `dbt_packages/`; then the build needs no network |

```bash
satchel source add dbt jaffle --git-url https://github.com/acme/analytics.git --git-ref main \
  --dbt-project-dir dbt --dbt-target-schema analytics --dbt-vars '{"start_date": "2024-01-01"}' \
  --connection warehouse
```

Each sync checks out the project folder (shallow and sparse), runs `dbt deps` (when `packages.yml` or `dependencies.yml` lists packages, unless skipped) and `dbt parse`, and hands the `manifest.json` and `semantic_manifest.json` they write to the adapter. The sync report records the commit (`git_commit`) and the dbt version (`dbt_version`). A failed parse fails the sync with the end of dbt's log (local paths replaced). An upload at sync time still wins over the build. A source uses either the build or a committed manifest path (`manifest`, for repositories that commit `target/`), never both.

**No catalog.json.** `dbt parse` does not connect to the warehouse, and `catalog.json` (from `dbt docs generate`) needs it, so the build does not produce one. Column types come from crawling the source's warehouse connection instead (`satchel connection crawl`, or **Crawl** in Settings → Connections). If you do commit a `catalog.json`, its path is still read.

**How it is parsed.** dbt runs with a generated `profiles.yml` whose only target is an in-memory DuckDB (`dbt-duckdb`), whatever warehouse the project targets. Parse needs an adapter but never opens a connection. What Satchel reads (models, raw SQL, descriptions, columns, tests, exposures, semantic models, metrics, saved queries) does not depend on the adapter. What can differ: `target.type` is `duckdb`, so macros dispatched per adapter (`adapter.dispatch`) and Jinja that branches on `target.type` take their DuckDB or default branch, and a project that only parses with its own adapter's macros (an adapter-specific materialization referenced in Jinja at parse time) can fail; give such projects a committed or uploaded manifest instead. `env_var('X', 'default')` takes its default, because dbt sees none of the server's environment; `env_var('X')` without a default fails the parse with dbt's message. Use `var()` and **Variables**, or give the call a default. Python models are recorded but never executed by parse; hooks and `run_query` do not run.

**Requirements.** dbt Core 1.8+ with `dbt-duckdb`, in its own virtualenv: the image ships `/opt/dbt/bin/dbt` (dbt-core 1.12, dbt-duckdb 1.11, hash-locked in `deploy/docker/dbt-requirements.txt`; about 195 MB uncompressed, 57 MB compressed; `--build-arg INSTALL_DBT=false` leaves it out). Elsewhere set `SATCHEL_DBT_BIN`. dbt Fusion is refused (it is a different engine with its own manifest), as are dbt Core before 1.8 and dbt Core without `dbt-duckdb`; Satchel checks `dbt --version` once per binary. Without a usable dbt the option shows as unavailable, with the reason. The Sources page shows the dbt Core version that built the last manifest. `SATCHEL_DBT_TIMEOUT_S` (default 300) caps deps and parse together.

### Sandbox and security

The repository, its Jinja macros, `dbt_project.yml` and packages are chosen by a workspace admin, so dbt is treated as untrusted code:

* It runs as a **separate process**, never inside the server, from its own virtualenv.
* Its **environment is built from scratch**: no `SATCHEL_*`, database URLs, cloud credentials (`AWS_*`, `GOOGLE_*`, `AZURE_*`), `PG*` or `PYTHONPATH`; only proxy and CA settings pass through (`HTTPS_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, ...). `HOME` and `TMPDIR` are a temporary folder, telemetry is off (`DBT_SEND_ANONYMOUS_USAGE_STATS=false`, `DO_NOT_TRACK=1`), and git (for git packages) runs https-only without prompts or host config.
* **Limits**: a wall-clock timeout that kills the whole process group; on Linux `setrlimit` caps address space (4 GB), CPU time, file size (1 GB) and core dumps; output goes to a file and only its tail is read; `manifest.json` is refused above 256 MB.
* **Checks before dbt runs**: the project's `*-paths`, `packages-install-path` and `local:` packages must be relative paths inside the checkout (a project cannot point `macro-paths` or `docs-paths` at server folders and import their contents); `git:` and `tarball:` package URLs must be `https://` and not resolve to link-local, loopback or metadata addresses; Jinja in those values and `private:` packages are refused. The checkout itself has no symlinks (see git sources).
* **Network**: `dbt deps` downloads from hub.getdbt.com and the package hosts (GitHub for git packages), so outbound 443 must be allowed for it; skip deps to build offline.

This is not a container sandbox. The process runs as the server's user, can read what that user can read, and can reach what the server's network can. On GKE and EKS that includes the metadata endpoint (`169.254.169.254`, workload identity credentials for the pod's service account) and, on EKS, the projected IRSA token file under `/var/run/secrets/` (its path is scrubbed from the environment, the file is not). Using either would need a dbt bug: dbt's Jinja context has no file, process or network access, parse executes no Python models, hooks or queries, and package URLs are checked. To reduce the risk further: give the pod's service account only the permissions Satchel needs (the foundation modules do), block the metadata address for the worker with a NetworkPolicy or GKE metadata concealment / IMDSv2 hop limit 1 where your setup allows it, restrict egress to the git and package hosts, and only let admins you trust configure dbt sources.

The token needs the curator role. Find `SOURCE_ID` with `GET /v1/sources`. Create the source once, either on the Sources page (**Add source**, leave the file inputs empty or point them at `s3://` / `gs://` locations; see [configuration](../deploy/configuration.md#sources-sources-page)) or with the CLI (`satchel source add dbt ...`; the paths can be placeholders when only CI syncs it, since uploads win over stored paths). The response is the sync report (created, updated, deprecated, warnings, release).

## Field ownership: what dbt owns and what analysts own

Every source-backed item keeps two blocks in its version `meta`:

* **`source`**: owned by dbt. Replaced on every sync, read-only in the UI and API (`PUT /v1/items/{key}` and deprecate return **409**).
* **`overlay`**: analyst additions that a sync never touches: notes, caveats, column notes, a replacement lead description, an endorsement. Edit it with `PUT /v1/items/{key}/overlay` or **Edit overlay** on the item page. Overlays are drafts that go through preview and publish like any change.

The text agents retrieve merges both. The dbt description is always shown; the overlay adds to it (or leads, if you wrote a lead description). On every sync the overlay is carried into the new version. Two safeguards:

* If dbt removes or renames a column your overlay documents, Satchel keeps your note and emits an **`overlay_out_of_sync`** curation signal (Inbox), plus a banner on the item.
* If an analyst has an unpublished overlay draft, an automatic sync does not publish it: the item is *held* (the sync report says so) and keeps its published version until a person publishes the draft.

dbt metadata that merely *suggests* endorsement (`access: public` with an enforced contract) becomes an `endorsement_candidate` signal in the Inbox. It is never applied automatically. An explicit `meta.satchel.endorsed: true` in dbt is a declaration by its author and is imported as such.

## Publishing

| `publish` | a sync that changes something |
|---|---|
| `auto` (default) | publishes a new release, exactly like a git import on the default branch. If the workspace has required evals (`publish.require_evals`) that are not passing, it leaves a **preview** release instead and says so; run the evals on the preview and publish it from the UI. |
| `preview` | only ever creates a preview release; a person publishes it. |

An identical second sync is a no-op: no new versions, no new release.

## Multiple sources for one table

dbt, [Cube](cube.md), [SQLMesh](sqlmesh.md) and hand-written docs (git or the UI) may all describe `analytics.orders`. Satchel keeps every doc as its own item (provenance, overlays, audit trail), but **exactly one is primary** and only the primary is served.

**Which one is primary** is decided by `priority` on the sources, **lower number wins** (1 beats 2):

1. the lowest `priority`;
2. at equal priority, a hand-written doc (git or UI) beats a source (a hand-written doc counts as priority 0);
3. then the source name, then the item key, so the choice is the same on every run.

```yaml
sources:
  - { kind: dbt,  name: jaffle,      manifest: ../dbt/target/manifest.json, priority: 1 }   # primary
  - { kind: cube, name: jaffle-cube, models: ../cube/model,                 priority: 2 }
```

Changing a priority takes effect at once (`satchel source add dbt jaffle ... --priority 5`); no sync has to run. The default is 0, so a source at the default loses a tie to a hand-written doc. Give the source you trust most the lowest number; a negative number outranks hand-written docs too.

**What agents are served.** `search_context`, `get_context_pack`, `describe_table` and `list_tables` return one doc per table: the primary. The facts the other docs add and the primary lacks are *folded* into it as an "Also described by cube (sources/jaffle-cube/assets/...)" block: a column only Cube describes, a description for a column the primary lists without one, enum values, units, caveats (Cube segments), join hints, owner. The primary always wins a conflict, so a differing description is never echoed. A question that only the Cube description matches still finds the table, and the answer is the one served doc. `describe_table` also returns `also_described_by` (source and key of each other doc) and `also_described_facts`. The non-primary docs are never deleted: they stay in the library, the catalog and the audit trail, and an analyst can still add an overlay to them.

**Where else the primary counts.** The catalog (`doc_item_key`, the endorsed flag), the implicit signals (`undocumented_asset`, `unendorsed_used`) and the context the curation engine shows when drafting suggestions read the primary doc only.

**What you can see.**

* `satchel source sync` prints `overlap: analytics.orders also described by cube; primary is jaffle`; the REST sync report and `last_report` of `GET /v1/sources` carry `overlaps`: `[{type: "asset_doc", asset, primary: {source, key}, also_described_by: [{source, key}], this_source_is_primary}]`. A sync warning `also_described_by` repeats it.
* `GET /v1/items/{key}` has `primary` (true when this doc is the one served for its table, false when another outranks it, null for other item types) and `also_described_by: [{source, key, primary}]`.
* `GET /v1/assets` and `GET /v1/assets/{id}` have `primary: {source, key, primary: true}` and `also_described_by`; `doc_item_key` is the primary's key.

**Metrics.** Two sources that define the same metric model or cube names (dbt's `orders` semantic model and Cube's `orders` cube) are not mixed: the semantic model with the better precedence is served whole to `query_metrics` and `list_metrics` (which lists the other under `shadowed_models`), and the sync report says `semantic_shadowed`. Two hand-written semantic models that collide remain an error. Give metrics distinct names, or accept that the primary source's definitions are the governed ones.
