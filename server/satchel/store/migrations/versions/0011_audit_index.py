"""Index audit_event for keyset pagination by (workspace_id, created_at, id) (design §9, M8).

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "audit_event_ws_created_idx", "audit_event", ["workspace_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("audit_event_ws_created_idx", table_name="audit_event")
