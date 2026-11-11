"""Database roles: ``satchel db bootstrap`` and the local ``satchel_worker`` repair.

Roles (docs/deploy/database.md):
- owner: runs migrations and owns the tables (Compose: ``satchel``; Cloud SQL: ``postgres``).
- ``satchel_app``: request traffic; workspace RLS policies apply.
- ``satchel_worker``: server-internal jobs; every RLS table has a ``TO satchel_worker USING (true)``
  policy (migration 0010). No BYPASSRLS needed, which Cloud SQL cannot grant anyway.

``bootstrap`` is idempotent: it creates what is missing, fixes role attributes that differ, sets
passwords that were given, and returns one line per change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url

APP_ROLE = "satchel_app"
WORKER_ROLE = "satchel_worker"
ROLES = (APP_ROLE, WORKER_ROLE)
EXTENSIONS = ("vector",)  # no migration needs pg_trgm
TABLE_PRIVS = "SELECT, INSERT, UPDATE, DELETE"
SEQ_PRIVS = "USAGE, SELECT"
# Attribute -> (pg_roles column, wanted value).
ATTRS = {
    "LOGIN": ("rolcanlogin", True),
    "SUPERUSER": ("rolsuper", False),
    "BYPASSRLS": ("rolbypassrls", False),
    "CREATEDB": ("rolcreatedb", False),
    "CREATEROLE": ("rolcreaterole", False),
}


class BootstrapError(RuntimeError):
    pass


@dataclass
class BootstrapResult:
    owner: str
    database: str
    changes: list[str] = field(default_factory=list)


def _q(conn: Connection, ident: str) -> str:
    return str(conn.execute(text("select quote_ident(:i)"), {"i": ident}).scalar_one())


def _lit(conn: Connection, value: str) -> str:
    return str(conn.execute(text("select quote_literal(:v)"), {"v": value}).scalar_one())


def _role(conn: Connection, name: str) -> dict[str, bool] | None:
    row = conn.execute(
        text(
            "select rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
            "from pg_roles where rolname = :r"
        ),
        {"r": name},
    ).mappings().first()
    return dict(row) if row else None


def _ensure_role(conn: Connection, name: str, password: str | None, out: list[str]) -> None:
    q = _q(conn, name)
    have = _role(conn, name)
    if have is None:
        if not password:
            raise BootstrapError(f"role {name} does not exist and no password was given for it")
        attrs = " ".join(k if want else f"NO{k}" for k, (_, want) in ATTRS.items())
        conn.execute(text(f"CREATE ROLE {q} {attrs} PASSWORD {_lit(conn, password)}"))
        out.append(f"created role {name} ({attrs})")
        return
    # Only name the attributes that differ: on Cloud SQL / PG16+ a CREATEROLE (non-superuser)
    # caller may not even mention SUPERUSER or BYPASSRLS in ALTER ROLE.
    diff = [k if want else f"NO{k}" for k, (col, want) in ATTRS.items() if have[col] != want]
    if diff and "LOGIN" in diff and not password:
        raise BootstrapError(f"role {name} cannot log in and no password was given for it")
    if diff:
        conn.execute(text(f"ALTER ROLE {q} {' '.join(diff)}"))
        out.append(f"altered role {name}: {' '.join(diff)}")
    if password:
        conn.execute(text(f"ALTER ROLE {q} PASSWORD {_lit(conn, password)}"))
        out.append(f"set password for {name}")


def _ensure_not_member(conn: Connection, member: str, of: str, out: list[str]) -> None:
    found = conn.execute(
        text(
            "select 1 from pg_auth_members m join pg_roles r on r.oid = m.roleid "
            "join pg_roles u on u.oid = m.member where r.rolname = :of and u.rolname = :m"
        ),
        {"of": of, "m": member},
    ).first()
    if found:
        conn.execute(text(f"REVOKE {_q(conn, of)} FROM {_q(conn, member)}"))
        out.append(f"revoked membership of {member} in {of}")


def _ensure_grants(
    conn: Connection, name: str, owner: str, *, can_grant_existing: bool, out: list[str]
) -> None:
    """CONNECT, schema USAGE, default privileges for ``owner``'s future tables/sequences, and
    the same privileges on tables that already exist (bootstrap run after migrations)."""
    qr, qown = _q(conn, name), _q(conn, owner)
    db = str(conn.execute(text("select current_database()")).scalar_one())
    if not conn.execute(
        text("select has_database_privilege(:r, current_database(), 'CONNECT')"), {"r": name}
    ).scalar():
        conn.execute(text(f"GRANT CONNECT ON DATABASE {_q(conn, db)} TO {qr}"))
        out.append(f"granted CONNECT on database {db} to {name}")
    if not conn.execute(
        text("select has_schema_privilege(:r, 'public', 'USAGE')"), {"r": name}
    ).scalar():
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {qr}"))
        out.append(f"granted USAGE on schema public to {name}")
    for kind, objs, privs, probe in (
        ("r", "TABLES", TABLE_PRIVS, "d"),  # aclitem letters: d = DELETE
        ("S", "SEQUENCES", SEQ_PRIVS, "U"),  # U = USAGE
    ):
        if not _has_default_acl(conn, owner, kind, name, probe):
            conn.execute(text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {qown} IN SCHEMA public "
                f"GRANT {privs} ON {objs} TO {qr}"
            ))
            out.append(f"default privileges: {owner}'s new {objs.lower()} grant {privs} to {name}")
    if not can_grant_existing:
        return
    missing = conn.execute(
        text(
            "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind in ('r', 'p') and not ("
            "has_table_privilege(:r, c.oid, 'SELECT') and has_table_privilege(:r, c.oid, 'INSERT') "
            "and has_table_privilege(:r, c.oid, 'UPDATE') "
            "and has_table_privilege(:r, c.oid, 'DELETE'))"
        ),
        {"r": name},
    ).scalar_one()
    if missing:
        conn.execute(text(f"GRANT {TABLE_PRIVS} ON ALL TABLES IN SCHEMA public TO {qr}"))
        conn.execute(text(f"GRANT {SEQ_PRIVS} ON ALL SEQUENCES IN SCHEMA public TO {qr}"))
        out.append(f"granted {TABLE_PRIVS} on {missing} existing table(s) to {name}")


def _has_default_acl(conn: Connection, owner: str, kind: str, grantee: str, priv: str) -> bool:
    """Does ``owner`` have a default ACL in schema public granting ``priv`` to ``grantee``?"""
    acl = conn.execute(
        text(
            "select d.defaclacl::text[] from pg_default_acl d "
            "join pg_roles o on o.oid = d.defaclrole "
            "join pg_namespace n on n.oid = d.defaclnamespace "
            "where o.rolname = :o and n.nspname = 'public' and d.defaclobjtype = :k"
        ),
        {"o": owner, "k": kind},
    ).scalar()
    for entry in acl or []:
        who, _, rest = str(entry).partition("=")
        if who.strip('"') == grantee and priv in rest.split("/")[0]:
            return True
    return False


def bootstrap(
    url: str,
    passwords: Mapping[str, str | None],
    *,
    owner: str | None = None,
) -> BootstrapResult:
    """Create the extension, roles, grants and default privileges satchel needs.

    ``url`` connects as an admin: a superuser, or on Cloud SQL the ``postgres`` user (member of
    ``cloudsqlsuperuser``, has CREATEROLE). ``passwords`` maps role -> password (None keeps an
    existing role's password). Only the roles in ``passwords`` are managed. ``owner`` (default:
    the connecting user) is the role that runs migrations; default privileges are set for it."""
    unknown = set(passwords) - set(ROLES)
    if unknown:
        raise BootstrapError(f"unknown role(s): {', '.join(sorted(unknown))}")
    eng = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as conn:
            row = conn.execute(text("select current_user, current_database()")).one()
            me, db = str(row[0]), str(row[1])
            own = owner or me
            if _role(conn, own) is None:
                raise BootstrapError(f"owner role {own} does not exist")
            res = BootstrapResult(owner=own, database=db)
            out = res.changes

            for ext in EXTENSIONS:
                present = conn.execute(
                    text("select 1 from pg_extension where extname = :e"), {"e": ext}
                ).first()
                if not present:
                    conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {_q(conn, ext)}"))
                    out.append(f"created extension {ext}")

            for name in ROLES:
                if name in passwords:
                    _ensure_role(conn, name, passwords[name], out)
            if set(ROLES) <= set(passwords):
                # Neither role may act as the other (no SET ROLE, no inherited policies).
                _ensure_not_member(conn, APP_ROLE, WORKER_ROLE, out)
                _ensure_not_member(conn, WORKER_ROLE, APP_ROLE, out)

            for name in ROLES:
                if name in passwords:
                    _ensure_grants(conn, name, own, can_grant_existing=own == me or _is_super(conn),
                                   out=out)
            return res
    finally:
        eng.dispose()


def _is_super(conn: Connection) -> bool:
    return bool(
        conn.execute(text("select rolsuper from pg_roles where rolname = current_user")).scalar()
    )


# ---- local development repair ---------------------------------------------------------------


@dataclass(frozen=True)
class WorkerStatus:
    exists: bool
    can_login: bool


def worker_status(url: str) -> WorkerStatus:
    """Does ``satchel_worker`` exist and can it log in?

    ``url``: any role that can read pg_roles."""
    eng = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        with eng.connect() as conn:
            r = _role(conn, WORKER_ROLE)
        return WorkerStatus(r is not None, bool(r and r["rolcanlogin"]))
    finally:
        eng.dispose()


def can_manage_roles(url: str) -> bool:
    """Is the user of ``url`` a superuser (so it may create/alter satchel_worker)?"""
    eng = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        with eng.connect() as conn:
            return _is_super(conn)
    finally:
        eng.dispose()


def worker_fix_sql(password: str) -> str:
    return (
        f"ALTER ROLE {WORKER_ROLE} LOGIN PASSWORD '{password}';  "
        f"-- as a superuser, after `satchel db upgrade` created the role"
    )


def ensure_local_worker(migration_url: str, admin_url: str) -> list[str]:
    """Give the local ``satchel_worker`` LOGIN and the password in ``admin_url`` when the migration
    user is a superuser (the Docker Compose ``satchel`` owner). Returns the changes made; [] when
    nothing was needed or the caller lacks the rights (``satchel doctor`` then prints the fix)."""
    target = make_url(admin_url)
    if target.username != WORKER_ROLE or not target.password:
        return []
    st = worker_status(migration_url)
    if st.exists and st.can_login:
        return []
    if not can_manage_roles(migration_url):
        return []
    return bootstrap(migration_url, {WORKER_ROLE: str(target.password)}).changes
