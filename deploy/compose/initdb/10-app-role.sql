-- Runs once, on first start of an empty data volume, as the POSTGRES_USER superuser.
-- The app must connect as plain roles: superusers and BYPASSRLS roles skip row-level security.
-- Migrations run as POSTGRES_USER (`satchel`, the table owner). Request traffic runs as `satchel_app`
-- (workspace RLS); server-internal jobs run as `satchel_worker`, which every table's
-- `<table>_worker` policy admits by role (migration 0010). Neither role is a member of the other.
-- Dev passwords only; change them (and SATCHEL_DATABASE_URL / SATCHEL_ADMIN_DATABASE_URL) for anything
-- shared, or use `satchel db bootstrap` (docs/deploy/database.md).
CREATE ROLE satchel_app LOGIN PASSWORD 'satchel_app' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE ROLE satchel_worker LOGIN PASSWORD 'satchel_worker' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

GRANT CONNECT ON DATABASE satchel TO satchel_app, satchel_worker;
GRANT USAGE ON SCHEMA public TO satchel_app, satchel_worker;

-- Tables and sequences created by later migrations (run as `satchel`) get the same grants.
ALTER DEFAULT PRIVILEGES FOR ROLE satchel IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO satchel_app, satchel_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE satchel IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO satchel_app, satchel_worker;
