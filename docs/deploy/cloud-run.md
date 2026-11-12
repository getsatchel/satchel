# Deploy Satchel on Google Cloud Run

Terraform in `deploy/terraform` builds the whole stack: Cloud SQL for PostgreSQL 17 with pgvector, Secret Manager, two buckets, two least-privilege service accounts (`satchel-runtime` for the service, worker and audit export; `satchel-migrate` for the migrate job only, the one identity that can read the schema owner's credentials and the role passwords), a Cloud Run service (`satchel`), three Cloud Run Jobs (`satchel-migrate`, `satchel-worker`, `satchel-audit-export`) and Cloud Scheduler. An external HTTPS load balancer with Cloud Armor and IAP is optional.

```
Claude / browser ──https──▶ Cloud Run service "satchel"  (serve api: UI + REST + MCP + OAuth)
                                  │  unix socket /cloudsql (built-in Cloud SQL connector)
Cloud Scheduler ──▶ Cloud Run Jobs│  satchel-worker (every 15 min) · satchel-audit-export (daily) · satchel-migrate (on demand)
                                  ▼
              Cloud SQL PG17 (private IP)   GCS results bucket (SATCHEL_RESULT_DIR)   GCS audit bucket
```

The code is split into modules you can reuse: `modules/foundation` (shared by Cloud Run and GKE), `modules/cloud-run` and `examples/cloud-run` (a ready root config).

Test locally: `scripts/floci-e2e.sh` applies this stack against the floci-gcp emulator and runs the migrate, worker and audit jobs and the service for real ([floci.md](floci.md)).

<!-- site: InteractiveCloudDiagram initialCloud=cloudrun -->

## 1. Prerequisites

- A Google Cloud project with billing enabled, and `roles/owner` (or equivalent) for the first apply.
- `gcloud` authenticated: `gcloud auth login && gcloud auth application-default login`.
- Terraform 1.6 or newer.
- An OIDC identity provider (Google, Okta, Entra, Auth0, Keycloak...). Satchel does its own login; production refuses to start without OIDC.
- A decision on the URL. Without a domain you get `https://satchel-<project number>.<region>.run.app`. With a domain set `domain` and Terraform adds a load balancer and a managed certificate.
- A state backend. State contains the generated database passwords, so use a private GCS bucket (`examples/cloud-run/backend.tf.example`) and restrict access to it.

The image is `ghcr.io/getsatchel/satchel:<version>`. **Cloud Run cannot pull from ghcr.io directly.** The foundation module therefore creates an Artifact Registry remote repository that proxies `ghcr.io`, and the service pulls `REGION-docker.pkg.dev/PROJECT/satchel-ghcr/getsatchel/satchel:<version>` through it. To use your own image instead, set `create_ghcr_remote_repository = false` and `image_repository_override`.

## 2. Create the OIDC client

In your identity provider create a web application client:

- Redirect URI: `<public url>/auth/callback` (for example `https://satchel.example.com/auth/callback`; with no domain use the run.app URL, which Terraform prints as `oidc_redirect_uri_hint`).
- Scopes: `openid email profile`.

Note the issuer URL, client id and client secret. You will pass the first two as variables and put the secret into Secret Manager yourself (step 4), so it never touches Terraform state.

## 3. terraform apply

```bash
cd deploy/terraform/examples/cloud-run
cp terraform.tfvars.example terraform.tfvars    # edit
cp backend.tf.example backend.tf                # edit, optional but recommended
terraform init
terraform apply -var deploy_service=false
```

The first apply deliberately skips the service. It would fail its `/readyz` startup probe (the schema does not exist yet) and the OIDC secret has no value yet. Everything else is created: APIs, VPC and Private Service Access, Cloud SQL (takes 10 to 15 minutes), secrets, buckets, service account, the jobs and the schedules.

Shortcut: if `gcloud` is installed on the machine running Terraform, set `run_migrations_on_apply = true`. The migrate job is then executed (and waited on) before the service is created or updated whenever the image changes. You still need step 4 first, so run `terraform apply -var deploy_service=false`, add the secret, then do a normal apply.

## 4. Populate secrets

Generated for you (do not touch): owner, `satchel_app` and `satchel_worker` passwords and the three database URLs. Yours to fill in:

```bash
printf '%s' "$OIDC_CLIENT_SECRET" | gcloud secrets versions add satchel-oidc-client-secret --data-file=- --project "$PROJECT"
# only if enable_chat_api_key = true
printf '%s' "$CHAT_API_KEY"       | gcloud secrets versions add satchel-chat-api-key --data-file=- --project "$PROJECT"
```

Both are read as `latest` when a container starts. If you rotate one later, deploy a new revision (`terraform apply` after changing anything, or `gcloud run services update satchel --region REGION --update-labels rotated=$(date +%s)`).

## 5. Run migrations

```bash
gcloud run jobs execute satchel-migrate --region REGION --project PROJECT --wait
```

`satchel-migrate` runs `satchel db bootstrap && satchel db upgrade`. Bootstrap is idempotent: it creates the `vector` extension and the `satchel_app` and `satchel_worker` roles (both LOGIN, no BYPASSRLS) and the grants. Upgrade applies migrations as the owner. **pgvector on Cloud SQL needs no database flag.** It is an ordinary extension, and the `satchel` owner user is a member of `cloudsqlsuperuser`, which may create it.

Then bring up the service:

```bash
terraform apply            # deploy_service defaults to true
```

Check it: `curl -fsS "$(terraform output -raw url)/readyz"`.

## 6. Create the first workspace

```bash
gcloud run jobs execute satchel-migrate --region REGION --project PROJECT --wait \
  --args="-c,satchel workspace create acme --name Acme --admin-email you@example.com"
```

(This reuses the migrate job because it already has every database setting; `--args` replaces its normal command for this one execution.) The command is idempotent. When `you@example.com` logs in through OIDC they become admin of the workspace.

## 7. Connect Claude

The MCP endpoint is `<url>/mcp` (`terraform output mcp_url`). Authentication is OAuth: the client discovers it from `/.well-known`, sends you through your OIDC login once, and keeps a token.

- Claude Code: `claude mcp add --transport http satchel https://satchel.example.com/mcp`, then `/mcp` inside Claude Code to authenticate.
- Claude Desktop: Settings, Connectors, Add custom connector, paste the `/mcp` URL, then sign in.

## 8. Upgrades

1. Pick the new tag, set `image_tag = "0.2.0"`.
2. `gcloud run jobs execute satchel-migrate ...` is run for you if `run_migrations_on_apply = true`; otherwise it must run **before** the service rolls out. Do it with the new image: `terraform apply -target=module.cloud_run.google_cloud_run_v2_job.this` first (this updates the jobs to the new image), run the migrate command, then a full `terraform apply`.
3. If you forget, the new revision cannot pass its `/readyz` startup probe (schema not at head), so Cloud Run keeps serving the old revision and the apply fails without downtime. Run the migration and apply again.

Migrations are forward-only. Take an on-demand backup first for anything risky: `gcloud sql backups create --instance INSTANCE`.

Use version tags. A floating tag such as `latest` does not create a new revision.

## 9. Backups and restore

Cloud SQL takes a daily automated backup (14 kept) and keeps 7 days of logs for point-in-time recovery.

```bash
gcloud sql backups list --instance INSTANCE
# restore a backup over the instance (destructive: replaces current data)
gcloud sql backups restore BACKUP_ID --restore-instance INSTANCE
# or clone to a point in time without touching production
gcloud sql instances clone INSTANCE satchel-restore --point-in-time 2026-11-05T09:30:00Z
```

Result objects in the results bucket are disposable (deleted after `result_ttl_days + 1` days). Audit exports in the audit bucket are versioned; set `audit_retention_days` for a retention policy (and `audit_retention_locked` only if you accept that it can never be shortened).

## 10. Costs

Rough monthly estimate for a small team (under 20 people), list prices in `us-central1`, check the current Google Cloud pricing page:

| Item | Setup | Approx. USD / month |
|---|---|---|
| Cloud SQL PG17, `db-custom-1-3840`, zonal, 10 GB SSD, backups | default | 50 to 60 |
| Cloud SQL regional (HA) | `database_availability_type = "REGIONAL"` | add about 50 |
| Cloud Run service, 1 CPU / 2 GiB, 1 always-on instance (idle billing) plus light traffic | default | 20 to 30 |
| Jobs, scheduler (3 free), secrets, logs, buckets, registry | | 2 to 5 |
| **Total without a load balancer** | | **about 75 to 100** |
| External HTTPS load balancer (optional) | `domain` set | add about 20 |
| Cloud Armor (optional) | | add about 6 |

`min_instances = 1` is the default because MCP clients time out on cold starts. Setting it to 0 saves most of the Cloud Run line but the first request after a quiet period takes several seconds. Private IP and Direct VPC egress have no extra charge.

## 11. Teardown

Cloud SQL is protected twice (`deletion_protection` on the instance, and Terraform's own lifecycle guard), so destroy fails by design.

```bash
cd deploy/terraform/examples/cloud-run
# 1. take a final backup if you might want the data
gcloud sql backups create --instance INSTANCE
# 2. lift protection (this applies an in-place change)
terraform apply -var database_deletion_protection=false
# 3. empty the audit bucket (it refuses to be destroyed while it holds exports)
gcloud storage rm -r "gs://PROJECT-satchel-audit/**"
terraform destroy -var database_deletion_protection=false
```

If a locked audit retention policy is set, objects cannot be deleted until they age out, and the bucket cannot be removed before then. Destroying the Private Service Access peering sometimes fails with "producer services are still using this connection" for a few minutes after Cloud SQL is deleted; wait and rerun `terraform destroy`. Secret Manager secret names can be reused immediately; Cloud SQL instance names cannot for about a week (the module appends a random suffix, so a fresh apply is not blocked).

## Reference

### Why the service is publicly invokable

`allow_unauthenticated = true` grants `roles/run.invoker` to `allUsers`. Satchel authenticates every request itself (OIDC session cookie for the UI, personal access tokens or OAuth access tokens for REST and MCP), and MCP and OAuth clients cannot present Google identity tokens, so Cloud Run IAM in front of the service would lock them out. If an organization policy forbids `allUsers`, set it to false and put an authenticating front end in front of the service.

### Load balancer, IAP and the path split

With `domain` set the module creates a global external HTTPS load balancer (serverless NEG, managed certificate, HTTP to HTTPS redirect) and switches the service ingress to `INTERNAL_LOAD_BALANCER`. Create an A record for the `lb_ip_address` output; the certificate becomes active once DNS resolves (it can take up to an hour).

Two backend services point at the same Cloud Run service:

- Public (never behind IAP): `/mcp`, `/oauth/*`, `/auth/*`, `/.well-known/*`, `/v1/*`, `/healthz`, `/readyz`.
- UI (behind IAP if `enable_iap = true`): everything else, which is the single-page UI shell.

MCP and OAuth must stay reachable for OAuth clients, and REST callers send bearer tokens that IAP would reject. IAP therefore gates only who can load the UI shell; data access is always enforced by Satchel itself. `enable_iap` needs an OAuth client (`iap_oauth_client_id` / `iap_oauth_client_secret`) and `enable_cloud_armor` adds a per-IP rate limit and an optional CIDR allow-list.

### Scheduling

`satchel-worker` runs `satchel worker --once` every 15 minutes (`worker_schedule`) and `satchel-audit-export` exports the previous 24 hours to `gs://<audit bucket>/audit/YYYY/MM/DD/` daily (`audit_export_schedule`). Set either to `null` to disable. Cloud Scheduler calls the Cloud Run Admin API as a dedicated service account that holds `run.invoker` on those two jobs only.
