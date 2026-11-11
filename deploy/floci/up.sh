#!/usr/bin/env bash
# Start floci-gcp for the satchel deployment tests and wait until it is healthy.
#   deploy/floci/up.sh                 Cloud Run in execution mode (containers really run)
#   deploy/floci/up.sh --cloudrun-mock Cloud Run control plane only (tier 1)
# Env: FLOCI_PORT (default 4588).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
port="${FLOCI_PORT:-4588}"
export FLOCI_CLOUDRUN_MOCK=false
for arg in "$@"; do
  case "$arg" in
    --cloudrun-mock) FLOCI_CLOUDRUN_MOCK=true ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [ "$(docker inspect -f '{{.State.Running}}' floci-satchel-gcp 2>/dev/null || true)" != "true" ]; then
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $port is already in use; set FLOCI_PORT" >&2
    exit 1
  fi
fi

# Images floci starts on demand: pull them up front so the first apply does not time out.
docker pull -q pgvector/pgvector:pg17 >/dev/null
docker pull -q alpine:3.20 >/dev/null

docker compose -f "$here/docker-compose.yml" up -d --force-recreate

for _ in $(seq 1 60); do
  status="$(docker inspect -f '{{.State.Health.Status}}' floci-satchel-gcp 2>/dev/null || true)"
  if [ "$status" = "healthy" ]; then
    echo "floci-gcp is up on http://localhost:$port (cloud run mock=$FLOCI_CLOUDRUN_MOCK)"
    exit 0
  fi
  sleep 2
done
echo "floci-gcp did not become healthy" >&2
docker logs --tail 50 floci-satchel-gcp >&2 || true
exit 1
