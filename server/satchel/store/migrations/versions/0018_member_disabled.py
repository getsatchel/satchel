"""``app_user.disabled_at``: a deactivated member. Their tokens, sessions and sign-ins are
refused until an admin reactivates them.

Frozen: explicit DDL.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_user", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("app_user", "disabled_at")
