#!/usr/bin/env bash
# End-to-end test of satchel's AWS deployment (deploy/terraform/modules/{aws-foundation,eks} +
# deploy/helm) against the floci AWS emulator and a k3s stand-in for the EKS data plane. See
# docs/deploy/floci.md for what this proves and what it does not.
#
#   scripts/floci-aws-e2e.sh [--skip-build] [--keep]
#
# examples/floci-eks applies the real modules: VPC, KMS, RDS for PostgreSQL (a real pgvector
# container behind floci's proxy), S3, Secrets Manager, IAM policies and IRSA roles, ACM, WAF and
# the EKS control plane. The Helm release installs on k3s: migrate hook (bootstrap + upgrade) as
# the satchel-migrate service account, first-workspace Job, web Deployment, worker CronJob. Then
# MCP over HTTP stores a query result in emulated S3 and reads a page back, and an audit export
# lands in the audit bucket.
#
# Needs docker, terraform, helm, kubectl, curl and python3 (with boto3 for the S3 checks: the
# server venv has it) on PATH. Prints PASS/FAIL lines and exits non-zero when anything fails.
# Always tears down (destroy + deploy/floci-aws/down.sh) unless --keep is given.
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
# Latest migration revision (file names start with it), so the schema check follows new migrations.
SCHEMA_HEAD="$(ls "$root/server/satchel/store/migrations/versions" | grep -E '^[0-9]{4}_' | sort | tail -1 | cut -c1-4)"
floci="$root/deploy/floci-aws"
ex="$root/deploy/terraform/examples/floci-eks"
image="${SATCHEL_FLOCI_IMAGE:-satchel:floci}"
port="${FLOCI_AWS_PORT:-4566}"
endpoint="http://localhost:$port"
work="$(mktemp -d)"
py="${PYTHON:-$root/server/.venv/bin/python}"
[ -x "$py" ] || py=python3
export TF_IN_AUTOMATION=1 TF_INPUT=0
export KUBECONFIG="$floci/.kubeconfig"
# Only ever the emulator: fake credentials and an explicit local endpoint (never real AWS).
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
export AWS_ENDPOINT_URL="$endpoint"
unset AWS_PROFILE AWS_SESSION_TOKEN

build=1
keep=0
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-build) build=0; shift ;;
    --keep) keep=1; shift ;;
    *) echo "usage: $0 [--skip-build] [--keep]" >&2; exit 2 ;;
  esac
done

passed=0
failed=0
pass() { echo "PASS: $*"; passed=$((passed + 1)); }
fail() { echo "FAIL: $*"; failed=$((failed + 1)); }
section() { echo; echo "=== $*"; }
check() { local d="$1"; shift; if "$@" >"$work/last.log" 2>&1; then pass "$d"; else fail "$d"; tail -n 40 "$work/last.log" | sed 's/^/    /'; fi; }
must() { local d="$1"; shift; if "$@" >"$work/last.log" 2>&1; then pass "$d"; else fail "$d"; tail -n 60 "$work/last.log" | sed 's/^/    /'; return 1; fi; }

# Options go right after the subcommand: `terraform import` takes ADDR ID last.
tf() {
  local cmd="$1"; shift
  terraform -chdir="$ex" "$cmd" -no-color -var "floci_host=$fip" -var "floci_endpoint=$endpoint" "$@"
}
# apply, tolerating one emulator race: floci can finish CreateCluster after the SDK has already
# given up on the response (it is busy starting the RDS container), so the SDK's retry gets
# "ResourceInUseException: Cluster already exists" for a cluster that is not in state yet.
# Import it and apply again; any other failure is reported as is. Runs inside must(), whose
# output goes to $work/last.log.
apply() {
  tf apply -auto-approve && return 0
  grep -q "ResourceInUseException: Cluster already exists" "$work/last.log" || return 1
  echo "--- floci created the EKS cluster on a retried request; importing it and applying again"
  tf import 'module.eks.aws_eks_cluster.this[0]' satchel && tf apply -auto-approve
}
pg_container() { docker ps -q --filter label=io.floci.service=rds --filter label=floci_namespace=satchel-aws | head -n1; }
# has_key <bucket> <python regex>: some object key in the emulated bucket matches.
has_key() { "$py" -c "import boto3,re,sys; ks=[o['Key'] for o in boto3.client('s3').list_objects_v2(Bucket=sys.argv[1]).get('Contents', [])]; sys.exit(0 if any(re.search(sys.argv[2], k) for k in ks) else 1)" "$1" "$2"; }
pf_pid=""

teardown() {
  [ -n "$pf_pid" ] && kill "$pf_pid" 2>/dev/null
  if [ "$keep" = 1 ]; then
    echo "--keep: leaving floci, k3s and the Terraform state in place"
    return
  fi
  if [ -s "$ex/terraform.tfstate" ] && terraform -chdir="$ex" state list 2>/dev/null | grep -q .; then
    tf destroy -auto-approve >"$work/destroy.log" 2>&1 \
      || echo "destroy failed (down.sh removes the containers anyway)"
  fi
  rm -f "$ex"/terraform.tfstate "$ex"/terraform.tfstate.backup
  "$floci/down.sh" >/dev/null 2>&1 || true
}
trap teardown EXIT

run() {
  section "build and start"
  if [ "$build" = 1 ]; then
    must "build $image" docker build -f "$root/deploy/docker/Dockerfile" --build-arg BAKE_EMBEDDINGS=false \
      -t "$image" "$root" || return 1
  fi
  must "floci (AWS) + k3s up" "$floci/up.sh" || return 1
  fip="$("$floci/floci-ip.sh")"
  must "import $image into k3s" "$floci/k3s-load-image.sh" "$image" || return 1
  must "terraform init" terraform -chdir="$ex" init -input=false -no-color || return 1

  section "terraform apply (aws-foundation + eks + Helm release)"
  must "floci-eks apply" apply || return 1

  local results audit
  results="$(terraform -chdir="$ex" output -raw results_bucket)"
  audit="$(terraform -chdir="$ex" output -raw audit_bucket)"

  section "AWS resources (emulated)"
  check "RDS instance runs PostgreSQL with pgvector" sh -c \
    "docker exec $(pg_container) psql -U satchel -d satchel -Atc \"select extversion from pg_extension where extname='vector'\" | grep -q ."
  check "results bucket: SSE-KMS and lifecycle expiry" "$py" -c "
import boto3; s = boto3.client('s3'); b = '$results'
enc = s.get_bucket_encryption(Bucket=b)['ServerSideEncryptionConfiguration']['Rules'][0]
assert enc['ApplyServerSideEncryptionByDefault']['SSEAlgorithm'] == 'aws:kms', enc
assert s.get_bucket_lifecycle_configuration(Bucket=b)['Rules'][0]['Expiration']['Days'] == 8
assert s.get_public_access_block(Bucket=b)['PublicAccessBlockConfiguration']['BlockPublicPolicy']"
  check "audit bucket is versioned" "$py" -c "
import boto3; assert boto3.client('s3').get_bucket_versioning(Bucket='$audit')['Status'] == 'Enabled'"
  check "Secrets Manager holds every role's secret" "$py" -c "
import boto3; sm = boto3.client('secretsmanager')
names = {s['Name'] for s in sm.list_secrets()['SecretList']}
want = {'satchel/' + k for k in ['database-url', 'admin-database-url', 'migration-database-url', 'owner-password', 'app-password', 'worker-password', 'oidc-client-secret']}
assert want <= names, want - names
assert sm.get_secret_value(SecretId='satchel/database-url')['SecretString'].startswith('postgresql+psycopg://satchel_app:')"
  check "runtime policy cannot read owner-level secrets" "$py" -c "
import boto3, json; iam = boto3.client('iam')
arn = 'arn:aws:iam::000000000000:policy/satchel-runtime'
doc = iam.get_policy_version(PolicyArn=arn, VersionId=iam.get_policy(PolicyArn=arn)['Policy']['DefaultVersionId'])['PolicyVersion']['Document']
doc = json.loads(doc) if isinstance(doc, str) else doc
res = [r for s in doc['Statement'] if s['Sid'] == 'RuntimeSecrets' for r in ([s['Resource']] if isinstance(s['Resource'], str) else s['Resource'])]
assert res and not any(k in r for r in res for k in ('migration-database-url', 'owner-password', 'app-password', 'worker-password')), res"
  check "IRSA roles trust exactly their service accounts" "$py" -c "
import boto3, json, urllib.parse; iam = boto3.client('iam')
for role, sa in [('satchel-runtime', 'satchel:satchel'), ('satchel-migrate', 'satchel:satchel-migrate')]:
    d = iam.get_role(RoleName=role)['Role']['AssumeRolePolicyDocument']
    d = json.loads(urllib.parse.unquote(d)) if isinstance(d, str) else d
    assert 'system:serviceaccount:' + sa in json.dumps(d), (role, d)"
  check "WAF web ACL and ACM certificate exist" sh -c \
    "[ -n \"\$(terraform -chdir='$ex' output -raw waf_acl_arn)\" ] && [ -n \"\$(terraform -chdir='$ex' output -raw certificate_arn)\" ]"

  section "Helm release on k3s"
  check "migrate hook Job completed (bootstrap + upgrade)" sh -c \
    "kubectl get events -n satchel --field-selector involvedObject.name=satchel-migrate,reason=Completed -o name | grep -q ."
  check "migrate Job ran as satchel-migrate (IRSA: migrate role)" sh -c \
    "[ \"\$(kubectl get sa -n satchel satchel-migrate -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}')\" = arn:aws:iam::000000000000:role/satchel-migrate ]"
  check "web runs as satchel (IRSA: runtime role)" sh -c \
    "[ \"\$(kubectl get deploy -n satchel satchel -o jsonpath='{.spec.template.spec.serviceAccountName}')\" = satchel ] && [ \"\$(kubectl get sa -n satchel satchel -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}')\" = arn:aws:iam::000000000000:role/satchel-runtime ]"
  check "first-workspace Job completed" sh -c \
    "kubectl get events -n satchel --field-selector involvedObject.name=satchel-workspace,reason=Completed -o name | grep -q ."
  check "web Deployment Ready" kubectl rollout status -n satchel deploy/satchel --timeout=300s
  check "web pod /readyz 200" kubectl exec -n satchel deploy/satchel -- python -c \
    "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8080/readyz'); assert r.status == 200"
  check "worker CronJob present" kubectl get cronjob -n satchel satchel-worker
  check "SATCHEL_RESULT_DIR is s3:// and AWS_REGION is set" sh -c \
    "kubectl get cm -n satchel satchel -o jsonpath='{.data.SATCHEL_RESULT_DIR} {.data.AWS_REGION}' | grep -qx 's3://$results/results us-east-1'"
  check "schema at head ($SCHEMA_HEAD)" sh -c "[ \"\$(docker exec $(pg_container) psql -U satchel -d satchel -Atc 'select version_num from alembic_version')\" = $SCHEMA_HEAD ]"
  check "satchel_app and satchel_worker exist without BYPASSRLS" sh -c \
    "docker exec $(pg_container) psql -U satchel -d satchel -Atc \"select count(*) from pg_roles where rolname in ('satchel_app','satchel_worker') and not rolbypassrls and not rolsuper\" | grep -qx 2"
  check "web pod reads and writes the results bucket (S3, ranged reads)" \
    kubectl exec -n satchel deploy/satchel -- python -c \
    "from satchel.data.objstore import open_bucket; b=open_bucket('s3','$results'); b.put('probe/x', b'hello'); assert b.size('probe/x')==5 and b.read_range('probe/x',1,4)==b'ell' and b.delete('probe/x') and b.size('probe/x') is None"

  section "MCP over HTTP: a stored result in S3"
  must "DuckDB connection and PAT" kubectl exec -n satchel deploy/satchel -- sh -c \
    "satchel connection add -w demo --name e2e --kind duckdb --parquet sample=/opt/venv/lib/python3.12/site-packages/pyarrow/tests/data/parquet/v0.7.1.parquet && satchel token create -w demo --user admin@example.com --name floci-e2e" || return 1
  local token
  token="$(grep -oE 'satchel_pat_[A-Za-z0-9_-]+' "$work/last.log" | head -n1)"
  kubectl port-forward -n satchel deploy/satchel 18080:8080 >"$work/pf.log" 2>&1 &
  pf_pid=$!
  for _ in $(seq 1 30); do curl -fsS -m 2 http://127.0.0.1:18080/healthz >/dev/null 2>&1 && break; sleep 1; done
  must "MCP: run_sql stores a result, get_result reads a page back" \
    python3 "$root/deploy/floci/mcp_smoke.py" http://127.0.0.1:18080 "$token" || return 1
  local rid
  rid="$(sed -n 's/^RESULT_ID=//p' "$work/last.log")"
  check "result row points at s3://" sh -c \
    "docker exec $(pg_container) psql -U satchel -d satchel -Atc \"select path from query_result where id='$rid'\" | grep -q '^s3://$results/results/'"
  check "result object is in the emulated bucket" has_key "$results" "$rid\\.parquet\$"

  section "audit export to S3"
  check "satchel audit export --to s3://" kubectl exec -n satchel deploy/satchel -- \
    satchel audit export --since 2020-01-01 --to "s3://$audit/exports"
  check "one JSONL object per day under audit/demo/" has_key "$audit" '^exports/audit/demo/[0-9]{4}/[0-9]{2}/[0-9]{2}\.jsonl$'

  section "plan stability"
  # floci reports the RDS proxy port instead of 5432 and its own security-group reference form,
  # so three in-place updates remain; nothing may be replaced.
  check "re-plan replaces nothing" sh -c "terraform -chdir='$ex' plan -no-color -var floci_host=$fip -var floci_endpoint=$endpoint | (! grep -q 'must be replaced')"
}

run || true

section "summary"
echo "passed: $passed, failed: $failed"
[ "$failed" -eq 0 ]
