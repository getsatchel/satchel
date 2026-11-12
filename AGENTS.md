# Satchel — contributor and agent guide

Satchel is a self-hosted context server for data agents. Read these first:
- [`docs/concepts/how-it-works.md`](docs/concepts/how-it-works.md): context, sessions, signals, the curation loop, releases.
- [`docs/architecture/system.md`](docs/architecture/system.md): the architecture diagram (Mermaid).
- [`CONTRIBUTING.md`](CONTRIBUTING.md): setup and the checks to run before a pull request.

## Layout
- `server/`: the FastAPI server, MCP endpoint, CLI and worker (Python 3.12, uv).
- `ui/`: the curator UI (React, Vite). `ui-widgets/`: the MCP app widgets.
- `sdk/`: `satchel-adapter-sdk`, for source adapters. `templates/adapter/`: a starter adapter.
- `deploy/`: the container image, Docker Compose, the Helm chart and Terraform modules.
- `docs/`: user documentation. `examples/jaffle-shop/`: the demo project.

## Rules
1. **Keep the architecture diagram current.** Update `docs/architecture/system.md` in the same change when you add, remove or rename a component or service, a DB table, an MCP tool, an external integration or a data flow, and bump "Last updated".
2. **Keep the docs current.** When behaviour, configuration or the CLI changes, update the page under `docs/` in the same change. Keep the `<!-- site: Widget -->` markers: they place the website's interactive diagrams and explorers. Never replace one with a Mermaid diagram, and give any new ```` ```mermaid ```` block outside `docs/architecture/` a marker (see `docs/README.md`, Working on these docs).
3. **Python:** 3.12 via uv (`cd server && uv sync`). Run `uv run ruff check . && uv run mypy satchel && uv run pytest -q` before you call something done. The tests start Postgres with testcontainers, so Docker must be running.
4. **Postgres is the only required dependency.** The app connects as a non-superuser role so RLS applies. Use `workspace_session()` for user requests and `admin_session()` only for server-internal jobs.
5. **Migrations:** every migration, including the frozen `0001_initial`, is explicit `op.*` DDL and must never import `satchel.store.models` or call `create_all`. When you change a model, add a new revision. `tests/test_migrations.py` runs `alembic upgrade head` on an empty database and fails if it differs from `Base.metadata`.
6. **Nothing auto-publishes context.** Agents and the suggestion engine only propose changes.
7. **UI:** follow [`ui/PRODUCT.md`](ui/PRODUCT.md) (who it is for, what earns space on a page) and [`ui/DESIGN.md`](ui/DESIGN.md), and reuse the components in `ui/src/components/ui.tsx`. Run `npm run typecheck && npm run lint && npm run build` in `ui/`.
8. **API contract:** if you change an API route, regenerate it: `cd server && uv run satchel openapi > ../ui/openapi.json && cd ../ui && npm run gen:api`.
9. **Commits:** sign off every commit with `git commit -s` (the [DCO](https://developercertificate.org/), see `CONTRIBUTING.md`). Never add AI attribution: no `Co-Authored-By` trailers for AI tools and no "Generated with" lines in commit messages or pull request descriptions.
