# Operations: upgrades, backups, troubleshooting

## Upgrading

Satchel migrations are explicit Alembic revisions applied by `satchel db upgrade`. They run on an empty
database and on existing ones, so upgrades are in place.

| Deployment | How |
|---|---|
| Local | `satchel upgrade` (reinstalls and migrates). |
| Helm | `helm upgrade`; the pre-upgrade hook Job runs migrations before new pods start. |
| Cloud Run | Roll the image, then run the migrate Job. See [Cloud Run §8](../deploy/cloud-run.md). |
| Container | Run `satchel db upgrade` with the owner URL, then roll the replicas. |

Back up first. Roll web and worker to the same version. `satchel status` shows the schema revision against head.

## Backups

- **Postgres** holds everything that matters: context, releases, sessions, sealed secrets. Use your platform's
  snapshots or PITR (Cloud SQL, RDS) and test a restore.
- **`SATCHEL_ENCRYPTION_KEY`** is not in the database. Back it up with your other secrets; without it stored
  secrets are unreadable ([details](../deploy/configuration.md)).
- **Result cache** is disposable (expires after `SATCHEL_RESULT_TTL_DAYS`).
- **Audit trail**: export with `satchel audit export` ([details](../deploy/storage-and-audit.md)).

## Health and logs

- `/healthz` (liveness) and `/readyz` (readiness).
- `SATCHEL_LOG_FORMAT=json` for structured logs.
- `satchel doctor` checks the database, RLS role, schema, token, embedder, result dir, connections and client
  configs, and prints the fix for each failure. It exits 1 on failure, so it works in CI.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `password authentication failed for user "satchel_worker"` | Run `satchel db upgrade`; otherwise reset the role password ([Database](../deploy/database.md)). |
| UI redirects forever or login fails | `SATCHEL_PUBLIC_URL` must match the URL users visit and the OIDC redirect URI. |
| MCP client connects then drops | Proxy buffering or idle timeout on the stream ([Helm: exposure](../deploy/helm.md)). |
| "sealed with key …" on a connection | `SATCHEL_ENCRYPTION_KEY` changed; restore it or re-enter secrets ([rotation](../deploy/configuration.md)). |
| Cannot save an LLM API key | `SATCHEL_ENCRYPTION_KEY` is not set. |
| Suggestions never appear | No chat model configured (Settings → Models or `SATCHEL_CHAT_*`); clustering still runs. |
| Search quality dropped after changing embeddings | `satchel sync reembed -w WORKSPACE`. |
| Results missing on another replica | Local `SATCHEL_RESULT_DIR` with several replicas; use `gs://` or `s3://`. |
