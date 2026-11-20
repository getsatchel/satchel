"""``llm_connection.provider`` may be ``gemini``: a Gemini API key, called through Google's
OpenAI-compatible endpoint.

Frozen: explicit DDL.

Revision ID: 0019
Revises: 0018
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("llm_connection_provider_ck", "llm_connection", type_="check")
    op.create_check_constraint("llm_connection_provider_ck", "llm_connection",
                               "provider in ('anthropic','openai_compat','gemini')")


def downgrade() -> None:
    op.execute("delete from llm_connection where provider = 'gemini'")
    op.drop_constraint("llm_connection_provider_ck", "llm_connection", type_="check")
    op.create_check_constraint("llm_connection_provider_ck", "llm_connection",
                               "provider in ('anthropic','openai_compat')")
