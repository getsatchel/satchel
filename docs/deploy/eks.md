# Deploy Satchel on Amazon EKS

Terraform builds the whole stack in your AWS account:

- **Foundation** (`modules/aws-foundation`):
  - a VPC with public and private subnets in 2 or 3 AZs, NAT and an S3 gateway endpoint (or bring your own VPC);
  - a KMS key;
  - RDS for PostgreSQL 17 with pgvector (private, encrypted, TLS enforced);
  - two S3 buckets;
  - Secrets Manager entries for every database role;
  - least-privilege IAM policies.
- **Cluster** (`modules/eks`):
  - an EKS cluster on managed nodes or Fargate, with KMS envelope encryption for Secrets;
  - IRSA roles for the runtime and the migrate Job;
  - the AWS Load Balancer Controller;
  - the Secrets Store CSI driver with the AWS provider;
  - an ACM certificate and an AWS WAF web ACL.
- **Release:** the Helm chart in `deploy/helm/satchel`, installed with a `helm_release`.

```
Claude / browser ──https──▶ ALB (ACM certificate, AWS WAF, 3600 s idle timeout for /mcp streams)
                                   │ target-type ip
                         EKS (private subnets, NAT) · managed nodes or Fargate
                         Deployment satchel (2-6 replicas, HPA, PDB) · CronJob satchel-worker
                         pre-upgrade hook Job satchel-migrate (db bootstrap + upgrade)
                                   │ IRSA: SA satchel ⇒ satchel-runtime · SA satchel-migrate ⇒ satchel-migrate
                                   ▼
   RDS PostgreSQL 17 (pgvector, private) · S3 results (s3://) + audit · Secrets Manager · KMS
```

Test locally: `scripts/floci-aws-e2e.sh` applies this stack against the floci AWS emulator. It installs the chart on a k3s cluster that stands in for the EKS data plane, then stores a query result in emulated S3 over MCP ([floci.md](floci.md#aws-floci)).

## 1. Prerequisites

- An AWS account and an IAM principal that can create VPC, EKS, RDS, IAM, KMS, S3, Secrets Manager, ACM and WAF resources. `AdministratorAccess` works for the first apply.
- Terraform 1.6 or newer, the AWS CLI v2 and `kubectl`.
- An OIDC identity provider (Okta, Entra ID, Google, Cognito, ...) and a DNS name you control. A Route 53 public hosted zone for it lets Terraform validate the certificate automatically.
- A private, versioned S3 bucket for Terraform state (see `examples/eks/backend.tf.example`). State holds the generated database passwords, so restrict access to it.
- If you restrict the Kubernetes API (`cluster_public_access_cidrs`), include the address you run Terraform from. Terraform's Kubernetes and Helm providers reach the endpoint from there.

The image defaults to `ghcr.io/getsatchel/satchel:<tag>`. Nodes pull it directly through the NAT gateway. To pull from your own ECR instead, set `helm_extra_values = { image = { repository = "ACCOUNT.dkr.ecr.REGION.amazonaws.com/satchel", tag = "0.1.1" } }`.

## 2. Create the OIDC client

Create a web application client in your identity provider:

- Redirect URI: `https://<domain>/auth/callback`
- Scopes: `openid email profile`

Keep the issuer, client id and client secret.

## 3. terraform apply

```bash
cd deploy/terraform/examples/eks
cp terraform.tfvars.example terraform.tfvars    # region, VPC, domain, SSO
cp backend.tf.example backend.tf                # edit
terraform init
# the Kubernetes and Helm providers are configured from the cluster, so create it first
terraform apply -target=module.eks.aws_eks_cluster.this -target=module.foundation
terraform apply
```

The first apply takes 20 to 30 minutes, mostly RDS and the EKS control plane. The second apply installs the add-ons and the chart. Each `helm_release` runs with `atomic = true`, so a failed install is rolled back.

The certificate:

- **With `route53_zone_id`:** Terraform creates the ACM validation records and waits for the certificate.
- **Without it:** create the records from `terraform output certificate_validation_records` at your DNS provider. The ALB serves HTTPS once ACM shows the certificate as issued.

Then point the domain at the ALB. `terraform output -raw alb_hostname_command` prints a `kubectl` command that returns the ALB's DNS name. Create a CNAME to it, or a Route 53 alias record.

## 4. Populate secrets

Terraform generates every database credential and writes it to Secrets Manager under `satchel/...`. Each secret is a plain string encrypted with the stack's KMS key:

| Secret | Read by |
|---|---|
| `satchel/database-url`, `satchel/admin-database-url`, `satchel/oidc-client-secret`, `satchel/chat-api-key` (optional) | web, worker, first-workspace Job (role `satchel-runtime`) |
| `satchel/migration-database-url`, `satchel/owner-password`, `satchel/app-password`, `satchel/worker-password` | the migrate Job only (role `satchel-migrate`) |

The OIDC client secret is the one value you supply. Leave `oidc_client_secret` unset so it never enters Terraform state, and store it after the apply:

```bash
aws secretsmanager put-secret-value \
  --secret-id satchel/oidc-client-secret \
  --secret-string "$OIDC_CLIENT_SECRET"
```

(`terraform output -raw oidc_secret_command` prints this command with the region filled in.)

On managed nodes, pods read secrets through the Secrets Store CSI driver at start, and the driver re-reads them every 2 minutes. A rotated value reaches the synced Kubernetes Secret without a redeploy. Restart the pods to pick it up as environment: `kubectl -n satchel rollout restart deploy/satchel`.

**Fargate** cannot run the CSI driver, which is a DaemonSet. With `compute = "fargate"`, Terraform writes the secrets as Kubernetes Secrets instead (`satchel-secrets` and `satchel-bootstrap`), which are envelope-encrypted with the KMS key. In that mode `oidc_client_secret` is required (pass it with `TF_VAR_oidc_client_secret`), and you rotate it by changing the variable and applying.

## 5. Run migrate

The chart runs `satchel db bootstrap` and `satchel db upgrade` in a Helm pre-install and pre-upgrade hook Job before the Deployment rolls out, so the first apply already migrated. The Job runs as the `satchel-migrate` service account. It is the only identity that can read the owner-level secrets.

```bash
$(terraform output -raw update_kubeconfig_command)
kubectl -n satchel get jobs
kubectl -n satchel logs job/satchel-migrate
```

RDS is private, so run any ad hoc `satchel` command from inside the cluster, never from your laptop. After the first install, set `bootstrap_enabled = false` and apply. The admin credential then stops being mounted.

## 6. Create the first workspace

The easiest way is to set `first_workspace = { slug = "acme", name = "Acme", admin_email = "you@example.com" }` and apply. The chart runs the command as a post-install Job. You can also run it by hand:

```bash
kubectl -n satchel exec deploy/satchel -- \
  satchel workspace create acme --name Acme --admin-email you@example.com
```

The command is idempotent. When `you@example.com` signs in through OIDC, they become admin.

## 7. Connect Claude

The MCP endpoint is `https://<domain>/mcp`.

- Claude Code: `claude mcp add --transport http satchel https://<domain>/mcp`, then run `/mcp` inside Claude Code to sign in.
- Claude Desktop: Settings, Connectors, Add custom connector, then paste the `/mcp` URL.

The ALB idle timeout is raised to an hour so MCP server-sent-event streams are not cut.

## 8. Security posture

- **Network:**
  - nodes, pods and RDS sit in private subnets;
  - RDS accepts 5432 only from the cluster security group, and `rds.force_ssl = 1` rejects plaintext;
  - the ALB is the only public entry point (set `alb_scheme = "internal"` via `helm_extra_values` for a private deployment);
  - on managed nodes, the chart's NetworkPolicy, enforced by the VPC CNI, admits only the VPC CIDR to the web port.
- **Identity:**
  - no static AWS keys anywhere;
  - pods get credentials through IRSA, and each role trusts exactly one Kubernetes service account;
  - the Kubernetes API uses EKS access entries (`cluster_admin_principal_arns`).
- **Encryption:**
  - one customer managed KMS key, rotated yearly, covers RDS storage, Performance Insights, Secrets Manager, S3 (SSE-KMS with bucket keys) and Kubernetes Secrets;
  - both buckets deny non-TLS requests and block public access.
- **Edge:**
  - AWS WAF applies the AWS managed IP reputation, common and known-bad-inputs rule sets, plus a per-IP rate limit (`waf_rate_limit`, default 3000 requests per 5 minutes);
  - the common rule set's body-size rule only counts, because MCP and the curator API carry large JSON bodies;
  - TLS policy is `ELBSecurityPolicy-TLS13-1-2-2021-06`.
- **Audit:**
  - EKS control plane `api`, `audit` and `authenticator` logs go to CloudWatch (90 days);
  - RDS Postgres logs go to CloudWatch;
  - `satchel audit export --to s3://<audit bucket>/exports` writes the application trail ([storage-and-audit.md](storage-and-audit.md)). Run it from a CronJob, or add it to your SIEM pipeline.

## 9. Upgrades

1. Set `image_tag = "0.2.0"`.
2. Run `terraform apply`. The Helm pre-upgrade hook migrates first. If it fails, `atomic = true` rolls the release back and the old pods keep serving.
3. Chart-only changes apply the same way.

Migrations are forward-only. Take a manual snapshot before risky upgrades: `aws rds create-db-snapshot --db-instance-identifier <id> --db-snapshot-identifier satchel-pre-0-2-0`.

## 10. Backups and restore

RDS keeps 14 days of automated backups with point-in-time recovery (`database_backup_retention_days`). A final snapshot is taken on deletion while deletion protection is on.

```bash
aws rds describe-db-instance-automated-backups --db-instance-identifier <id>
aws rds restore-db-instance-to-point-in-time --source-db-instance-identifier <id> \
  --target-db-instance-identifier satchel-restore --restore-time 2026-11-05T09:30:00Z
```

A restore creates a new instance. Repoint the three database URL secrets at it (or swap instance identifiers), then restart the pods. Results in the results bucket are disposable and expire a day after `result_ttl_days`. Audit exports are versioned. `audit_retention_days` adds S3 Object Lock in GOVERNANCE mode, and that can only be turned on when the bucket is created.

## 11. Costs

A rough monthly estimate for a small team, at list prices in `eu-west-2`. Check the current pricing pages before relying on it.

| Item | Approx. USD / month |
|---|---|
| EKS control plane | 73 |
| 2 × `m7i.large` managed nodes (or Fargate for about 1.5 vCPU / 4 GiB of pods) | 150 to 190 |
| RDS PostgreSQL 17, `db.t4g.medium`, single-AZ, 20 GB gp3, backups (Multi-AZ roughly doubles it) | 60 to 75 |
| NAT gateway (one), ALB, WAF (web ACL, 4 rules, low traffic) | 75 to 100 |
| KMS key, Secrets Manager (8 secrets), S3, CloudWatch logs | 10 to 20 |
| **Total** | **about 370 to 460** |

Running on an existing shared cluster (`existing_cluster_name`) removes the control plane and most of the node cost.

## 12. Teardown

The cluster and RDS are both deletion-protected, so `terraform destroy` fails by design until you lift protection.

```bash
cd deploy/terraform/examples/eks
terraform apply -var database_deletion_protection=false -var cluster_deletion_protection=false
aws s3 rm --recursive s3://<audit bucket>    # the audit bucket refuses to be destroyed while non-empty
terraform destroy -var database_deletion_protection=false -var cluster_deletion_protection=false
```

The ALB and its target groups belong to the AWS Load Balancer Controller. Destroy deletes the satchel release, and with it the Ingress, before the controller, so the controller can clean them up. If the cluster is already gone, delete any leftover `k8s-satchel-*` load balancers and target groups by hand. Then run `terraform state rm` on the module's `helm_release` and `kubernetes_*` resources. Secrets are scheduled for deletion with a 7-day recovery window (`secret_recovery_window_days`), so a reinstall with the same `name` within that window needs `aws secretsmanager restore-secret` or a different `name`.

## Reference: Helm values the module sets

The values come from `modules/aws-helm-values` and use the chart's own keys (`deploy/helm/satchel/values.yaml`, which has a strict `values.schema.json`). It sets:

- `image.{repository,tag}`, `publicUrl`, `env`, `logFormat`;
- `resultDir` (an `s3://` path);
- `oidc.{issuer,clientId}`, `chat.*`;
- `web.replicaCount`, `autoscaling.*`, `worker.mode = cronjob`;
- `serviceAccount.{create,name,awsRoleArn}` and `serviceAccount.migration.{create,name,awsRoleArn}` (IRSA);
- `secretManager.{enabled,provider=aws,secrets,migrationSecrets,bootstrapSecrets}` with CSI delivery, or `existingSecret` and `bootstrap.existingSecret` with Kubernetes Secrets;
- `bootstrap.enabled`, `firstWorkspace.*`;
- `aws.{enabled,region,alb.*}`;
- `exposure.{type=ingress,host,timeoutSec}`;
- `networkPolicy.{enabled,allowCidrs}`.

Add anything else through `helm_extra_values`.

CI runs `deploy/terraform/scripts/check-helm-values.sh`. It renders these values for both secret delivery modes and runs `helm lint --strict`, `helm template` and kubeconform against the real chart, so the module and the chart can't drift apart unnoticed.
