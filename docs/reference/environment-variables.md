# Environment variables

All variables use the `SATCHEL_` prefix (a `.env` file is also read). Source of truth: `server/satchel/settings.py`. Where a value is also a workspace setting, see [Configuration](../deploy/configuration.md); the environment wins except for Limits, where it sets the default.

Secrets (`*_URL` with passwords, keys, client secret) belong in a secret manager ([Helm: secrets](../deploy/helm.md)).

| Variable | Default | Description |
|---|---|---|
| `SATCHEL_WORKSPACE` | — | Default workspace slug for CLI commands. |
| `SATCHEL_ENV` | `development` | `production` enforces HTTPS public URL and disables dev auth. |
| `SATCHEL_ALLOW_INSECURE_PUBLIC_URL` | `False` | Permit an `http://` public URL in production. |
| `SATCHEL_LOG_FORMAT` | `text` | `text` or `json`. |
| `SATCHEL_TOKEN` | — | Personal access token used by the CLI and `mcp stdio`. |
| `SATCHEL_ENCRYPTION_KEY` | — | 256-bit hex key sealing secrets stored in the database. |
| `SATCHEL_ENCRYPTION_KEY_PREVIOUS` | — | Old key(s), comma-separated, accepted for decryption during rotation. |
| `SATCHEL_DATABASE_URL` | `postgresql+psycopg://satchel_app:satchel_app@localhost:5433/satchel` | Runtime URL (`satchel_app`, RLS applies). |
| `SATCHEL_ADMIN_DATABASE_URL` | — | Worker URL (`satchel_worker`) for server-internal jobs. |
| `SATCHEL_MIGRATION_DATABASE_URL` | — | Owner URL used by `db upgrade`. |
| `SATCHEL_BOOTSTRAP_DATABASE_URL` | — | Superuser URL used once by `db bootstrap`. |
| `SATCHEL_APP_DB_PASSWORD` | — | Password `db bootstrap` sets on `satchel_app`. |
| `SATCHEL_WORKER_DB_PASSWORD` | — | Password `db bootstrap` sets on `satchel_worker`. |
| `SATCHEL_EMBEDDING_DIM` | `1024` | Embedding vector size (must match the model). |
| `SATCHEL_EMBEDDING_PROVIDER` | `fastembed` | `hash` (tests), `fastembed` (local) or `openai_compat`. |
| `SATCHEL_FASTEMBED_MODEL` | `mixedbread-ai/mxbai-embed-large-v1` | Model for the local `fastembed` provider. |
| `SATCHEL_FASTEMBED_CACHE_DIR` | `~/.cache/satchel/fastembed` | Model download cache. |
| `SATCHEL_EMBEDDING_BASE_URL` | `https://api.openai.com/v1` | Base URL for `openai_compat` embeddings. |
| `SATCHEL_EMBEDDING_MODEL` | `text-embedding-3-large` | Model name for `openai_compat` embeddings. |
| `SATCHEL_EMBEDDING_API_KEY` | — | API key for `openai_compat` embeddings. |
| `SATCHEL_CHAT_PROVIDER` | — | `anthropic` or `openai_compat`; wins over workspace Settings → Models. |
| `SATCHEL_CHAT_MODEL` | — | Chat model name. |
| `SATCHEL_CHAT_BASE_URL` | — | Chat API base URL (default per provider). |
| `SATCHEL_CHAT_API_KEY` | — | Chat API key. |
| `SATCHEL_CHAT_TIMEOUT_S` | `120.0` | Chat request timeout. |
| `SATCHEL_CHAT_MAX_RETRIES` | `4` | Chat request retries. |
| `SATCHEL_CURATE_INTERVAL_MIN` | `1440` | Minutes between worker passes. |
| `SATCHEL_SOURCE_TICK_MIN` | `5` | Minutes between the worker's checks for scheduled source syncs; `0` turns them off. |
| `SATCHEL_EVAL_TICK_MIN` | `15` | Minutes between the worker's checks for scheduled eval runs; `0` turns them off ([evals](../concepts/evals.md#scheduled-runs)). |
| `SATCHEL_RESULT_DIR` | `~/.cache/satchel/results` | Result cache location: local path, `gs://` or `s3://`. |
| `SATCHEL_RESULT_TTL_DAYS` | `7` | Default result cache lifetime. |
| `SATCHEL_SQL_ROW_CAP` | `1000` | Default max rows per query (workspace Limits may override). |
| `SATCHEL_SQL_TIMEOUT_S` | `30.0` | Default query timeout in seconds (workspace Limits may override). |
| `SATCHEL_SESSION_RETENTION_DAYS` | — | Default session retention; unset keeps forever. |
| `SATCHEL_PUBLIC_URL` | `http://localhost:8080` | Externally visible base URL (OIDC callbacks, OAuth metadata). |
| `SATCHEL_OIDC_ISSUER` | — | OIDC issuer URL. |
| `SATCHEL_OIDC_CLIENT_ID` | — | OIDC client id. |
| `SATCHEL_OIDC_CLIENT_SECRET` | — | OIDC client secret. |
| `SATCHEL_OIDC_SCOPES` | `openid email profile` | OIDC scopes requested. |
| `SATCHEL_OIDC_REQUIRE_VERIFIED_EMAIL` | `True` | Reject logins without a verified email. |
| `SATCHEL_DEV_AUTH` | `False` | Header-based dev auth. Localhost only. |
| `SATCHEL_UI_DIR` | — | Built UI directory (default `ui/dist`). |
| `SATCHEL_OAUTH_CODE_TTL_S` | `60` | OAuth authorization code lifetime (s). |
| `SATCHEL_OAUTH_ACCESS_TTL_S` | `3600` | OAuth access token lifetime (s). |
| `SATCHEL_OAUTH_REFRESH_TTL_S` | `2592000` | OAuth refresh token lifetime (s). |
| `SATCHEL_OAUTH_FLOW_TTL_S` | `600` | In-progress login flow lifetime (s). |
| `SATCHEL_SESSION_TTL_S` | `43200` | UI session cookie lifetime (s). |
| `SATCHEL_OAUTH_CIMD_ENABLED` | `True` | Accept client ID metadata documents as OAuth clients. |
| `SATCHEL_OAUTH_CIMD_ALLOW_PRIVATE_HOSTS` | `False` | Allow CIMD fetches from private hosts (leave off). |
