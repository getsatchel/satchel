"""``job.result``: what a finished job returns (a sync report, a crawl report), read by the UI
through ``GET /v1/jobs/{id}``.

Frozen: explicit DDL.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job", sa.Column("result", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("job", "result")
