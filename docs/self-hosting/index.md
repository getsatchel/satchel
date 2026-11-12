# Self-hosting Satchel

Satchel is designed to be self-hosted. This page covers what you run, what you need, and how to choose a
deployment path.

## Components

| Component | Required | Notes |
|---|---|---|
| **web** (`satchel serve api`) | yes | UI, REST `/v1`, MCP `/mcp`, OAuth/OIDC on one port (8080). Stateless; scale horizontally. Also runs queued eval runs, source syncs, crawls and curation runs (`SATCHEL_WEB_JOB_THREADS`). |
| **worker** (`satchel worker`) | recommended | Queues curation per workspace, expired result purge, session retention, scheduled source syncs, [scheduled eval runs](../concepts/evals.md#scheduled-runs) and any queued job. Run as a Deployment, or a CronJob with `--once`. |
| **migrate Job** (`satchel db upgrade`) | yes | Runs before each release. Optionally runs `satchel db bootstrap` first. |
| **Postgres 17 + pgvector** | yes | The only required dependency. The app connects as a non-superuser role so RLS applies. |
| **Object store** (GCS or S3) | optional | Result cache (Parquet) and audit exports. A local directory works for one replica. |
| **OIDC identity provider** | production | Okta, Google, Entra, Dex. `SATCHEL_DEV_AUTH` is for localhost only. |
| **LLM / embedding APIs** | optional | Needed to draft suggestions. Embeddings default to local `fastembed`. |

## Deployment options

| Option | Best for | Guide |
|---|---|---|
| `satchel init` + `satchel up` | Laptop, evaluation | [Install](../install.md) |
| Terraform on Cloud Run | Google Cloud, single service + scheduled Jobs | [Cloud Run](../deploy/cloud-run.md) |
| Container | Single host with Caddy/Nginx, Docker Compose, ECS, your own platform | [Container](../deploy/container.md) |
| Helm | Any Kubernetes 1.29+ | [Helm](../deploy/helm.md) |
| Terraform + Helm on GKE | Google Cloud, multi-replica Kubernetes | [GKE](../deploy/gke.md) |
| Terraform + Helm on EKS | AWS, multi-replica Kubernetes | [EKS](../deploy/eks.md) |

<!-- site: InteractiveCloudDiagram initialCloud=cloudrun -->

## Prerequisites

- A Postgres 17 database with the `vector` extension and a superuser-capable URL for the one-time bootstrap.
- A public HTTPS URL for `SATCHEL_PUBLIC_URL` (required in production; MCP clients and OIDC callbacks use it).
- An OIDC client for the UI and for MCP OAuth.
- A 256-bit `SATCHEL_ENCRYPTION_KEY` if admins will store API keys or warehouse passwords in the UI
  (`satchel secrets generate-key`).

<!-- site: SelfHostingSizer -->

## Production checklist

- [ ] `SATCHEL_ENV=production`, `SATCHEL_DEV_AUTH` unset.
- [ ] HTTPS in front, and the MCP stream not buffered or timed out by the proxy ([Helm: exposure](../deploy/helm.md)).
- [ ] Three database roles: owner (migrations), `satchel_app` (requests, RLS applies), `satchel_worker` (internal jobs). See [Database](../deploy/database.md).
- [ ] Secrets in a secret manager, not in Helm values ([Helm: secrets](../deploy/helm.md)).
- [ ] `SATCHEL_ENCRYPTION_KEY` backed up separately from the database.
- [ ] Result store on GCS or S3 if running more than one replica ([Storage](../deploy/storage-and-audit.md)).
- [ ] Audit export scheduled (`satchel audit export`).
- [ ] Postgres backups and a tested restore ([Operations](operations.md)).
- [ ] `satchel doctor` clean.

## Configuration model

Deployment-level values live in the environment; per-workspace values (models, connections, limits,
sources) live in Settings in the UI. The environment wins. See [Configuration](../deploy/configuration.md)
and the [environment variable reference](../reference/environment-variables.md).
