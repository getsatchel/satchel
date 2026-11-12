"""The job queue (``satchel.jobs``): ``job``, workspace RLS plus the worker policy. Due jobs are
claimed with ``FOR UPDATE SKIP LOCKED``; a running job's lease (``locked_until``) is renewed by
its heartbeat, and an expired lease makes the job claimable again.

Frozen: explicit DDL with literal policy expressions.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

WS = "nullif(current_setting('satchel.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"),
                  nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('queued','running','done','failed')",
                           name="job_status_ck"),
    )
    op.create_index("job_queued_idx", "job", ["run_after", "id"],
                    postgresql_where=sa.text("status = 'queued'"))
    op.create_index("job_running_idx", "job", ["locked_until"],
                    postgresql_where=sa.text("status = 'running'"))
    op.create_index("job_dedupe_uq", "job", ["kind", "dedupe_key"], unique=True,
                    postgresql_where=sa.text(
                        "status in ('queued','running') and dedupe_key is not null"))
    op.create_index("job_ws_created_idx", "job", ["workspace_id", "created_at"])
    op.execute("ALTER TABLE job ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE job FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY job_rls ON job USING (workspace_id = {WS}) "
        f"WITH CHECK (workspace_id = {WS})"
    )
    op.execute(
        "CREATE POLICY job_worker ON job FOR ALL TO satchel_worker USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.drop_table("job")
