# Connecting Claude (and other MCP clients) to Satchel

Satchel speaks MCP over **Streamable HTTP** (`/mcp`) and **stdio**. Remote HTTP supports **OAuth 2.1** (browser login through your IdP: Claude Desktop / claude.ai custom connectors, Claude Code, Cursor) as well as personal access tokens (PATs). stdio uses a PAT.

## 0. The easy way: `satchel connect`

After [installing](../install.md) Satchel as a tool and running `satchel init`:

```bash
satchel connect claude-desktop     # or claude-code, cursor
satchel doctor                     # verifies the entry (command path, token, environment)
satchel disconnect claude-desktop  # removes the entry and revokes its token
```

`connect` finds the absolute path of the installed `satchel`, mints a named token, backs up the client config, merges a `satchel` entry (other servers are untouched) and prints the change. It refuses a `uv run --project` / `.venv` dev checkout unless you pass `--dev`: a dev checkout runs uncommitted code and a database that may not be migrated. `--print` shows the config without writing; `--http URL` writes the remote variant. The sections below are the manual equivalents.

## 1. Create a token (PAT, for stdio and headless use)

```bash
satchel token create --workspace jaffle --user you@example.com --name "claude-desktop"
# prints the token once (satchel_pat_...). The user is created (role analyst) if missing.
satchel token revoke --workspace jaffle --name "claude-desktop"   # later
```

The database URL comes from `~/.config/satchel/config.toml` (written by `satchel init`), or `SATCHEL_DATABASE_URL`. Always connect as the non-superuser `satchel_app` role (created by the bundled compose file); a superuser bypasses row-level security. Only `satchel db upgrade` needs the owner (`migration_database_url`).

## 2. Claude Desktop

### Local (stdio), by hand

Edit `claude_desktop_config.json` (Settings > Developer > Edit Config). Use the installed tool's absolute path (`which satchel`), not `uv run --project`:

```json
{
  "mcpServers": {
    "satchel": {
      "command": "/Users/you/.local/bin/satchel",
      "args": ["mcp", "stdio"],
      "env": { "SATCHEL_TOKEN": "satchel_pat_..." }
    }
  }
}
```

The database URL and `embedding_provider` come from `config.toml`; to configure the entry without a config file, add `SATCHEL_DATABASE_URL` and `SATCHEL_EMBEDDING_PROVIDER` (it must match the provider the workspace was imported with) to `env`. If the database is behind the installed Satchel, every tool call returns `database is at … but this satchel needs … — run: satchel upgrade`.
Proxying stdio to a remote Satchel (`SATCHEL_URL`) is not implemented; use the bridge below.

### Remote (Streamable HTTP) with OAuth: custom connector

Claude Desktop and claude.ai connect to a **public HTTPS URL** (Anthropic's side opens it), so Satchel needs a real hostname, for example a Cloud Run service or, for testing, a tunnel such as `cloudflared tunnel --url http://localhost:8080` or `ngrok http 8080`. Set `SATCHEL_PUBLIC_URL` to that exact external URL (it becomes the OAuth issuer and the token audience), and register `<SATCHEL_PUBLIC_URL>/auth/callback` as a redirect URI at your IdP (section 5).

1. Run Satchel with the OIDC settings: `SATCHEL_PUBLIC_URL=https://satchel.example.com SATCHEL_OIDC_ISSUER=... SATCHEL_OIDC_CLIENT_ID=... SATCHEL_OIDC_CLIENT_SECRET=... uv run satchel serve api --host 0.0.0.0 --port 8080`.
2. Decide who may sign in. Existing `app_user` rows match by IdP subject, then by verified email. For everyone else: `uv run satchel workspace auth -w jaffle --provision explorer --allow-domain example.com` (creates explorers on first login) or leave the default `reject` and pre-create users (`satchel token create ... --user alice@example.com` creates one).
3. In Claude: **Settings > Connectors > Add custom connector**, name it, and enter `https://satchel.example.com/mcp`. Leave the OAuth client ID/secret fields empty: Claude registers itself dynamically (`/oauth/register`).
4. Click **Connect**. A browser tab opens: sign in at your IdP, pick a workspace if you belong to several (or pass nothing and be auto-selected), then **Approve** the Satchel consent page. Claude receives a 1 hour access token and a rotating refresh token.
5. To revoke: disconnect the connector (Claude calls `/oauth/revoke`), or revoke server-side with `update api_token set revoked_at = now() where client_id = ...` (a CLI for this is future work).

Troubleshooting: `curl -i -X POST $URL/mcp` must return `401` with `WWW-Authenticate: Bearer ... resource_metadata="$URL/.well-known/oauth-protected-resource/mcp"`, and that URL plus `$URL/.well-known/oauth-authorization-server` must return JSON naming `$URL` as the issuer. A `SATCHEL_PUBLIC_URL` that differs from the URL Claude uses (http vs https, port, trailing path) fails the audience check.

### Remote with a PAT (no OAuth)

- **Bridge** with `mcp-remote`:

```json
{
  "mcpServers": {
    "satchel": {
      "command": "npx",
      "args": ["mcp-remote", "http://localhost:8080/mcp",
               "--header", "Authorization: Bearer ${SATCHEL_TOKEN}"],
      "env": { "SATCHEL_TOKEN": "satchel_pat_..." }
    }
  }
}
```

## 3. Claude Code

OAuth (browser login, tokens refreshed automatically):

```bash
claude mcp add --transport http satchel http://localhost:8080/mcp
claude mcp list            # satchel: "Needs authentication" until you sign in
# inside Claude Code run /mcp, pick satchel, choose Authenticate; a browser opens for the IdP
# login and the satchel consent page, then returns to the local callback.
```

`claude mcp list` / `claude mcp get satchel` show the connection state afterwards. Discovery (401 -> protected-resource metadata -> authorization-server metadata) is verified against Satchel by `claude mcp list`; the browser step needs an interactive session.

Personal access token instead:

```bash
claude mcp add --transport http satchel http://localhost:8080/mcp \
  --header "Authorization: Bearer satchel_pat_..."
# or stdio:
claude mcp add satchel -e SATCHEL_TOKEN=satchel_pat_... -- "$(which satchel)" mcp stdio
# (or simply: satchel connect claude-code)
```

Headless: `claude -p "<question>" --mcp-config satchel.json --strict-mcp-config --allowedTools "mcp__satchel__*"`.

## 4. Cursor

Cursor supports OAuth for remote servers (it follows the same discovery), so `{ "mcpServers": { "satchel": { "url": "https://satchel.example.com/mcp" } } }` is enough once Satchel is publicly reachable (note Cursor's redirect is a custom URL scheme, which Satchel does not accept yet: use a PAT for Cursor). With a PAT, `.cursor/mcp.json`:

```json
{ "mcpServers": { "satchel": {
    "url": "http://localhost:8080/mcp",
    "headers": { "Authorization": "Bearer satchel_pat_..." } } } }
```

## 5. Identity provider setup (OIDC)

Satchel is an OIDC relying party (authorization code + PKCE, confidential client). In every case the redirect/callback URI is `<SATCHEL_PUBLIC_URL>/auth/callback`, and Satchel needs the `openid email profile` scopes with `email_verified` available.

| IdP | Steps | `SATCHEL_OIDC_ISSUER` |
|---|---|---|
| **Google** | Cloud Console > APIs & Services > Credentials > Create OAuth client ID > Web application; authorised redirect URI as above; configure the consent screen (Internal for a Workspace org). | `https://accounts.google.com` |
| **Okta** | Applications > Create App Integration > OIDC > Web Application; sign-in redirect URI as above; assign users or groups. | `https://<org>.okta.com` (or the custom authorization server, `https://<org>.okta.com/oauth2/default`) |
| **Microsoft Entra ID** | App registrations > New registration > Web, redirect URI as above; Certificates & secrets > new client secret; Token configuration > add optional claim `email` to the ID token. Entra does not send `email_verified`, so also set `SATCHEL_OIDC_REQUIRE_VERIFIED_EMAIL=false` (the tenant controls the addresses; do not do this for a multi-tenant app). | `https://login.microsoftonline.com/<tenant-id>/v2.0` |
| **Dex** (local) | `docker compose -f deploy/compose/docker-compose.yml --profile auth up -d`. Static user `analyst@satchel.test` / `password`, client `satchel` / `satchel-secret`, callback `http://localhost:8080/auth/callback` (see `deploy/compose/dex/config.yaml`). | `http://localhost:5556/dex` |

Set `SATCHEL_OIDC_CLIENT_ID` and `SATCHEL_OIDC_CLIENT_SECRET` from the environment or your secret manager; Satchel never stores them in the database.

### Local end-to-end with Dex

```bash
cd deploy/compose && docker compose --profile auth up -d && cd ../../server
export SATCHEL_MIGRATION_DATABASE_URL=postgresql+psycopg://satchel:satchel@localhost:5433/satchel
export SATCHEL_EMBEDDING_PROVIDER=hash
uv run satchel db upgrade
uv run satchel sync import ../examples/jaffle-shop/context -w jaffle
uv run satchel token create -w jaffle --user analyst@satchel.test --name seed   # creates the user the IdP login maps to
export SATCHEL_PUBLIC_URL=http://localhost:8080 SATCHEL_OIDC_ISSUER=http://localhost:5556/dex \
       SATCHEL_OIDC_CLIENT_ID=satchel SATCHEL_OIDC_CLIENT_SECRET=satchel-secret
uv run satchel serve api --port 8080 &
.venv/bin/python ../scripts/oauth_smoke.py     # discovery -> DCR -> Dex login -> token -> /mcp -> refresh -> revoke -> web login
```

### Human login for the UI

`GET /auth/login?workspace=<slug>&next=/path` signs the user in through the IdP and sets the `satchel_session` cookie (`next` must be a relative path). `GET /auth/me` returns the user and a `csrf_token`; send it as `X-CSRF-Token` on `POST` requests; `POST /auth/logout` ends the session. The REST API (`/v1/*`) accepts that cookie or `Authorization: Bearer <PAT or OAuth access token>`. The unverified `X-Satchel-Workspace` header only works with `SATCHEL_DEV_AUTH=1`.

## 6. Result widgets (MCP Apps)

When Claude runs `run_sql`, `query_metrics` or pages a result, supporting clients render an interactive widget in the conversation instead of (only) a text table:

- a single-value result (one row, 1 to 3 numbers) as a big **number tile** with a "Show as table" toggle; anything else is a sortable table (first 50 rows, "N more", **Load more rows**);
- the filters and time range of a metric query as chips under the title (for example `order_date: 2025-01-01 → 2025-12-31`, `status ≠ returned`), so a person can judge "was this right?";
- an automatic chart (a time column gives a line, one category plus a measure gives a bar);
- numbers formatted with the unit the context declares (an MDL metric's `currency` / `unit` / `format` / `decimals` properties, or the `unit` of an asset doc column for `run_sql`); with no unit metadata the number is shown plain, never with an invented currency;
- a collapsed "How this was calculated" block: a one-line summary ("revenue · by month · order_date in 2025"), the pretty-printed SQL (formatted server-side in the connection's dialect) and the raw metric query behind it;
- a collapsed "Context: N items" list of chips (readable title and type: table doc, glossary, guide, golden query, rule). Each chip links to the item page in the curator UI, built from `SATCHEL_PUBLIC_URL`; the model still gets the full `[ctx:key@vN]` tags;
- a feedback bar, "Was this number right? Your data team sees this." with **👍 / 👎** (Claude Desktop shows its own thumbs under the reply; these go to your data team, not to Anthropic). Both record immediately and say "Thanks — sent to your data team." A 👎 then opens "What was off? (optional)" with chips (Wrong number, Wrong definition, Missing a filter, Not what I asked) and a comment box; Send updates the same rating with the reasons, which the session inspector shows as chips next to the comment.

The buttons record feedback in Satchel, tagged as coming from a person ("human via widget" in the curator's session inspector, and a human/agent split in Observability). A 👎 also becomes one `user_doubt` signal (carrying the reasons: "correction" when the number or definition was wrong, else "negative"; adding reasons later updates it instead of adding another) that counts double in gap detection; a 👍 on a high-confidence answer feeds golden-query promotion; both weigh on how context is ranked. Nothing is published automatically.

Support: the widget needs a client that implements the MCP Apps extension (`io.modelcontextprotocol/ui`): Claude (Desktop and claude.ai), ChatGPT, VS Code and Goose at the time of writing. Other clients, including Claude Code in the terminal, get the same complete text answer and can still use `record_user_feedback`. The widget is one self-contained file and makes no network requests.

## 7. Make agents follow the workflow

The server sends an `instructions` block, an MCP prompt `satchel_workflow`, and a resource `satchel://workspace-rules`. For clients that ignore `instructions`:

- Install the Claude Skill at [`skills/satchel/SKILL.md`](../../skills/satchel/SKILL.md) (copy to `~/.claude/skills/satchel/`).
- Or add this to your project `AGENTS.md` / `CLAUDE.md`:

```markdown
## Data questions
This project has a `satchel` MCP server. For every data question: call `start_analysis(question)`,
then `get_context_pack(session_id, question)` before writing SQL, use `list_tables` /
`describe_table`, call `flag_context_gap` if a term has no definition, call
`record_user_feedback` if the user doubts or corrects an answer, and always finish with
`report_answer`. Pass `session_id` and the user's original question on every satchel call.
```

## Tools

`start_analysis`, `search_context`, `get_context_pack`, `get_item`, `list_tables`, `describe_table`, `report_answer`, `flag_context_gap`, `record_user_feedback`, `propose_context_change` (analyst role; saved as a `proposed` item, never published). When the workspace has a connection, `run_sql`, `query_metrics`, `get_result`, `list_metrics` and `describe_metric` are also available, plus `rate_result`, which only the result widget calls (hidden from the model).
