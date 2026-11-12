# CLI reference

`satchel --help` and `satchel <command> --help` are authoritative. Global option: `--profile`
(`SATCHEL_PROFILE`, default `local`) selects a profile in `~/.config/satchel/config.toml`.

## Local onboarding

| Command | Purpose |
|---|---|
| `init` | Set up Postgres, migrations, workspace, admin, token and config. `--example jaffle` loads sample data. |
| `up` / `down` | Start or stop the server. `down --all` also stops Postgres; `--purge` deletes data. |
| `status` | Version, config, database, schema, server, token, workspace, connections. |
| `doctor` | Check everything and print the fix for each failure. Exits 1 on failure. |
| `connect <client>` / `disconnect <client>` | Add or remove Satchel in a client's MCP config (`claude-desktop`, `claude-code`, `cursor`). |
| `upgrade` | Reinstall and migrate. |
| `version` | Print the version. |

## Servers and jobs

| Command | Purpose |
|---|---|
| `serve api` | UI + REST + `/mcp` + OAuth/OIDC. |
| `serve mcp` | Only `/mcp` and the OAuth routes. |
| `mcp stdio` | MCP over stdio (needs `SATCHEL_TOKEN` and `SATCHEL_WORKSPACE`). |
| `worker [--once]` | Queues curation for every workspace on an interval, and scheduled syncs and eval runs, and runs queued jobs; `--once` runs one pass and every due job, for Jobs/CronJobs. |
| `curate run` / `curate show` | Run the curation pipeline now, in this process; list suggestions. `--no-llm` skips model calls. |

## Database and secrets

| Command | Purpose |
|---|---|
| `db bootstrap` | Create the extension and the `satchel_app` and `satchel_worker` roles (superuser, once). |
| `db upgrade [REVISION]` | Run migrations (default `head`). |
| `secrets generate-key` | Print a new `SATCHEL_ENCRYPTION_KEY`. |
| `secrets rotate [--dry-run]` | Re-encrypt stored secrets under the current key. |
| `audit export` | Export audit events as JSON Lines. |
| `results purge` | Delete expired cached results. |

## Workspaces, access, data

| Command | Purpose |
|---|---|
| `workspace create` / `auth` / `curation` | Create a workspace and admin; OIDC user mapping; curation thresholds and instructions. |
| `token create` / `token revoke` | Personal access tokens (printed once). |
| `connection add` / `list` / `crawl` | Warehouse connections; `crawl` introspects into assets. |
| `search` | Hybrid search over published context. |

## Context, sources, evals

| Command | Purpose |
|---|---|
| `sync import` / `sync reembed` | Import a context repo folder; re-embed chunks after a model change. |
| `source kinds` / `list` / `info` / `add` / `sync` / `remove` / `validate` | Manage dbt, Cube, SQLMesh and external adapters. |
| `eval run` / `compare` / `gate` / `retrieval` / `suite` | Run suites, compare runs, gate publishing in CI, measure retrieval. |
| `openapi` | Print the REST OpenAPI document. |
