#!/usr/bin/env bash
# Import a local Docker image into the containerd of an emulated GKE (k3s) cluster, so pods can
# run it with imagePullPolicy: Never (the floci examples build satchel locally; nothing is pushed).
#   k3s-load-image.sh <cluster> <image>
# Env: FLOCI_NAMESPACE (satchel).
set -euo pipefail

cluster="$1"
image="$2"
ns="${FLOCI_NAMESPACE:-satchel}"

cid="$(docker ps -q --filter label=io.floci.service=gke \
  --filter "label=io.floci.resource-id=$cluster" --filter "label=floci_namespace=$ns" | head -n1)"
if [ -z "$cid" ]; then
  echo "no floci GKE (k3s) container for cluster $cluster" >&2
  exit 1
fi
docker save "$image" | docker exec -i "$cid" ctr -n k8s.io images import - >/dev/null
docker exec "$cid" ctr -n k8s.io images ls -q | grep -F ":${image##*:}" | grep -F "${image%%:*}" | head -n1
echo "imported $image into cluster $cluster"
