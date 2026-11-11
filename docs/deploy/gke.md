# Deploy Satchel on GKE Autopilot

Terraform creates the shared foundation (Cloud SQL PG17 with pgvector, Secret Manager, buckets, runtime service account), a private GKE Autopilot cluster with Workload Identity and the Secret Manager CSI add-on, a Certificate Manager certificate, and installs the Helm chart in `deploy/helm/satchel` with a `helm_release`. Choose this over [Cloud Run](cloud-run.md) when you already run Kubernetes or need more control over networking and scheduling.

```
Claude / browser ──https──▶ Global external Gateway created by the chart (Certificate Manager cert)
                                   │
                          GKE Autopilot (private nodes, Cloud NAT)
                          Deployment satchel (2 replicas) + Cloud SQL Auth Proxy sidecar (127.0.0.1:5432)
                          CronJob satchel-worker · pre-upgrade hook Job (db bootstrap + upgrade)
                                   │ Workload Identity: KSA satchel ⇒ runtime GSA
                                   ▼
        Cloud SQL PG17 (private IP) · GCS results bucket (gs://) · Secret Manager
```

Test locally: `scripts/floci-e2e.sh` applies this stack on a k3s cluster from the floci-gcp emulator and installs the chart for real ([floci.md](floci.md)).

## 1. Prerequisites

- A Google Cloud project with billing and `roles/owner` for the first apply; `gcloud auth login && gcloud auth application-default login`.
- Terraform 1.6 or newer and `kubectl` with the `gke-gcloud-auth-plugin` (`gcloud components install gke-gcloud-auth-plugin`).
- An OIDC identity provider and a DNS name you control.
- A private GCS bucket for Terraform state (see `examples/gke/backend.tf.example`). State holds the generated database passwords and the OIDC secret, so restrict access to it.
- Your workstation's public IP in `master_authorized_networks` if you lock down the control plane, because Terraform's Kubernetes and Helm providers reach the public endpoint from where you run it.

The image defaults to `ghcr.io/getsatchel/satchel:<tag>`. GKE nodes pull ghcr.io directly through Cloud NAT, so no Artifact Registry remote repository is created in this example.

## 2. Create the OIDC client

Create a web application client in your identity provider:

- Redirect URI: `https://<domain>/auth/callback`
- Scopes: `openid email profile`

Keep the issuer, client id and client secret.

## 3. terraform apply

```bash
cd deploy/terraform/examples/gke
cp terraform.tfvars.example terraform.tfvars    # edit
cp backend.tf.example backend.tf                # edit
export TF_VAR_oidc_client_secret='...'
terraform init
# the Kubernetes and Helm providers are configured from the cluster, so create it first
terraform apply -target=module.gke.google_container_cluster.this -target=module.foundation
terraform apply
```

The first apply takes 15 to 25 minutes (Cloud SQL and the cluster). The second installs the chart. `helm_release` runs with `atomic = true`, so a failed install is rolled back.

Then create a DNS A record for the domain pointing at `terraform output gateway_address`. The module reserves this global static IP up front and the chart attaches it to the Gateway (`gateway.staticIpName`), so you can set DNS before the chart installs and the address survives Gateway changes. The Certificate Manager certificate and the Gateway become healthy once DNS resolves (up to an hour).

## 4. Populate secrets

On GKE the secrets reach pods through two Kubernetes Secrets that Terraform builds from the generated values and `oidc_client_secret`:

- `satchel-secrets` (the chart's `existingSecret`): `SATCHEL_DATABASE_URL`, `SATCHEL_ADMIN_DATABASE_URL`, `SATCHEL_MIGRATION_DATABASE_URL`, `SATCHEL_OIDC_CLIENT_SECRET`, optionally `SATCHEL_CHAT_API_KEY`. Web and worker pods read this.
- `satchel-bootstrap` (the chart's `bootstrap.existingSecret`): `SATCHEL_BOOTSTRAP_DATABASE_URL`, `SATCHEL_APP_DB_PASSWORD`, `SATCHEL_WORKER_DB_PASSWORD`. Only the migrate hook reads it, so the owner credential never reaches web or worker pods. After the first install set `bootstrap_enabled = false` and apply.

The same values are also in Secret Manager (`satchel-database-url`, `satchel-oidc-client-secret`, ...) if you would rather manage them outside Terraform with the chart's `secretManager` values.

To rotate the OIDC secret: change `TF_VAR_oidc_client_secret`, `terraform apply`, and restart the pods (`kubectl -n satchel rollout restart deploy/satchel`).

## 5. Run migrate

The chart runs `satchel db bootstrap` and `satchel db upgrade` in a Helm pre-install and pre-upgrade hook Job, before the Deployment rolls out, so the first `terraform apply` already migrated. To run it by hand, or to see the result:

```bash
$(terraform output -raw get_credentials_command)
kubectl -n satchel get jobs
kubectl -n satchel logs job/<migrate job>
```

The database is private-IP only, so run any ad hoc `satchel` command from inside the cluster, never from your laptop.

## 6. Create the first workspace

Easiest: set `first_workspace = { slug = "acme", name = "Acme", admin_email = "you@example.com" }` and apply; the chart runs it as a post-install Job. Or by hand:

```bash
kubectl -n satchel exec deploy/satchel -c satchel -- \
  satchel workspace create acme --name Acme --admin-email you@example.com
```

The command is idempotent. When `you@example.com` signs in through OIDC they become admin.

## 7. Connect Claude

The MCP endpoint is `https://<domain>/mcp`.

- Claude Code: `claude mcp add --transport http satchel https://<domain>/mcp`, then `/mcp` inside Claude Code to sign in.
- Claude Desktop: Settings, Connectors, Add custom connector, paste the `/mcp` URL.

The Gateway backend timeout is raised to an hour so MCP server-sent-event streams are not cut.

## 8. Upgrades

1. Set `image_tag = "0.2.0"`.
2. `terraform apply`. The Helm pre-upgrade hook migrates first; if it fails, `atomic = true` rolls the release back and the old pods keep serving.
3. Chart-only changes (a new chart version in the repository) apply the same way.

Migrations are forward-only. Take `gcloud sql backups create --instance INSTANCE` before risky upgrades.

## 9. Backups and restore

Cloud SQL keeps 14 daily automated backups and 7 days of point-in-time recovery logs.

```bash
gcloud sql backups list --instance INSTANCE
gcloud sql backups restore BACKUP_ID --restore-instance INSTANCE         # replaces current data
gcloud sql instances clone INSTANCE satchel-restore --point-in-time 2026-11-05T09:30:00Z
```

Scale the Deployment to zero while restoring in place (`kubectl -n satchel scale deploy/satchel --replicas=0`). Results in the results bucket are disposable; audit exports in the audit bucket are versioned, with an optional retention policy (`audit_retention_days`).

## 10. Costs

Rough monthly estimate for a small team, list prices in `us-central1`, check the current pricing pages:

| Item | Approx. USD / month |
|---|---|
| Cloud SQL PG17, `db-custom-1-3840`, zonal, 10 GB, backups | 50 to 60 |
| GKE Autopilot cluster fee (one zonal or Autopilot cluster per billing account is covered by Google's monthly free credit, about 74) | 0 to 73 |
| Autopilot pods: 2 replicas plus Auth Proxy sidecars, roughly 1 vCPU and 3 GiB requested in total | 40 to 70 |
| Cloud NAT and external HTTPS load balancer (Gateway) | 25 to 50 |
| Secrets, logs, buckets | 2 to 5 |
| **Total** | **about 120 to 260** |

For a small team Cloud Run is roughly half the price. GKE is worth it when the cluster is shared with other workloads (the cluster fee and NAT are then spread).

## 11. Teardown

The cluster and Cloud SQL are both deletion-protected, so `terraform destroy` fails by design until you lift protection.

```bash
cd deploy/terraform/examples/gke
gcloud sql backups create --instance INSTANCE         # final backup, if wanted
terraform apply \
  -var database_deletion_protection=false \
  -var cluster_deletion_protection=false
gcloud storage rm -r "gs://PROJECT-satchel-audit/**"     # audit bucket refuses to be destroyed while non-empty
terraform destroy \
  -var database_deletion_protection=false \
  -var cluster_deletion_protection=false
```

Destroy order matters for the Kubernetes providers: if the cluster is already gone, run `terraform state rm module.gke.helm_release.satchel module.gke.kubernetes_secret_v1.satchel module.gke.kubernetes_namespace_v1.this` first. The Gateway's load balancer resources are created by GKE; deleting the Helm release removes them, so delete it before the cluster. Private Service Access sometimes refuses to be removed for a few minutes after Cloud SQL is deleted; rerun `terraform destroy`.

## Reference: Helm values the module sets

The values are produced by `modules/helm-values` and use the chart's own keys (`deploy/helm/satchel/values.yaml`; the chart has a strict `values.schema.json`): `image.{repository,tag}`, `publicUrl`, `env`, `logFormat`, `resultDir` (a `gs://` path), `existingSecret` (a Secret name), `oidc.{issuer,clientId}`, `chat.*`, `web.replicaCount`, `autoscaling.*`, `worker.mode = cronjob`, `serviceAccount.{create,name,gcpServiceAccount}` (Workload Identity), `cloudSqlProxy.{enabled,instanceConnectionName,privateIp}`, `bootstrap.{enabled,existingSecret}`, `firstWorkspace.*`, `gke.enabled`, `exposure.{type=gateway,host,timeoutSec}` and `gateway.{create,className,certificateMap}`. Add anything else through `helm_extra_values`.

CI runs `deploy/terraform/scripts/check-helm-values.sh`, which renders these values with sample inputs and runs `helm lint --strict`, `helm template` and kubeconform against the real chart, so the module and chart cannot drift apart unnoticed.
