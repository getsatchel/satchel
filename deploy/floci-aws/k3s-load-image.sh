#!/usr/bin/env bash
# Import a local Docker image into the k3s container's containerd, so pods can run it with
# imagePullPolicy: Never (nothing is pushed to a registry).
#   k3s-load-image.sh <image>
set -euo pipefail
image="$1"
docker save "$image" | docker exec -i floci-satchel-aws-k3s ctr -n k8s.io images import - >/dev/null
echo "imported $image into k3s"
