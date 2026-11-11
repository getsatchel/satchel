# Deploying Satchel with Helm

The chart in `deploy/helm/satchel` runs Satchel on GKE, EKS or any Kubernetes cluster (1.29+). One image serves
every role: `satchel serve api` (UI, REST, MCP at `/mcp`, OAuth/OIDC on one port), `satchel worker`, and the
database commands. Everything is stateless; sessions and OAuth state live in Postgres, so replicas are safe.

```
helm install satchel oci://ghcr.io/getsatchel/charts/satchel --version 0.2.0 \
  -n satchel --create-namespace -f my-values.yaml
```

Each `v*` release publishes the chart to `oci://ghcr.io/getsatchel/charts/satchel`, with the chart version and `appVersion`
equal to the release, so the chart deploys the image of the same version (`image.tag` defaults to `appVersion`). Upgrade
with `helm upgrade satchel oci://ghcr.io/getsatchel/charts/satchel --version <new>`. To install from a checkout instead,
use the path `deploy/helm/satchel`. The example values files below are in that directory.

Example values: `values-gke.yaml` (Cloud SQL Auth Proxy, Workload Identity, Gateway API, Secret Manager),
`values-eks.yaml` (RDS, IRSA, ALB Ingress, AWS Secrets Manager) and `values-dev.yaml` (kind, in-cluster
pgvector, dev auth; not for production). The Terraform stacks in [gke.md](gke.md) and [eks.md](eks.md) render
these values for you.

## What gets installed

| Piece | Resource | Notes |
|---|---|---|
| web | Deployment, Service, optional HPA, PDB | `satchel serve api`, port 8080. Startup and readiness probes on `/readyz`, liveness on `/healthz`. Non-root (10001), read-only root filesystem with an `emptyDir` `/tmp`, all capabilities dropped, seccomp `RuntimeDefault`, zone and node topology spread. |
| worker | Deployment (`satchel worker`) or CronJob (`satchel worker --once`) | `worker.mode: deployment`, `cronjob` or `disabled`. |
| migrate | Helm pre-install and pre-upgrade hook Job | Runs `satchel db bootstrap` (when `bootstrap.enabled`) as an init container, then `satchel db upgrade`. One Job, so ordering is guaranteed. `backoffLimit`, `ttlSecondsAfterFinished` and `before-hook-creation,hook-succeeded` are set. With `serviceAccount.migration.create` it runs as its own ServiceAccount (its own IAM identity). |
| first workspace | Post-install hook Job | `firstWorkspace.enabled`; runs `satchel workspace create` (idempotent). |
| config | ConfigMap | Non-secret `SATCHEL_*` env. |
| secrets | existing Secret, chart Secret (dev), or SecretProviderClass | See below. |
| Cloud SQL proxy | native sidecar (init container with `restartPolicy: Always`) | In web, worker and every Job, so Jobs still terminate. |
| exposure | HTTPRoute (Gateway API) or Ingress | GKE adds HealthCheckPolicy and GCPBackendPolicy (Gateway) or BackendConfig and optional ManagedCertificate (Ingress). `aws.enabled` makes the Ingress an ALB (AWS Load Balancer Controller annotations, `ingressClassName: alb`). |
| NetworkPolicy | optional | Ingress from listed peers, `allowCidrs` (and GKE LB ranges) on the web port; egress to DNS, https, Cloud SQL (3307) or RDS (5432, with `aws.enabled`), the metadata server and the EKS Pod Identity agent. |

The ServiceAccount, ConfigMap, Secret, SecretProviderClass and dev Postgres are also pre-install hooks
(weights -10 and -9, `before-hook-creation`) because the migration Job needs them before the main
release resources exist. Consequence: `helm uninstall` does not delete them; remove them by hand if you want a clean slate.

## Secrets

The container reads these keys, and the key name equals the env var name:
`SATCHEL_DATABASE_URL` (satchel_app), `SATCHEL_ADMIN_DATABASE_URL` (satchel_worker), `SATCHEL_MIGRATION_DATABASE_URL` (owner),
`SATCHEL_OIDC_CLIENT_SECRET`, `SATCHEL_CHAT_API_KEY`, `SATCHEL_ENCRYPTION_KEY` (the last three optional; without the encryption key, admins cannot store LLM API keys in Settings → Models). Choose one source:

1. `existingSecret: <name>` (default name `satchel-secrets`). Create it yourself or with External Secrets.
2. `secretManager.enabled` with `secretManager.secrets`. The chart creates a SecretProviderClass that
   syncs a Kubernetes Secret, so the same `secretKeyRef`s work. `secretManager.provider` picks the backend:
   - `gke` (default): GCP Secret Manager resource paths, through the GKE Secret Manager add-on.
   - `aws`: AWS Secrets Manager names or ARNs (plain-string secrets), through the upstream Secrets Store
     CSI driver with `syncSecret.enabled` and the AWS provider (ASCP). The pod's IRSA role
     (`serviceAccount.awsRoleArn`) or EKS Pod Identity (`secretManager.aws.usePodIdentity`) reads them;
     `aws.region` sets the region.
3. `secrets.create: true` (development only, the values land in Helm release state; NOTES warns).

The bootstrap admin URL and role passwords (`SATCHEL_BOOTSTRAP_DATABASE_URL`, `SATCHEL_APP_DB_PASSWORD`,
`SATCHEL_WORKER_DB_PASSWORD`) come from `bootstrap.existingSecret` or `secretManager.bootstrapSecrets` and are only mounted
into the migration Job, never web or worker. Turn `bootstrap.enabled` off after the first install.

For least privilege with a secret store, put `SATCHEL_MIGRATION_DATABASE_URL` in `secretManager.migrationSecrets`
instead of `secretManager.secrets`, and give the migrate Job its own identity with `serviceAccount.migration`
(`create: true` plus `awsRoleArn` or `gcpServiceAccount`). The owner-level secrets (migration and bootstrap) are then
mounted by the migrate Job only, and only its identity needs permission to read them. `values-eks.yaml` does this.

## Cloud SQL

Set `cloudSqlProxy.enabled` and `instanceConnectionName`. The proxy listens on 127.0.0.1:5432, so DB URLs look like
`postgresql+psycopg://satchel_app:PASS@127.0.0.1:5432/satchel`. `privateIp: true` uses the private address, `autoIamAuthn: true`
enables IAM database authentication. Bind `serviceAccount.gcpServiceAccount` through Workload Identity with
`roles/cloudsql.client` and, for results, object access on the bucket.

## RDS for PostgreSQL

No sidecar: point the DB URLs at the RDS endpoint with TLS, for example
`postgresql+psycopg://satchel_app:PASS@satchel.xxxx.eu-west-2.rds.amazonaws.com:5432/satchel?sslmode=require`.
The RDS master user holds `rds_superuser`, which is enough for `satchel db bootstrap` to create the `vector`
extension and the `satchel_app` / `satchel_worker` roles. Allow 5432 from the cluster security group.

## Results storage

`SATCHEL_RESULT_DIR` must be `gs://bucket/prefix` or `s3://bucket/prefix` when `web.replicaCount > 1` or `autoscaling.enabled`;
the chart fails to render otherwise (any replica must read a result back). A local path is only valid for a single replica and
uses an `emptyDir`. GCS uses Workload Identity (`serviceAccount.gcpServiceAccount`); S3 uses IRSA (`serviceAccount.awsRoleArn`)
or EKS Pod Identity. See [storage-and-audit.md](storage-and-audit.md) for the permissions.

## Exposure and the MCP stream

`/mcp` is a long-lived SSE stream, so responses must not be buffered and the request timeout must be at least 300 s
(`exposure.timeoutSec`, enforced, default 330).

- **GKE Gateway** (`exposure.type: gateway`, `gke.enabled`): HTTPRoute plus `HealthCheckPolicy` (`/readyz`) and `GCPBackendPolicy` (`timeoutSec`).
  Attach to an existing Gateway with `gateway.parentRefs`, or `gateway.create` (use `gateway.certificateMap` for Certificate Manager).
- **GKE Ingress** (`exposure.type: ingress`, `gke.enabled`): `BackendConfig` (`timeoutSec`, health check `/readyz`) and optional `ManagedCertificate`.
- **EKS ALB** (`exposure.type: ingress`, `aws.enabled`): needs the AWS Load Balancer Controller. The chart sets `ingressClassName: alb`,
  `target-type: ip`, HTTPS on 443 with a redirect from 80, a TLS 1.3 policy, health checks on `/readyz`, the ALB idle timeout to
  `exposure.timeoutSec` and invalid-header dropping. `aws.alb.certificateArn` (ACM; empty lets the controller discover one by host),
  `wafv2AclArn`, `groupName` (share one ALB) and `securityGroups` are optional. `aws.alb.scheme: internal` keeps the ALB private.
- **Other clusters**: use `exposure.type: ingress` or a Gateway. For ingress-nginx add:

```yaml
ingress:
  className: nginx
  annotations:
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "330"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "330"
    nginx.ingress.kubernetes.io/proxy-body-size: 16m
```

## Connecting a client

`<publicUrl>/mcp`. Claude Code: `claude mcp add --transport http satchel https://satchel.example.com/mcp`. Claude Desktop: add a custom
connector with that URL. Sign-in runs through your OIDC provider; `firstWorkspace.adminEmail` is mapped to admin.

## Local try-out on kind

```
docker build -f deploy/docker/Dockerfile -t satchel:dev .
kind create cluster && kind load docker-image satchel:dev
helm install satchel deploy/helm/satchel -f deploy/helm/satchel/values-dev.yaml --wait --timeout 10m
kubectl port-forward svc/satchel 8080:80
```

`values-dev.yaml` enables `devPostgres` (single-pod pgvector, dev passwords, no backups), `SATCHEL_DEV_AUTH` and plain http. The schema rejects
`env: production` together with either.

## Values

Generated from `values.yaml` (helm-docs style); `values.schema.json` is strict, so unknown keys fail.

| Key | Default | Description |
|---|---|---|
| `nameOverride` | `""` |  |
| `fullnameOverride` | `""` |  |
| `image.repository` | `ghcr.io/getsatchel/satchel` |  |
| `image.tag` | `""` | Defaults to the chart appVersion. |
| `image.pullPolicy` | `IfNotPresent` |  |
| `imagePullSecrets` | `[]` |  |
| `publicUrl` | `https://satchel.example.com` | External https URL users reach Satchel on. OAuth issuer; the MCP resource is <publicUrl>/mcp. |
| `env` | `production` | SATCHEL_ENV. Production refuses to start without OIDC, with SATCHEL_DEV_AUTH, or with a non-https publicUrl. |
| `logFormat` | `json` |  |
| `resultDir` | `/var/lib/satchel/results` | SATCHEL_RESULT_DIR. Must be gs://bucket/prefix or s3://bucket/prefix when more than one web replica can run. |
| `sessionRetentionDays` | `null` | SATCHEL_SESSION_RETENTION_DAYS: the worker deletes agent sessions older than this (7-3650; sessions with feedback are kept). null keeps them forever. Workspace admins can override it in Settings -> Limits. |
| `oidc.issuer` | `""` |  |
| `oidc.clientId` | `""` |  |
| `embedding.provider` | `fastembed` |  |
| `embedding.extra` | `{}` | Extra SATCHEL_EMBEDDING_* settings, as {SUFFIX: value} (for example {MODEL: "..."}). |
| `chat.provider` | `""` |  |
| `chat.model` | `""` |  |
| `extraEnv` | `{}` | Extra non-secret environment (ConfigMap). Keys are full names, for example SATCHEL_DEV_AUTH: "1". |
| `existingSecret` | `satchel-secrets` | Secrets. The container reads these keys from one Secret; the key name equals the env var name: SATCHEL_DATABASE_URL, SATCHEL_ADMIN_DATABASE_URL, SATCHEL_MIGRATION_DATABASE_URL, SATCHEL_OIDC_CLIENT_SECRET, SATCHEL_CHAT_API_KEY, SATCHEL_ENCRYPTION_KEY (optional; needed to store LLM API keys from the UI) Pick ONE source: existingSecret (default), secrets.create (dev only) or secretManager.enabled. |
| `secrets.create` | `false` | Chart-created Secret from the values below. For development only: the values land in Helm release state. Use existingSecret or secretManager in production. |
| `secrets.databaseUrl` | `""` |  |
| `secrets.adminDatabaseUrl` | `""` |  |
| `secrets.migrationDatabaseUrl` | `""` |  |
| `secrets.oidcClientSecret` | `""` |  |
| `secrets.chatApiKey` | `""` |  |
| `secrets.encryptionKey` | `""` | SATCHEL_ENCRYPTION_KEY (64 hex characters, `openssl rand -hex 32`): encrypts the LLM API keys admins add in Settings → Models. Without it those keys cannot be stored. |
| `secretManager.enabled` | `false` |  |
| `secretManager.provider` | `gke` |  |
| `secretManager.secrets` | `{}` | Map of Secret key (env var name) to the secret. gke: resource path, e.g. projects/my-proj/secrets/satchel-database-url/versions/latest aws: secret name or ARN, e.g. satchel/database-url (a plain-string secret) |
| `secretManager.migrationSecrets` | `{}` | Owner-level secrets mounted by the migrate Job only (SATCHEL_MIGRATION_DATABASE_URL). Put the migration URL here instead of in secrets so web and worker identities never need to read it. |
| `secretManager.bootstrapSecrets` | `{}` | Same for the bootstrap Job (SATCHEL_BOOTSTRAP_DATABASE_URL, SATCHEL_APP_DB_PASSWORD, SATCHEL_WORKER_DB_PASSWORD). |
| `secretManager.aws.usePodIdentity` | `false` | true: EKS Pod Identity; false: IRSA (serviceAccount.awsRoleArn). |
| `web.replicaCount` | `1` |  |
| `web.command` | `["satchel", "serve", "api", "--host", "0.0.0.0"]` |  |
| `web.port` | `8080` |  |
| `web.terminationGracePeriodSeconds` | `60` |  |
| `web.resources.requests` | `{cpu: 250m, memory: 768Mi}` |  |
| `web.resources.limits` | `{memory: 1536Mi}` |  |
| `web.probes.startup` | `{periodSeconds: 5, failureThreshold: 60, timeoutSeconds: 3}` |  |
| `web.probes.readiness` | `{periodSeconds: 10, failureThreshold: 3, timeoutSeconds: 3}` |  |
| `web.probes.liveness` | `{periodSeconds: 20, failureThreshold: 3, timeoutSeconds: 3}` |  |
| `web.topologySpreadConstraints` | `true` |  |
| `web.nodeSelector` | `{}` |  |
| `web.tolerations` | `[]` |  |
| `web.affinity` | `{}` |  |
| `web.podAnnotations` | `{}` |  |
| `web.podLabels` | `{}` |  |
| `web.extraEnv` | `[]` |  |
| `autoscaling.enabled` | `false` |  |
| `autoscaling.minReplicas` | `2` |  |
| `autoscaling.maxReplicas` | `6` |  |
| `autoscaling.targetCPUUtilizationPercentage` | `70` |  |
| `pdb.enabled` | `true` |  |
| `pdb.minAvailable` | `1` |  |
| `service.type` | `ClusterIP` |  |
| `service.port` | `80` |  |
| `service.annotations` | `{}` |  |
| `worker.mode` | `deployment` |  |
| `worker.replicaCount` | `1` |  |
| `worker.schedule` | `"*/15 * * * *"` |  |
| `worker.resources.requests` | `{cpu: 100m, memory: 512Mi}` |  |
| `worker.resources.limits` | `{memory: 1Gi}` |  |
| `worker.nodeSelector` | `{}` |  |
| `worker.tolerations` | `[]` |  |
| `worker.affinity` | `{}` |  |
| `worker.podAnnotations` | `{}` |  |
| `migration.backoffLimit` | `2` |  |
| `migration.ttlSecondsAfterFinished` | `600` |  |
| `migration.activeDeadlineSeconds` | `900` |  |
| `migration.resources.requests` | `{cpu: 100m, memory: 256Mi}` |  |
| `migration.resources.limits` | `{memory: 512Mi}` |  |
| `bootstrap.enabled` | `false` |  |
| `bootstrap.existingSecret` | `""` | Secret with SATCHEL_BOOTSTRAP_DATABASE_URL (admin URL), SATCHEL_APP_DB_PASSWORD, SATCHEL_WORKER_DB_PASSWORD. Kept separate from existingSecret so the admin credential never reaches web or worker pods. |
| `firstWorkspace.enabled` | `false` |  |
| `firstWorkspace.slug` | `""` |  |
| `firstWorkspace.name` | `""` |  |
| `firstWorkspace.adminEmail` | `""` |  |
| `serviceAccount.create` | `true` |  |
| `serviceAccount.name` | `""` |  |
| `serviceAccount.annotations` | `{}` |  |
| `serviceAccount.gcpServiceAccount` | `""` | Workload Identity: GSA email (iam.gke.io/gcp-service-account), for GCS results and Cloud SQL IAM. |
| `serviceAccount.awsRoleArn` | `""` | IRSA: IAM role ARN (eks.amazonaws.com/role-arn), for S3 results, audit export and Secrets Manager. |
| `serviceAccount.migration.create` | `false` |  |
| `serviceAccount.migration.name` | `""` |  |
| `serviceAccount.migration.annotations` | `{}` |  |
| `serviceAccount.migration.awsRoleArn` | `""` |  |
| `serviceAccount.migration.gcpServiceAccount` | `""` |  |
| `cloudSqlProxy.enabled` | `false` |  |
| `cloudSqlProxy.image` | `gcr.io/cloud-sql-connectors/cloud-sql-proxy:2.14.1` |  |
| `cloudSqlProxy.instanceConnectionName` | `""` |  |
| `cloudSqlProxy.privateIp` | `false` |  |
| `cloudSqlProxy.autoIamAuthn` | `false` |  |
| `cloudSqlProxy.port` | `5432` |  |
| `cloudSqlProxy.resources.requests` | `{cpu: 50m, memory: 64Mi}` |  |
| `gke.enabled` | `false` |  |
| `aws.enabled` | `false` |  |
| `aws.region` | `""` | AWS_REGION for the pods (S3, STS) and the region of Secrets Manager secrets. IRSA injects it too; set it explicitly for EKS Pod Identity or when secrets live in another region. |
| `aws.alb.scheme` | `internet-facing` |  |
| `aws.alb.certificateArn` | `""` | ACM certificate for exposure.host; empty lets the controller discover one by host name. |
| `aws.alb.sslPolicy` | `ELBSecurityPolicy-TLS13-1-2-2021-06` |  |
| `aws.alb.groupName` | `""` | Share one ALB between Ingresses (alb.ingress.kubernetes.io/group.name). |
| `aws.alb.wafv2AclArn` | `""` |  |
| `aws.alb.securityGroups` | `[]` | Extra security groups for the ALB (e.g. to restrict source ranges). |
| `exposure.type` | `none` |  |
| `exposure.host` | `""` |  |
| `exposure.timeoutSec` | `330` | Request timeout in seconds on the load balancer. Must be >= 300 for /mcp SSE. |
| `gateway.parentRefs` | `[]` | Attach the HTTPRoute to an existing Gateway. |
| `gateway.create` | `false` | e.g. [{name: external-http, namespace: infra, sectionName: https}] Or have the chart create a Gateway. |
| `gateway.className` | `gke-l7-global-external-managed` |  |
| `gateway.annotations` | `{}` |  |
| `gateway.certificateMap` | `""` | Certificate Manager map (GKE) or a TLS Secret name. |
| `gateway.tlsSecretName` | `""` |  |
| `gateway.staticIpName` | `""` | Name of a reserved global static IP (`gcloud compute addresses create ... --global`). Only used when create is true. |
| `gateway.healthCheckPath` | `/readyz` |  |
| `ingress.className` | `""` |  |
| `ingress.annotations` | `{}` |  |
| `ingress.tlsSecretName` | `""` |  |
| `ingress.managedCertificate` | `false` | GKE: create a ManagedCertificate for exposure.host and reference it from the Ingress. |
| `ingress.staticIpName` | `""` | GKE: static IP name for kubernetes.io/ingress.global-static-ip-name. |
| `networkPolicy.enabled` | `false` |  |
| `networkPolicy.ingressFrom` | `[]` | Peers allowed to reach the web port (NetworkPolicyPeer list), e.g. the gateway/ingress namespace. |
| `networkPolicy.allowGkeLoadBalancers` | `true` | Allow GKE load balancer / health check ranges (130.211.0.0/22, 35.191.0.0/16). Ignored when aws.enabled. |
| `networkPolicy.allowCidrs` | `[]` | Extra source CIDRs for the web port, e.g. the VPC CIDR so an ALB in ip target mode reaches pods. |
| `networkPolicy.extraEgress` | `[]` |  |
| `devPostgres.enabled` | `false` |  |
| `devPostgres.image` | `pgvector/pgvector:pg16` |  |
| `devPostgres.database` | `satchel` |  |
| `devPostgres.password` | `satchel-dev` |  |
| `devPostgres.appPassword` | `satchel-app-dev` |  |
| `devPostgres.workerPassword` | `satchel-worker-dev` |  |
| `devPostgres.storage` | `1Gi` |  |