# Storage and audit

How Satchel stores cached query results when you run more than one replica, how expired results are
cleaned up, and how to get the audit trail out of Satchel.

## Result storage

`run_sql` and `query_metrics` write each result set as Parquet; `get_result` pages through it
later, possibly on a different replica. `SATCHEL_RESULT_DIR` picks the store:

| Value | Store |
|---|---|
| a path (default `~/.cache/satchel/results`) | local directory. Single replica / local use only. |
| `gs://bucket/prefix` | Google Cloud Storage. One object store is required with more than one replica. |
| `s3://bucket/prefix` | Amazon S3 (or an S3-compatible store). |

GCS needs the `gcp` extra (`uv tool install --force "satchel-server[gcp]"`) and S3 the `aws` extra
(`satchel-server[aws]`); the container image includes both. If the extra is missing,
Satchel fails with an error naming it. Credentials never come from Satchel's own settings:

- **GCS:** Application Default Credentials only (workload identity on GKE / Cloud Run, or
  `gcloud auth application-default login` locally); key files are not supported. The service
  account needs `roles/storage.objectAdmin` on the bucket (create, read, delete objects).
- **S3:** the AWS default credential chain (IRSA or EKS Pod Identity on EKS, an ECS task role, an
  instance profile, or `AWS_PROFILE` locally). The role needs `s3:GetObject`, `s3:PutObject`,
  `s3:DeleteObject` on `bucket/prefix/*` and `s3:ListBucket` on the bucket, plus `kms:Decrypt` /
  `kms:GenerateDataKey` when the bucket uses SSE-KMS (the `aws-foundation` Terraform module
  writes exactly this policy). Objects inherit the bucket's default encryption. The region comes
  from `AWS_REGION`. `AWS_ENDPOINT_URL_S3` (or `AWS_ENDPOINT_URL`) points the client at an
  S3-compatible endpoint such as MinIO or an emulator, and switches it to path-style addressing.

Details:

- `query_result.path` stores a URI: `gs://` or `s3://bucket/prefix/<workspace>/<id>.parquet`, or
  `file:///abs/path.parquet`. Rows written by older versions hold a path relative to the result
  directory and keep working locally.
- Reads never depend on the currently configured store, so switching `SATCHEL_RESULT_DIR` leaves
  existing results readable until they expire (the old bucket/directory must stay reachable).
- Parquet is written with 1000-row row groups. An object-store page read fetches the footer plus
  the row groups the page touches with ranged reads (HTTP `Range` on S3); it does not download the
  object.
- Results stay private to the session's user, as before; the bucket holds query output, so give it
  the same access controls as the warehouse data. Use bucket lifecycle rules only as a backstop
  (Satchel deletes expired objects itself).

### Expiry cleanup

Results expire after `SATCHEL_RESULT_TTL_DAYS` (default 7). `satchel worker` deletes expired
`query_result` rows and their objects at the end of every pass (including `--once`). To run it by
hand:

```
satchel results purge --dry-run   # count only
satchel results purge
```

The object is deleted first; if that fails the row is kept and retried on the next pass. An object
that is already gone does not block deleting the row. The command exits 1 if any deletion failed.

## Audit trail

`audit_event` records who did what, per workspace. Actions currently recorded:

| Action | Written by |
|---|---|
| `item.create`, `item.update`, `item.overlay`, `item.deprecate` | curator API |
| `release.publish`, `release.rollback`, `release.publish_override` | curator API |
| `proposal.accept`, `proposal.reject`, `suggestion.*` | curator API |
| `curate.run` | **Run curation** (`POST /v1/curate/run`, admin) or the worker's pass; a queued run has `job_id` and the admin who asked as actor, a scheduled one no actor |
| `job.retry` | **Retry** on the Jobs page (`POST /v1/jobs/{id}/retry`, curator), with `job_id` and `kind` |
| `eval.suite.save`, `eval.run.start`, `eval.gate.start` | evals API; scheduled runs write `eval.run.start` from the worker (`trigger: schedule`, no actor) |
| `session.sensitive` | observability API |
| `source.queue_sync` | **Sync now** / `POST /v1/sources/{id}/sync` without files: the sync was queued (`job_id`); the `source.sync` event follows when it runs, with the same actor |
| `source.sync` | sources API (`trigger: manual`), worker schedule and push webhook (`trigger: schedule` / `webhook`, no actor; webhook adds `provider`, `ref`, `delivery`; failed automatic syncs have `status: error`) |
| `sync.preview`, `sync.import` | context repo uploads (`POST /v1/sync/preview`, `POST /v1/sync/import`, e.g. the GitHub Action), with the git `ref` |
| `source.webhook.create`, `source.webhook.rotate`, `source.webhook.remove` | A source's page → Automation (never the secret) |
| `token.create`, `token.revoke` | PAT creation / revocation (CLI, `satchel init`) |
| `user.create`, `user.role_change`, `user.provision` | token / workspace bootstrap, OIDC auto-provisioning |
| `workspace.create`, `workspace.auth.update` | workspace bootstrap, `satchel workspace auth` |
| `source.add`, `source.update`, `source.remove` | `satchel source add/remove`, Sources page (changed field names, never secret values) |
| `connection.add`, `connection.update`, `connection.remove` | `satchel connection add`, Settings → Connections (`secret_ref` for env references or `"stored"`, never a secret) |
| `auth.login` | OIDC login (web and MCP flows) |
| `oauth.consent.approve`, `oauth.consent.deny` | OAuth consent screen (client id, scope, redirect host) |
| `llm_connection.create`, `llm_connection.update`, `llm_connection.delete` | Settings → Models (names of changed fields, never secret values) |
| `workspace.limits` | Settings → Limits (changed limit names and the workspace's overrides) |
| `retention.purge` | worker session retention (counts of sessions and tool calls deleted, no actor) |

CLI-originated events have no actor (`actor_user_id` null). OAuth dynamic client registration is
global (the `oauth_client` table has no workspace), so it is not in a workspace trail; it is logged
by the `satchel.oauth` logger, and the workspace-scoped record is `oauth.consent.*` when a user approves
the client.

### `GET /v1/audit` (SIEM pull)

Admin role, scoped to the caller's workspace. Query: `since`, `until` (ISO-8601; `since` inclusive,
`until` exclusive), `cursor`, `limit` (1-1000, default 500). Events are ordered by
`(created_at, id)`; follow `next_cursor` until it is null. To poll, keep the last event's
`created_at` as the next `since`.

```
curl -H "Authorization: Bearer $TOKEN" \
  "$SATCHEL_URL/v1/audit?since=2026-11-01T00:00:00Z&limit=500"
```

### `satchel audit export`

```
satchel audit export --since 2026-11-01 [--until 2026-12-01] [--workspace acme] \
  [--to events.jsonl | --to gs://bucket/prefix | --to s3://bucket/prefix]
```

JSON Lines, one event per line (`id`, `workspace`, `workspace_id`, `actor_user_id`, `actor_email`,
`action`, `target`, `created_at`). With no `--to` it writes to stdout; with a file it writes exactly
the requested range. With `gs://bucket/prefix` or `s3://bucket/prefix` it writes one object per workspace and UTC day,
`<prefix>/audit/<workspace>/<yyyy>/<mm>/<dd>.jsonl`. Object-store exports are idempotent: the range is
widened to whole UTC days and each day's object is overwritten, so a daily Cloud Scheduler / CronJob
run with an overlapping `--since` never leaves a partial day. Without `--workspace` every workspace
is exported (needs the worker role; run it as a Job).
