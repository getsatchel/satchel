"""Keyless model providers: ``bedrock`` (Amazon Bedrock) and ``vertex`` (Vertex AI), which use the
server's cloud identity instead of an API key, and ``llm_connection.region`` / ``project`` for
where the model runs.

Frozen: explicit DDL.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_connection", sa.Column("region", sa.Text(), nullable=True))
    op.add_column("llm_connection", sa.Column("project", sa.Text(), nullable=True))
    op.drop_constraint("llm_connection_provider_ck", "llm_connection", type_="check")
    op.create_check_constraint(
        "llm_connection_provider_ck", "llm_connection",
        "provider in ('anthropic','openai_compat','gemini','bedrock','vertex')")


def downgrade() -> None:
    op.execute("delete from llm_connection where provider in ('bedrock', 'vertex')")
    op.drop_constraint("llm_connection_provider_ck", "llm_connection", type_="check")
    op.create_check_constraint("llm_connection_provider_ck", "llm_connection",
                               "provider in ('anthropic','openai_compat','gemini')")
    op.drop_column("llm_connection", "project")
    op.drop_column("llm_connection", "region")
