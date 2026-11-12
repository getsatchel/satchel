"""Remove the ``user_memory`` item type. Per-user memory and preferences belong to the agent's
own client; Satchel serves only reviewed, shared context. Existing ``user_memory`` items, their
versions and everything that points at them are deleted. ``scope_kind = 'user'`` and its RLS
policy stay.

Frozen: explicit SQL.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

ITEMS = "select id from context_item where type = 'user_memory'"
VERSIONS = f"select id from context_version where item_id in ({ITEMS})"


def upgrade() -> None:
    op.execute(f"delete from served_context where version_id in ({VERSIONS})")
    op.execute(f"delete from release_item where item_id in ({ITEMS})")
    op.execute(f"delete from context_entity_link where version_id in ({VERSIONS})")
    op.execute(f"delete from context_chunk where version_id in ({VERSIONS})")
    op.execute(f"update suggestion_change set target_item_id = null "
               f"where target_item_id in ({ITEMS})")
    op.execute("update context_item set current_version_id = null, published_version_id = null "
               "where type = 'user_memory'")
    op.execute(f"delete from context_version where item_id in ({ITEMS})")
    op.execute("delete from context_item where type = 'user_memory'")


def downgrade() -> None:
    # The deleted memories cannot be restored; the type is gone from the code either way.
    pass
