#!/usr/bin/env bash
# floci's IP on the compose network: what pods in k3s use for S3 and RDS (k3s's CoreDNS cannot
# resolve compose service names).
set -euo pipefail
docker inspect -f '{{(index .NetworkSettings.Networks "floci-satchel-aws").IPAddress}}' floci-satchel-aws
