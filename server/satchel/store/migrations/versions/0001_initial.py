"""Initial schema: extension, tables, indexes, RLS.

Frozen: explicit DDL, independent of satchel.store.models (later changes are new revisions).

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

WS = "nullif(current_setting('satchel.workspace_id', true), '')::uuid"
USR = "nullif(current_setting('satchel.user_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('satchel.bypass_rls', true), '') = 'on'"

# Tables with their own workspace_id column.
DIRECT = [
    "app_user",
    "domain",
    "connection",
    "asset",
    "context_item",
    "release",
    "session",
    "signal",
    "audit_event",
]
# Child tables: visible iff the parent row is visible (parent's RLS applies to the subquery).
CHILD = {
    "context_version": "exists (select 1 from context_item p where p.id = item_id)",
    "context_entity_link": "exists (select 1 from context_version p where p.id = version_id)",
    "context_chunk": "exists (select 1 from context_version p where p.id = version_id)",
    "release_item": "exists (select 1 from release p where p.id = release_id)",
    "tool_call": "exists (select 1 from session p where p.id = session_id)",
    "served_context": "exists (select 1 from tool_call p where p.id = tool_call_id)",
    "feedback": "exists (select 1 from session p where p.id = session_id)",
}


def _policy(table: str, expr: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_rls ON {table} USING (({BYPASS}) or ({expr})) "
        f"WITH CHECK (({BYPASS}) or ({expr}))"
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "workspace",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("settings", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('slug', name=None),
    )
    op.create_table(
        "app_user",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"), nullable=False),
        sa.Column("external_sub", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("role in ('admin','curator','analyst','explorer')", name='app_user_role_ck'),
    )
    op.create_table(
        "connection",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("secret_ref", sa.Text(), nullable=True),
        sa.Column("default_for_ai", sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    op.create_table(
        "asset",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("connection.id"), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("fqn", sa.Text(), nullable=False),
        sa.Column("parent_id", UUID(as_uuid=True), sa.ForeignKey("asset.id"), nullable=True),
        sa.Column("data_type", sa.Text(), nullable=True),
        sa.Column("stats", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("dropped_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('workspace_id', 'connection_id', 'fqn', name='asset_ws_conn_fqn_uq'),
        sa.CheckConstraint("kind in ('database','schema','table','column','semantic_model','metric')", name='asset_kind_ck'),
    )
    op.create_table(
        "audit_event",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"), nullable=False),
        sa.Column("actor_user_id", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_table(
        "context_item",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("scope_kind", sa.Text(), nullable=False, server_default=sa.text("'workspace'")),
        sa.Column("scope_id", UUID(as_uuid=True), nullable=True),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("current_version_id", UUID(as_uuid=True), nullable=True),
        sa.Column("published_version_id", UUID(as_uuid=True), nullable=True),
        sa.Column("managed_by", sa.Text(), nullable=False, server_default=sa.text("'ui'")),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint("scope_kind in ('workspace','domain','connection','asset','user')", name='context_item_scope_kind_ck'),
        sa.CheckConstraint("managed_by in ('ui','git')", name='context_item_managed_by_ck'),
        sa.UniqueConstraint('workspace_id', 'key', name='context_item_ws_key_uq'),
        sa.CheckConstraint("state in ('proposed','draft','published','deprecated')", name='context_item_state_ck'),
    )
    op.create_table(
        "domain",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=True),
    )
    op.create_table(
        "release",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("parent_release_id", UUID(as_uuid=True), sa.ForeignKey("release.id"), nullable=True),
        sa.UniqueConstraint('workspace_id', 'seq', 'kind', name='release_ws_seq_kind_uq'),
        sa.CheckConstraint("kind in ('published','preview')", name='release_kind_ck'),
    )
    op.create_table(
        "context_version",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("item_id", UUID(as_uuid=True), sa.ForeignKey("context_item.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("meta", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("author_user_id", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("provenance", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('item_id', 'seq', name='context_version_item_seq_uq'),
    )
    op.create_table(
        "session",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("client", sa.Text(), nullable=True),
        sa.Column("client_version", sa.Text(), nullable=True),
        sa.Column("mcp_session_id", sa.Text(), nullable=True),
        sa.Column("release_id", UUID(as_uuid=True), sa.ForeignKey("release.id"), nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
    )
    op.create_table(
        "context_chunk",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("version_id", UUID(as_uuid=True), sa.ForeignKey("context_version.id"), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tsv", TSVECTOR(), sa.Computed("to_tsvector('english', text)", persisted=True), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
    )
    op.create_index('context_chunk_tsv_gin', 'context_chunk', ['tsv'], postgresql_using='gin')
    op.create_index('context_chunk_embedding_hnsw', 'context_chunk', ['embedding'], postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'})
    op.create_index('context_chunk_version_idx', 'context_chunk', ['version_id'])
    op.create_table(
        "context_entity_link",
        sa.Column("version_id", UUID(as_uuid=True), sa.ForeignKey("context_version.id"), primary_key=True),
        sa.Column("asset_id", UUID(as_uuid=True), sa.ForeignKey("asset.id"), primary_key=True),
        sa.Column("relation", sa.Text(), primary_key=True),
        sa.CheckConstraint("relation in ('mentions','defines','applies_to','uses')", name='context_entity_link_rel_ck'),
        sa.PrimaryKeyConstraint('version_id', 'asset_id', 'relation'),
    )
    op.create_table(
        "release_item",
        sa.Column("release_id", UUID(as_uuid=True), sa.ForeignKey("release.id"), primary_key=True),
        sa.Column("item_id", UUID(as_uuid=True), sa.ForeignKey("context_item.id"), primary_key=True),
        sa.Column("version_id", UUID(as_uuid=True), sa.ForeignKey("context_version.id"), nullable=False),
        sa.PrimaryKeyConstraint('release_id', 'item_id'),
    )
    op.create_table(
        "signal",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("session.id"), nullable=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("asset_ids", ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("item_ids", ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column("clustered_into", UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("source in ('agent','human','implicit','hygiene')", name='signal_source_ck'),
    )
    op.create_table(
        "tool_call",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("session.id"), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("args", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result_meta", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("session.id"), nullable=False),
        sa.Column("tool_call_id", sa.BigInteger(), sa.ForeignKey("tool_call.id"), nullable=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_table(
        "served_context",
        sa.Column("tool_call_id", sa.BigInteger(), sa.ForeignKey("tool_call.id"), primary_key=True, autoincrement=True),
        sa.Column("version_id", UUID(as_uuid=True), sa.ForeignKey("context_version.id"), primary_key=True),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint('tool_call_id', 'version_id'),
    )
    op.create_foreign_key('context_item_current_version_fk', 'context_item', 'context_version', ['current_version_id'], ["id"])
    op.create_foreign_key('context_item_published_version_fk', 'context_item', 'context_version', ['published_version_id'], ["id"])

    _policy("workspace", f"id = {WS}")
    for t in DIRECT:
        _policy(t, f"workspace_id = {WS}")
    # User-scoped items are private to their owner.
    op.execute("DROP POLICY context_item_rls ON context_item")
    expr = f"workspace_id = {WS} and (scope_kind <> 'user' or scope_id = {USR})"
    op.execute(
        f"CREATE POLICY context_item_rls ON context_item USING (({BYPASS}) or ({expr})) "
        f"WITH CHECK (({BYPASS}) or ({expr}))"
    )
    for t, e in CHILD.items():
        _policy(t, e)


def downgrade() -> None:
    op.drop_constraint("context_item_current_version_fk", "context_item", type_="foreignkey")
    op.drop_constraint("context_item_published_version_fk", "context_item", type_="foreignkey")
    for t in ('served_context', 'feedback', 'tool_call', 'signal', 'release_item', 'context_entity_link', 'context_chunk', 'session', 'context_version', 'release', 'domain', 'context_item', 'audit_event', 'asset', 'connection', 'app_user', 'workspace'):
        op.drop_table(t)
