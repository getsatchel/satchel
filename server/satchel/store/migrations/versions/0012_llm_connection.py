"""LLM connections configured in the UI (design §13): ``llm_connection``, workspace RLS plus the
worker policy, at most one default per workspace. Secrets are ciphertext (satchel.core.crypto).

Frozen: explicit DDL with literal policy expressions.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

WS = "nullif(current_setting('satchel.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "llm_connection",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"),
                  nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("default_model", sa.Text(), nullable=True),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("api_key_hint", sa.Text(), nullable=True),
        sa.Column("extra_headers_enc", sa.Text(), nullable=True),
        sa.Column("extra_header_names", JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "name", name="llm_connection_ws_name_uq"),
        sa.CheckConstraint("provider in ('anthropic','openai_compat')",
                           name="llm_connection_provider_ck"),
    )
    op.create_index("llm_connection_ws_default_uq", "llm_connection", ["workspace_id"],
                    unique=True, postgresql_where=sa.text("is_default"))
    op.execute("ALTER TABLE llm_connection ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE llm_connection FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY llm_connection_rls ON llm_connection USING (workspace_id = {WS}) "
        f"WITH CHECK (workspace_id = {WS})"
    )
    op.execute(
        "CREATE POLICY llm_connection_worker ON llm_connection FOR ALL TO satchel_worker "
        "USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.drop_table("llm_connection")
