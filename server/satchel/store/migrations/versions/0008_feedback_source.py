"""Human feedback from result widgets (design §6.4): feedback.source and feedback.client.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feedback",
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'agent'")),
    )
    op.add_column("feedback", sa.Column("client", sa.Text(), nullable=True))
    op.create_check_constraint("feedback_source_ck", "feedback", "source in ('agent','human')")


def downgrade() -> None:
    op.drop_constraint("feedback_source_ck", "feedback", type_="check")
    op.drop_column("feedback", "client")
    op.drop_column("feedback", "source")
