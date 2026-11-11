"""OAuth 2.1 authorization server + OIDC login (design §9, M2.1).

New tables: oauth_client, auth_flow (global, admin-only via RLS), web_session (workspace RLS).
api_token gains kind / client_id / family_id / resource / rotated_at. app_user gets a partial
unique index so one IdP subject maps to one user per workspace.

Revision ID: 0004
Revises: 0003
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

WS = "nullif(current_setting('satchel.workspace_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('satchel.bypass_rls', true), '') = 'on'"


def _uuid_pk() -> sa.Column[uuid.UUID]:
    return sa.Column(
        "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _ts(name: str, *, nullable: bool = True, now: bool = False) -> sa.Column[datetime]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable and not now,
        server_default=sa.func.now() if now else None,
    )


def _admin_only(table: str) -> None:
    """RLS that admits only ``admin_session`` (satchel.bypass_rls = 'on'): pre-auth tables."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {table}_rls ON {table} USING ({BYPASS}) WITH CHECK ({BYPASS})")


def upgrade() -> None:
    op.create_table(
        "oauth_client",
        _uuid_pk(),
        sa.Column("client_id", sa.Text, nullable=False),
        sa.Column("client_name", sa.Text, nullable=False),
        sa.Column("redirect_uris", ARRAY(sa.Text), nullable=False),
        sa.Column("grant_types", ARRAY(sa.Text), nullable=False),
        sa.Column("token_endpoint_auth_method", sa.Text, nullable=False,
                  server_default=sa.text("'none'")),
        sa.Column("scope", sa.Text, nullable=True),
        sa.Column("registration_kind", sa.Text, nullable=False),
        _ts("fetched_at"),
        _ts("created_at", now=True),
        sa.UniqueConstraint("client_id", name="oauth_client_client_id_uq"),
        sa.CheckConstraint("registration_kind in ('dcr','cimd')", name="oauth_client_kind_ck"),
        sa.CheckConstraint("token_endpoint_auth_method = 'none'", name="oauth_client_auth_ck"),
    )
    _admin_only("oauth_client")

    op.add_column("api_token", sa.Column("kind", sa.Text, nullable=False,
                                         server_default=sa.text("'pat'")))
    op.add_column("api_token", sa.Column("client_id", sa.Text, nullable=True))
    op.add_column("api_token", sa.Column("family_id", UUID(as_uuid=True), nullable=True))
    op.add_column("api_token", sa.Column("resource", sa.Text, nullable=True))
    op.add_column("api_token", _ts("rotated_at"))
    op.create_foreign_key(
        "api_token_client_id_fkey", "api_token", "oauth_client", ["client_id"], ["client_id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "api_token_kind_ck", "api_token", "kind in ('pat','oauth_access','oauth_refresh')"
    )
    op.create_index("api_token_family_idx", "api_token", ["family_id"])

    op.create_table(
        "auth_flow",
        _uuid_pk(),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("state_hash", sa.Text, nullable=False),
        sa.Column("browser_hash", sa.Text, nullable=False),
        sa.Column("nonce", sa.Text, nullable=False),
        sa.Column("code_verifier", sa.Text, nullable=False),
        sa.Column("csrf", sa.Text, nullable=False),
        sa.Column("client_id", sa.Text, sa.ForeignKey("oauth_client.client_id",
                                                      ondelete="CASCADE"), nullable=True),
        sa.Column("redirect_uri", sa.Text, nullable=True),
        sa.Column("code_challenge", sa.Text, nullable=True),
        sa.Column("scope", sa.Text, nullable=True),
        sa.Column("resource", sa.Text, nullable=True),
        sa.Column("client_state", sa.Text, nullable=True),
        sa.Column("next_url", sa.Text, nullable=True),
        sa.Column("requested_workspace", sa.Text, nullable=True),
        sa.Column("idp_subject", sa.Text, nullable=True),
        sa.Column("idp_email", sa.Text, nullable=True),
        sa.Column("idp_email_verified", sa.Boolean, nullable=False,
                  server_default=sa.text("false")),
        sa.Column("idp_name", sa.Text, nullable=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"),
                  nullable=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("code_hash", sa.Text, nullable=True),
        _ts("code_expires_at"),
        _ts("code_used_at"),
        sa.Column("family_id", UUID(as_uuid=True), nullable=True),
        _ts("created_at", now=True),
        _ts("expires_at", nullable=False),
        sa.UniqueConstraint("state_hash", name="auth_flow_state_hash_uq"),
        sa.UniqueConstraint("code_hash", name="auth_flow_code_hash_uq"),
        sa.CheckConstraint("kind in ('mcp','web')", name="auth_flow_kind_ck"),
        sa.CheckConstraint(
            "status in ('idp','workspace','consent','code','redeemed','done','denied')",
            name="auth_flow_status_ck",
        ),
    )
    op.create_index("auth_flow_expires_idx", "auth_flow", ["expires_at"])
    _admin_only("auth_flow")

    op.create_table(
        "web_session",
        _uuid_pk(),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspace.id"),
                  nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("hash", sa.Text, nullable=False),
        sa.Column("csrf", sa.Text, nullable=False),
        _ts("created_at", now=True),
        _ts("expires_at", nullable=False),
        _ts("last_seen_at"),
        _ts("revoked_at"),
        sa.UniqueConstraint("hash", name="web_session_hash_uq"),
    )
    op.create_index("web_session_workspace_idx", "web_session", ["workspace_id"])
    op.execute("ALTER TABLE web_session ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE web_session FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY web_session_rls ON web_session USING (({BYPASS}) or (workspace_id = {WS})) "
        f"WITH CHECK (({BYPASS}) or (workspace_id = {WS}))"
    )

    op.create_index(
        "app_user_ws_sub_uq", "app_user", ["workspace_id", "external_sub"], unique=True,
        postgresql_where=sa.text("external_sub is not null"),
    )


def downgrade() -> None:
    op.drop_index("app_user_ws_sub_uq", table_name="app_user")
    op.drop_table("web_session")
    op.drop_table("auth_flow")
    op.drop_index("api_token_family_idx", table_name="api_token")
    op.drop_constraint("api_token_kind_ck", "api_token", type_="check")
    op.drop_constraint("api_token_client_id_fkey", "api_token", type_="foreignkey")
    for col in ("rotated_at", "resource", "family_id", "client_id", "kind"):
        op.drop_column("api_token", col)
    op.drop_table("oauth_client")
