# Wanted integrations and community adapters

## Wanted integrations

These are tools we would like Satchel to read and that nobody is building yet. To claim one, comment on (or open)
its [integration request](https://github.com/getsatchel/satchel/issues/new?template=integration_request.yml), so
two people don't build the same adapter. Then follow [Writing a source adapter](writing-an-adapter.md) and the
[naming rule](naming.md).

Adapters that read **files** (a project folder, an export) are preferred: they are pure, deterministic and easy to
test. An adapter that calls an API has to declare the `network` capability, and should only call it when the user
passes a flag. *Good first adapter* marks small, file-based ones.

Capabilities: **A** assets (table and column docs, owners, tags) · **S** semantic (governed metrics, `query_metrics`) ·
**Q** queries (golden queries) · **G** glossary · **L** lineage.

### Semantic layers and models

| Tool | What to read | Capabilities | Notes |
|---|---|---|---|
| LookML (Looker) | `.lkml` files in the project repository | A, S | Views, explores, dimensions and measures. Liquid and complex derived tables go in `notes` as not queryable. |
| Malloy | `.malloy` source files | A, S, Q | Sources, measures and named queries. |
| Snowflake semantic views and Cortex Analyst models | Semantic model YAML | A, S, Q | Verified queries become golden queries. |
| Databricks metric views | Metric view YAML definitions | A, S | |
| Power BI and Analysis Services | TMDL folders or `.bim` files | A, S | DAX measures that can't be translated to SQL go in `notes`. |
| Superset | Dataset YAML from the export bundle | A, S, Q | *Good first adapter.* Dataset metrics and saved queries. |

### BI tools (saved queries and usage)

| Tool | What to read | Capabilities | Notes |
|---|---|---|---|
| Looker (API) | Looks, dashboards and their queries | Q, A | Separate from LookML (`looker_api`). Needs `network`. |
| Metabase | Models, metrics, segments and saved questions (API or serialization export) | Q, S, A | Prefer the serialization export (files). |
| Tableau | Published data sources (`.tds`/`.tdsx`) | A | Calculated fields and descriptions; Tableau's metadata API needs `network`. |
| Hex and Mode | Saved queries and project metadata (API) | Q | Needs `network`. |

### Catalogs and lineage

| Tool | What to read | Capabilities | Notes |
|---|---|---|---|
| OpenMetadata | Entity and glossary exports, or the API | A, G, L | Glossary terms and owners map directly. |
| DataHub | Metadata (MCE/MCP) files, or GraphQL | A, G, L | |
| Unity Catalog | Table and column comments, tags, lineage (API) | A, L | Needs `network`. |
| OpenLineage | Run events (JSON lines) | L | *Good first adapter.* Fills `depends_on`. |

### Transformation and data quality

| Tool | What to read | Capabilities | Notes |
|---|---|---|---|
| Dataform | `.sqlx` files and `dataform.json` | A, L | *Good first adapter.* Descriptions, column docs and dependencies. |
| Soda and Great Expectations | Check and suite definitions | A | Checks become caveats on assets (for example, "`status` is one of ..."). |
| dbt Cloud | Discovery API | A, S, Q, L | Only for teams that can't produce `manifest.json`; the built-in dbt adapter covers files. |

### Glossaries and docs

| Tool | What to read | Capabilities | Notes |
|---|---|---|---|
| Glossary spreadsheet | CSV or XLSX with term, definition and synonyms | G | *Good first adapter.* |
| Confluence and Notion | Pages under a chosen parent | G | Needs `network`. Map only pages that are structured as definitions. |

Already built in: [dbt](../integrations/dbt.md) (including MetricFlow semantic models and saved queries),
[Cube](../integrations/cube.md) and [SQLMesh](../integrations/sqlmesh.md).

## Community adapters

Adapters by other authors, checked by the maintainers as described in [Getting listed](naming.md#getting-listed).
Tiers are explained in [Maturity tiers](maturity.md).

| Package | Kind | Tool | Tier | Capabilities | Repository |
|---|---|---|---|---|---|
| *None yet. Yours could be the first.* | | | | | |
