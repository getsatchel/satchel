"""Pull-request previews (design §8.2): ``release.ref`` names the git ref a preview was built
from (``POST /v1/sync/preview``, the GitHub Action). Such a preview is published by merging the
pull request, never from the UI.

Frozen: explicit DDL.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("release", sa.Column("ref", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("release", "ref")
