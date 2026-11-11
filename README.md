# Satchel

An open-source, self-hosted **context server for data agents**.

Bring your own agent (Claude, Claude Code, Cursor, ChatGPT, Codex) and connect it to Satchel over MCP. Satchel does four things:

- It serves curated, versioned, permissioned context about your data: guides, glossary, metrics, golden queries, table docs and endorsements.
- It learns from how agents use that context. It records tool traces, the gaps agents flag, and user feedback.
- It turns those signals into suggestions that analysts review, then evaluates each change before it is published.

The semantic SQL engine (wren-core) and notebooks (marimo) are reused open-source projects. Satchel builds the part nobody ships: shared context, a feedback loop, and curation.

**Status:** pre-alpha. Not ready for production use.

- Documentation: [`docs/README.md`](docs/README.md)
- Architecture: [`docs/architecture/system.md`](docs/architecture/system.md)

## Quickstart

Needs [uv](https://docs.astral.sh/uv/) and Docker. This installs the latest release from PyPI, where the package is `satchel-server` ([release notes](https://github.com/getsatchel/satchel/releases)). The full guide is [`docs/install.md`](docs/install.md); in short:

```bash
uv tool install satchel-server   # the command is `satchel`
satchel init --example jaffle      # Postgres, migrations, workspace, admin, token, sample data
satchel up                         # UI + REST + MCP at http://localhost:8080
satchel connect claude-desktop     # writes the client config (also: claude-code, cursor)
satchel doctor                     # checks everything and prints the fix for anything wrong
```

### Demo from a checkout

`scripts/demo.sh` is a thin wrapper over `satchel init --example jaffle --yes` and `satchel up`, plus a UI build with dev header auth (`SATCHEL_DEV_AUTH=1`, no identity provider: never expose it beyond localhost). `scripts/demo.sh --detach` keeps your terminal; `scripts/demo-down.sh` stops it (`--volumes` also deletes the data). Ask Claude a question about the Jaffle Shop data, then watch the session appear under **Sessions**. To fill the UI without a client, run `SATCHEL_TOKEN=... uv run --project server python scripts/demo_activity.py`.

![Library](docs/screenshots/library-light.png)
![Session inspector](docs/screenshots/session-inspector-light.png)

More screenshots (light and dark) are in [`docs/screenshots/`](docs/screenshots/).

## Curator UI

The UI (`ui/`: React 18, TypeScript, Vite, TanStack Query, Tailwind, Monaco, Recharts) talks to the REST API in `/v1`. `satchel serve api` serves the built UI at `/` when `ui/dist` exists (override with `SATCHEL_UI_DIR`).

```bash
cd ui
npm ci
npm run dev          # http://localhost:5173, proxies /v1 /auth /mcp /oauth to localhost:8080
npm run typecheck && npm run lint
npm run build        # outputs ui/dist
npm run gen:api      # regenerate src/api/schema.d.ts after `satchel openapi -o ../ui/openapi.json`
```

The MCP Apps result widget lives in `ui-widgets/` (TypeScript + esbuild). `cd ui-widgets && npm ci && npm run build` writes the committed single-file `server/satchel/resources/widgets/result.html`; `npm run check` (CI) fails when it is stale and `npm run test:e2e` drives it headlessly with a fake host.

Sign-in: without any setting the UI redirects to `/auth/login` (OIDC session cookie, CSRF handled). For local work without an IdP, run the API with `SATCHEL_DEV_AUTH=1` and start the UI with `VITE_DEV_AUTH=1` (optionally `VITE_DEV_WORKSPACE` and `VITE_DEV_USER`, a user UUID); it then asks for a workspace slug and user id and sends the dev headers.

## Curation loop

Satchel turns what agents struggle with into reviewable suggestions. A job embeds the signals agents write (flagged gaps, user corrections, SQL errors, metric divergence), runs hygiene checks on the context itself (stale references, dead, duplicate and contradictory items, memories shared across users), clusters the signals (default: at least 2 users, 2 sessions and 3 signals), and has a chat model draft concrete changes. Nothing is published: curators review each change in the **Inbox**, and accepting one creates a draft that still goes through preview and publish.

```bash
cd server
uv run satchel curate run --workspace jaffle            # embed -> hygiene -> cluster -> draft
uv run satchel curate run --workspace jaffle --no-llm   # everything except model calls
uv run satchel curate show --workspace jaffle --bodies  # suggestions and proposed changes
uv run satchel worker                                   # every workspace, every SATCHEL_CURATE_INTERVAL_MIN (1440)
uv run satchel worker --once                            # one pass, for a Cloud Run Job / Kubernetes CronJob
uv run satchel workspace curation -w jaffle --min-users 3 --instructions "Churn is owned by finance"
```

Drafting needs a chat model; without one, clustering and hygiene still run and suggestions wait for a draft (a warning is logged). Configure it in the environment:

| Variable | Meaning |
|---|---|
| `SATCHEL_CHAT_PROVIDER` | `anthropic` or `openai_compat` (Ollama, vLLM, LiteLLM, OpenAI). Unset = no model. |
| `SATCHEL_CHAT_MODEL` | Model name. Default for `anthropic`: `claude-sonnet-5-5`. Required for `openai_compat`. |
| `SATCHEL_CHAT_BASE_URL` | Defaults to the provider's public API; set it for local servers, e.g. `http://localhost:11434/v1`. |
| `SATCHEL_CHAT_API_KEY` | Environment only. Falls back to `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. |
| `SATCHEL_CURATE_INTERVAL_MIN` | Minutes between `satchel worker` passes. |

Evidence sent to the model is redacted like a curator's view (SQL literals masked, text truncated, users anonymised, sensitive sessions withheld). To see the loop on the Jaffle Shop demo, generate gap activity with `SATCHEL_TOKENS=tokA,tokB,tokC uv run --project server python scripts/demo_curation_activity.py`, then run curation; `scripts/demo_curation_fake.py WORKSPACE` does the drafting step offline with a scripted model. Admins can also press **Run curation now** in the Inbox.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
cd server
uv sync                                                    # Python 3.12 env
docker compose -f ../deploy/compose/docker-compose.yml up -d   # Postgres 17 + pgvector on :5433
# Runtime connects as satchel_app (non-superuser, so RLS applies); migrations run as the owner `satchel`.
export SATCHEL_DATABASE_URL=postgresql+psycopg://satchel_app:satchel_app@localhost:5433/satchel
export SATCHEL_MIGRATION_DATABASE_URL=postgresql+psycopg://satchel:satchel@localhost:5433/satchel
uv run satchel db upgrade                                     # run migrations (as the owner)
uv run ruff check . && uv run mypy satchel && uv run pytest -q   # tests use testcontainers
```

Load the example context repo, then search and evaluate retrieval:

```bash
# embeddings: fastembed (local ONNX, default; first run downloads ~640 MB),
# hash (offline, BM25-level), or openai_compat (SATCHEL_EMBEDDING_BASE_URL / _MODEL / _API_KEY)
export SATCHEL_EMBEDDING_PROVIDER=fastembed
uv run satchel sync import ../examples/jaffle-shop/context --workspace jaffle   # idempotent
uv run satchel search "revenue last month" --workspace jaffle
uv run satchel eval retrieval ../examples/jaffle-shop/retrieval_eval.yaml --workspace jaffle
# eval suites (imported from context/evals/*.yaml by `sync import`); the retrieval suite needs no LLM
uv run satchel eval run retrieval --workspace jaffle
export ANTHROPIC_API_KEY=...                     # the reference agent + judges (any ChatProvider)
uv run satchel eval run core --workspace jaffle --model claude-haiku-4-5
uv run satchel eval compare RUN_A RUN_B --workspace jaffle
uv run satchel eval gate PREVIEW_ID --workspace jaffle   # run publish.require_evals on a preview; exit 1 unless it passes
uv run satchel sync reembed --workspace jaffle      # after changing the embedding provider
export SATCHEL_DEV_AUTH=1                           # dev-only header auth; otherwise use a token or OIDC login
uv run satchel serve api --port 8080                # REST: /healthz, /v1/items, /v1/search, /v1/context-pack
curl -s localhost:8080/v1/search -H 'X-Satchel-Workspace: jaffle' \
  -H 'content-type: application/json' -d '{"query": "revenue last month", "k": 5}'
```

The REST API accepts a PAT / OAuth access token (`Authorization: Bearer`) or the session cookie from
OIDC login (`/auth/login`). The unverified `X-Satchel-Workspace` / `X-Satchel-User` headers only work
with `SATCHEL_DEV_AUTH=1` (off by default). OAuth 2.1 and OIDC setup: see
[`docs/integrations/claude.md`](docs/integrations/claude.md).

MCP server (context + signal tools; connect Claude Desktop, Claude Code or Cursor, see
[`docs/integrations/claude.md`](docs/integrations/claude.md)):

```bash
uv run satchel token create --workspace jaffle --user you@example.com --name laptop   # prints token once
uv run satchel serve api --port 8080          # REST + MCP Streamable HTTP at http://localhost:8080/mcp
uv run satchel serve mcp --port 8081          # MCP only
SATCHEL_TOKEN=satchel_pat_... uv run satchel mcp stdio   # stdio (Claude Desktop config)
uv run satchel token revoke --workspace jaffle --name laptop
```

Row-level security: workspace tables use `FORCE ROW LEVEL SECURITY`, keyed on the
`satchel.workspace_id` / `satchel.user_id` settings (see `satchel/store/db.py`). Connect the app as a
non-superuser role (`satchel_app`); superusers bypass RLS. Server-internal jobs use
`admin_session()`, which connects as the `satchel_worker` role (`SATCHEL_ADMIN_DATABASE_URL`) that
every table's RLS policy admits by role. See [docs/deploy/database.md](docs/deploy/database.md).

## Licence

Apache-2.0
