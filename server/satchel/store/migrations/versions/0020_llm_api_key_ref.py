"""``llm_connection.api_key_ref``: the API key as a secret manager reference (``aws-sm:``,
``gcp-sm:``), read when needed, instead of a value sealed in ``api_key_enc``.

Frozen: explicit DDL.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_connection", sa.Column("api_key_ref", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_connection", "api_key_ref")
