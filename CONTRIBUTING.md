# Contributing to Satchel

Thank you for helping. The best places to start are **source adapters** (integrations with the tools teams already use), documentation, and bug reports with a reproduction.

## Setup

You need [uv](https://docs.astral.sh/uv/), Python 3.12, Docker (the tests start Postgres with testcontainers) and, for the UI, Node 22.

```bash
git clone https://github.com/getsatchel/satchel && cd satchel
cd server && uv sync            # server (installs the local ../sdk too)
cd ../sdk && uv sync            # the adapter SDK on its own
cd ../ui && npm ci              # curator UI
```

Read [`AGENTS.md`](AGENTS.md) first: it lists the rules (keep the architecture diagram and docs current, explicit migrations, RLS sessions, nothing auto-publishes).

## Checks to run before a pull request

```bash
cd server && uv run ruff check . && uv run mypy satchel && uv run pytest -q
cd sdk    && uv run ruff check . && uv run mypy satchel_adapter_sdk && uv run pytest -q
cd ui     && npm run typecheck && npm run lint && npm run build     # UI changes only
```

Adapter work needs no Postgres: `sdk/` tests and an adapter's own conformance tests run without a database (CI runs them as the `adapters` job).

If you change an API route, regenerate the contract: `cd server && uv run satchel openapi -o ../ui/openapi.json && cd ../ui && npm run gen:api`.

Never point tests or migrations at your own Satchel database. Tests use their own containers; `SATCHEL_CONFIG` can point at an empty file to be safe.

## Writing an adapter

See [`docs/adapters/writing-an-adapter.md`](docs/adapters/writing-an-adapter.md), copy [`templates/adapter/`](templates/adapter) and read the contract in [`sdk/README.md`](sdk/README.md). File an [integration request](.github/ISSUE_TEMPLATE/integration_request.yml) first if you want to check the shape of a mapping.

## Pull requests

* One focused change; explain the why. Update the docs when behaviour changes.
* Add tests, and update `docs/architecture/system.md` for architecture changes.
* Sign off every commit (`git commit -s`). The `Signed-off-by` line certifies the
  [Developer Certificate of Origin](https://developercertificate.org/): that you wrote the change, or
  otherwise have the right to submit it under the project's licence. There is no CLA to sign.

## Code of conduct

Everyone taking part in this project follows the [Code of Conduct](CODE_OF_CONDUCT.md). Report conduct issues to security@getsatchel.dev.

## Reporting security issues

Do not open a public issue. See [`SECURITY.md`](SECURITY.md).

## Licence

Contributions are licensed under Apache-2.0, the licence of the project.
