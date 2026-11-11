#!/usr/bin/env bash
# Stop the demo server and Postgres. Pass --volumes to also delete the database (asks first).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/.satchel"
export SATCHEL_CONFIG="$STATE/config.toml" XDG_STATE_HOME="$STATE/state" XDG_DATA_HOME="$STATE/data"
satchel() { uv run --quiet --project "$ROOT/server" satchel "$@"; }

if [[ "${1:-}" == "--volumes" ]]; then
  satchel down --all --purge
else
  satchel down --all
fi
