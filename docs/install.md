# Install and get started

This is the canonical getting-started guide. You need [uv](https://docs.astral.sh/uv/) and Docker (Docker is only used to run Postgres; bring your own Postgres with `--database-url` instead if you prefer).

## 1. Install Satchel as a tool

```bash
uv tool install satchel-server
satchel version
```

The PyPI package is `satchel-server` and includes the built curator UI. The command it installs is `satchel`. The PyPI project named `satchel` is unrelated, so don't install that one. To pin a release, use `uv tool install "satchel-server==0.2.0"` ([release notes](https://github.com/getsatchel/satchel/releases)). To run the latest commit on `main`, install from git: `uv tool install "satchel-server @ git+https://github.com/getsatchel/satchel#subdirectory=server"`. A git install has no built UI. This installs a stable copy at `~/.local/bin/satchel`, independent of any checkout. Do not point Claude at a development checkout (`uv run --project`): uncommitted code and an un-migrated database break every call. `satchel connect` refuses to do that unless you pass `--dev`.

Hacking on Satchel itself? Use `cd server && uv sync` and `uv run satchel ...`, and see the README.

## 2. Initialise

```bash
satchel init
```

It asks a few questions (all have defaults; add `--yes` to accept them) and then:

1. checks Docker and starts Postgres 17 + pgvector from a compose file shipped inside the package (project `satchel`, port 5433, data in the `satchel_satchel-pgdata` volume). If a healthy Satchel Postgres is already listening on that port, for example one you started from `deploy/compose`, it is reused and left alone. Change the port with `--pg-port`;
2. runs migrations as the owner role (`satchel`). The local Postgres has two non-superuser roles, so row-level security applies: `satchel_app`, which request traffic uses, and `satchel_worker`, which server-internal jobs use and which every table's RLS policy admits by role. Both have dev passwords equal to their names. See [deploy/database.md](deploy/database.md);
3. creates your workspace and an admin user (the email defaults to `git config user.email`);
4. mints a personal access token for the CLI;
5. picks the embedding provider: `fastembed` (default, a local model, ~640 MB on first use) or `hash` (offline, instant, keyword-level quality);
6. optionally loads content: `--example jaffle` (the bundled Jaffle Shop warehouse and context) or `--context PATH` (your own context repo folder);
7. writes `~/.config/satchel/config.toml` (mode 0600).

It is idempotent: run it again and it reuses what exists. Useful flags: `--workspace/-w`, `--email`, `--embedding hash`, `--database-url` / `--migration-database-url` / `--admin-database-url` (your own Postgres, no Docker; run `satchel db bootstrap` there first, see [deploy/database.md](deploy/database.md)), `--chat-key-env ANTHROPIC_API_KEY` (see below).

### The config file

`~/.config/satchel/config.toml` (override with `SATCHEL_CONFIG`, or `XDG_CONFIG_HOME`) holds profiles. The default profile is `local`; pick another with `satchel --profile NAME ...` or `SATCHEL_PROFILE`.

```toml
[profile.local]
database_url = "postgresql+psycopg://satchel_app:satchel_app@localhost:5433/satchel"
admin_database_url = "postgresql+psycopg://satchel_worker:satchel_worker@localhost:5433/satchel"
migration_database_url = "postgresql+psycopg://satchel:satchel@localhost:5433/satchel"
workspace = "main"
token = "satchel_pat_..."          # the CLI's own token; the file is 0600
embedding_provider = "fastembed"
public_url = "http://localhost:8080"
result_dir = "~/.cache/satchel/results"

[profile.local.chat]
api_key_env = "ANTHROPIC_API_KEY"   # the NAME of the variable that holds your LLM key
```

Precedence is **environment variable > config file profile > built-in default**, so every `SATCHEL_*` variable still works and wins. LLM API keys are never written to the file, only the name of the environment variable that holds them. Any setting from the README's tables can appear in a profile (for example `chat_provider = "anthropic"`).

## 3. Run it

```bash
satchel up              # starts Postgres if it is down, checks migrations, serves UI + REST + MCP on :8080
satchel up --detach     # same in the background (pidfile and log under ~/.local/state/satchel/)
satchel status          # version, config, database, schema vs head, server, token, workspace, connections
satchel down            # stop the background server (Postgres keeps running)
satchel down --all      # also stop satchel's Postgres (data is kept)
satchel down --purge    # ... and delete its data volume, after asking
```

`satchel up` offers to upgrade an out-of-date database (`--yes` to skip the question). The UI is served when a built `ui/dist` exists (a repo checkout; an installed tool serves REST and MCP only).

## 4. Connect a client

```bash
satchel connect claude-desktop      # also: claude-code, cursor
```

This resolves the absolute path to the installed `satchel`, mints a fresh named token, backs up the client's config (`...json.bak-<timestamp>`), merges a `satchel` entry without touching your other servers, and prints what changed. The database URL and embedding settings come from `config.toml`, so the entry only carries the token. Restart Claude Desktop afterwards.

- `satchel connect NAME --print` shows the config without writing anything or minting a token.
- `--http URL` writes the remote variant instead (Claude Code `claude mcp add --transport http`, `mcp-remote` for Desktop, a `url` entry for Cursor).
- `--token T` reuses an existing token. `--dev` allows a `.venv`/dev-checkout path.
- `satchel disconnect NAME` removes the entry and revokes its token.

Config locations: Claude Desktop `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), `~/.config/Claude/` (Linux), `%APPDATA%\Claude\` (Windows); Claude Code via `claude mcp add` (or `~/.claude.json`); Cursor `~/.cursor/mcp.json`.

## 5. Check it

```bash
satchel doctor
```

Prints a check mark or a cross for each of: Docker, database connection, app role is not a superuser (RLS), worker role (`satchel_worker`) can log in, schema at head, token valid, embedder loads and matches the stored chunks, result directory writable, data connections reachable, and every Claude Desktop/Code/Cursor entry that mentions Satchel (command exists, not a dev checkout, environment resolves, token valid). Every failure comes with the exact fix command, and the exit code is non-zero if anything failed.

## 6. Upgrade

```bash
satchel upgrade                                   # uv tool upgrade satchel-server, then migrate the database
satchel upgrade --from /path/to/satchel           # or install a local checkout (or a git URL) instead
```

A version pinned with `==` stays pinned. To move to a specific release, run `uv tool install --force "satchel-server==X.Y.Z"`, then `satchel db upgrade`. Releases are listed on the [GitHub releases page](https://github.com/getsatchel/satchel/releases).

**Installed 0.1.0 from git?** That install is named `satchel`, and its `satchel upgrade` stays on 0.1.0. Switch to the PyPI package once with `uv tool uninstall satchel && uv tool install satchel-server`, then run `satchel db upgrade`. Your config (`~/.config/satchel/config.toml`) and your client entries are kept, because the command is still `satchel` at the same path.

`mcp stdio`, `serve api` and `serve mcp` check the database revision when they start. If the database is behind the installed code they say so (`database is at 0005 but this satchel needs 0006 — run: satchel upgrade`); over stdio every tool call returns that message so your agent can tell you.

**Upgrading past migration 0010 (the `satchel_worker` role).** Server-internal jobs used to rely on a `satchel.bypass_rls` session setting. They now connect as the `satchel_worker` role. A local database created earlier has no such login. `satchel upgrade` (and `satchel db upgrade`, `satchel up`, `satchel init`) creates it with `LOGIN` and the dev password `satchel_worker` when the migration URL is the local Docker superuser (`satchel`, the default). Then run `satchel init --yes` once to record `admin_database_url` in your config. Until then Satchel derives that URL and logs a warning. If `satchel doctor` still reports **Worker role**, run the fix it prints.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Claude says Satchel is not ready | `satchel upgrade` (or `satchel doctor` to see what is wrong) |
| `docker is installed but the daemon is not running` | Start Docker Desktop, then `satchel up` |
| `localhost:5433 is in use by something that is not a satchel Postgres` | `satchel init --pg-port 5434` |
| Token invalid or revoked | `satchel init --yes` (mints a new CLI token); `satchel connect <client>` for a client |
| Search quality dropped after changing embeddings | `satchel sync reembed -w WORKSPACE` |
| `password authentication failed for user "satchel_worker"` / doctor: Worker role | `satchel db upgrade`; otherwise, as a superuser: `ALTER ROLE satchel_worker LOGIN PASSWORD 'satchel_worker';` |
