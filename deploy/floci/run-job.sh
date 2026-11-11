#!/usr/bin/env bash
# Run a Cloud Run job on floci-gcp through the Cloud Run Admin API v2 (gcloud is not supported
# for Cloud Run on floci) and wait for it. Prints the task output and exits non-zero when the
# execution fails.
#   run-job.sh <job> [container args as a JSON array]
#   run-job.sh satchel-migrate '["-c","satchel workspace create demo --name Demo --admin-email a@b.c"]'
# Env: FLOCI_ENDPOINT (http://localhost:4588), FLOCI_PROJECT (floci-local), FLOCI_REGION
#      (us-central1), RUN_JOB_TIMEOUT seconds (900).
set -euo pipefail

job="$1"
args="${2:-}"
endpoint="${FLOCI_ENDPOINT:-http://localhost:4588}"
project="${FLOCI_PROJECT:-floci-local}"
region="${FLOCI_REGION:-us-central1}"
timeout="${RUN_JOB_TIMEOUT:-900}"

body='{}'
if [ -n "$args" ]; then
  body="{\"overrides\":{\"containerOverrides\":[{\"args\":$args}]}}"
fi

since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
op="$(curl -fsS -X POST "$endpoint/v2/projects/$project/locations/$region/jobs/$job:run" \
  -H 'content-type: application/json' -d "$body")"
name="$(printf '%s' "$op" | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"

deadline=$(( $(date +%s) + timeout ))
while :; do
  op="$(curl -fsS "$endpoint/v2/$name")"
  done_="$(printf '%s' "$op" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("done", False))')"
  [ "$done_" = "True" ] && break
  if [ "$(date +%s)" -gt "$deadline" ]; then
    echo "timed out waiting for $job" >&2
    exit 1
  fi
  sleep 3
done

# Task output: floci logs job containers at DEBUG (category io.floci.gcp.services.cloudrun).
echo "--- $job output"
docker logs --since "$since" floci-satchel-gcp 2>&1 | grep -E "\[$job-[a-z0-9]+-task[0-9]+ attempt [0-9]+ (STDOUT|STDERR)\]" \
  | sed -E 's/^.*(STDOUT|STDERR)\] /  /' || true

err="$(printf '%s' "$op" | python3 -c 'import json,sys; e=json.load(sys.stdin).get("error"); print(json.dumps(e) if e else "")')"
if [ -n "$err" ]; then
  echo "--- $job FAILED: $err"
  exit 1
fi
echo "--- $job succeeded"
