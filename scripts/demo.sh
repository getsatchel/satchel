#!/usr/bin/env bash
# One-command local demo: Postgres + the Jaffle Shop example + the curator UI + MCP.
#
#   scripts/demo.sh             # set everything up, then run the server in the foreground
#   scripts/demo.sh --detach    # same, but leave the server running in the background
#
# A thin wrapper over the CLI: `satchel init --example jaffle` then `satchel up`. Idempotent. Uses a
# repo-local config (.satchel/config.toml) so it never touches ~/.config/satchel. Tear down with
# scripts/demo-down.sh. Requires docker and uv; npm is optional (without it, no UI).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$ROOT/.satchel"
mkdir -p "$STATE"
export SATCHEL_CONFIG="$STATE/config.toml" XDG_STATE_HOME="$STATE/state" XDG_DATA_HOME="$STATE/data"

WS="${SATCHEL_DEMO_WORKSPACE:-jaffle}"
EMAIL="${SATCHEL_DEMO_EMAIL:-demo@satchel.test}"
PORT="${SATCHEL_DEMO_PORT:-8080}"
DETACH=()
[[ "${1:-}" == "--detach" ]] && DETACH=(--detach)

command -v docker >/dev/null 2>&1 || { echo "error: docker is required" >&2; exit 1; }
command -v uv >/dev/null 2>&1 || { echo "error: uv is required" >&2; exit 1; }
satchel() { uv run --quiet --project "$ROOT/server" satchel "$@"; }

# "hash" embeddings are offline and instant; use `satchel init --embedding fastembed` for real search.
satchel down >/dev/null 2>&1 || true   # a server from a previous run holds the DuckDB file + port
satchel init --yes --example jaffle -w "$WS" --email "$EMAIL" \
  --embedding "${SATCHEL_EMBEDDING_PROVIDER:-hash}" --public-url "http://localhost:$PORT"

UI_NOTE="skipped (npm not found): the REST API and MCP still work"
if command -v npm >/dev/null 2>&1; then
  USER_ID="$(satchel status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"]["user_id"])')"
  (
    cd "$ROOT/ui"
    [[ -d node_modules ]] || npm ci --no-audit --no-fund
    # Dev header auth is baked in so the demo needs no identity provider. Never ship this build.
    VITE_DEV_AUTH=1 VITE_DEV_WORKSPACE="$WS" VITE_DEV_USER="$USER_ID" npm run build
  )
  UI_NOTE="http://localhost:$PORT/"
fi

cat <<MSG

--------------------------------------------------------------------------------
satchel demo is ready.   UI: $UI_NOTE   MCP: http://localhost:$PORT/mcp
Connect a client (see docs/install.md for a stable install; --dev allows this checkout):

  satchel connect claude-desktop --dev     # or claude-code, cursor
  satchel doctor

Stop everything with scripts/demo-down.sh
--------------------------------------------------------------------------------
MSG

export SATCHEL_DEV_AUTH=1
satchel up --yes --port "$PORT" ${DETACH[@]+"${DETACH[@]}"}
