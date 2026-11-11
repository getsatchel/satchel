"""Database roles replace the old ``satchel.bypass_rls`` setting (migration 0010).

- satchel_app: workspace RLS only; setting ``satchel.bypass_rls`` grants nothing.
- satchel_worker: admitted to every RLS table by role; what ``admin_session`` connects as.
- ``satchel db bootstrap``: idempotent role/grant setup, password rotation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError
from testcontainers.postgres import PostgresContainer
from typer.testing import CliRunner

from satchel.cli.doctor import FAIL, OK, worker_check
from satchel.cli.main import app
from satchel.settings import Settings
from satchel.store.db import (
    AdminDatabaseUrlMissing,
    admin_database_url,
    admin_session,
    derived_admin_url,
    workspace_session,
)
from satchel.store.models import Base, ContextItem, Workspace
from satchel.store.roles import (
    APP_ROLE,
    ROLES,
    WORKER_ROLE,
    BootstrapError,
    bootstrap,
    ensure_local_worker,
)
from satchel.store.schema import alembic_config

IMAGE = "pgvector/pgvector:pg17"


def _as(url: str, user: str, pw: str, **kw: object) -> str:
    return make_url(url).set(username=user, password=pw, **kw).render_as_string(
        hide_password=False
    )


def _can_connect(url: str) -> bool:
    eng = create_engine(url)
    try:
        with eng.connect() as c:
            c.execute(text("select 1"))
        return True
    except OperationalError:
        return False
    finally:
        eng.dispose()


def _policies(eng: Engine) -> list[tuple[str, str, list[str], str, str | None, str | None]]:
    with eng.connect() as c:
        rows = c.execute(text(
            "select tablename, policyname, roles::text[], cmd, qual, with_check "
            "from pg_policies where schemaname = 'public'"
        )).all()
    return [(r[0], r[1], list(r[2]), r[3], r[4], r[5]) for r in rows]


def _rls_tables(eng: Engine) -> set[str]:
    with eng.connect() as c:
        return set(c.execute(text(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind = 'r' and c.relrowsecurity "
            "and c.relforcerowsecurity"
        )).scalars())


# ---- policies at head ---------------------------------------------------------------------


def test_no_policy_mentions_bypass_and_every_table_has_worker_policy(
    admin_engine: Engine,
) -> None:
    pols = _policies(admin_engine)
    assert pols
    for table, name, _, _, qual, check in pols:
        assert "bypass_rls" not in (qual or "") + (check or ""), (table, name)
    tables = _rls_tables(admin_engine)
    assert tables == set(Base.metadata.tables)  # every model table is FORCE RLS
    worker = {(t, n): (roles, cmd, q, c) for t, n, roles, cmd, q, c in pols
              if n == f"{t}_worker"}
    for t in tables:
        assert worker.get((t, f"{t}_worker")) == ([WORKER_ROLE], "ALL", "true", "true"), t
    # Workspace policies apply to every role; the pre-auth OAuth tables have none.
    rls = {t for t, n, roles, *_ in pols if n == f"{t}_rls" and roles == ["public"]}
    assert rls == tables - {"oauth_client", "auth_flow"}


# ---- security -------------------------------------------------------------------------------


def test_app_setting_bypass_rls_grants_nothing(
    app_engine: Engine, two_workspaces: dict[str, uuid.UUID]
) -> None:
    ids = two_workspaces
    with app_engine.begin() as c:
        c.execute(text("select set_config('satchel.bypass_rls', 'on', true)"))
        assert c.execute(text("select count(*) from context_item")).scalar() == 0
        assert c.execute(text("select count(*) from workspace")).scalar() == 0
        assert c.execute(text("select count(*) from oauth_client")).scalar() == 0
    with workspace_session(ids["wa"], ids["u2"], engine=app_engine) as s:
        s.execute(text("select set_config('satchel.bypass_rls', 'on', true)"))
        assert [w.id for w in s.scalars(select(Workspace))] == [ids["wa"]]
        keys = [i.key for i in s.scalars(select(ContextItem))]
        assert keys and all(k.startswith("a-") for k in keys)
        assert not any(i.scope_kind == "user" for i in s.scalars(select(ContextItem)))
    with pytest.raises(DBAPIError), workspace_session(ids["wa"], ids["u1"], engine=app_engine) as s:
        s.execute(text("select set_config('satchel.bypass_rls', 'on', true)"))
        s.add(ContextItem(workspace_id=ids["wb"], type="guide", key=f"evil-{uuid.uuid4().hex}"))
        s.flush()


def test_worker_reads_across_workspaces(
    worker_engine: Engine, two_workspaces: dict[str, uuid.UUID]
) -> None:
    ids = two_workspaces
    with worker_engine.begin() as c:
        ws = set(c.execute(text("select id from workspace")).scalars())
        assert {ids["wa"], ids["wb"]} <= ws
        # Includes another user's private memory (hygiene needs it).
        assert c.execute(
            text("select count(*) from context_item where scope_kind = 'user' and scope_id = :u"),
            {"u": ids["u1"]},
        ).scalar() == 1


def test_admin_session_runs_as_worker(
    app_engine: Engine, migrated: str, two_workspaces: dict[str, uuid.UUID]
) -> None:
    with admin_session(engine=app_engine) as s:
        assert s.execute(text("select current_user")).scalar() == WORKER_ROLE
        assert s.scalar(select(Workspace).where(Workspace.id == two_workspaces["wb"]))
    # An unpaired satchel_app engine is used as-is: admin access fails closed.
    plain = create_engine(_as(migrated, APP_ROLE, "app"))
    try:
        with admin_session(engine=plain) as s:
            assert s.execute(text("select current_user")).scalar() == APP_ROLE
            assert s.scalar(select(Workspace).where(Workspace.id == two_workspaces["wb"])) is None
    finally:
        plain.dispose()


def test_app_cannot_become_worker(app_engine: Engine, admin_engine: Engine) -> None:
    with admin_engine.connect() as c:
        for a, b in ((APP_ROLE, WORKER_ROLE), (WORKER_ROLE, APP_ROLE)):
            assert c.execute(
                text("select pg_has_role(:a, :b, 'MEMBER')"), {"a": a, "b": b}
            ).scalar() is False
        attrs = c.execute(text(
            "select rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolcanlogin "
            "from pg_roles where rolname = any(:r)"
        ), {"r": list(ROLES)}).all()
    assert {r[0]: tuple(r[1:]) for r in attrs} == {
        n: (False, False, False, False, True) for n in ROLES
    }
    with pytest.raises(DBAPIError, match="permission denied"), app_engine.begin() as c:
        c.execute(text(f"SET ROLE {WORKER_ROLE}"))


# ---- admin URL resolution -------------------------------------------------------------------


def test_admin_database_url_resolution() -> None:
    app_url = "postgresql+psycopg://satchel_app:secret@db.example:5432/satchel"
    assert admin_database_url(Settings(database_url=app_url)) == (
        "postgresql+psycopg://satchel_worker:satchel_worker@db.example:5432/satchel"
    )
    assert derived_admin_url(app_url) == admin_database_url(Settings(database_url=app_url))
    explicit = "postgresql+psycopg://satchel_worker:w@x/satchel"
    assert admin_database_url(Settings(admin_database_url=explicit, env="production")) == explicit
    with pytest.raises(AdminDatabaseUrlMissing, match="SATCHEL_ADMIN_DATABASE_URL"):
        admin_database_url(Settings(database_url=app_url, env="production"))


# ---- existing database: 0009 -> 0010, local repair, downgrade -------------------------------


@pytest.fixture(scope="module")
def fresh_pg() -> Iterator[str]:
    with PostgresContainer(IMAGE, driver="psycopg") as pg:
        yield pg.get_connection_url()


def test_existing_database_upgrade_path(fresh_pg: str, tmp_path: object) -> None:
    su = create_engine(fresh_pg, isolation_level="AUTOCOMMIT")
    try:
        # A migration user without CREATEROLE cannot invent satchel_worker: clear error.
        with su.connect() as c:
            c.execute(text("create role weak login password 'weak'"))
            c.execute(text("create database weakdb owner weak"))
        weak_db = _as(fresh_pg, "weak", "weak", database="weakdb")
        su_weak = create_engine(make_url(fresh_pg).set(database="weakdb"))
        with su_weak.begin() as c:
            c.execute(text("create extension vector"))
        su_weak.dispose()
        with pytest.raises(DBAPIError, match="satchel db bootstrap"):
            command.upgrade(alembic_config(weak_db), "head")

        # A pre-0010 local database: satchel_app from the old initdb, data written at 0009.
        with su.connect() as c:
            c.execute(text("create role satchel_app login password 'satchel_app'"))
            c.execute(text(
                "alter default privileges in schema public "
                "grant select, insert, update, delete on tables to satchel_app"
            ))
            c.execute(text(
                "alter default privileges in schema public grant usage, select on sequences "
                "to satchel_app"
            ))
        command.upgrade(alembic_config(fresh_pg), "0009")
        wid = uuid.uuid4()
        with su.connect() as c:
            c.execute(text("insert into workspace (id, slug, name) values (:i, 'old', 'Old')"),
                      {"i": wid})

        command.upgrade(alembic_config(fresh_pg), "head")
        with su.connect() as c:
            row = c.execute(text(
                "select rolcanlogin, rolbypassrls from pg_roles where rolname = 'satchel_worker'"
            )).one()
        assert tuple(row) == (False, False)  # created NOLOGIN by the migration

        app_url = _as(fresh_pg, APP_ROLE, "satchel_app")
        worker_url = derived_admin_url(app_url)
        assert not _can_connect(worker_url)
        st = Settings(database_url=app_url)
        chk = worker_check(st, local=True)
        assert chk.status == FAIL and "cannot log in" in chk.detail
        assert "ALTER ROLE satchel_worker LOGIN PASSWORD 'satchel_worker'" in chk.fix

        # The migration user is not a superuser here: no repair, doctor keeps the fix.
        assert ensure_local_worker(app_url, worker_url) == []
        # As the local superuser owner: LOGIN + dev password.
        changes = ensure_local_worker(fresh_pg, worker_url)
        assert any("LOGIN" in c for c in changes)
        assert any("set password for satchel_worker" in c for c in changes)
        assert ensure_local_worker(fresh_pg, worker_url) == []
        assert worker_check(st, local=True).status == OK

        # Existing data is visible to the worker, not to satchel_app without a workspace.
        weng = create_engine(worker_url)
        aeng = create_engine(app_url)
        try:
            with weng.connect() as c:
                assert c.execute(text("select count(*) from workspace")).scalar() == 1
            with aeng.begin() as c:
                c.execute(text("select set_config('satchel.bypass_rls', 'on', true)"))
                assert c.execute(text("select count(*) from workspace")).scalar() == 0
            with workspace_session(wid, engine=aeng) as s:
                assert s.execute(text("select count(*) from workspace")).scalar() == 1
        finally:
            weng.dispose()
            aeng.dispose()

        # Downgrade restores the setting-based policies.
        command.downgrade(alembic_config(fresh_pg), "0009")
        pols = _policies(su)
        assert not [p for p in pols if p[1].endswith("_worker")]
        assert all("bypass_rls" in (p[4] or "") for p in pols)
        command.upgrade(alembic_config(fresh_pg), "head")
    finally:
        su.dispose()


# ---- satchel db bootstrap ----------------------------------------------------------------------


def test_bootstrap_idempotent_and_rotates_passwords() -> None:
    with PostgresContainer(IMAGE, driver="psycopg") as pg:
        su = pg.get_connection_url()
        first = bootstrap(su, {APP_ROLE: "a1", WORKER_ROLE: "w1"})
        assert first.owner == make_url(su).username
        joined = "\n".join(first.changes)
        assert "created extension vector" in joined
        assert f"created role {APP_ROLE}" in joined and f"created role {WORKER_ROLE}" in joined
        assert "default privileges" in joined

        # Re-run: nothing but the (re)set passwords.
        again = bootstrap(su, {APP_ROLE: "a1", WORKER_ROLE: "w1"})
        assert again.changes == [f"set password for {APP_ROLE}", f"set password for {WORKER_ROLE}"]
        # No password given: an existing role keeps its password, nothing changes.
        assert bootstrap(su, {APP_ROLE: None, WORKER_ROLE: None}).changes == []

        # Migrations as the owner; default privileges cover the new tables.
        command.upgrade(alembic_config(su), "head")
        assert _can_connect(_as(su, APP_ROLE, "a1"))
        weng = create_engine(_as(su, WORKER_ROLE, "w1"))
        with weng.begin() as c:
            c.execute(text("insert into workspace (slug, name) values ('b', 'B')"))
        weng.dispose()

        # Rotation.
        rot = bootstrap(su, {APP_ROLE: None, WORKER_ROLE: "w2"})
        assert rot.changes == [f"set password for {WORKER_ROLE}"]
        assert not _can_connect(_as(su, WORKER_ROLE, "w1"))
        assert _can_connect(_as(su, WORKER_ROLE, "w2"))
        assert _can_connect(_as(su, APP_ROLE, "a1"))

        # Drifted attributes and membership are put back.
        eng = create_engine(su, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            c.execute(text(f"alter role {APP_ROLE} createdb"))
            c.execute(text(f"grant {WORKER_ROLE} to {APP_ROLE}"))
        fixed = bootstrap(su, {APP_ROLE: None, WORKER_ROLE: None})
        assert f"altered role {APP_ROLE}: NOCREATEDB" in fixed.changes
        assert f"revoked membership of {APP_ROLE} in {WORKER_ROLE}" in fixed.changes
        eng.dispose()

        with pytest.raises(BootstrapError, match="unknown role"):
            bootstrap(su, {"someone": "x"})


def test_bootstrap_as_createrole_owner_like_cloud_sql() -> None:
    """Cloud SQL: the admin (postgres) has CREATEROLE but is not a superuser and cannot grant
    BYPASSRLS. Bootstrap, migrate and re-run all work as that user."""
    with PostgresContainer(IMAGE, driver="psycopg") as pg:
        su = pg.get_connection_url()
        eng = create_engine(su, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            c.execute(text("create role cloudowner login password 'co' createrole createdb"))
            c.execute(text("create database cloud owner cloudowner"))
        eng.dispose()
        # cloudsqlsuperuser may create the vector extension; emulate that step as superuser.
        ext = create_engine(make_url(su).set(database="cloud"))
        with ext.begin() as c:
            c.execute(text("create extension vector"))
        ext.dispose()
        owner = _as(su, "cloudowner", "co", database="cloud")
        res = bootstrap(owner, {APP_ROLE: "a", WORKER_ROLE: "w"})
        assert res.owner == "cloudowner"
        assert any(c.startswith(f"created role {WORKER_ROLE}") for c in res.changes)
        command.upgrade(alembic_config(owner), "head")
        assert bootstrap(owner, {APP_ROLE: None, WORKER_ROLE: "w2"}).changes == [
            f"set password for {WORKER_ROLE}"
        ]
        weng = create_engine(_as(su, WORKER_ROLE, "w2", database="cloud"))
        with weng.begin() as c:
            c.execute(text("insert into workspace (slug, name) values ('c', 'C')"))
            assert c.execute(text("select count(*) from workspace")).scalar() == 1
        weng.dispose()


def test_db_bootstrap_cli(fresh_pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.delenv("SATCHEL_BOOTSTRAP_DATABASE_URL", raising=False)
    r = runner.invoke(app, ["db", "bootstrap"])
    assert r.exit_code == 1 and "SATCHEL_BOOTSTRAP_DATABASE_URL" in r.output

    monkeypatch.setenv("SATCHEL_BOOTSTRAP_DATABASE_URL", fresh_pg)
    monkeypatch.setenv("SATCHEL_APP_DB_PASSWORD", "cli-app")
    monkeypatch.setenv("SATCHEL_WORKER_DB_PASSWORD", "cli-worker")
    r = runner.invoke(app, ["db", "bootstrap"])
    assert r.exit_code == 0, r.output
    # Created or re-passworded, depending on whether an earlier test in this module (which
    # xdist may have run on another worker) already made the role.
    assert ("created role satchel_worker" in r.output
            or "set password for satchel_worker" in r.output), r.output
    assert _can_connect(_as(fresh_pg, WORKER_ROLE, "cli-worker"))
    monkeypatch.delenv("SATCHEL_APP_DB_PASSWORD")
    monkeypatch.delenv("SATCHEL_WORKER_DB_PASSWORD")
    r = runner.invoke(app, ["db", "bootstrap"])
    assert r.exit_code == 0 and "nothing to change" in r.output


def test_local_initdb_sql_creates_both_roles() -> None:
    """The packaged initdb SQL (Docker Compose / `satchel init`) + migrations as `satchel` give
    working dev logins for satchel_app and satchel_worker; the derived admin URL connects."""
    from importlib import resources

    sql = (resources.files("satchel.resources") / "initdb" / "10-app-role.sql").read_text()
    with PostgresContainer(IMAGE, username="satchel", password="satchel", dbname="satchel",
                           driver="psycopg") as pg:
        owner = pg.get_connection_url()
        eng = create_engine(owner, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            c.exec_driver_sql(sql)
        eng.dispose()
        command.upgrade(alembic_config(owner), "head")
        app_url = _as(owner, APP_ROLE, "satchel_app")
        assert ensure_local_worker(owner, derived_admin_url(app_url)) == []  # nothing to fix
        weng = create_engine(derived_admin_url(app_url))
        with weng.begin() as c:
            assert c.execute(text("select current_user")).scalar() == WORKER_ROLE
            c.execute(text("insert into workspace (slug, name) values ('l', 'L')"))
        weng.dispose()
        aeng = create_engine(app_url)
        with aeng.connect() as c:
            assert c.execute(text("select count(*) from workspace")).scalar() == 0
        aeng.dispose()
