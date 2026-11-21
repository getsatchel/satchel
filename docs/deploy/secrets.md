# Secrets: references instead of stored values

Satchel needs a few credentials to do its work:
- a warehouse password or key, for each connection;
- a model API key, for curation and evals;
- a git or Cube token, for each source that reads from one.

By default, a secret typed into the UI is stored in Postgres, encrypted with
[`SATCHEL_ENCRYPTION_KEY`](configuration.md#satchel_encryption_key).

A **secret reference** keeps the secret in your cloud's secret manager instead. Satchel stores
only the reference, and reads the secret with the server's own cloud identity when it needs it.
Rotating the secret needs no change in Satchel, and revoking the server's access cuts it off.

With `SATCHEL_SECRETS=references`, the server refuses to store any secret value. Satchel then
holds no credentials at all.

## References

| Reference | Where the secret lives | How the server reads it |
|---|---|---|
| `aws-sm:<name or ARN>` | AWS Secrets Manager | Its AWS identity: EKS Pod Identity, IRSA or the instance role. A full ARN picks the region; a bare name uses the default region. |
| `gcp-sm:projects/<p>/secrets/<s>[/versions/<v>]` | GCP Secret Manager (version defaults to `latest`) | Its Google identity: Workload Identity, or the VM's service account. |

Add `#field` to read one field of a secret stored as a JSON object. For example,
`aws-sm:prod/satchel/warehouse#password` reads `password` from `{"user": "...", "password": "..."}`.

Use a reference anywhere a secret goes:
- **Connections:** in the credential step, choose **From a secret manager** and paste the
  reference.
  - Store a Snowflake private key unencrypted in the secret: the secret manager encrypts it, and
    a reference takes no passphrase.
  - Store a BigQuery service account key as its whole JSON file.
- **Settings > Models:** under the API key, choose **From a secret manager**.
- **Sources:** type the reference into the token field (git access token, Cube API token).
- **The API:** send the reference in the field that takes the secret: `secret` on
  `/v1/connections`, `api_key` on `/v1/llm-connections`, or the token key in a source's
  `config`.
- **The deployment's model key:** `SATCHEL_CHAT_API_KEY` may itself be a reference.
- **The CLI:** `satchel connection add --secret-ref` takes `aws-sm:`, `gcp-sm:` or `env:NAME`.

The UI shows the reference itself: it names the secret but is not the secret. Admins can see it;
curators can't.

## Turning it on

1. **Pick a path for Satchel's secrets** and grant the server read access to that path only:
   - **AWS:** `secretsmanager:GetSecretValue` on
     `arn:aws:secretsmanager:<region>:<account>:secret:satchel/*`. If the secrets use a customer
     managed KMS key, also `kms:Decrypt` on that key.
   - **GCP:** `roles/secretmanager.secretAccessor` on each secret, or on the project with an IAM
     condition on the name prefix.
2. **Set the allow list.** `SATCHEL_SECRET_REF_ALLOW` is a comma-separated list of patterns, and
   `*` matches anything:

   ```
   SATCHEL_SECRET_REF_ALLOW=aws-sm:arn:aws:secretsmanager:eu-west-1:123456789012:secret:satchel/*
   ```

   The UI and API accept only references inside this list. Without it, an admin of the UI could
   point a connection at any secret the server's role can read, and read it back through a test
   query.

   The setting is unset by default, so references are off.
3. **Optionally, store nothing:** `SATCHEL_SECRETS=references`. Secret values are then refused
   everywhere: in the UI, the API and the forms. The forms offer only **From a secret manager**,
   and the generated-password and generated-key options are hidden.

   Secrets stored before the switch keep working. `satchel doctor` lists them so you can replace
   each one with a reference.
4. **Set these the way your deployment sets environment variables:**
   - **Helm:** `extraEnv`.
   - **Terraform:** `extra_values` / `extra_env`.
   - **One VM:** `.env`.

   Restart to apply.

## Caching and rotation

A secret read from a secret manager is kept in memory for `SATCHEL_SECRET_CACHE_S` seconds (300 by
default). A rotated secret is picked up within that time, without a restart.

**Test**, on a connection, model or git source, always reads the secret again, so it shows the
current value straight away. Values are never written to disk or the database, and never
appear in logs, errors or the audit log.

## What still needs no secret at all

Prefer a connection that needs no credential where the platform supports it:

- **BigQuery:** leave the key empty, and the server's own Google identity runs the queries.
  Grant that identity `roles/bigquery.jobUser` and `roles/bigquery.dataViewer`.
- **DuckDB:** a file path, no credential.

## What Satchel still stores

- **The push-webhook secret of a git source.** Satchel generates it to check that a push came from
  your git host; it is not a credential to another system. It stays encrypted with
  `SATCHEL_ENCRYPTION_KEY`.
- **Sign-in tokens.** Only their SHA-256 hashes are stored.

The [security model](../architecture/system.md) shows the rest.
