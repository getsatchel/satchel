# Satchel documentation

Satchel is a self-hosted context server for data agents. These docs are organised the way you'd approach it:
try it, understand it, run it in production, then integrate and extend it.

> **Status:** pre-alpha. Pages marked *(planned)* are stubs for work we intend to write.

## 1. Get started

| Page | What you'll do |
|---|---|
| [Install and quickstart](install.md) | Run Satchel locally with `satchel init`, connect Claude, ask a question. |
| [How Satchel works](concepts/how-it-works.md) | Context, sessions, signals, the curation loop, releases. |
| [Evals](concepts/evals.md) | Measure answers with eval suites, gate publishing, run suites on a schedule. |

## 2. Self-hosting

Start with the [self-hosting overview](self-hosting/index.md) (architecture, prerequisites, production checklist), then pick a deployment:

| Target | Page |
|---|---|
| Any container runtime | [Container image](deploy/container.md) |
| Kubernetes (any cluster) | [Helm chart](deploy/helm.md) |
| GKE (Terraform + Helm) | [GKE](deploy/gke.md) |
| Amazon EKS (Terraform + Helm) | [EKS](deploy/eks.md) |
| Google Cloud Run (Terraform) | [Cloud Run](deploy/cloud-run.md) |
| Rehearse AWS locally | [floci](deploy/floci.md) |

Configure and operate it:

- [Configuration: environment vs Settings](deploy/configuration.md), including the encryption key.
- [Database](deploy/database.md): roles, RLS, `satchel db bootstrap`.
- [Storage and audit](deploy/storage-and-audit.md): result cache and audit export.
- [Upgrading, backups and troubleshooting](self-hosting/operations.md)

## 3. Integrate

- Clients: [Claude](integrations/claude.md) (Desktop, Code), Cursor and others via `satchel connect`.
- CI: the [GitHub Action](integrations/github-action.md) previews context repo pull requests, runs the eval gate and publishes on merge.
- Sources: [dbt](integrations/dbt.md), [Cube](integrations/cube.md), [SQLMesh](integrations/sqlmesh.md).
- [Writing a source adapter](adapters/writing-an-adapter.md) with `satchel-adapter-sdk`. Rules for community adapters: [compatibility](adapters/compatibility.md), [maturity tiers](adapters/maturity.md), [naming and listing](adapters/naming.md), and [wanted integrations](adapters/wanted.md).

## 4. Reference

- [CLI](reference/cli.md)
- [MCP tools](reference/mcp-tools.md)
- [Environment variables](reference/environment-variables.md)
- REST API: `satchel openapi` prints the OpenAPI document (`ui/openapi.json` is the committed copy).

## 5. Design and contributing

- [Master architecture diagram](architecture/system.md)
- [Release notes](releases/0.2.0.md) (all releases: [GitHub releases](https://github.com/getsatchel/satchel/releases))
- [Contributing](https://github.com/getsatchel/satchel/blob/main/CONTRIBUTING.md)

## Working on these docs

The pages are plain markdown. Edit them here, in the same pull request as the change they describe; the website renders them from this repository. A push to `main` that changes `docs/` rebuilds https://getsatchel.dev (the `docs-site` workflow). New pages need an entry in the website's navigation, so mention them in the pull request.
