"""Evals (design §3.3, §10): eval_suite, eval_run, eval_case_result and session.eval_run_id.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0007"
down_revision = "0006"
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


def _uuid_pk() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column("id", UUID(as_uuid=True), primary_key=True,
                     server_default=sa.text("gen_random_uuid()"))


def _ws() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"),
                     nullable=False)


def upgrade() -> None:
    op.add_column("session", sa.Column("eval_run_id", UUID(as_uuid=True), nullable=True))

    op.create_table(
        "eval_suite",
        _uuid_pk(), _ws(),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("version_seq", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("spec", JSONB(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("managed_by", sa.Text(), nullable=False, server_default=sa.text("'ui'")),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "key", "version_seq", name="eval_suite_ws_key_ver_uq"),
        sa.CheckConstraint("managed_by in ('ui','git')", name="eval_suite_managed_by_ck"),
    )
    op.create_table(
        "eval_run",
        _uuid_pk(), _ws(),
        sa.Column("suite_id", UUID(as_uuid=True), sa.ForeignKey("eval_suite.id"), nullable=False),
        sa.Column("release_id", UUID(as_uuid=True), sa.ForeignKey("release.id"), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'running'")),
        sa.Column("started_by", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("status in ('running','completed','failed')",
                           name="eval_run_status_ck"),
    )
    op.create_index("eval_run_ws_started_idx", "eval_run", ["workspace_id", "started_at"])
    op.create_table(
        "eval_case_result",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        _ws(),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("eval_run.id"), nullable=False),
        sa.Column("case_key", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("session.id"), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("grades", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("cost", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("outcome in ('pass','fail','error','skipped')",
                           name="eval_case_result_outcome_ck"),
    )
    op.create_index("eval_case_result_run_idx", "eval_case_result", ["run_id"])

    for t in ("eval_suite", "eval_run", "eval_case_result"):
        _policy(t, f"workspace_id = {WS}")


def downgrade() -> None:
    op.drop_table("eval_case_result")
    op.drop_table("eval_run")
    op.drop_table("eval_suite")
    op.drop_column("session", "eval_run_id")
