#!/usr/bin/env bash
# Start floci (AWS) and the k3s stand-in for the EKS data plane, wait until both are healthy, and
# write deploy/floci-aws/.kubeconfig for Terraform's Kubernetes and Helm providers and kubectl.
# Env: FLOCI_AWS_PORT (4566), K3S_PORT (6551).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
port="${FLOCI_AWS_PORT:-4566}"
k3s_port="${K3S_PORT:-6551}"
export FLOCI_AWS_PORT="$port" K3S_PORT="$k3s_port"

if [ "$(docker inspect -f '{{.State.Running}}' floci-satchel-aws 2>/dev/null || true)" != "true" ]; then
  for p in "$port" "$k3s_port"; do
    if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "port $p is already in use; set FLOCI_AWS_PORT / K3S_PORT" >&2
      exit 1
    fi
  done
fi

# floci starts RDS containers on demand: pull the image up front so the first apply is quick.
docker pull -q pgvector/pgvector:pg17 >/dev/null

docker compose -f "$here/docker-compose.yml" up -d --force-recreate

ok=0
for _ in $(seq 1 90); do
  if curl -fsS -m 2 "http://localhost:$port/_floci/health" >/dev/null 2>&1 \
     && docker exec floci-satchel-aws-k3s kubectl get --raw /readyz >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 2
done
if [ "$ok" != 1 ]; then
  echo "floci or k3s did not become healthy" >&2
  docker logs --tail 40 floci-satchel-aws >&2 || true
  docker logs --tail 40 floci-satchel-aws-k3s >&2 || true
  exit 1
fi

docker exec floci-satchel-aws-k3s cat /output/kubeconfig.yaml \
  | sed "s#https://127.0.0.1:6443#https://127.0.0.1:$k3s_port#" > "$here/.kubeconfig"
chmod 600 "$here/.kubeconfig"
echo "floci (AWS) is up on http://localhost:$port; k3s on https://127.0.0.1:$k3s_port"
echo "kubeconfig: $here/.kubeconfig; floci address for pods: $("$here/floci-ip.sh")"
