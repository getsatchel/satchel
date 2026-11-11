"""Curation tables (design §3.3, §7): suggestion, suggestion_change, suggestion_signal, plus the
deferred ``signal.clustered_into`` foreign key.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0006"
down_revision = "0005"
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
        "suggestion",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"),
                  nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'signals'")),
        sa.Column("dismiss_reason", sa.Text(), nullable=True),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("distinct_users", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("distinct_sessions", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("drafted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("draft_model", sa.Text(), nullable=True),
        sa.Column("draft_note", sa.Text(), nullable=True),
        sa.Column("resolved_by", UUID(as_uuid=True), sa.ForeignKey("app_user.id"),
                  nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("status in ('open','completed','dismissed')",
                           name="suggestion_status_ck"),
        sa.CheckConstraint("source in ('signals','hygiene','golden')",
                           name="suggestion_source_ck"),
    )
    op.create_index("suggestion_ws_status_idx", "suggestion", ["workspace_id", "status"])

    op.create_table(
        "suggestion_change",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("suggestion_id", UUID(as_uuid=True), sa.ForeignKey("suggestion.id"),
                  nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column("item_type", sa.Text(), nullable=True),
        sa.Column("proposed_key", sa.Text(), nullable=True),
        sa.Column("target_item_id", UUID(as_uuid=True), sa.ForeignKey("context_item.id"),
                  nullable=True),
        sa.Column("target_asset_id", UUID(as_uuid=True), sa.ForeignKey("asset.id"),
                  nullable=True),
        sa.Column("proposed_body", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("proposed_meta", JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("decided_by", UUID(as_uuid=True), sa.ForeignKey("app_user.id"),
                  nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "op in ('create_item','update_item','deprecate_item','endorse_asset',"
            "'update_asset_doc','promote_golden_query')",
            name="suggestion_change_op_ck",
        ),
        sa.CheckConstraint("status in ('pending','accepted','rejected','applied')",
                           name="suggestion_change_status_ck"),
    )
    op.create_index("suggestion_change_suggestion_idx", "suggestion_change", ["suggestion_id"])

    op.create_table(
        "suggestion_signal",
        sa.Column("suggestion_id", UUID(as_uuid=True), sa.ForeignKey("suggestion.id"),
                  primary_key=True),
        sa.Column("signal_id", sa.BigInteger(), sa.ForeignKey("signal.id"), primary_key=True),
    )
    op.create_index("suggestion_signal_signal_idx", "suggestion_signal", ["signal_id"])

    op.create_foreign_key(
        "signal_clustered_into_fk", "signal", "suggestion", ["clustered_into"], ["id"]
    )
    op.create_index(
        "signal_unclustered_idx", "signal", ["workspace_id", "created_at"],
        postgresql_where=sa.text("clustered_into is null"),
    )

    _policy("suggestion", f"workspace_id = {WS}")
    _policy("suggestion_change",
            "exists (select 1 from suggestion p where p.id = suggestion_id)")
    _policy("suggestion_signal",
            "exists (select 1 from suggestion p where p.id = suggestion_id)")


def downgrade() -> None:
    op.drop_index("signal_unclustered_idx", table_name="signal")
    op.drop_constraint("signal_clustered_into_fk", "signal", type_="foreignkey")
    op.drop_table("suggestion_signal")
    op.drop_table("suggestion_change")
    op.drop_table("suggestion")
