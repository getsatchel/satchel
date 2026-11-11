# Warehouses: BigQuery and Snowflake

Satchel connects to a warehouse so agents can run guarded read-only SQL (`run_sql`), query
metrics compiled from a semantic model (`query_metrics`), and so the crawler can catalog tables
and columns. PostgreSQL and DuckDB are covered in
[configuration.md](../deploy/configuration.md#settings--connections); this page covers BigQuery
and Snowflake.

Add a connection in **Settings → Connections** (admins), press **Test**, save, then **Crawl**.

## What every connection gets

- **Guardrail.** Each statement is parsed with sqlglot in the warehouse's dialect. Exactly one
  `SELECT` / `WITH` / set operation is allowed; DML, DDL, `SET`, `USE`, `CALL`, scripting,
  `EXPORT DATA`, `EXTERNAL_QUERY`, Snowflake stages (`@stage`), `INFER_SCHEMA` and `SYSTEM$`
  functions are refused before anything reaches the warehouse.
- **Row cap and timeout** from **Settings → Limits** (`sql_row_cap`, `sql_timeout_s`). The
  timeout is enforced by the warehouse too (see each section).
- **Least privilege.** Neither BigQuery nor Snowflake has a read-only session, so the identity
  Satchel uses must only be able to read. The guardrail is the second line, not the first.

## Installing

The libraries are optional extras. The Docker image (`ghcr.io/getsatchel/satchel`) includes both.

```bash
pip install 'satchel[bigquery]'    # google-cloud-bigquery
pip install 'satchel[snowflake]'   # snowflake-connector-python
```

Without the library the kind shows as unavailable in the form, with the command to install it.

## BigQuery

| Field | Meaning |
| --- | --- |
| Project | The job project: runs and bills the query jobs. |
| Data project | Holds the datasets, when it is not the job project (e.g. a shared data project). |
| Location | The datasets' location (`US`, `EU`, `europe-west1`). |
| Datasets | Comma-separated dataset IDs to catalog. Empty: every dataset in the data project the identity can list. With exactly one, it is the default dataset, so `select * from orders` works. |
| Max GiB billed per query | The cost guard (default 10 GiB). |

### Auth

1. **Application Default Credentials (recommended).** Leave the key empty. On GKE use workload
   identity, on Cloud Run the service's identity; elsewhere `GOOGLE_APPLICATION_CREDENTIALS`.
   Nothing secret is stored in Satchel.
2. **Service-account JSON key.** Paste the whole `.json` file. It is checked
   (`"type": "service_account"`), sealed with `SATCHEL_ENCRYPTION_KEY` and never shown again.
   Prefer ADC: keys are long-lived and many organizations block creating them.

### IAM

Grant the identity exactly two roles:

```bash
# Run query jobs (on the job project).
gcloud projects add-iam-policy-binding JOB_PROJECT \
  --member=serviceAccount:satchel-runtime@JOB_PROJECT.iam.gserviceaccount.com \
  --role=roles/bigquery.jobUser
# Read the data: per dataset is tightest, per project is simplest.
bq add-iam-policy-binding --member=serviceAccount:satchel-runtime@JOB_PROJECT.iam.gserviceaccount.com \
  --role=roles/bigquery.dataViewer DATA_PROJECT:analytics
```

Do not grant `bigquery.dataEditor`, `bigquery.admin` or `bigquery.user` (it can create datasets).
With the GCP Terraform foundation, set `bigquery_projects = ["JOB_PROJECT", ...]` to grant both
roles on whole projects (see `deploy/terraform/modules/foundation/variables.tf`).

### Cost guard and timeouts

Every job sets `maximum_bytes_billed` to the configured GiB. BigQuery refuses a query that would
bill more, and the agent is told to select fewer columns or filter on the partition column. The
crawl's `INFORMATION_SCHEMA` queries go through the same guard (they bill at least 10 MB per
dataset). The job also gets `job_timeout_ms` from the SQL timeout (BigQuery cancels it server-
side), the client stops waiting at the same time and cancels the job, and every job carries the
label `app=satchel`, so `INFORMATION_SCHEMA.JOBS` and billing exports can attribute the spend.

Results are read through the REST API (no BigQuery Storage API, no extra role), at most row cap
+ 1 rows.

### Crawl

Per dataset, one query over `INFORMATION_SCHEMA.COLUMNS`, `COLUMN_FIELD_PATHS` (column
descriptions) and `TABLE_OPTIONS` (table descriptions). Assets: the data project is the
database, each dataset a schema, tables are `dataset.table`, columns `dataset.table.column` with
the BigQuery type (`STRING`, `STRUCT<...>`). Descriptions become draft table docs as for other
warehouses.

## Snowflake

| Field | Meaning |
| --- | --- |
| Account | The account identifier, `orgname-accountname` (or the account locator), not the URL. |
| User | The service user. |
| Role | The read-only role below. Empty: the user's default role. |
| Warehouse | The warehouse queries run on. |
| Database | The database to query and catalog. |
| Schemas | Comma-separated schemas to catalog. Empty: every schema except `INFORMATION_SCHEMA`. |

### Auth: key pair only

Satchel connects with key-pair (JWT) auth. Password auth is not offered: Snowflake is phasing
out single-factor password sign-in, and service users (`TYPE = SERVICE`) cannot have a password
at all. Key pair is Snowflake's recommended auth for programs and needs no MFA prompt.

```bash
openssl genrsa 2048 | openssl pkcs8 -topk8 -v2 aes-256-cbc -out satchel_key.p8   # encrypted
openssl rsa -in satchel_key.p8 -pubout -out satchel_key.pub
```

Paste `satchel_key.p8` (the whole file, BEGIN / END lines included) into **Private key** and its
passphrase into **Key passphrase**. Satchel decrypts the key once, when it is saved or tested,
and seals the unencrypted PKCS#8 key with `SATCHEL_ENCRYPTION_KEY`; the passphrase is not stored
(it would be sealed with the same key, so storing it adds nothing). With the CLI
(`--secret-ref env:NAME`) the variable must hold an unencrypted key.

### A read-only role

```sql
USE ROLE SECURITYADMIN;
CREATE ROLE IF NOT EXISTS SATCHEL_READ;
GRANT USAGE ON WAREHOUSE SATCHEL_WH TO ROLE SATCHEL_READ;
GRANT USAGE ON DATABASE ANALYTICS TO ROLE SATCHEL_READ;
GRANT USAGE ON ALL SCHEMAS IN DATABASE ANALYTICS TO ROLE SATCHEL_READ;
GRANT USAGE ON FUTURE SCHEMAS IN DATABASE ANALYTICS TO ROLE SATCHEL_READ;
GRANT SELECT ON ALL TABLES IN DATABASE ANALYTICS TO ROLE SATCHEL_READ;
GRANT SELECT ON FUTURE TABLES IN DATABASE ANALYTICS TO ROLE SATCHEL_READ;
GRANT SELECT ON ALL VIEWS IN DATABASE ANALYTICS TO ROLE SATCHEL_READ;
GRANT SELECT ON FUTURE VIEWS IN DATABASE ANALYTICS TO ROLE SATCHEL_READ;

CREATE USER IF NOT EXISTS SATCHEL_READER
  TYPE = SERVICE
  DEFAULT_ROLE = SATCHEL_READ
  DEFAULT_WAREHOUSE = SATCHEL_WH
  RSA_PUBLIC_KEY = 'MIIBIjANBgkqh...';   -- satchel_key.pub without the BEGIN / END lines
GRANT ROLE SATCHEL_READ TO USER SATCHEL_READER;

-- Optional: cap what a runaway query can cost.
ALTER WAREHOUSE SATCHEL_WH SET STATEMENT_TIMEOUT_IN_SECONDS = 600 AUTO_SUSPEND = 60;
```

Grant nothing on stages, no `CREATE` privileges, and no ownership. With `FUTURE` grants on a
database, check that no schema-level future grants override them.

### Session parameters

Each connection sets `STATEMENT_TIMEOUT_IN_SECONDS` to the SQL timeout limit (rounded up; the
crawl uses 120 s) and `QUERY_TAG = 'satchel'`, so `QUERY_HISTORY` shows Satchel's queries. A
warehouse-level timeout that is lower still wins.

### Crawl

One query over `<database>.INFORMATION_SCHEMA.COLUMNS` joined to `TABLES` (filtered to the
configured schemas). Assets keep Snowflake's names (usually upper case): the database, schemas,
`SCHEMA.TABLE` and `SCHEMA.TABLE.COLUMN`, with table and column comments as draft docs. Asset
matching for signals ignores case.

## The semantic layer

`query_metrics` compiles the metric query with wren-core and transpiles it with sqlglot to the
connection's dialect (`bigquery` or `snowflake`). Semantic models name tables without quotes
(`analytics.orders`), which Snowflake resolves case-insensitively and BigQuery resolves against
the job project (or the default dataset).

## Testing

The connector tests (`server/tests/test_connectors_bigquery.py`, `test_connectors_snowflake.py`)
use stand-ins for the BigQuery client and the Snowflake connector that transpile the SQL Satchel
sends onto the Jaffle Shop DuckDB, so run_sql and query_metrics run end to end without cloud
access. They do not exercise the real wire protocol, auth handshakes or IAM; test a new
connection with **Test** before relying on it.
