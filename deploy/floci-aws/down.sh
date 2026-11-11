#!/usr/bin/env bash
# Stop floci (AWS), k3s and every container, volume and network floci started for them (RDS
# Postgres, the EKS/ECR helpers, VPC networks), matched by the floci_namespace=satchel-aws label. Never touches anything else.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
ids="$(docker ps -aq --filter label=floci_namespace=satchel-aws)"
# floci names its children floci-aws-satchel-aws-*; catch any without the label.
ids="$ids $(docker ps -aq --filter name=floci-aws-satchel-aws-)"
docker compose -f "$here/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1 || true
for id in $ids; do
  docker rm -f "$id" >/dev/null 2>&1 || true
done
vols="$(docker volume ls -q --filter label=floci_namespace=satchel-aws)"
for v in $vols; do
  docker volume rm -f "$v" >/dev/null 2>&1 || true
done
# floci backs each emulated VPC with a Docker network of the same namespace.
for n in $(docker network ls -q --filter label=floci_namespace=satchel-aws); do
  docker network rm "$n" >/dev/null 2>&1 || true
done
rm -f "$here/.kubeconfig"
echo "floci (AWS) and k3s are down"
