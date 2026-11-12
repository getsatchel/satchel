# Database roles and `satchel db bootstrap`

Satchel uses Postgres row-level security (RLS) to keep workspaces apart. Superusers and roles with
the `BYPASSRLS` attribute skip RLS, so Satchel connects with three plain roles.

| Role | Used by | Setting | RLS |
|---|---|---|---|
| owner (e.g. `satchel`, or Cloud SQL's `postgres`) | `satchel db upgrade` only | `SATCHEL_MIGRATION_DATABASE_URL` | Owns the tables. Tables use `FORCE ROW LEVEL SECURITY`, so the owner is filtered too. |
| `satchel_app` | request traffic (UI, REST, MCP) | `SATCHEL_DATABASE_URL` | Sees only the workspace in `satchel.workspace_id`, and only its own user-scoped items. |
| `satchel_worker` | server-internal jobs (`admin_session()`: workers, sync, OAuth bookkeeping, token checks) | `SATCHEL_ADMIN_DATABASE_URL` | Every RLS table has a `<table>_worker` policy `FOR ALL TO satchel_worker USING (true) WITH CHECK (true)`. |

`satchel db bootstrap` connects with a fourth URL, `SATCHEL_BOOTSTRAP_DATABASE_URL`: an admin that may
create roles.


## Why a role, not a setting

Up to migration 0009, each policy also admitted `current_setting('satchel.bypass_rls') = 'on'`, and
`admin_session()` set that value. Any connection that could run SQL as `satchel_app` could set it
too, so it was not a real boundary. Migration 0010 removes that clause from every policy. Access
now depends on which role you log in as, and `satchel_app` cannot become `satchel_worker`:

- the two roles are not members of each other, so `SET ROLE satchel_worker` fails;
- both roles are `NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`.

`satchel_worker` does not need `BYPASSRLS`, which Cloud SQL cannot grant: the policies admit it by
role.

`oauth_client` and `auth_flow` hold pre-authentication state, so only `satchel_worker` can read
them. `satchel_app` sees no rows there.

<!-- site: DatabaseRolesDiagram -->

## `satchel db bootstrap`

Run it once before the first `satchel db upgrade`. It is safe to run again.

```bash
export SATCHEL_BOOTSTRAP_DATABASE_URL='postgresql+psycopg://postgres:ADMIN_PW@/satchel?host=/cloudsql/PROJECT:REGION:INSTANCE'
export SATCHEL_APP_DB_PASSWORD=...      # from Secret Manager / a K8s Secret
export SATCHEL_WORKER_DB_PASSWORD=...
satchel db bootstrap                    # --owner ROLE if migrations run as someone else
```

It does the following, and prints one line for each thing it changes:

1. creates the `vector` extension if it is missing (no migration needs `pg_trgm`);
2. creates `satchel_app` and `satchel_worker` as `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
   NOCREATEROLE`. If a role already exists, it resets any attribute that has drifted and sets the
   password. It removes any membership between the two roles;
3. grants `CONNECT` on the database and `USAGE` on schema `public` to both roles;
4. runs `ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public`, which grants `SELECT, INSERT,
   UPDATE, DELETE` on new tables and `USAGE, SELECT` on new sequences to both roles. The owner is
   the bootstrap user, or the role you pass with `--owner`;
5. if you run it after migrations, it grants the same privileges on the tables that already
   exist.

An unset password variable keeps the existing role's password. A role that does not exist yet, or
that cannot log in, needs its password.

Postgres can't show whether a password changed, so bootstrap always reports
`set password for <role>` when you pass one. If the server logs DDL (`log_statement = ddl` or
`all`), the password appears in the log. Turn that off for the bootstrap session, or rotate the
password through Cloud SQL / your platform instead.

### Order of operations

1. `satchel db bootstrap` (admin URL)
2. `satchel db upgrade` (owner URL)
3. `satchel serve api` / `satchel worker` (app and worker URLs)

If you run migration 0010 before bootstrap, it creates `satchel_worker` itself as `NOLOGIN`, so the
migration does not fail. Bootstrap later sets `LOGIN` and the password. To create the role, the
migration user needs `CREATEROLE` (or superuser). If it has neither, the migration stops with
`role satchel_worker does not exist ... Run satchel db bootstrap first`.

## Cloud SQL

- The built-in `postgres` user is a member of `cloudsqlsuperuser`. It has `CREATEROLE` and
  `CREATEDB`, but it is not a real superuser and has no `BYPASSRLS`. That is why the policies grant
  access to `satchel_worker` by role, not by attribute.
- Use `postgres` (or another `cloudsqlsuperuser` member) for both bootstrap and migrations. The
  owner is then the bootstrap user, and you don't need `--owner`.
- `cloudsqlsuperuser` may create the `vector` extension, so bootstrap can create it.
- From Postgres 16, a non-superuser with `CREATEROLE` may alter only roles it created (it holds
  `ADMIN OPTION` on them). Create the roles with bootstrap or migration 0010 as `postgres`, not by
  hand as another user. Bootstrap names only the attributes that differ, because a `CREATEROLE`
  user may not mention `SUPERUSER` or `BYPASSRLS` in `ALTER ROLE` at all.
- URLs use the unix-socket form:
  `postgresql+psycopg://USER:PASS@/satchel?host=/cloudsql/PROJECT:REGION:INSTANCE`.

## Environment

| Variable | Needed by |
|---|---|
| `SATCHEL_DATABASE_URL` | web, worker (`satchel_app`) |
| `SATCHEL_ADMIN_DATABASE_URL` | web, worker (`satchel_worker`). Required when `SATCHEL_ENV=production`, and `serve` refuses to start without it. In development, when it is unset, Satchel uses `SATCHEL_DATABASE_URL` with user and password `satchel_worker` and logs a warning once. |
| `SATCHEL_MIGRATION_DATABASE_URL` | `satchel db upgrade` (owner) |
| `SATCHEL_BOOTSTRAP_DATABASE_URL`, `SATCHEL_APP_DB_PASSWORD`, `SATCHEL_WORKER_DB_PASSWORD` | `satchel db bootstrap` only |

## Rotating passwords

1. Put the new password in your secret store, under a new version.
2. Run `satchel db bootstrap` with only the variable for the role you rotate (for example
   `SATCHEL_WORKER_DB_PASSWORD`). The other role keeps its password.
3. Restart or roll out the web and worker services so they read the new
   `SATCHEL_ADMIN_DATABASE_URL` / `SATCHEL_DATABASE_URL`. Connections that are already open stay up
   until they close. New connections with the old password fail.

## Local development

`satchel init` and `deploy/compose` start Postgres with `initdb/10-app-role.sql`. That script
creates `satchel_app` / `satchel_app` and `satchel_worker` / `satchel_worker` with dev passwords. `satchel init`
writes `admin_database_url` into the config profile.

**Existing local databases** (created before migration 0010) have no `satchel_worker` login.
`satchel upgrade`, `satchel db upgrade`, `satchel up` and `satchel init` fix it themselves when the migration
URL points at a local superuser, which is the default for the Docker Postgres (`satchel`). They
create or alter `satchel_worker` with `LOGIN` and the dev password, and set its grants. `satchel doctor`
has a **Worker role** check that prints the exact fix if anything is still missing:

```sql
-- as a superuser, after `satchel db upgrade` created the role
ALTER ROLE satchel_worker LOGIN PASSWORD 'satchel_worker';
```
