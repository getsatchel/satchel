# Cube

Satchel reads the data-model files of a [Cube](https://cube.dev) project and turns them into ordinary context items. It does not run Cube and has no Cube dependency. The adapter is built in (`satchel source kinds` shows it as *built-in*, maturity *beta*).

| Cube | becomes in Satchel |
|---|---|
| a cube with `sql_table` (or a single-table `sql:`) | `asset_doc` for that table: the cube's description, owner and tags |
| dimensions whose `sql` is a column | the table's columns: description, `primary_key` as grain and unique, `meta` (the Cube type is kept as `cube_type`) |
| a column that only a measure aggregates | listed as a column, "Aggregated by Cube measure revenue (sum)." |
| `joins` (`{CUBE}.customer_id = {customers}.customer_id`) | join hints ("customer_id -> analytics.customers.customer_id") |
| `segments` | caveats with their filter ("Cube segment valid_orders filters orders to rows where status NOT IN (...)") |
| `views` | "Used by: Cube view orders_view" on the cubes they include |
| `meta.owner`, `meta.satchel.endorsed`, `meta.satchel.caveats`, `meta.satchel.grain`, dimension `meta.satchel.pii/unit/enum_values` | owner, endorsement, caveats, grain, column facts (the same keys as dbt's `meta.satchel`) |
| measures: `sum`, `avg`, `min`, `max`, `count`, `count_distinct`, `number` (with `filters`), and dimensions (string, number, boolean, time) | one Wren MDL `semantic_model` per Cube project, so `query_metrics` works |
| rolling windows, running totals, multi-stage measures, `median` and other aggregations, sub-query and geo dimensions, SQL that needs Cube at run time (`COMPILE_CONTEXT`, `SECURITY_CONTEXT`, `FILTER_PARAMS`, Jinja), members of another cube | description only, listed as "not queryable through Satchel" with the reason |
| `extends` | resolved: the child's members win |

Cubes over the same table merge into one asset doc (a cube over the whole table describes it first). A cube defined by a `sql:` query that joins or aggregates is queryable but not documented as a table, because there is no single table to attach the doc to (a warning says so).

Unsupported or malformed input never fails an import. It becomes a warning in the sync report: JavaScript models, a YAML file that does not parse (for example Jinja that is not rendered), a cube that does not validate, a join that is not a plain equality.

## Set up

```bash
satchel connection add --name warehouse --kind duckdb --path ./warehouse.duckdb   # once
satchel connection crawl warehouse

satchel source add cube shop-cube \
  --models cube/model \
  --connection warehouse \
  --dialect duckdb \
  --publish auto
satchel source sync shop-cube
```

* `--models` is the data-model folder (`model/`, or `schema/` in older projects) or one YAML file. It is read recursively: `.yml`, `.yaml` and `.js` files (JavaScript models are only warned about).
* `--dialect` (duckdb, postgres, snowflake, bigquery, ...) sets the MDL's data source. `--default-schema` qualifies a `sql_table` written without a schema. `--project NAME` names the semantic model (default `cube`).
* Cube data models carry no column types, so the types of the metric model are guessed from the dimension and measure types (a `types_guessed` warning says so). `query_metrics` does not depend on them.
* `--connection` names the warehouse connection the tables live in; cubes are matched to the crawler's `asset` rows by `schema.table`, exactly like dbt models. Missing assets are created.
* From a git repository instead of a local folder: on the Sources page choose **Git repository**, or with the CLI add `--git-url https://github.com/acme/analytics.git --git-ref main` and give the path inside the repository. Each sync checks out just that folder; see [configuration](../deploy/configuration.md#from-a-git-repository).
* Try it without a database: `satchel source validate cube --models cube/model` prints what would be imported (`--json` for the full snapshot).
* In a context repository (`satchel.config.yaml`):

  ```yaml
  sources:
    - kind: cube
      name: shop-cube
      models: ../cube/model          # relative to this folder
      connection: warehouse
      dialect: duckdb
      priority: 2
  ```

A runnable example lives in [`examples/jaffle-shop/cube`](../../examples/jaffle-shop/cube), with the same three tables and the same metrics as the dbt example. Over it, `query_metrics` returns March 2025 revenue 1716.15 over 25 orders (the dbt-derived and hand-written models agree, and so does a real Cube 1.8.18 running the model).

## Optional: the `/meta` endpoint

Cube's REST API describes the compiled model at `GET /cubejs-api/v1/meta`. Satchel can read it to fill in descriptions the YAML lacks and to learn about cubes that only exist in JavaScript models:

```bash
# a saved response (works offline, good for CI)
curl -H "Authorization: $CUBE_TOKEN" http://localhost:4000/cubejs-api/v1/meta > meta.json
satchel source add cube shop-cube --models cube/model --meta meta.json --connection warehouse

# or read it from the running server on every sync (the adapter's only network access)
export CUBE_TOKEN=...
satchel source add cube shop-cube --models cube/model \
  --meta-url http://localhost:4000 --meta-token-env CUBE_TOKEN --connection warehouse
```

* The adapter declares the `network` capability and uses it **only** when `--meta-url` is set; without it a sync never touches the network.
* A failing `/meta` request fails the sync (the previously published items stay as they are) instead of silently importing less.
* The YAML wins wherever both describe a member. `/meta` fills missing descriptions. A cube that `/meta` knows but no YAML file defines (a JavaScript model) cannot be linked to a table or queried, so its documented members are imported as glossary terms and a `cube_meta_only` warning says so.
* Prefer `--meta-token-env` (the name of an environment variable) over `--meta-token`: the second is stored in the source's configuration. The REST API masks token-like keys in `GET /v1/sources`.
* Only `http` and `https` URLs are accepted.

## Keep it in sync from CI

```bash
args=()
while IFS= read -r f; do args+=(-F "models=@$f;filename=${f#cube/model/}"); done < <(
  find cube/model \( -name '*.yml' -o -name '*.yaml' -o -name '*.js' \))
curl -sf -X POST "$SATCHEL_URL/v1/sources/$SOURCE_ID/sync" \
  -H "Authorization: Bearer $SATCHEL_TOKEN" "${args[@]}"
```

One repeated `models` field per file, named by its path inside the model folder (`filename=`); uploads win over the stored path. The token needs the curator role. Creating the source (paths can be placeholders when only CI syncs it) and the rest of the behaviour (field ownership, overlays, publishing) are as for [dbt](dbt.md).

**Without CI:** when the Cube model lives in git, read it from the repository and let Satchel keep it current. Set **Sync automatically** (every N hours) on the source, or add a **Push webhook** (GitHub or GitLab, push events) so every push to the source's branch syncs it within seconds. A push with no new commit for that branch costs one `git ls-remote`. See [Syncing automatically](../deploy/configuration.md#syncing-automatically).

## Field ownership, publishing, several sources

Identical to dbt: each item keeps a read-only `source` block and an analyst `overlay`; `publish: auto|preview`; an identical second sync is a no-op. If dbt and Cube both describe `analytics.orders`, one of them is primary: see [Multiple sources](dbt.md#multiple-sources-for-one-table). Metric definitions work the same way: two sources defining the same metric model are served from the primary one only.
