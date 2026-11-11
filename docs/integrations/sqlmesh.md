# SQLMesh

Satchel reads a [SQLMesh](https://sqlmesh.com) project folder and turns its models into ordinary context items. It does not run SQLMesh and does not import it. The adapter is built in (`satchel source kinds` shows it as *built-in*, maturity *beta*).

| SQLMesh | becomes in Satchel |
|---|---|
| a model (`MODEL (name analytics.orders, ...)` in a `.sql` file, or an `@model` Python model) | `asset_doc` for `analytics.orders` (a three-part name keeps the database) |
| `description`, `owner` (or `model_defaults.owner`), `tags` | description, owner, tags |
| `columns (id INT, ...)` | column data types (`NOT NULL` becomes a required column) |
| `column_descriptions (id = '...')` | column descriptions |
| no `columns`: the final `SELECT` | column names (types unknown) |
| `grain order_id`, `grain (a, b)`, `grains ((a, b), (c))` (the first) | the table's grain |
| audit `not_null(columns := (a, b))` | required columns |
| audit `unique_values(columns := (a))` | unique columns; with `not_null`, a single-column grain when none is declared |
| audit `unique_combination_of_columns(columns := (a, b))` | the grain when none is declared |
| audit `accepted_values(column := status, is_in := ('a', 'b'))` | the column's enum values |
| `references (customer_id)` | a join hint to the model whose single-column grain is `customer_id` |
| tables read in `FROM` / `JOIN` (and `depends_on`) | lineage (`depends_on` of the asset doc) |
| `kind VIEW` | a view; `kind EMBEDDED` models and `enabled false` models are skipped |
| `kind`, `cron`, `start`, `dialect`, the incremental time column, audit names | kept as the asset's source metadata |

SQLMesh has no governed-metrics layer, so there is no semantic model and no `query_metrics` from this adapter. Audits filtered with `where` and audits that state no column fact (`number_of_rows`, custom audits) are ignored; `not_null_non_blocking` counts like `not_null`.

Unreadable input never fails an import. It becomes a warning in the sync report: a `MODEL` block that does not close, a model without a name, two files defining one model (the first by path is used), a malformed `config.yaml`, and a query that uses macros (`@DEF`, `@AGG(...)`, Jinja), whose columns and upstream tables are then not inferred (declare `columns` to get them).

## Set up

```bash
satchel connection add --name warehouse --kind duckdb --path ./warehouse.duckdb   # once
satchel connection crawl warehouse

satchel source add sqlmesh shop-mesh \
  --project path/to/sqlmesh-project \
  --connection warehouse \
  --publish auto
satchel source sync shop-mesh
```

* `--project` is the folder with `config.yaml` and `models/`. Every `.sql` file that contains a `MODEL (...)` block and every `.py` file with an `@model` function under it is read; `audits/`, `macros/`, `tests/` and `seeds/` folders, dot folders and `logs/` are skipped. `config.yaml` supplies `model_defaults` (`dialect`, `owner`); `--dialect` is the fallback for models and configs that set none.
* Models are matched to the crawler's `asset` rows by `schema.table` (also `database.schema.table`), like dbt models and cubes. Missing assets are created.
* From a git repository instead of a local folder: on the Sources page choose **Git repository**, or with the CLI add `--git-url https://github.com/acme/analytics.git --git-ref main` and give the path inside the repository. Each sync checks out just that folder; see [configuration](../deploy/configuration.md#from-a-git-repository).
* Try it without a database: `satchel source validate sqlmesh --project path/to/project` prints what would be imported (`--json` for the full snapshot).
* In a context repository (`satchel.config.yaml`):

  ```yaml
  sources:
    - kind: sqlmesh
      name: shop-mesh
      project: ../sqlmesh
      connection: warehouse
      priority: 2
  ```

A runnable example lives in [`examples/jaffle-shop/sqlmesh`](../../examples/jaffle-shop/sqlmesh): the same three tables as the dbt and Cube examples plus a view. It was built with SQLMesh itself (`sqlmesh plan --auto-apply`, audits passing) and the values the adapter reads (kind, owner, tags, grain, references, audits, dependencies) were compared with SQLMesh's own `Context` model objects.

## Why a static reader, and no `satchel[sqlmesh]` extra

The adapter reads the project files the way the dbt adapter reads `manifest.json`: with sqlglot (already a Satchel dependency), a small bracket-aware scanner for the `MODEL` block (plain sqlglot cannot parse SQLMesh's `MODEL` statement) and `ast` for Python models. It never imports `sqlmesh` and never executes project code. The alternative, loading a `sqlmesh.Context`, was rejected:

* it executes the project's Python (`config.py`, macros, Python models) inside the Satchel server process, a larger trust step than the installed-adapter trust that already exists;
* `sqlmesh` pins `sqlglot~=30.8` while Satchel needs `sqlglot>=30.25`, so no `satchel[sqlmesh]` extra can be installed or even locked next to Satchel;
* it needs a configured gateway and the warehouse's credentials for what is only metadata.

What a static reader cannot do: render macros, resolve a model's columns when the query uses macros or `SELECT *` (declare `columns`), or follow a custom `model_naming` convention. If SQLMesh relaxes its sqlglot pin, an optional `engine: sqlmesh` mode can be added behind a real extra: the host already lists an adapter whose optional module is missing as *unavailable* with an install hint (`satchel source kinds`, `GET /v1/source-kinds`), so that path needs no framework change.

## Keep it in sync from CI

```bash
args=()
while IFS= read -r f; do args+=(-F "project=@$f;filename=${f#./}"); done < <(
  find . \( -name '*.sql' -o -name '*.py' -o -name 'config.y*ml' \) -not -path './.*')
curl -sf -X POST "$SATCHEL_URL/v1/sources/$SOURCE_ID/sync" \
  -H "Authorization: Bearer $SATCHEL_TOKEN" "${args[@]}"
```

One repeated `project` field per file. `filename=` carries the path relative to the project, which Satchel keeps (a `config.yaml` at the top level supplies the defaults; `audits/` and `macros/` are skipped by path). Uploads win over the stored path.

**Without CI:** when the SQLMesh project lives in git, read it from the repository and let Satchel keep it current. Set **Sync automatically** (every N hours) on the source, or add a **Push webhook** (GitHub or GitLab, push events) so every push to the source's branch syncs it within seconds. A push with no new commit for that branch costs one `git ls-remote`. See [Syncing automatically](../deploy/configuration.md#syncing-automatically).

## Field ownership, publishing, several sources

Identical to dbt: a read-only `source` block and an analyst `overlay` per item, `publish: auto|preview`, an identical second sync is a no-op. If SQLMesh and dbt (or Cube) describe the same table, one is primary: see [Multiple sources](dbt.md#multiple-sources-for-one-table).
