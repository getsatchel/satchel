# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One curator UI serves every workspace role, and people move between them:

- **Curators and analysts** (analytics engineers, senior analysts) review what agents struggled with, accept or reject suggested changes, preview and publish releases, and watch evals.
- **Admins and platform engineers** set Satchel up and keep it running: sources, warehouse connections, models, webhooks, the job queue.
- **Explorers** look around read-only.

Roles are `admin > curator > analyst > explorer`; a page shows each person what their role can act on. There is no single primary user and no fixed visit rhythm: a page has to work for someone checking in daily and for someone arriving cold after weeks away.

## Product Purpose

Satchel is a self-hosted context server for data agents. Agents (Claude, Cursor, ChatGPT, Codex) connect over MCP and are served curated, versioned, permissioned context about a team's data: guides, glossary, metrics, golden queries, table docs and endorsements. Satchel records how agents use that context, turns gaps and feedback into suggestions, and evaluates each change before it is published.

The UI is where people run that loop: see what agents need, decide what changes, publish it, and confirm nothing regressed. Success is a team that keeps its agents' context correct with little effort, and can always see why the system is in the state it is in.

## Positioning

Shared, reviewed context with a feedback loop and an eval gate, all self-hosted. Agent memory tools keep private, unreviewed notes per user; Satchel keeps one reviewed source of truth that every user's agent gets, learns where it falls short from real sessions, and refuses to publish a change that makes evals worse. Nothing auto-publishes: agents and the suggestion engine only propose.

## Operating Context

- Agents use Satchel over MCP; people use this UI, the `satchel` CLI and the REST API; CI uses the API (the GitHub Action previews context pull requests and publishes on merge).
- Context can live in a git context repo (`satchel.config.yaml`) or be edited in the UI; sources (dbt, Cube, SQLMesh) sync into it.
- Background work (syncs, crawls, curation, eval runs) runs as jobs on the web servers and `satchel worker`.
- The core loop: agent sessions → signals → suggestions in the Inbox → drafts → preview release → eval gate → publish → releases (rollback anytime).

## Capabilities and Constraints

- **Self-hosted and air-gapped.** The UI ships inside the Python wheel and must work with no internet: no CDNs, remote fonts or external calls.
- **Pre-alpha.** Pages, labels, routes and flows may change freely; there is no compatibility promise for the UI yet.
- **Postgres is the only required dependency**; everything the UI shows comes from the Satchel API (`ui/openapi.json`).
- **Terminology** (use these words, consistently): workspace, context item, draft, preview, release, publish, rollback, source, sync, overlay, connection, crawl, catalog, session, signal, suggestion, Inbox, eval suite, run, publish gate, job.
- The CLI and API can do things the UI deliberately does not (for example set a source path on the server's disk); the UI says so and gives the command rather than offering a control that fails.

## Evidence on Hand

- The Jaffle Shop example (`examples/jaffle-shop/`): a DuckDB warehouse, a context repo, and dbt, Cube and SQLMesh sources, used for demos and tests.
- Screenshots in `docs/screenshots/` (light and dark).
- No customers, testimonials, usage numbers or benchmarks exist yet. Do not invent any.

## Product Principles

1. **Summary first, depth on demand.** Every view has layers. The default layer shows state and the next action: what needs a decision, what is broken, what changed. The next layer (an expanded row, a detail panel) shows setup and recent history. Granular diagnostics (logs, raw reports, fingerprints, payloads, IDs) sit one deliberate step further: always reachable, never in the default view. Before adding something to a page, decide which layer it belongs in; most new detail belongs a layer down.
2. **One home per fact.** A fact is shown in one place and linked from the others. If two sections say the same thing, merge them.
3. **Show the exception, not the default.** Flags, badges and colour appear only when something differs from normal or needs a person. A value that is the same on every row belongs in the detail, not the row.
4. **Every state explains itself.** Failed, waiting, retrying and empty states say why and what to do next, including the CLI command when the fix is outside the UI.
5. **People decide; the system proposes.** Nothing publishes on its own. The UI makes review fast and the consequences of publishing clear.
