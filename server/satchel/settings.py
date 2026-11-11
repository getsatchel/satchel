from typing import Any, Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SATCHEL_", env_file=".env", extra="ignore")

    # Precedence: init kwargs > environment > .env > config file profile > defaults.
    # The config file (~/.config/satchel/config.toml, see satchel.cli.config) is written by
    # `satchel init`.
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        class ConfigFileSource(PydanticBaseSettingsSource):
            def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
                return None, field_name, False  # unused: __call__ returns everything

            def __call__(self) -> dict[str, Any]:
                from satchel.cli.config import profile_settings

                return profile_settings(set(settings_cls.model_fields))

        return (
            init_settings,
            env_settings,
            dotenv_settings,
            ConfigFileSource(settings_cls),
            file_secret_settings,
        )

    # Default workspace slug for CLI commands (SATCHEL_WORKSPACE).
    workspace: str | None = None
    # `production` makes serve/worker refuse insecure configuration (see satchel.runtime) and makes
    # SATCHEL_ADMIN_DATABASE_URL mandatory (no derived dev fallback).
    env: Literal["development", "production"] = "development"
    # Production escape hatch for an http:// SATCHEL_PUBLIC_URL (TLS terminated elsewhere, tests).
    allow_insecure_public_url: bool = False
    # `json` = one Cloud Logging-compatible JSON object per line (server, worker, access log).
    log_format: Literal["text", "json"] = "text"
    # Personal access token for `satchel mcp stdio` (SATCHEL_TOKEN).
    token: str | None = None
    # AES-256-GCM key for secrets stored in Postgres (LLM API keys): 64 hex characters,
    # `openssl rand -hex 32`. Previous keys (comma-separated) still decrypt during a rotation
    # (`satchel secrets rotate`). See satchel.core.crypto.
    encryption_key: SecretStr | None = None
    encryption_key_previous: SecretStr | None = None

    # Runtime URL: must be a non-superuser role without BYPASSRLS (see deploy/compose/initdb).
    database_url: str = "postgresql+psycopg://satchel_app:satchel_app@localhost:5433/satchel"
    # Server-internal jobs (`admin_session`) connect as satchel_worker, which RLS policies admit by
    # role. Unset in development = database_url with user/password satchel_worker (the local
    # initdb / `satchel init` role); required when env = production.
    admin_database_url: str | None = None
    # Owner role for `satchel db upgrade`; falls back to database_url when unset.
    migration_database_url: str | None = None
    # `satchel db bootstrap` only: an admin URL (Cloud SQL: postgres) and the role passwords.
    bootstrap_database_url: str | None = None
    app_db_password: SecretStr | None = None
    worker_db_password: SecretStr | None = None
    embedding_dim: int = 1024

    # Embeddings (design §5.2). `hash` is deterministic and offline (BM25-level quality at
    # best); `fastembed` runs a local ONNX model; `openai_compat` calls any /embeddings API.
    embedding_provider: Literal["hash", "fastembed", "openai_compat"] = "fastembed"
    fastembed_model: str = "mixedbread-ai/mxbai-embed-large-v1"
    fastembed_cache_dir: str = "~/.cache/satchel/fastembed"
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_model: str = "text-embedding-3-large"
    embedding_api_key: str | None = None

    # Chat model used only to curate (design §7): drafting suggestions, contradiction checks.
    # Unset provider = no LLM: clustering and hygiene still run, drafting is skipped. The API
    # key comes from the environment only (SATCHEL_CHAT_API_KEY, else the provider's own variable:
    # ANTHROPIC_API_KEY / OPENAI_API_KEY). `openai_compat` also covers Ollama, vLLM, LiteLLM.
    chat_provider: Literal["anthropic", "openai_compat"] | None = None
    chat_model: str | None = None  # default claude-sonnet-5-5 for anthropic
    chat_base_url: str | None = None  # default per provider
    chat_api_key: str | None = None
    chat_timeout_s: float = 120.0
    chat_max_retries: int = 4
    # `satchel worker` runs curation for every workspace on this interval (minutes).
    curate_interval_min: int = 1440
    # ...and checks for sources whose scheduled sync (sync_interval_hours) is due this often.
    # 0 turns scheduled syncs off on this worker.
    source_tick_min: int = 5

    # Data plane (design §11). Results are Parquet files under result_dir: a local directory, or
    # gs://bucket/prefix / s3://bucket/prefix for multi-replica deployments.
    # Absolute default: MCP clients such as Claude Desktop launch satchel with cwd "/".
    result_dir: str = "~/.cache/satchel/results"
    result_ttl_days: int = 7
    sql_row_cap: int = 1000
    sql_timeout_s: float = 30.0
    # Delete agent sessions (and their tool calls) older than this; unset = keep forever.
    # These four are defaults: an admin can override them per workspace (satchel.limits).
    session_retention_days: int | None = None

    # dbt sources from git can build manifest.json with `dbt parse` (satchel.sources.dbt_build).
    # Unset = /opt/dbt/bin/dbt (the image's separate virtualenv), else `dbt` on PATH.
    dbt_bin: str | None = None
    dbt_timeout_s: float = 300.0  # deps + parse, wall clock

    # Auth (design §9). `public_url` is the externally visible base URL of this server: it is the
    # OAuth issuer and the base of the audience (resource) URIs tokens are bound to.
    public_url: str = "http://localhost:8080"
    # Workspace IdP (OIDC). Unset = OAuth login and /auth/login return 503; PATs still work.
    # The client secret is read from the environment only (never stored in the database).
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_scopes: str = "openid email profile"
    # Entra does not emit email_verified; set false there (tenant-controlled addresses).
    oidc_require_verified_email: bool = True
    # Dev-only header auth on the REST API (X-Satchel-Workspace / X-Satchel-User). Off by default.
    dev_auth: bool = False
    # Built curator UI (``npm run build`` in ui/). Unset = ../ui/dist next to the server package;
    # served at / by `satchel serve api` when it contains an index.html.
    ui_dir: str | None = None
    # OAuth authorization server lifetimes (seconds).
    oauth_code_ttl_s: int = 60
    oauth_access_ttl_s: int = 3600
    oauth_refresh_ttl_s: int = 30 * 86400
    oauth_flow_ttl_s: int = 600
    # Browser session cookie lifetime for the human UI login.
    session_ttl_s: int = 12 * 3600
    # Client ID Metadata Documents (MCP draft): fetch client metadata from https client_ids.
    oauth_cimd_enabled: bool = True
    oauth_cimd_allow_private_hosts: bool = False  # SSRF guard off: tests/dev only


def get_settings() -> Settings:
    return Settings()
