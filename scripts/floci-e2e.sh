#!/usr/bin/env bash
# End-to-end test of satchel's GCP deployment (deploy/terraform + deploy/helm) against the floci-gcp
# emulator. See docs/deploy/floci.md for what this proves and what it does not.
#
#   scripts/floci-e2e.sh [--tier 1|2|all] [--skip-build] [--keep]
#
# Tier 1 (control plane): examples/floci-cloud-run applies and destroys with Cloud Run in mock
#   mode; examples/floci-gke applies on a real k3s cluster and the Helm release installs (migrate
#   hook, first-workspace Job, web pod Ready), then destroys.
# Tier 2 (real workloads): examples/floci-cloud-run with Cloud Run in execution mode: migrate job
#   (bootstrap + upgrade), first workspace, the web service through floci's front door, the worker
#   job as satchel_worker, the audit-export job, and a stored query result in emulated GCS read back
#   over MCP.
#
# Needs docker, terraform, helm, kubectl, curl and python3 on PATH. Prints PASS/FAIL lines and
# exits non-zero when anything fails. Always tears down (destroy + deploy/floci/down.sh) unless
# --keep is given.
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
# Latest migration revision (file names start with it), so the schema check follows new migrations.
SCHEMA_HEAD="$(ls "$root/server/satchel/store/migrations/versions" | grep -E '^[0-9]{4}_' | sort | tail -1 | cut -c1-4)"
floci="$root/deploy/floci"
cr="$root/deploy/terraform/examples/floci-cloud-run"
gke="$root/deploy/terraform/examples/floci-gke"
image="${SATCHEL_FLOCI_IMAGE:-satchel:floci}"
endpoint="${FLOCI_ENDPOINT:-http://localhost:4588}"
project="floci-local"
region="us-central1"
work="$(mktemp -d)"
export FLOCI_ENDPOINT="$endpoint" FLOCI_PROJECT="$project" FLOCI_REGION="$region"
export TF_IN_AUTOMATION=1 TF_INPUT=0
export KUBECONFIG="$work/kubeconfig"

tier=all
build=1
keep=0
while [ $# -gt 0 ]; do
  case "$1" in
    --tier) tier="$2"; shift 2 ;;
    --skip-build) build=0; shift ;;
    --keep) keep=1; shift ;;
    *) echo "usage: $0 [--tier 1|2|all] [--skip-build] [--keep]" >&2; exit 2 ;;
  esac
done

passed=0
failed=0
pass() { echo "PASS: $*"; passed=$((passed + 1)); }
fail() { echo "FAIL: $*"; failed=$((failed + 1)); }
section() { echo; echo "=== $*"; }
# check <description> <command...>: PASS when the command succeeds.
check() { local d="$1"; shift; if "$@" >"$work/last.log" 2>&1; then pass "$d"; else fail "$d"; tail -n 40 "$work/last.log" | sed 's/^/    /'; fi; }
# must <description> <command...>: like check, but stop the tier on failure.
must() { local d="$1"; shift; if "$@" >"$work/last.log" 2>&1; then pass "$d"; else fail "$d"; tail -n 60 "$work/last.log" | sed 's/^/    /'; return 1; fi; }

tf() { terraform -chdir="$1" "${@:2}" -no-color; }

pg_container() {
  docker ps -q --filter label=io.floci.service=cloudsql --filter label=floci_namespace=satchel | head -n1
}
psql_q() { docker exec "$(pg_container)" psql -U postgres -d satchel -Atc "$1"; }

floci_ip() {
  docker inspect -f '{{(index .NetworkSettings.Networks "floci-satchel").IPAddress}}' floci-satchel-gcp
}

# Best-effort teardown of whatever a tier left behind: destroy, forget state, stop floci.
reset() {
  "$floci/empty-bucket.sh" "$project-satchel-results" "$project-satchel-audit" >/dev/null 2>&1 || true
  for d in "$gke" "$cr"; do
    if [ -s "$d/terraform.tfstate" ] && terraform -chdir="$d" state list 2>/dev/null | grep -q .; then
      terraform -chdir="$d" destroy -auto-approve -no-color -var "storage_emulator_host=" \
        >"$work/destroy.log" 2>&1 \
        || echo "destroy of $(basename "$d") failed (down.sh removes the containers anyway)"
    fi
    rm -f "$d"/terraform.tfstate "$d"/terraform.tfstate.backup
  done
  "$floci/down.sh" >/dev/null 2>&1 || true
}

diagnostics() {
  section "diagnostics"
  docker ps -a --filter label=floci_namespace=satchel --format '{{.Names}}  {{.Status}}' || true
  if [ -s "$KUBECONFIG" ]; then
    kubectl get all,jobs,events -n satchel 2>/dev/null | tail -n 60 || true
    kubectl logs -n satchel -l app.kubernetes.io/instance=satchel --all-containers --tail=50 --prefix 2>/dev/null || true
  fi
  docker logs --tail 200 floci-satchel-gcp 2>&1 | grep -v ' INFO ' | tail -n 100 || true
}

cleanup() {
  local rc=$?
  [ "$failed" -eq 0 ] || diagnostics
  if [ "$keep" = 1 ]; then
    echo "--keep: leaving floci and the deployment running (deploy/floci/down.sh removes them)"
  else
    reset
  fi
  rm -rf "$work"
  exit "$rc"
}
trap cleanup EXIT

for bin in docker terraform helm kubectl curl python3; do
  command -v "$bin" >/dev/null || { echo "missing $bin on PATH" >&2; exit 2; }
done

if [ "$build" = 1 ]; then
  section "build $image"
  must "docker build $image" docker build -f "$root/deploy/docker/Dockerfile" \
    --build-arg BAKE_EMBEDDINGS=false -t "$image" "$root" || exit 1
fi

for d in "$cr" "$gke"; do
  rm -f "$d"/terraform.tfstate "$d"/terraform.tfstate.backup
  must "terraform init $(basename "$d")" tf "$d" init -input=false || exit 1
done

# ---------------------------------------------------------------------------------------------
tier1() {
  section "tier 1: control plane"
  must "floci up (Cloud Run mock)" "$floci/up.sh" --cloudrun-mock || return 1

  must "floci-cloud-run apply (Cloud Run mock)" tf "$cr" apply -auto-approve || return 1
  check "Cloud Run service, 3 jobs, 2 scheduler jobs registered" python3 - "$endpoint" <<'PY'
import json, sys, urllib.request
e = sys.argv[1]
get = lambda p: json.load(urllib.request.urlopen(e + p))
base = "/v2/projects/floci-local/locations/us-central1"
svc = get(base + "/services/satchel")
jobs = {j["name"].rsplit("/", 1)[1] for j in get(base + "/jobs").get("jobs", [])}
sched = {j["name"].rsplit("/", 1)[1] for j in get("/v1/projects/floci-local/locations/us-central1/jobs").get("jobs", [])}
assert svc["template"]["containers"][0]["image"] == "satchel:floci", svc["template"]["containers"][0]["image"]
assert jobs == {"satchel-migrate", "satchel-worker", "satchel-audit-export"}, jobs
assert sched == {"satchel-worker", "satchel-audit-export"}, sched
print("service", svc["uri"], "jobs", sorted(jobs), "scheduler", sorted(sched))
PY
  "$floci/empty-bucket.sh" "$project-satchel-results" "$project-satchel-audit" >/dev/null 2>&1
  must "floci-cloud-run destroy" tf "$cr" destroy -auto-approve || return 1

  # GKE: the providers are configured from the cluster, so the cluster comes first.
  must "floci-gke apply step 1 (foundation + cluster)" tf "$gke" apply -auto-approve \
    -target=module.foundation -target=terraform_data.cloudsql_shim \
    -target=module.gke.google_container_cluster.this || return 1
  must "import $image into k3s" "$floci/k3s-load-image.sh" satchel "$image" || return 1
  local sem
  sem="http://$(floci_ip):4588"
  must "floci-gke apply step 2 (Helm release)" tf "$gke" apply -auto-approve \
    -var "storage_emulator_host=$sem" || return 1

  local ep
  ep="$(terraform -chdir="$gke" output -raw cluster_endpoint)"
  kubectl config set-cluster floci --server="https://$ep" --insecure-skip-tls-verify=true >/dev/null
  kubectl config set-credentials floci --token=floci-dummy-token >/dev/null
  kubectl config set-context floci --cluster=floci --user=floci --namespace=satchel >/dev/null
  kubectl config use-context floci >/dev/null

  check "migrate hook Job completed (bootstrap init container + upgrade)" \
    sh -c "kubectl get events -n satchel --field-selector involvedObject.name=satchel-migrate,reason=Completed -o name | grep -q ."
  check "bootstrap ran in the hook" \
    sh -c "kubectl get events -n satchel --field-selector reason=Started -o jsonpath='{.items[*].message}' | grep -q db-bootstrap"
  check "first-workspace Job completed" \
    sh -c "kubectl get events -n satchel --field-selector involvedObject.name=satchel-workspace,reason=Completed -o name | grep -q ."
  check "web Deployment Ready" kubectl rollout status -n satchel deploy/satchel --timeout=300s
  check "web pod /readyz 200" kubectl exec -n satchel deploy/satchel -- python -c \
    "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8080/readyz'); print(r.status, r.read().decode()); assert r.status == 200"
  check "worker CronJob present" kubectl get cronjob -n satchel satchel-worker
  check "schema at head ($SCHEMA_HEAD)" sh -c "[ \"\$(docker exec $(pg_container) psql -U postgres -d satchel -Atc 'select version_num from alembic_version')\" = $SCHEMA_HEAD ]"
  check "workspace demo exists" sh -c "docker exec $(pg_container) psql -U postgres -d satchel -Atc \"select slug from workspace\" | grep -qx demo"
  check "web pod reads and writes the results bucket (GCS emulator)" \
    kubectl exec -n satchel deploy/satchel -- python -c \
    "from satchel.data.objstore import open_bucket; b=open_bucket('gs','$project-satchel-results'); b.put('probe/x', b'hello'); assert b.size('probe/x')==5 and b.read_range('probe/x',1,4)==b'ell' and b.delete('probe/x'); print('ok')"
  check "re-plan does not replace the cluster" sh -c \
    "terraform -chdir='$gke' plan -no-color -var storage_emulator_host=$sem | (! grep -q 'must be replaced')"

  "$floci/empty-bucket.sh" "$project-satchel-results" "$project-satchel-audit" >/dev/null 2>&1
  must "floci-gke destroy" tf "$gke" destroy -auto-approve -var "storage_emulator_host=$sem" || return 1
  "$floci/down.sh" >/dev/null
}

# ---------------------------------------------------------------------------------------------
tier2() {
  section "tier 2: real workloads on Cloud Run (execution mode)"
  must "floci up (Cloud Run execution)" "$floci/up.sh" || return 1

  must "floci-cloud-run apply without the service" tf "$cr" apply -auto-approve -var deploy_service=false || return 1

  must "migrate job: bootstrap + upgrade" "$floci/run-job.sh" satchel-migrate || return 1
  if grep -q "created role satchel_app" "$work/last.log"; then
    pass "bootstrap created satchel_app and satchel_worker"
  else
    fail "bootstrap created satchel_app and satchel_worker"
  fi
  check "schema at head ($SCHEMA_HEAD)" sh -c "[ \"\$(docker exec $(pg_container) psql -U postgres -d satchel -Atc 'select version_num from alembic_version')\" = $SCHEMA_HEAD ]"
  check "tables owned by satchel, roles without BYPASSRLS" sh -c \
    "docker exec $(pg_container) psql -U postgres -d satchel -Atc \"select (select count(*) from pg_tables where schemaname='public' and tableowner<>'satchel') = 0 and (select bool_and(not rolbypassrls and not rolsuper) from pg_roles where rolname in ('satchel_app','satchel_worker'))\" | grep -qx t"
  check "migrate job is idempotent" "$floci/run-job.sh" satchel-migrate

  local ws='["-c","satchel workspace create demo --name Demo --admin-email admin@example.com"]'
  must "first workspace (migrate job with override args)" "$floci/run-job.sh" satchel-migrate "$ws" || return 1
  check "workspace create is idempotent" sh -c "'$floci/run-job.sh' satchel-migrate '$ws' | grep -q 'already exist'"

  must "floci-cloud-run apply with the service" tf "$cr" apply -auto-approve || return 1
  local url
  url="$(terraform -chdir="$cr" output -raw public_url)"
  echo "service: $url"
  check "/healthz 200" curl -fsS -m 20 "$url/healthz"
  check "/readyz 200" curl -fsS -m 20 "$url/readyz"
  check "/ serves the UI" sh -c "curl -fsS -m 20 '$url/' | grep -qi '<title>satchel</title>'"
  check "/.well-known/oauth-protected-resource/mcp names <public_url>/mcp" sh -c \
    "curl -fsS -m 20 '$url/.well-known/oauth-protected-resource/mcp' | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d[\"resource\"]==\"$url/mcp\", d; print(d[\"resource\"])'"

  psql_q "alter system set log_connections = on" >/dev/null && psql_q "select pg_reload_conf()" >/dev/null
  local since
  since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  check "worker job (satchel worker --once)" "$floci/run-job.sh" satchel-worker
  check "worker connected as satchel_worker" sh -c \
    "docker logs --since $since $(pg_container) 2>&1 | grep -q 'connection authorized: user=satchel_worker'"
  check "audit-export job" "$floci/run-job.sh" satchel-audit-export

  # A DuckDB connection (any parquet file in the image backs it) and a PAT, then MCP over HTTP.
  local setup='["-c","satchel connection add -w demo --name e2e --kind duckdb --parquet sample=/opt/venv/lib/python3.12/site-packages/pyarrow/tests/data/parquet/v0.7.1.parquet && satchel token create -w demo --user admin@example.com --name floci-e2e"]'
  must "DuckDB connection and PAT (migrate job)" "$floci/run-job.sh" satchel-migrate "$setup" || return 1
  local token
  token="$(grep -oE 'satchel_pat_[A-Za-z0-9_-]+' "$work/last.log" | head -n1)"
  must "MCP: run_sql stores a result, get_result reads a page back" python3 "$floci/mcp_smoke.py" "$url" "$token" || return 1
  local rid
  rid="$(sed -n 's/^RESULT_ID=//p' "$work/last.log")"
  check "result row points at gs://" sh -c \
    "docker exec $(pg_container) psql -U postgres -d satchel -Atc \"select path from query_result where id='$rid'\" | grep -q '^gs://$project-satchel-results/results/'"
  check "result object is in the emulated bucket" sh -c \
    "curl -fsS '$endpoint/storage/v1/b/$project-satchel-results/o?prefix=results/' | grep -q '$rid.parquet'"

  "$floci/empty-bucket.sh" "$project-satchel-results" "$project-satchel-audit" >/dev/null 2>&1
  must "floci-cloud-run destroy" tf "$cr" destroy -auto-approve || return 1
  "$floci/down.sh" >/dev/null
}

case "$tier" in
  1) tier1 || true ;;
  2) tier2 || true ;;
  all) tier1 || true; [ "$keep" = 1 ] || reset; tier2 || true ;;
  *) echo "unknown tier $tier" >&2; exit 2 ;;
esac

section "summary"
echo "passed: $passed, failed: $failed"
[ "$failed" -eq 0 ]
