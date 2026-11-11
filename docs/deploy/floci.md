# Test the cloud deployments locally with floci

This page covers two emulators:

- **GCP:** floci-gcp, run by `scripts/floci-e2e.sh`, described in the GCP sections below.
- **AWS:** floci, run by `scripts/floci-aws-e2e.sh`. See [AWS (floci)](#aws-floci).

[floci-gcp](https://github.com/floci-io/floci-gcp) (MIT) is a local GCP emulator. It serves the
Google APIs on one port (4588) and runs the data planes as real containers on the host Docker
daemon: Cloud SQL is a real postgres, GKE is a real k3s cluster, Cloud Run services and jobs run
your image. `scripts/floci-e2e.sh` uses it to apply the Terraform modules in `deploy/terraform`
and install the Helm chart in `deploy/helm/satchel` end to end, instead of only
`terraform validate`.

```
scripts/floci-e2e.sh                # build the image, tier 1 and tier 2, tear down
scripts/floci-e2e.sh --tier 2 --skip-build --keep
```

CI: `.github/workflows/floci.yml` runs both tiers (one job each) on demand and on pull requests
that touch the GCP side (`deploy/floci/`, `scripts/floci-e2e.sh`, the `foundation`, `cloud-run`,
`gke` and `helm-values` modules, the `floci-cloud-run` and `floci-gke` examples) or anything both
clouds share (`deploy/helm/`, `deploy/docker/Dockerfile`, `deploy/floci/mcp_smoke.py`, the
workflow itself). The workflow's `changes` job decides which cloud's jobs run.

## What it proves

**Tier 1, control plane.** With Cloud Run in mock mode, `examples/floci-cloud-run` applies the
`foundation` and `cloud-run` modules (APIs, VPC, Cloud SQL Postgres 17, database user, Secret
Manager secrets and IAM bindings, buckets and bucket IAM, service accounts, project IAM, the Cloud
Run service with its public invoker binding, the three jobs, the scheduler service account, job
IAM and the two Cloud Scheduler jobs) and destroys it again. Then `examples/floci-gke` applies the
`foundation` and `gke` modules on a real k3s cluster, and the `helm_release` really installs the
chart:

- the pre-install migration hook Job runs `satchel db bootstrap` (init container) and
  `satchel db upgrade` to head (0011) against the emulated Cloud SQL instance;
- the post-install first-workspace Job creates `demo`;
- the web Deployment becomes Ready on `/readyz` with `SATCHEL_ENV=production`;
- the worker CronJob exists; the web pod reads and writes the results bucket;
- a re-plan does not replace the cluster.

**Tier 2, real workloads on Cloud Run.** With Cloud Run in execution mode and
`runtime_profile = "emulator"`:

- apply with `deploy_service = false`, then the migrate job through the Cloud Run Admin API
  (`jobs/satchel-migrate:run`): bootstrap creates `satchel_app` and `satchel_worker` (no BYPASSRLS, no
  superuser), upgrade reaches 0011, tables are owned by `satchel`; a second run is a no-op;
- the first workspace, by running the migrate job with override args (as in
  [cloud-run.md](cloud-run.md) step 6), idempotent;
- the service (`SATCHEL_ENV=production`) serves `/healthz`, `/readyz`, the UI at `/` and
  `/.well-known/oauth-protected-resource/mcp` (resource `<public_url>/mcp`) through floci's front
  door;
- the worker job (`satchel worker --once`) connects as `satchel_worker` (Postgres `log_connections`);
- the audit-export job runs;
- over MCP (Streamable HTTP, a PAT): `run_sql` stores a result with `SATCHEL_RESULT_DIR=gs://...`,
  the object lands in the emulated bucket, `query_result.path` is a `gs://` URI, and `get_result`
  reads page 100..109 back with ranged reads.

`google-cloud-storage` honours `STORAGE_EMULATOR_HOST` (anonymous credentials, the emulator as
the API endpoint), so `server/satchel/data/gcs.py` needed no change: the examples set the variable
on the workloads.

## What it does not prove

floci reproduces API shapes and runs the data planes; it is not GCP.

- **IAM.** Bindings are stored, never enforced (floci's default authorization mode). Nothing here
  shows that the runtime service account can actually read a secret or a bucket on GCP.
- **Networking.** No VPC, Private Service Access, Direct VPC egress, private GKE nodes or Cloud
  NAT. Everything talks over one Docker network.
- **Load balancers and TLS.** Compute stores LB config but routes no traffic. The Cloud Run load
  balancer, Cloud Armor, IAP, managed certificates, Certificate Manager and the GKE Gateway are
  turned off in the floci examples and stay untested.
- **Cloud SQL specifics.** No Cloud SQL connector or Auth Proxy (TCP to the container instead),
  no SSL enforcement, backups, PITR or maintenance windows. Users are not cloudsqlsuperuser
  members (see the shim below), so the `CREATE EXTENSION vector` path of `satchel db bootstrap` is
  not exercised.
- **Secret Manager references.** The Cloud Run emulator profile passes secret values as plain env;
  the `secret_key_ref` wiring of the `gcp` profile is not executed.
- **Startup probes**, autoscaling, scale to zero, request timeouts beyond floci's front door.
- **Cloud Scheduler firing.** The jobs are created (dispatch is disabled in the compose file: their
  target is the real `run.googleapis.com`), and floci drops their OAuth token config.
- **Workload Identity**, GKE Autopilot behaviour, the Secret Manager CSI add-on.
- **Artifact Registry** (not emulated): the image is a local build.
- Plans are not clean after apply: floci drops Secret Manager labels and the scheduler OAuth
  token, so a re-plan shows in-place updates for those.

Checked against floci-gcp 0.12.0 (`curl` on the REST paths): Artifact Registry, Certificate
Manager, Service Networking and IAP answer 404; Compute `securityPolicies` (Cloud Armor),
`sslCertificates`, `targetHttpsProxies` and `routers` (Cloud Router / NAT) answer 501.

## Run it locally

Needs Docker (8 GB is plenty), terraform 1.9.8, helm, kubectl, curl and python3. Port 4588 must
be free (`FLOCI_PORT` changes it for `up.sh`) and k3s takes one port from 6550-6599.

```
deploy/floci/up.sh [--cloudrun-mock]   # floci-gcp on localhost:4588, pinned floci/floci-gcp:0.12.0
deploy/floci/down.sh                   # removes only what floci created (see below)
```

By hand, Cloud Run:

```
docker build -f deploy/docker/Dockerfile --build-arg BAKE_EMBEDDINGS=false -t satchel:floci .
deploy/floci/up.sh
cd deploy/terraform/examples/floci-cloud-run
terraform init && terraform apply -var deploy_service=false
../../../floci/run-job.sh satchel-migrate
../../../floci/run-job.sh satchel-migrate '["-c","satchel workspace create demo --name Demo --admin-email admin@example.com"]'
terraform apply
curl "$(terraform output -raw public_url)/readyz"
```

GKE needs a two-step apply (the Kubernetes and Helm providers are configured from the cluster the
same apply creates, as in [gke.md](gke.md) step 3) and the local image imported into k3s:

```
cd deploy/terraform/examples/floci-gke
terraform init
terraform apply -target=module.foundation -target=terraform_data.cloudsql_shim \
  -target=module.gke.google_container_cluster.this
../../../floci/k3s-load-image.sh satchel satchel:floci
ip=$(docker inspect -f '{{(index .NetworkSettings.Networks "floci-satchel").IPAddress}}' floci-satchel-gcp)
terraform apply -var storage_emulator_host=http://$ip:4588
```

Before `terraform destroy`, empty the buckets: `deploy/floci/empty-bucket.sh floci-local-satchel-results floci-local-satchel-audit`.

### Isolation and cleanup

The compose project is `floci-satchel` (container `floci-satchel-gcp`, network `floci-satchel`), memory
storage, and `FLOCI_GCP_DOCKER_RESOURCE_NAMESPACE=satchel`: every container and volume floci spawns is
named `floci-gcp-satchel-*` and labelled `floci_emulator=floci-gcp`, `floci_namespace=satchel`.
`down.sh` stops the emulator, then removes exactly those containers and volumes and the network.

The k3s API server is published by Docker on `0.0.0.0:6550` and floci's token webhook maps any
bearer token to cluster-admin. Run this on a trusted network, and run `down.sh` when done.

## How the examples differ from production

The production examples and module defaults are unchanged. The floci examples set:

| Setting | Why (emulator gap) |
|---|---|
| provider `*_custom_endpoint` for every API, `access_token` dummy, project `floci-local` | Point the provider at floci; it does not check credentials. hashicorp/google 6.x only strips API versions from **https** endpoints, so most endpoints are the bare `http://localhost:4588/`; GKE uses floci's host routing `http://container.localhost:4588/`. |
| `database_private_ip = false`, `create_private_service_access = false` | No Service Networking. |
| `database_url_style = "tcp"`, `database_tcp_host = "instance"` | No Cloud SQL connector or Auth Proxy: TCP to the instance IP floci reports. |
| `create_database = false` + `deploy/floci/cloudsql-shim.sh` | One `sql_custom_endpoint` cannot serve both the instance (client library) and `google_sql_database` (generated) on http; the shim creates the database through the Admin API. |
| `cloudsql-shim.sh` (also) | floci's Cloud SQL users are plain roles. The shim gives the owner what Cloud SQL gives every API-created user: CREATEROLE, CREATEDB, membership of `cloudsqlsuperuser` (which owns the database) and the `vector` extension. Then bootstrap and migrations run exactly as in production. |
| `create_ghcr_remote_repository = false`, `image_repository_override = "satchel"` | No Artifact Registry. floci runs a local image through the host Docker daemon; k3s gets it from `k3s-load-image.sh` (`pullPolicy: Never`). |
| cloud-run `runtime_profile = "emulator"` | floci's Cloud Run execution rejects env `valueSource` (secret refs), Cloud SQL and secret volumes and startup probes. The profile uses plain env values (`emulator_secret_values` = foundation `secret_values`), no `/cloudsql` volume, no startup probe; the liveness probe stays. |
| cloud-run `scheduler_http_headers` | floci omits Cloud Scheduler's default headers and the provider panics reading an empty header map. |
| `public_url` = floci front-door URL, `SATCHEL_ALLOW_INSECURE_PUBLIC_URL=1` | The front door is plain http (`http://satchel-<sha256(project)[:12]>.<region>.run.localhost.floci.io:4588`). `SATCHEL_ENV` stays `production`. |
| `embedding_provider = "hash"` | The test image does not bake the fastembed model. |
| `STORAGE_EMULATOR_HOST` | `http://floci-gcp:4588` for Cloud Run containers (Docker DNS); floci's IP for k3s pods, which cannot resolve Docker names. |
| gke `create_nat = false`, `enable_certificate_manager = false`; chart `cloudSqlProxy.enabled=false`, `exposure.type=none`, `gke.enabled=false`, one replica | No Cloud Router/NAT, Certificate Manager, Cloud SQL connect API, GKE Gateway classes or GKE policy CRDs in k3s. |

Module changes made for this (all no-ops at their defaults): cloud-run `runtime_profile`,
`emulator_secret_values`, `scheduler_http_headers`; foundation `database_tcp_host`,
`database_tcp_port`, `create_database` (with a `moved` block, so existing state keeps the
database) and the sensitive `secret_values` output; gke ignores `initial_node_count` on the
Autopilot cluster (floci reports a default of 3, which forced a replacement on every plan).

## Troubleshooting

- **Port 4588 in use**: `FLOCI_PORT=4600 deploy/floci/up.sh`, and pass `-var floci_endpoint=http://localhost:4600 -var floci_port=4600`.
- **Job output**: floci logs task stdout/stderr at DEBUG; `run-job.sh` prints it. Everything:
  `docker logs floci-satchel-gcp 2>&1 | grep 'STDOUT\|STDERR'`.
- **`ErrImageNeverPull` in k3s**: the image was imported into a different cluster container (a
  replaced cluster). Run `k3s-load-image.sh` again after step 1.
- **Bucket 409 on destroy**: run `empty-bucket.sh` first (the provider's force_destroy asks for
  Anywhere Caches, floci answers 405, and the provider skips emptying the bucket).
- **kubectl**: `kubectl config set-cluster floci --server=https://localhost:6550 --insecure-skip-tls-verify=true`, any token.
- **Leftovers after a crash**: `deploy/floci/down.sh` is safe to run any time.

## AWS (floci)

[floci](https://github.com/floci-io/floci) (MIT) is the AWS emulator of the same family. It serves
the AWS APIs on one port (4566) and keeps S3, IAM, STS, KMS and Secrets Manager in process. RDS is
a real Postgres container behind floci's TCP proxy: `pgvector/pgvector:pg17`, set with
`FLOCI_SERVICES_RDS_DEFAULT_POSTGRES_IMAGE`. floci's EKS API stores the cluster, node group,
access entries and add-ons as metadata only. Nothing runs Kubernetes behind it, so
`deploy/floci-aws/docker-compose.yml` adds a k3s server that stands in for the EKS data plane.

```
scripts/floci-aws-e2e.sh               # build the image, apply, verify, destroy, tear down
scripts/floci-aws-e2e.sh --skip-build --keep
```

CI: the `aws` job in `.github/workflows/floci.yml`, on demand and on pull requests that touch the
AWS side (`deploy/floci-aws/`, `scripts/floci-aws-e2e.sh`, the `aws-foundation`, `aws-helm-values`
and `eks` modules, the `floci-eks` example) or the shared paths above.

### What it proves

`examples/floci-eks` applies the real `aws-foundation` and `eks` modules:

- the VPC with public and private subnets, route tables, NAT, an EIP and the S3 gateway endpoint;
- the KMS key and alias;
- the RDS subnet group, security group and parameter group (`rds.force_ssl`), and the instance;
- the three generated role passwords and all Secrets Manager secrets, with their KMS key;
- both buckets with public access blocks, ownership controls, SSE-KMS, the TLS-only policy, the
  results lifecycle and audit versioning;
- the runtime and migrate IAM policies;
- the EKS cluster with KMS secrets encryption, access config, add-ons, a node group and the IAM
  OIDC provider;
- the IRSA roles and their attachments, and the RDS ingress rule from the cluster security group;
- the ACM certificate and the WAF web ACL with its managed rule groups and rate limit.

The Helm release from the same apply installs on k3s:

- the pre-install migrate Job runs `satchel db bootstrap` (init container) and `satchel db upgrade`
  to head (0011) against the RDS instance. It runs as the `satchel-migrate` service account, which is
  annotated with the migrate IRSA role;
- bootstrap creates `satchel_app` and `satchel_worker` (no BYPASSRLS, no superuser) and the `vector`
  extension;
- the first-workspace Job creates `demo`, and the web Deployment becomes Ready on `/readyz` with
  `SATCHEL_ENV=production`, running as `satchel` (the runtime IRSA role);
- the worker CronJob exists, and the ConfigMap carries `SATCHEL_RESULT_DIR=s3://...` and `AWS_REGION`;
- the web pod puts, ranged-reads and deletes an object in the results bucket through
  `satchel.data.s3` (boto3);
- over MCP (Streamable HTTP, a PAT), `run_sql` stores a result. `query_result.path` is an
  `s3://` URI, the Parquet object is in the bucket, and `get_result` reads page 100..109 back with
  HTTP range requests;
- `satchel audit export --to s3://...` writes `exports/audit/demo/<yyyy>/<mm>/<dd>.jsonl`.

The script also checks:

- the runtime policy grants no owner-level secret (migration URL, owner and role passwords);
- each IRSA role's trust policy names exactly its service account.

Finally, a re-plan replaces nothing, and destroy removes every resource.

### What it does not prove

floci reproduces API shapes and runs RDS; it is not AWS.

- **IAM and IRSA are not enforced.** Policies and trust policies are stored and inspected, but k3s
  has no EKS pod identity webhook. Pods get the emulator's static test credentials
  (`AWS_ACCESS_KEY_ID=test`) through `extraEnv`. Whether the runtime role can read exactly what it
  needs on real AWS is not tested here. The IAM policy-simulator checks belong in a staging account.
- **No real EKS.** The cluster, node group, add-ons and Fargate profiles are metadata. The AWS
  Load Balancer Controller and the Secrets Store CSI driver plus the AWS provider are not installed
  (`install_cluster_addons = false`), so the ALB Ingress, ACM on the listener, WAF association and
  CSI secret delivery stay untested. The example uses Kubernetes Secret delivery, the Fargate path.
  `helm template` and kubeconform in CI cover the rendered Ingress and SecretProviderClass objects.
- **Networking.** Subnets, routes and NAT are stored. Pods reach floci's RDS proxy and S3 over one
  Docker network, and security groups are not enforced.
- **RDS specifics.** floci's proxy speaks plain TCP (`sslmode=disable` in the example), so
  `rds.force_ssl` and TLS are not exercised. Neither are Multi-AZ, backups, PITR, Performance
  Insights or storage autoscaling. floci's master user is a Postgres superuser, whereas RDS gives
  `rds_superuser`.
- **Plans are not clean after apply.** floci reports the RDS proxy port (7001) instead of 5432, its
  own security-group reference form (`000000000000/sg-...`), and drops parameter group parameters.
  A re-plan therefore shows three in-place updates; none are replacements.

### Run it by hand

```
docker build -f deploy/docker/Dockerfile --build-arg BAKE_EMBEDDINGS=false -t satchel:floci .
deploy/floci-aws/up.sh                       # floci/floci:2.4.0 on :4566, k3s v1.34 on :6551
deploy/floci-aws/k3s-load-image.sh satchel:floci
cd deploy/terraform/examples/floci-eks
terraform init
terraform apply -var floci_host=$(../../../floci-aws/floci-ip.sh)
export KUBECONFIG=$PWD/../../../floci-aws/.kubeconfig
kubectl -n satchel get pods,jobs
terraform destroy -var floci_host=$(../../../floci-aws/floci-ip.sh)
../../../floci-aws/down.sh
```

Unlike GKE, this needs no two-step apply. The Kubernetes and Helm providers read the k3s
kubeconfig that `up.sh` writes, not the emulated cluster.

### How the AWS example differs from production

| Setting | Why (emulator gap) |
|---|---|
| provider `endpoints` for every service, `test`/`test` credentials, `skip_*` checks, `s3_use_path_style` | Point the provider at floci; it does not check credentials. |
| Kubernetes and Helm providers on `deploy/floci-aws/.kubeconfig` | floci's EKS has no API server; k3s stands in. |
| `database_sslmode = "disable"`, `database_host_override = "<floci ip>:7001"` | floci's RDS proxy is plain TCP, and it reports its proxy port, not 5432. Without the override, the URLs would use the planned port 5432 and the migrate Job could not connect. |
| `install_cluster_addons = false`, `secrets_delivery = "kubernetes"` | No real cluster for the AWS Load Balancer Controller or the CSI driver (both need IRSA against real AWS). |
| chart `exposure.type = none`, `autoscaling.enabled = false`, `networkPolicy.enabled = false`, one replica, `embedding.provider = hash` | k3s has no ALB controller or metrics, and the test image does not bake the fastembed model. |
| chart `extraEnv`: `AWS_ENDPOINT_URL=http://<floci ip>:4566`, static test credentials | No IRSA on k3s. A custom endpoint switches boto3 to path-style addressing (`satchel/data/s3.py`). |
| `database_deletion_protection = false`, `cluster_deletion_protection = false`, `secret_recovery_window_days = 0`, `audit_bucket_force_destroy = true` | Tear down in one destroy. |

Static addresses on the compose network (`10.251.88.2` for floci, `10.251.88.3` for k3s) keep the DB
URLs, the pods' S3 endpoint and k3s's kubelet tunnel stable. Without them, the two containers swap
addresses between runs. `down.sh` removes the compose project and everything floci started under
`floci_namespace=satchel-aws`: the RDS container, its EKS and ECR helpers, and the Docker networks
backing emulated VPCs.
