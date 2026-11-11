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

Before you publish a community adapter, read the rules for outside adapters:

* [Compatibility policy](docs/adapters/compatibility.md): what counts as a breaking change, and how the IR version relates to the SDK version.
* [Maturity tiers](docs/adapters/maturity.md): experimental, beta and stable, and what promotes an adapter to stable.
* [Naming and listing](docs/adapters/naming.md): name packages `satchel-source-<tool>`, and how to get listed.
* [Wanted integrations](docs/adapters/wanted.md): tools we'd like adapters for, and the community adapters list.

## Pull requests

* One focused change; explain the why. Update the docs when behaviour changes.
* Add tests, and update `docs/architecture/system.md` for architecture changes.
* Sign off every commit (`git commit -s`). The `Signed-off-by` line certifies the
  [Developer Certificate of Origin](https://developercertificate.org/): that you wrote the change, or
  otherwise have the right to submit it under the project's licence. There is no CLA to sign.

## Code of conduct

Everyone taking part in this project follows the [Code of Conduct](CODE_OF_CONDUCT.md). Report conduct issues to security@getsatchel.dev.

## Releasing (maintainers)

A server release is a `vX.Y.Z` tag on `main`. Before tagging, in one pull request:

1. Set the version in `server/pyproject.toml` and both `version` and `appVersion` in `deploy/helm/satchel/Chart.yaml`.
2. Add the release notes as `docs/releases/X.Y.Z.md`, including the `satchel-adapter-sdk` and IR versions the release ships with ([compatibility policy](docs/adapters/compatibility.md)).
3. Update the pinned version in the install docs (`README.md`, `docs/install.md`, `docs/deploy/container.md`, `docs/deploy/helm.md`, `docs/deploy/storage-and-audit.md`, `docs/integrations/warehouses.md`) and the `image_tag` in the Terraform `terraform.tfvars.example` files.

After merging, push the tag: `git tag vX.Y.Z && git push origin vX.Y.Z`. The `image` workflow pushes `ghcr.io/getsatchel/satchel:X.Y.Z`, and the `release` workflow checks that the versions match, publishes the chart to `oci://ghcr.io/getsatchel/charts/satchel` and creates the GitHub release from the notes file. 0.x releases are marked as pre-releases. SDK releases are separate (`sdk-v*` tags, see `release-sdk.yml`).

## Reporting security issues

Do not open a public issue. See [`SECURITY.md`](SECURITY.md).

## Licence

Contributions are licensed under Apache-2.0, the licence of the project.
