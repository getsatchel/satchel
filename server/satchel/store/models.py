"""SQLAlchemy models for the satchel store (design §3.3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )


def _fk(
    target: str, *, nullable: bool = False, use_alter: bool = False, name: str | None = None
) -> Mapped[Any]:
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey(target, use_alter=use_alter, name=name),
        nullable=nullable,
    )


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Workspace(Base):
    __tablename__ = "workspace"
    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created()


class AppUser(Base):
    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint(
            "role in ('admin','curator','analyst','explorer')", name="app_user_role_ck"
        ),
        # One IdP subject maps to one user per workspace (migration 0004).
        Index(
            "app_user_ws_sub_uq",
            "workspace_id",
            "external_sub",
            unique=True,
            postgresql_where=text("external_sub is not null"),
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    external_sub: Mapped[str | None] = mapped_column(Text, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class OAuthClient(Base):
    """An OAuth client: registered via RFC 7591 (``dcr``) or a Client ID Metadata Document
    (``cimd``, client_id = the https URL). Global (not per workspace); public clients only."""

    __tablename__ = "oauth_client"
    __table_args__ = (
        UniqueConstraint("client_id", name="oauth_client_client_id_uq"),
        CheckConstraint("registration_kind in ('dcr','cimd')", name="oauth_client_kind_ck"),
        CheckConstraint("token_endpoint_auth_method = 'none'", name="oauth_client_auth_ck"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    client_id: Mapped[str] = mapped_column(Text)
    client_name: Mapped[str] = mapped_column(Text)
    redirect_uris: Mapped[list[str]] = mapped_column(ARRAY(Text))
    grant_types: Mapped[list[str]] = mapped_column(ARRAY(Text))
    token_endpoint_auth_method: Mapped[str] = mapped_column(Text, server_default=text("'none'"))
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    registration_kind: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class ApiToken(Base):
    """Bearer token row (design §3.3, §9). Only the sha256 of the token is stored.

    ``kind`` = ``pat`` (personal access token), ``oauth_access`` (1h) or ``oauth_refresh``
    (rotating). OAuth rows carry ``client_id``, the ``family_id`` shared by every token minted
    from one authorization grant, and the ``resource`` (audience) they are bound to."""

    __tablename__ = "api_token"
    __table_args__ = (
        UniqueConstraint("hash", name="api_token_hash_uq"),
        Index("api_token_workspace_idx", "workspace_id"),
        Index("api_token_family_idx", "family_id"),
        CheckConstraint(
            "kind in ('pat','oauth_access','oauth_refresh')", name="api_token_kind_ck"
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    user_id: Mapped[uuid.UUID] = _fk("app_user.id")
    hash: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{mcp}'"))
    kind: Mapped[str] = mapped_column(Text, server_default=text("'pat'"))
    client_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("oauth_client.client_id", ondelete="CASCADE"), nullable=True
    )
    family_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class AuthFlow(Base):
    """One in-flight login: an OAuth authorization request (``mcp``) or a UI login (``web``).

    Lives from /oauth/authorize (or /auth/login) through the IdP round-trip, workspace choice
    and consent, then holds the single-use authorization code (hash only). Global: the
    workspace is only chosen during the flow. Rows are kept after redemption so a replayed code
    can revoke the whole token family."""

    __tablename__ = "auth_flow"
    __table_args__ = (
        UniqueConstraint("state_hash", name="auth_flow_state_hash_uq"),
        UniqueConstraint("code_hash", name="auth_flow_code_hash_uq"),
        Index("auth_flow_expires_idx", "expires_at"),
        CheckConstraint("kind in ('mcp','web')", name="auth_flow_kind_ck"),
        CheckConstraint(
            "status in ('idp','workspace','consent','code','redeemed','done','denied')",
            name="auth_flow_status_ck",
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    state_hash: Mapped[str] = mapped_column(Text)  # sha256 of the `state` sent to the IdP
    browser_hash: Mapped[str] = mapped_column(Text)  # sha256 of the flow cookie secret
    nonce: Mapped[str] = mapped_column(Text)
    code_verifier: Mapped[str] = mapped_column(Text)  # satchel's own PKCE verifier for the IdP leg
    csrf: Mapped[str] = mapped_column(Text)  # token embedded in the picker/consent forms
    # The OAuth authorization request (kind = mcp).
    client_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("oauth_client.client_id", ondelete="CASCADE"), nullable=True
    )
    redirect_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_challenge: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # kind = web
    requested_workspace: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set after the IdP callback / workspace choice.
    idp_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    idp_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    idp_email_verified: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    idp_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_id: Mapped[uuid.UUID | None] = _fk("workspace.id", nullable=True)
    user_id: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)
    # The authorization code and the token family it produced.
    code_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    code_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    family_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WebSession(Base):
    """Server-side browser session for the human UI (cookie holds a random secret; only its
    sha256 is stored). ``csrf`` is the token cookie-authenticated writes must echo."""

    __tablename__ = "web_session"
    __table_args__ = (
        UniqueConstraint("hash", name="web_session_hash_uq"),
        Index("web_session_workspace_idx", "workspace_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    user_id: Mapped[uuid.UUID] = _fk("app_user.id")
    hash: Mapped[str] = mapped_column(Text)
    csrf: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Domain(Base):
    __tablename__ = "domain"
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    name: Mapped[str] = mapped_column(Text)
    owner_user_id: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)


class Connection(Base):
    __tablename__ = "connection"
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_for_ai: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class LlmConnection(Base):
    """A model provider a workspace admin configured in the UI (design §13): used by curation and
    evals when the deployment sets no ``SATCHEL_CHAT_*``. The API key and extra header values are
    AES-256-GCM ciphertext (satchel.core.crypto); only a hint and the header names are plain."""

    __tablename__ = "llm_connection"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="llm_connection_ws_name_uq"),
        CheckConstraint(
            "provider in ('anthropic','openai_compat')", name="llm_connection_provider_ck"
        ),
        # At most one default per workspace (migration 0012).
        Index(
            "llm_connection_ws_default_uq", "workspace_id", unique=True,
            postgresql_where=text("is_default"),
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    name: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_headers_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_header_names: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_by: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class Asset(Base):
    __tablename__ = "asset"
    __table_args__ = (
        CheckConstraint(
            "kind in ('database','schema','table','column','semantic_model','metric')",
            name="asset_kind_ck",
        ),
        UniqueConstraint("workspace_id", "connection_id", "fqn", name="asset_ws_conn_fqn_uq"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    connection_id: Mapped[uuid.UUID | None] = _fk("connection.id", nullable=True)
    kind: Mapped[str] = mapped_column(Text)
    fqn: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = _fk("asset.id", nullable=True)
    data_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    first_seen_at: Mapped[datetime] = _created()
    last_seen_at: Mapped[datetime] = _created()
    dropped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QueryResult(Base):
    """A cached result set (design §11). Rows live in a Parquet file under SATCHEL_RESULT_DIR."""

    __tablename__ = "query_result"
    __table_args__ = (Index("query_result_workspace_idx", "workspace_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    session_id: Mapped[uuid.UUID | None] = _fk("session.id", nullable=True)
    tool_call_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tool_call.id"), nullable=True
    )
    connection_id: Mapped[uuid.UUID | None] = _fk("connection.id", nullable=True)
    sql: Mapped[str] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    columns: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ContextItem(Base):
    __tablename__ = "context_item"
    __table_args__ = (
        CheckConstraint(
            "scope_kind in ('workspace','domain','connection','asset','user')",
            name="context_item_scope_kind_ck",
        ),
        CheckConstraint(
            "state in ('proposed','draft','published','deprecated')", name="context_item_state_ck"
        ),
        CheckConstraint("managed_by in ('ui','git','source')", name="context_item_managed_by_ck"),
        UniqueConstraint("workspace_id", "key", name="context_item_ws_key_uq"),
        Index("context_item_source_idx", "source_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    type: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    scope_kind: Mapped[str] = mapped_column(Text, server_default=text("'workspace'"))
    scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    state: Mapped[str] = mapped_column(Text, server_default=text("'draft'"))
    current_version_id: Mapped[uuid.UUID | None] = _fk(
        "context_version.id", nullable=True, use_alter=True, name="context_item_current_version_fk"
    )
    published_version_id: Mapped[uuid.UUID | None] = _fk(
        "context_version.id",
        nullable=True,
        use_alter=True,
        name="context_item_published_version_fk",
    )
    managed_by: Mapped[str] = mapped_column(Text, server_default=text("'ui'"))
    owner_user_id: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)
    # Items synced from an upstream tool (migration 0009): managed_by = 'source'; ``source_key``
    # is the id in that tool, e.g. ``model.jaffle_shop.orders``.
    source_id: Mapped[uuid.UUID | None] = _fk("context_source.id", nullable=True)
    source_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class ContextSource(Base):
    """An upstream system whose metadata satchel imports and keeps in sync (design §8.3): dbt now,
    then Cube and SQLMesh. ``config`` is adapter config plus ``publish``/``priority``."""

    __tablename__ = "context_source"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="context_source_ws_name_uq"),
        CheckConstraint(
            "last_status is null or last_status in ('ok','error')",
            name="context_source_last_status_ck",
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    connection_id: Mapped[uuid.UUID | None] = _fk("connection.id", nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_report: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = _created()


class ContextVersion(Base):
    __tablename__ = "context_version"
    __table_args__ = (UniqueConstraint("item_id", "seq", name="context_version_item_seq_uq"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    item_id: Mapped[uuid.UUID] = _fk("context_item.id")
    seq: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    content_hash: Mapped[str] = mapped_column(Text)
    author_user_id: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created()


class ContextEntityLink(Base):
    __tablename__ = "context_entity_link"
    __table_args__ = (
        CheckConstraint(
            "relation in ('mentions','defines','applies_to','uses')",
            name="context_entity_link_rel_ck",
        ),
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("context_version.id"), primary_key=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("asset.id"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(Text, primary_key=True)


class ContextChunk(Base):
    __tablename__ = "context_chunk"
    __table_args__ = (
        Index(
            "context_chunk_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("context_chunk_tsv_gin", "tsv", postgresql_using="gin"),
        Index("context_chunk_version_idx", "version_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version_id: Mapped[uuid.UUID] = _fk("context_version.id")
    ord: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)


class Release(Base):
    __tablename__ = "release"
    __table_args__ = (
        CheckConstraint("kind in ('published','preview')", name="release_kind_ck"),
        UniqueConstraint("workspace_id", "seq", "kind", name="release_ws_seq_kind_uq"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()
    parent_release_id: Mapped[uuid.UUID | None] = _fk("release.id", nullable=True)
    # Pull-request previews (migration 0013): the git ref the preview was built from, e.g.
    # ``refs/pull/12/merge@3f2c1a9``. Published by merging the pull request, not from the UI.
    ref: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReleaseItem(Base):
    __tablename__ = "release_item"
    release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("release.id"), primary_key=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("context_item.id"), primary_key=True
    )
    version_id: Mapped[uuid.UUID] = _fk("context_version.id")


class Session(Base):
    __tablename__ = "session"
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    user_id: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)
    client: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    mcp_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    release_id: Mapped[uuid.UUID | None] = _fk("release.id", nullable=True)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = _created()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    # Evidence visibility (design §9, migration 0005): metadata only for everyone but admins.
    sensitive: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Set on reference-agent sessions run by an eval (migration 0007); excluded from
    # observability, signals and curation by default. No FK so deleting runs never cascades.
    eval_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class ToolCall(Base):
    __tablename__ = "tool_call"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = _fk("session.id")
    tool: Mapped[str] = mapped_column(Text)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    result_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _created()


class ServedContext(Base):
    __tablename__ = "served_context"
    tool_call_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tool_call.id"), primary_key=True
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("context_version.id"), primary_key=True
    )
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(nullable=True)


class Signal(Base):
    __tablename__ = "signal"
    __table_args__ = (
        CheckConstraint(
            "source in ('agent','human','implicit','hygiene')", name="signal_source_ck"
        ),
        # The gap detector's scan: unclustered signals of a workspace, newest window first.
        Index(
            "signal_unclustered_idx",
            "workspace_id",
            "created_at",
            postgresql_where=text("clustered_into is null"),
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    session_id: Mapped[uuid.UUID | None] = _fk("session.id", nullable=True)
    user_id: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)
    kind: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    asset_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default=text("'{}'")
    )
    item_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default=text("'{}'")
    )
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    created_at: Mapped[datetime] = _created()
    # The suggestion this signal was clustered into (migration 0006); NULL = not clustered yet.
    clustered_into: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("suggestion.id", name="signal_clustered_into_fk"),
        nullable=True,
    )


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint("source in ('agent','human')", name="feedback_source_ck"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = _fk("session.id")
    tool_call_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tool_call.id"), nullable=True
    )
    user_id: Mapped[uuid.UUID] = _fk("app_user.id")
    rating: Mapped[int] = mapped_column(SmallInteger)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()
    # 'agent' = record_user_feedback (the agent relaying the user); 'human' = a person clicked
    # a button in the result widget (migration 0008). ``client`` = the host that rendered it.
    source: Mapped[str] = mapped_column(Text, server_default=text("'agent'"))
    client: Mapped[str | None] = mapped_column(Text, nullable=True)


class Suggestion(Base):
    """A cluster of signals plus proposed changes waiting for review (design §3.3, §7)."""

    __tablename__ = "suggestion"
    __table_args__ = (
        CheckConstraint("status in ('open','completed','dismissed')", name="suggestion_status_ck"),
        CheckConstraint("source in ('signals','hygiene','golden')", name="suggestion_source_ck"),
        Index("suggestion_ws_status_idx", "workspace_id", "status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    # signals = behavioural cluster over the thresholds; hygiene = a hygiene finding;
    # golden = a session proposed as a golden query.
    source: Mapped[str] = mapped_column(Text, server_default=text("'signals'"))
    dismiss_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    distinct_users: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    distinct_sessions: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    drafted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    draft_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class SuggestionChange(Base):
    """One proposed change. ``target_item_id`` set = change that item; otherwise a new item of
    ``item_type`` is created at ``proposed_key``. Bodies are full (not patches) so the UI can
    diff them against the target's current version."""

    __tablename__ = "suggestion_change"
    __table_args__ = (
        CheckConstraint(
            "op in ('create_item','update_item','deprecate_item','endorse_asset',"
            "'update_asset_doc','promote_golden_query')",
            name="suggestion_change_op_ck",
        ),
        CheckConstraint(
            "status in ('pending','accepted','rejected','applied')",
            name="suggestion_change_status_ck",
        ),
        Index("suggestion_change_suggestion_idx", "suggestion_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    suggestion_id: Mapped[uuid.UUID] = _fk("suggestion.id")
    ord: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    op: Mapped[str] = mapped_column(Text)
    item_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_item_id: Mapped[uuid.UUID | None] = _fk("context_item.id", nullable=True)
    target_asset_id: Mapped[uuid.UUID | None] = _fk("asset.id", nullable=True)
    proposed_body: Mapped[str] = mapped_column(Text, server_default=text("''"))
    proposed_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    rationale: Mapped[str] = mapped_column(Text, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    decided_by: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Outcome: {item_key, seq}, or {manual: true, ...} for git-managed targets, or {reason}.
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created()


class SuggestionSignal(Base):
    __tablename__ = "suggestion_signal"
    __table_args__ = (Index("suggestion_signal_signal_idx", "signal_id"),)
    suggestion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suggestion.id"), primary_key=True
    )
    signal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("signal.id"), primary_key=True
    )


class EvalSuite(Base):
    """A versioned eval suite (design §10): one row per (key, version_seq); ``spec`` is the
    validated suite JSON."""

    __tablename__ = "eval_suite"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key", "version_seq", name="eval_suite_ws_key_ver_uq"),
        CheckConstraint("managed_by in ('ui','git')", name="eval_suite_managed_by_ck"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    key: Mapped[str] = mapped_column(Text)
    version_seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(Text, server_default=text("''"))
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(Text)
    managed_by: Mapped[str] = mapped_column(Text, server_default=text("'ui'"))
    created_by: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)
    created_at: Mapped[datetime] = _created()


class EvalRun(Base):
    __tablename__ = "eval_run"
    __table_args__ = (
        CheckConstraint(
            "status in ('running','completed','failed')", name="eval_run_status_ck"
        ),
        Index("eval_run_ws_started_idx", "workspace_id", "started_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    suite_id: Mapped[uuid.UUID] = _fk("eval_suite.id")
    release_id: Mapped[uuid.UUID] = _fk("release.id")
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    started_by: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class EvalCaseResult(Base):
    __tablename__ = "eval_case_result"
    __table_args__ = (
        CheckConstraint(
            "outcome in ('pass','fail','error','skipped')", name="eval_case_result_outcome_ck"
        ),
        Index("eval_case_result_run_idx", "run_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    run_id: Mapped[uuid.UUID] = _fk("eval_run.id")
    case_key: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(Text)
    session_id: Mapped[uuid.UUID | None] = _fk("session.id", nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    grades: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    cost: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        # Keyset pagination for GET /v1/audit and `satchel audit export` (migration 0011).
        Index("audit_event_ws_created_idx", "workspace_id", "created_at", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = _fk("workspace.id")
    actor_user_id: Mapped[uuid.UUID | None] = _fk("app_user.id", nullable=True)
    action: Mapped[str] = mapped_column(String)
    target: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created()
