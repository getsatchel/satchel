# Security

This page lists the controls Satchel applies itself, whatever it runs on. The infrastructure
side (network, identity, encryption at rest, edge) is covered per platform: see the
[EKS security posture](eks.md#8-security-posture) and the GKE and Cloud Run pages.

| Area | Control | More |
|---|---|---|
| Workspaces | Every table has Postgres row-level security, forced, and Satchel connects as roles without `BYPASSRLS`. A test fails if a table lacks it. | [Database](database.md) |
| Sign-in | OIDC through your identity provider. Agents use OAuth 2.1 with PKCE. Tokens are stored as SHA-256 hashes only. | [Connecting Claude](../integrations/claude.md) |
| Warehouse SQL | `run_sql` takes one read-only `SELECT`/`WITH`, parsed with sqlglot, row-capped and timed out, in a read-only session. Also connect with a role that can only read: the setup SQL in **Add connection** creates one, and **Test** warns when the role can write. | [MCP tools](../reference/mcp-tools.md) |
| Secrets | Encrypted with `SATCHEL_ENCRYPTION_KEY`, or kept in a secret manager and read by reference. `SATCHEL_SECRETS=references` makes Satchel store none. | [Secrets](secrets.md) |
| Audit | Admin and curation actions are recorded, and can be exported to object storage. | [Storage and audit](storage-and-audit.md) |
| Supply chain | Published images are signed with cosign and carry an SBOM and SLSA provenance. Locked dependencies are audited for known vulnerabilities, and Dependabot proposes updates weekly. | [Verifying the image](container.md#verifying-the-image) |
| HTTP | Security headers on every response, and a strict Content-Security-Policy on the UI (below). | |
| Rate limits | Sign-in and OAuth: 30 requests a minute per client IP. `/mcp`: 600 a minute per token. Past the limit the answer is `429`. | [Rate limits](configuration.md#rate-limits) |

## HTTP headers

**Every response** carries:

| Header | Value | Why |
|---|---|---|
| `X-Content-Type-Options` | `nosniff` | Browsers use the declared type and never guess one. |
| `X-Frame-Options` | `DENY` | Nothing Satchel serves can be framed by another site. The MCP result card reaches Claude as an MCP resource, not as a URL, so it is unaffected. |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Links out of Satchel don't send its paths. |
| `Strict-Transport-Security` | `max-age=31536000` | Only when `SATCHEL_PUBLIC_URL` is `https://`. It tells browsers to use HTTPS for a year. |

**The curator UI page** also carries a Content-Security-Policy:
- scripts only from Satchel itself, plus the page's one inline script (the theme switch), allowed
  by its SHA-256 hash rather than by `unsafe-inline`;
- no `eval`, no plugins (`object-src 'none'`), and no framing (`frame-ancestors 'none'`);
- styles, fonts, images and the editor's workers from Satchel only;
- `connect-src 'self'`, so the page can call only its own API.

The policy is worked out when the server starts, from the built `index.html`. A rebuilt UI gets
its own hash without a configuration change.

The sign-in, consent and other server-rendered pages keep their own, stricter policy:
`default-src 'none'`. The API docs at `/docs` load Swagger UI from a CDN, so they get the
headers above but not the UI's policy.

A reverse proxy in front of Satchel may add headers. It should not remove these.
