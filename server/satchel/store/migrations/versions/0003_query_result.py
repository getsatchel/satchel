"""Result cache table for the data plane (design §11).

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

WS = "nullif(current_setting('satchel.workspace_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('satchel.bypass_rls', true), '') = 'on'"


def upgrade() -> None:
    op.create_table(
        "query_result",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"),
                  nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("session.id"), nullable=True),
        sa.Column("tool_call_id", sa.BigInteger, sa.ForeignKey("tool_call.id"), nullable=True),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("connection.id"),
                  nullable=True),
        sa.Column("sql", sa.Text, nullable=False),
        sa.Column("row_count", sa.Integer, nullable=False),
        sa.Column("truncated", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("columns", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("query_result_workspace_idx", "query_result", ["workspace_id"])
    op.execute("ALTER TABLE query_result ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE query_result FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY query_result_rls ON query_result USING (({BYPASS}) or "
        f"(workspace_id = {WS})) WITH CHECK (({BYPASS}) or (workspace_id = {WS}))"
    )


def downgrade() -> None:
    op.drop_table("query_result")
