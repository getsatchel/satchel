# Configuration: environment vs Settings

Satchel has two places configuration lives, the same split Langfuse uses for self-hosting:

| Where | What | Who changes it |
|---|---|---|
| **Environment** (`SATCHEL_*`, set by Helm / Terraform / your secret store) | Anything needed before the database is reachable or that could lock people out: database URLs, `SATCHEL_PUBLIC_URL`, `SATCHEL_ENV`, OIDC, session and OAuth lifetimes, the result store, `SATCHEL_ENCRYPTION_KEY`. | Whoever runs the deployment. Restart to apply. |
| **Settings** in the UI (per workspace, stored in Postgres) | Things a workspace admin should manage without a redeploy: **Models** (LLM connections), **Connections** (warehouses), **Limits** (SQL row cap and timeout, result cache, session retention) and, on the Sources page, **sources** (dbt, Cube, SQLMesh). **Deployment** shows the environment's values read-only. | Workspace admins. Applies immediately on every replica (read from the database per use). |

**The environment wins.** When a value is set in the environment it applies to every workspace,
and Settings shows it read-only ("managed by deployment"). GitOps installs can pin values that
way and still let the UI handle everything else.

The one exception is **Limits**: there the environment sets the *default* and a workspace admin
may override it within fixed bounds (below), because limits are a per-team decision.

## `SATCHEL_ENCRYPTION_KEY`

Secrets entered in the UI (LLM API keys, gateway headers, warehouse passwords, source tokens such
as a Cube API token) are stored encrypted. The key is
256 bits as 64 hex characters:

```
openssl rand -hex 32        # or: satchel secrets generate-key
```

- **Where it comes from.** The Terraform modules generate it (`random_id`) and store it in GCP
  Secret Manager / AWS Secrets Manager next to the database URLs (`<name>-encryption-key` /
  `<name>/encryption-key`); the runtime identity can read it, as with the other runtime secrets.
  With Helm alone, put `SATCHEL_ENCRYPTION_KEY` in the `existingSecret`. `satchel init` writes
  one into the local profile (`~/.config/satchel/config.toml`, mode 0600) for development.
- **Without it** Satchel still runs. Settings explains that API keys cannot be saved, keyless
  connections (a local Ollama) still work, and `satchel doctor` warns.
- **Losing it** makes every stored secret unreadable. Connections then fail with "sealed with
  key …" and must be re-entered. Back it up with the rest of your secrets, not with the database.

How values are sealed (`satchel/core/crypto.py`; every stored secret is listed in
`satchel/secrets.py`, which `satchel secrets rotate` and `satchel doctor` cover):

- AES-256-GCM, a fresh 96-bit nonce per value.
- The associated data names the row and column (`llm_connection:<workspace>:<id>:api_key`), so
  a ciphertext copied into another row or workspace does not decrypt.
- Stored as `enc:v1:<key id>:<base64url>`. The key id is a short hash of the key, which lets a
  rotation tell old values from new.
- The API is write-only for secrets: responses carry a hint (`…abcd`) and header *names*, never
  values. Audit events (`llm_connection.*`) list which fields changed, never their values.
- Postgres row-level security keeps each workspace's rows to itself, as for every other table.

### Rotating the key

1. Generate a new key. Set it as `SATCHEL_ENCRYPTION_KEY` and move the old one to
   `SATCHEL_ENCRYPTION_KEY_PREVIOUS` (comma-separated if there are several). Deploy.
   Both keys decrypt; new writes use the new one.
2. Run `satchel secrets rotate` (as a Job, with the worker database URL). `--dry-run` counts.
3. `satchel doctor` should report no secrets on a previous key. Then remove
   `SATCHEL_ENCRYPTION_KEY_PREVIOUS` and deploy again.

With Terraform: copy the current value from the secret manager into
`SATCHEL_ENCRYPTION_KEY_PREVIOUS` (via `extra_values` / `extraEnv` from a secret), then
`terraform apply -replace=module.foundation.random_id.encryption_key`, and continue at step 2.

## Settings → Models

The LLM that curation uses to draft suggestions and that evals use to drive the reference agent
and judges. Retrieval and the MCP tools never call it.

- **Providers.** Anthropic, and anything that speaks OpenAI's `/chat/completions` (OpenAI, Azure
  OpenAI, vLLM, Ollama, LiteLLM, gateways). Optional base URL, default model, API key and extra
  headers (for gateways that authenticate with their own header).
- **Default.** One connection per workspace is the default: curation uses it, and so do eval
  suites that do not pin a model. A suite pinned to a provider uses that provider's connection.
- **Test** makes one tiny call with no retries, before or after saving. Errors are shown with any
  secret scrubbed.
- **Roles.** Curators can see the page; only admins add, edit, test or delete.
- **Base URLs** must be http(s), must not embed credentials, and must not point at link-local or
  cloud metadata addresses (169.254.0.0/16, `metadata.google.internal`). Private hosts are allowed
  because self-hosted models usually live there. The test call runs from the server, so admins
  can reach anything the server can: treat the admin role accordingly.

REST: `GET/POST /v1/llm-connections`, `PATCH/DELETE /v1/llm-connections/{id}`,
`POST /v1/llm-connections/test` (OpenAPI at `/docs`).

### With `SATCHEL_CHAT_*`

If the deployment sets `SATCHEL_CHAT_PROVIDER` (plus `_MODEL`, `_BASE_URL`, `_API_KEY`), that
provider is used for every workspace. Settings shows it read-only with a Test button, and the
workspace connections apply once it is removed. Existing installs keep working unchanged.

## Settings → Connections

Warehouses agents query (`run_sql`, the semantic layer) and that Satchel crawls into the catalog.

- **Kinds.** PostgreSQL (host, port, database, user, optional role for `SET ROLE`, password or a full
  `postgresql://` URL) and DuckDB (a database file on the server). Sessions are read-only with a
  statement timeout; give the user `SELECT` only.
- **BigQuery**: job project, optional data project, location, datasets to catalog (empty: all)
  and a cost guard, **Max GiB billed per query** (default 10). Auth is Application Default
  Credentials (workload identity on GKE / Cloud Run, recommended) or a pasted service-account JSON
  key. Grant `roles/bigquery.dataViewer` and `roles/bigquery.jobUser` only.
- **Snowflake**: account identifier, user, role, warehouse, database, schemas to catalog. Key-pair
  auth only: paste the PEM private key, and its passphrase if it is encrypted (the key is
  decrypted once and the passphrase is not stored). Queries carry `QUERY_TAG = 'satchel'` and
  `STATEMENT_TIMEOUT_IN_SECONDS` from **Limits**. Use a role with only `USAGE` / `SELECT`.
- Setup for both (IAM roles, the read-only Snowflake role, auth choices, cost guard):
  [docs/integrations/warehouses.md](../integrations/warehouses.md). Their libraries are the extras
  `satchel-server[bigquery]` and `satchel-server[snowflake]`; the Docker image includes both. Without the
  library the kind is listed unavailable with the install command.
- Keys (a PEM or JSON key file) are entered in a multi-line box, so newlines are kept. Every
  secret is write-only and sealed with `SATCHEL_ENCRYPTION_KEY`.
- **Test** opens the connection and runs `select 1`, before or after saving. **Crawl** refreshes
  the catalog. **Default for agents** is the connection tools use when the agent names none.
- **Remove** marks the connection's catalogued tables and columns as dropped and detaches them,
  so context that mentions them is flagged by the stale-reference check; items and their history
  are kept. It is refused while a source uses the connection.
- Connections added with the CLI keep working, including `env:NAME` secret references; the UI
  shows them as "environment" and can replace them with a stored secret.

## Sources (Sources page)

Admins add, edit and remove dbt, Cube and SQLMesh sources; curators sync them. The form is
generated from each adapter's own configuration schema.

Where a source's files come from:

1. **A git repository** (Cube, SQLMesh, a committed dbt `target/`): see below.
2. **`s3://` or `gs://` locations**, read on every sync with the server's own cloud identity
   (IRSA / workload identity). A folder input (Cube models, a SQLMesh project) is a prefix. Grant
   read access with the foundation module's `source_artifact_buckets` variable (GCP:
   `roles/storage.objectViewer`; AWS: `s3:GetObject` and `s3:ListBucket`).
3. **Upload and sync** in the UI, or from CI on every build:
   `curl -F manifest=@target/manifest.json -F catalog=@target/catalog.json $SATCHEL_URL/v1/sources/<id>/sync`.
   Uploaded files are used for that sync only.
4. **Server paths**, set with the CLI or `satchel.config.yaml` only.


### From a git repository

A source can read its files straight from git: on the Sources page choose **Git repository**,
give the `https://` URL, a branch, tag or commit (default: the default branch) and, for a private
repository, a read-only token (GitHub fine-grained token with *Contents: read*, GitLab project or
deploy token with `read_repository`, Bitbucket access token plus your username). The file inputs
then become paths inside the repository (`.` is the root). **Test repository** checks the URL,
ref and token (`git ls-remote`) before you save.

Each sync makes a shallow, sparse clone of just those paths into a temporary folder (seconds, even
for a large monorepo), runs the adapter and deletes it; the sync report and the Sources page show
the commit that was synced. The clone is locked down because the repository is chosen by a
workspace admin: https only; the host is resolved once, refused if it is link-local, loopback or a
cloud metadata address, and pinned; redirects are refused; no hooks, submodules, prompts or
system/user git config; symlinks are checked out as plain files, so a committed symlink cannot
expose a server file; paths must stay inside the checkout. The token is stored encrypted like
other source secrets and is sent as an `Authorization` header, never in the URL or on a command
line. The image includes `git`; outbound 443 to the git host must be allowed (the chart's
NetworkPolicy already allows it).

Fits Cube (`model/`) and SQLMesh (the project folder) directly. For dbt, choose **Build it from
the dbt project (dbt parse)** and give the project folder (the one with `dbt_project.yml`) and
the production target schema: each sync runs `dbt deps` and `dbt parse` on the server and imports
the `manifest.json` and `semantic_manifest.json` they write. Parse does not connect to the
warehouse, so there is no `catalog.json`; column types come from crawling the connection. A
repository that commits `target/manifest.json` can give that path instead. See
[dbt › From a git repository](../integrations/dbt.md#from-a-git-repository) for the options and
the sandbox.

dbt runs from its own virtualenv, `/opt/dbt/bin/dbt` in the image (dbt-core and dbt-duckdb,
pinned in `deploy/docker/dbt-requirements.txt`; build with `--build-arg INSTALL_DBT=false` to
leave it out, about 195 MB uncompressed / 57 MB compressed). Elsewhere point `SATCHEL_DBT_BIN` at a dbt
Core 1.8+ binary with `dbt-duckdb` (default: `/opt/dbt/bin/dbt`, then `dbt` on `PATH`). Satchel
runs `dbt --version` once per binary and refuses dbt Fusion, dbt Core older than 1.8, and dbt Core
without `dbt-duckdb`, saying which; without a usable dbt the option is shown as unavailable with
that reason, and the source form shows the dbt Core version when it is available. `SATCHEL_DBT_TIMEOUT_S` (default 300) caps deps + parse. `dbt deps` needs
outbound 443 to hub.getdbt.com and the package hosts unless the source skips it.

### Syncing automatically

Two ways to sync without anyone clicking **Sync now**. Both need a stored location (git path,
`s3://` / `gs://`, or a CLI server path) for every required file input. A source whose files are
only uploaded can't use them (422), because only the uploader has the files.

- **Schedule.** In the source form, **Sync automatically** → every 1, 3, 6, 12, 24 or 168 hours
  (config key `sync_interval_hours`, 1–168; CLI `--sync-interval-hours N`). `satchel worker` checks
  every `SATCHEL_SOURCE_TICK_MIN` minutes (default 5, `0` turns it off on that worker) on its own
  thread, so curation keeps its `SATCHEL_CURATE_INTERVAL_MIN` cadence. A source is due when its
  last sync (manual or not) is older than the interval. After a failed sync it is retried sooner:
  15 minutes later, then 30, 60, 120, ... doubling with each failure in a row, never later than the
  interval; a successful sync resets this. The Sources page shows the next sync time
  (`next_sync_at` over REST). `satchel worker --once`
  (CronJob) runs one check after the curation pass, so the CronJob interval is the shortest
  schedule it can honour.
- **Push webhook.** On the source's detail, an admin clicks **Push webhook → Set up webhook**.
  This shows the URL (`$SATCHEL_PUBLIC_URL/v1/hooks/sources/<id>`) and a generated secret once.
  The secret is sealed in `context_source.config` like other source secrets, so it needs
  `SATCHEL_ENCRYPTION_KEY`. **Rotate secret** replaces it and **Remove webhook** turns it off.
  From the CLI: `satchel source webhook NAME` sets it up and prints the URL and secret (with one
  already set up it prints only the URL), `--rotate` makes a new secret, `--remove` turns it off.
  `satchel source list` shows each source's schedule, next sync and whether a webhook is set up.
  - GitHub: repository → Settings → Webhooks, content type `application/json`, the secret, *Just
    the push event*. Deliveries are checked with `X-Hub-Signature-256`.
  - GitLab: project → Settings → Webhooks, *Secret token*, *Push events*. Deliveries are checked
    with `X-Gitlab-Token`.
  - Any CI can call it too: `curl -X POST -H "X-Gitlab-Token: $SECRET" $URL`.

  The endpoint needs no Satchel token; the secret is compared in constant time. An unknown
  source, a source without a webhook and a bad signature all get the same 401, so the endpoint
  never says whether a source exists. Bodies over 1 MiB get 413.

  For a git source, a push to a branch other than `git_ref` (or, when `git_ref` is unset, the
  repository's default branch named in the payload) gets 200 `ignored`. So does a deleted ref,
  a source pinned to a commit, and any event that isn't a push. GitHub's `ping` gets 200 `pong`.
  A matching push gets 202 and the sync runs after the response. If a sync of that source is
  already running, it waits for it (up to 10 minutes). Deliveries arriving while a webhook sync
  of the same source is still queued or waiting on that server get 202 without starting
  another: the pending sync resolves the ref when it runs, so it picks up the newest commit. A sync lost to a server restart is caught
  by the schedule, if one is set.

For a git source, an automatic sync first resolves the ref (`git ls-remote`). It skips the work
when the ref still points at the commit of the last successful sync and the source's config and
adapter version are unchanged. The skipped check sets `last_synced_at`, because the source is
known to be current, and shows on the Sources page as *Last check … no new commit*. Manual and
CLI syncs always run. Every sync path (Sync now, CLI, schedule, webhook) holds a Postgres
advisory lock on the source, so two replicas never sync the same source at once: a manual sync
during another one gets 409 and the CLI exits with an error. Automatic syncs are audited as
`source.sync` with `trigger` `schedule` or `webhook`, and failures too, with `status: error`. A
skipped unchanged check is not audited.

Restrictions that apply to sources created or edited over REST (the CLI is unchanged):

- File inputs accept only paths inside the source's git repository, `s3://` / `gs://` locations,
  or nothing (for upload). A server path set
  with the CLI is kept when the UI saves the source unchanged, but cannot be set or changed there.
- `*_env` fields (the *name* of a server environment variable, such as Cube's `meta_token_env`)
  are refused, because they could send a server secret to a URL the caller chose. Use the secret
  field (`meta_token`) instead; it is encrypted.
- `*_url` fields must not point at link-local or cloud metadata addresses.
- Removing a source deprecates its items and detaches them; nothing is deleted.

## Settings → Limits

Each limit's default comes from the environment; an admin can override it for their workspace or
set it back to the default. Overrides are stored in `workspace.settings.limits` and audited as
`workspace.limits`.

| Limit | Default (environment) | Allowed override | Applies to |
|---|---|---|---|
| SQL row cap | `SATCHEL_SQL_ROW_CAP` (1000) | 1 – 100,000 rows | `run_sql`, `query_metrics`, eval ground-truth SQL; larger results are truncated |
| SQL timeout | `SATCHEL_SQL_TIMEOUT_S` (30) | 1 – 600 seconds | the same queries; cancelled in the warehouse |
| Result cache | `SATCHEL_RESULT_TTL_DAYS` (7) | 1 – 365 days | new results (existing ones keep their expiry) |
| Session retention | `SATCHEL_SESSION_RETENTION_DAYS` (unset: keep forever) | 7 – 3,650 days, or keep forever | agent sessions, see below |

**Session retention.** After each pass the worker deletes sessions that started more than the
retention period ago, with their tool calls and the record of context served to them, at most 500
per workspace per pass. What points at them is kept and detached: signals (and so the suggestions
built from them), eval results and cached query results. Sessions with feedback are kept, since
feedback is evidence for suggestions. Each deletion is audited as `retention.purge` with counts.
The audit trail itself is never pruned by Satchel; retain or expire the exported objects with your
bucket's lifecycle rules ([storage-and-audit.md](storage-and-audit.md)).

The deployment default can also be set without `extraEnv`: Helm value `sessionRetentionDays`, or
Terraform variable `session_retention_days` on the `cloud-run`, `gke` and `eks` examples and
modules. Both accept 7 to 3,650 days, or null to keep sessions forever.

## Settings → Deployment

Admins see the server's effective configuration, grouped (server, database, sign-in, encryption,
embeddings, curation model, data), each value with the `SATCHEL_*` variable that sets it and
whether it was set or is the default. Secrets show only whether they are set; database URLs hide
their password. It is read-only: change the environment and restart.
