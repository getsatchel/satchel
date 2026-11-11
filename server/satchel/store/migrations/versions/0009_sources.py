"""Source adapters (design §8.3): context_source, context_item.source_id/source_key and the
'source' value of context_item.managed_by.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

WS = "nullif(current_setting('satchel.workspace_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('satchel.bypass_rls', true), '') = 'on'"


def _policy(table: str, expr: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_rls ON {table} USING (({BYPASS}) or ({expr})) "
        f"WITH CHECK (({BYPASS}) or ({expr}))"
    )


def upgrade() -> None:
    op.create_table(
        "context_source",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"),
                  nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("connection.id"),
                  nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_fingerprint", sa.Text(), nullable=True),
        sa.Column("last_report", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "name", name="context_source_ws_name_uq"),
        sa.CheckConstraint("last_status is null or last_status in ('ok','error')",
                           name="context_source_last_status_ck"),
    )
    _policy("context_source", f"workspace_id = {WS}")

    op.add_column("context_item", sa.Column(
        "source_id", UUID(as_uuid=True), sa.ForeignKey("context_source.id"), nullable=True))
    op.add_column("context_item", sa.Column("source_key", sa.Text(), nullable=True))
    op.create_index("context_item_source_idx", "context_item", ["source_id"])
    op.drop_constraint("context_item_managed_by_ck", "context_item", type_="check")
    op.create_check_constraint(
        "context_item_managed_by_ck", "context_item", "managed_by in ('ui','git','source')"
    )


def downgrade() -> None:
    op.drop_constraint("context_item_managed_by_ck", "context_item", type_="check")
    op.execute("UPDATE context_item SET managed_by = 'ui' WHERE managed_by = 'source'")
    op.create_check_constraint(
        "context_item_managed_by_ck", "context_item", "managed_by in ('ui','git')"
    )
    op.drop_index("context_item_source_idx", table_name="context_item")
    op.drop_column("context_item", "source_key")
    op.drop_column("context_item", "source_id")
    op.drop_table("context_source")
