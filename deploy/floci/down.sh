#!/usr/bin/env bash
# Stop floci-gcp and remove ONLY what it created for these tests: the compose project
# (floci-satchel), and the containers and volumes floci spawned under the "satchel" namespace
# (labels floci_emulator=floci-gcp and floci_namespace=satchel, names floci-gcp-satchel-*), and the
# floci-satchel network. Nothing else on the Docker host is touched.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
ns=satchel

# Stop the emulator first so it cannot start new sidecars while they are removed.
docker compose -f "$here/docker-compose.yml" stop --timeout 20 >/dev/null 2>&1 || true

# Sidecars (Cloud SQL postgres, Cloud Run revisions/tasks, k3s, GCS helpers).
filters=(--filter "label=floci_emulator=floci-gcp" --filter "label=floci_namespace=$ns")
ids="$(docker ps -aq "${filters[@]}")"
if [ -n "$ids" ]; then
  echo "removing floci containers:"; docker ps -a "${filters[@]}" --format '  {{.Names}}'
  # shellcheck disable=SC2086
  docker rm -f -v $ids >/dev/null
fi
vols="$(docker volume ls -q "${filters[@]}")"
if [ -n "$vols" ]; then
  echo "removing floci volumes:"
  # shellcheck disable=SC2086
  printf '  %s\n' $vols
  # shellcheck disable=SC2086
  docker volume rm -f $vols >/dev/null
fi
# Belt and braces for anything spawned without labels: the namespaced name prefix only.
left="$(docker ps -aq --filter "name=^floci-gcp-$ns-")"
# shellcheck disable=SC2086
[ -z "$left" ] || docker rm -f -v $left >/dev/null
left="$(docker volume ls -q --filter "name=^floci-gcp-$ns-")"
# shellcheck disable=SC2086
[ -z "$left" ] || docker volume rm -f $left >/dev/null

docker compose -f "$here/docker-compose.yml" down --volumes --remove-orphans --timeout 20 || true

if docker network inspect floci-satchel >/dev/null 2>&1; then
  docker network rm floci-satchel >/dev/null
fi
echo "floci-gcp stopped and cleaned up"
