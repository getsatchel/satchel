#!/usr/bin/env bash
# Delete every object in an emulated GCS bucket. Emulator gap: hashicorp/google 6.x lists the
# bucket's Anywhere Caches before force_destroy empties it, floci answers 405, and the provider
# then deletes the (non-empty) bucket and gets 409. Run this before `terraform destroy`.
#   empty-bucket.sh <bucket>...
# Env: FLOCI_ENDPOINT (default http://localhost:4588).
set -euo pipefail
endpoint="${FLOCI_ENDPOINT:-http://localhost:4588}"
for bucket in "$@"; do
  listing="$(curl -fsS "$endpoint/storage/v1/b/$bucket/o?versions=true" 2>/dev/null || true)"
  if [ -z "$listing" ]; then
    echo "gs://$bucket: not found, skipped"
    continue
  fi
  names="$(printf '%s' "$listing" | python3 -c 'import json,sys,urllib.parse
for o in json.load(sys.stdin).get("items", []):
    print(urllib.parse.quote(o["name"], safe="") + "?generation=" + o.get("generation", ""))')"
  n=0
  for obj in $names; do
    curl -fsS -X DELETE "$endpoint/storage/v1/b/$bucket/o/$obj" >/dev/null && n=$((n + 1))
  done
  echo "emptied gs://$bucket ($n object(s))"
done
