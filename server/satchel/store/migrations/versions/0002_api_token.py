"""Personal access tokens (design §3.3, §9).

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

WS = "nullif(current_setting('satchel.workspace_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('satchel.bypass_rls', true), '') = 'on'"


def upgrade() -> None:
    op.create_table(
        "api_token",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"),
                  nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("hash", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("scopes", ARRAY(sa.Text), nullable=False, server_default=sa.text("'{mcp}'")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("hash", name="api_token_hash_uq"),
    )
    op.create_index("api_token_workspace_idx", "api_token", ["workspace_id"])
    op.execute("ALTER TABLE api_token ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_token FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY api_token_rls ON api_token USING (({BYPASS}) or (workspace_id = {WS})) "
        f"WITH CHECK (({BYPASS}) or (workspace_id = {WS}))"
    )


def downgrade() -> None:
    op.drop_table("api_token")
