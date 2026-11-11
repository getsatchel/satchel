"""Replace the ``satchel.bypass_rls`` setting with the ``satchel_worker`` database role.

Before: every policy admitted ``current_setting('satchel.bypass_rls') = 'on'``. Any connection
that could run SQL as ``satchel_app`` could set that, so it was not a real boundary.

After: the workspace/user policies (``<table>_rls``) apply to every role and no longer mention
the setting, and each RLS table gets ``<table>_worker FOR ALL TO satchel_worker USING (true)``.
Server-internal jobs (``admin_session``) connect as ``satchel_worker``. The role needs no
BYPASSRLS attribute (Cloud SQL cannot grant it); the policies grant it access by role.
``oauth_client`` and ``auth_flow`` used to admit only the setting, so they keep only the
worker policy: ``satchel_app`` sees no rows there.

The role is created here as NOLOGIN when missing, so the migration works before
``satchel db bootstrap`` ran; bootstrap (or the local initdb) sets LOGIN and the password.
Creating it needs a superuser (Docker Compose: the ``satchel`` owner) or CREATEROLE (Cloud SQL:
the ``postgres`` owner has CREATEROLE but not BYPASSRLS).

Frozen: explicit DDL with literal policy expressions, independent of earlier revisions.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

WS = "nullif(current_setting('satchel.workspace_id', true), '')::uuid"
USR = "nullif(current_setting('satchel.user_id', true), '')::uuid"
BYPASS = "coalesce(current_setting('satchel.bypass_rls', true), '') = 'on'"
ROLE = "satchel_worker"

# Every RLS table and its workspace/user expression (None = no non-worker access).
POLICIES: dict[str, str | None] = {
    "workspace": f"id = {WS}",
    "app_user": f"workspace_id = {WS}",
    "domain": f"workspace_id = {WS}",
    "connection": f"workspace_id = {WS}",
    "asset": f"workspace_id = {WS}",
    "context_item": f"workspace_id = {WS} and (scope_kind <> 'user' or scope_id = {USR})",
    "release": f"workspace_id = {WS}",
    "session": f"workspace_id = {WS}",
    "signal": f"workspace_id = {WS}",
    "audit_event": f"workspace_id = {WS}",
    "context_version": "exists (select 1 from context_item p where p.id = item_id)",
    "context_entity_link": "exists (select 1 from context_version p where p.id = version_id)",
    "context_chunk": "exists (select 1 from context_version p where p.id = version_id)",
    "release_item": "exists (select 1 from release p where p.id = release_id)",
    "tool_call": "exists (select 1 from session p where p.id = session_id)",
    "served_context": "exists (select 1 from tool_call p where p.id = tool_call_id)",
    "feedback": "exists (select 1 from session p where p.id = session_id)",
    "api_token": f"workspace_id = {WS}",
    "query_result": f"workspace_id = {WS}",
    "oauth_client": None,
    "auth_flow": None,
    "web_session": f"workspace_id = {WS}",
    "suggestion": f"workspace_id = {WS}",
    "suggestion_change": "exists (select 1 from suggestion p where p.id = suggestion_id)",
    "suggestion_signal": "exists (select 1 from suggestion p where p.id = suggestion_id)",
    "eval_suite": f"workspace_id = {WS}",
    "eval_run": f"workspace_id = {WS}",
    "eval_case_result": f"workspace_id = {WS}",
    "context_source": f"workspace_id = {WS}",
}

ENSURE_ROLE = f"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
    IF NOT EXISTS (
      SELECT 1 FROM pg_roles WHERE rolname = current_user AND (rolsuper OR rolcreaterole)
    ) THEN
      RAISE EXCEPTION 'role {ROLE} does not exist and % cannot create it (needs CREATEROLE)',
        current_user
        USING HINT = 'Run `satchel db bootstrap` first, or grant CREATEROLE to the migration user.';
    END IF;
    CREATE ROLE {ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END
$$
"""

GRANT_CONNECT = f"""
DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO {ROLE}', current_database());
END
$$
"""

REVOKE_CONNECT = f"""
DO $$
BEGIN
  EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {ROLE}', current_database());
END
$$
"""


def upgrade() -> None:
    op.execute(ENSURE_ROLE)
    op.execute(GRANT_CONNECT)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    # Same privileges satchel_app gets (initdb / bootstrap), now and for later migrations.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {ROLE}"
    )

    for table, expr in POLICIES.items():
        op.execute(f"DROP POLICY IF EXISTS {table}_rls ON {table}")
        if expr is not None:
            op.execute(
                f"CREATE POLICY {table}_rls ON {table} USING ({expr}) WITH CHECK ({expr})"
            )
        op.execute(
            f"CREATE POLICY {table}_worker ON {table} FOR ALL TO {ROLE} "
            "USING (true) WITH CHECK (true)"
        )


def downgrade() -> None:
    for table, expr in POLICIES.items():
        op.execute(f"DROP POLICY IF EXISTS {table}_worker ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_rls ON {table}")
        if expr is None:
            op.execute(
                f"CREATE POLICY {table}_rls ON {table} USING ({BYPASS}) WITH CHECK ({BYPASS})"
            )
        else:
            op.execute(
                f"CREATE POLICY {table}_rls ON {table} USING (({BYPASS}) or ({expr})) "
                f"WITH CHECK (({BYPASS}) or ({expr}))"
            )

    # The role itself is cluster-wide and may be managed by `satchel db bootstrap`: keep it.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM {ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {ROLE}"
    )
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
    op.execute(REVOKE_CONNECT)
