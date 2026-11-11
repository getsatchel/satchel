# satchel-server

An open-source, self-hosted **context server for data agents**. Connect your agent (Claude, Claude Code, Cursor,
ChatGPT, Codex) over MCP. Satchel serves curated, versioned, permissioned context about your data, learns from
how agents use it, and turns what they struggle with into changes that analysts review.

**Status:** pre-alpha.

```bash
uv tool install satchel-server     # the command is `satchel`
satchel init --example jaffle      # Postgres (Docker), migrations, workspace, token, sample data
satchel up                         # UI + REST + MCP at http://localhost:8080
satchel connect claude-desktop
```

The PyPI package includes the built curator UI. Extras: `gcp`, `aws` (result storage and audit export),
`bigquery`, `snowflake` (warehouse connections), e.g. `uv tool install "satchel-server[bigquery]"`.

The PyPI project named `satchel` is unrelated; install `satchel-server`.

- Documentation: https://github.com/getsatchel/satchel/blob/main/docs/README.md
- Source and issues: https://github.com/getsatchel/satchel
- Licence: Apache-2.0
