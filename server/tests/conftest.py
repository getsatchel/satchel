import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from testcontainers.postgres import PostgresContainer

from satchel.store.db import admin_session, pair_admin_engine

pytest_plugins = ["satchel_adapter_sdk.pytest_plugin"]

APP_PW = "app"
WORKER_PW = "satchel_worker"  # the dev password admin_database_url derives


@pytest.fixture(autouse=True)
def _isolated_satchel_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never read the developer's real ~/.config/satchel/config.toml in tests."""
    monkeypatch.setenv("SATCHEL_CONFIG", str(tmp_path_factory.mktemp("cfg") / "config.toml"))
    # The CLI root callback exports these into os.environ (and `satchel up` sets
    # SATCHEL_PUBLIC_URL to its port); undo that after each test.
    for var in ("SATCHEL_WORKSPACE", "SATCHEL_PROFILE", "SATCHEL_ADMIN_DATABASE_URL",
                "SATCHEL_MIGRATION_DATABASE_URL", "SATCHEL_PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)
    # Fail closed: a test that forgets to point at its container must never fall through to the
    # settings default (a developer's local database on :5433). Port 1 refuses at once.
    monkeypatch.setenv("SATCHEL_DATABASE_URL", "postgresql+psycopg://nobody:nobody@127.0.0.1:1/none")
    from satchel.store.db import get_admin_engine, get_engine

    for cached in (get_engine, get_admin_engine):
        cached.cache_clear()


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("pgvector/pgvector:pg17", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated(pg_url: str) -> str:
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    import satchel.store
    from satchel.store.roles import bootstrap

    # Superusers bypass RLS, so the app and worker use plain roles, created the way production
    # does it: `satchel db bootstrap` first (the superuser is the owner), then migrations.
    bootstrap(pg_url, {"satchel_app": APP_PW, "satchel_worker": WORKER_PW})
    store = Path(satchel.store.__file__).parent
    cfg = Config(str(store / "alembic.ini"))
    cfg.set_main_option("script_location", str(store / "migrations"))
    cfg.set_main_option("sqlalchemy.url", pg_url)
    command.upgrade(cfg, "head")
    return pg_url


@pytest.fixture(scope="session")
def admin_engine(migrated: str) -> Iterator[Engine]:
    eng = create_engine(migrated)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def worker_engine(migrated: str) -> Iterator[Engine]:
    """satchel_worker: what admin_session runs on (RLS policies admit it by role)."""
    eng = create_engine(make_url(migrated).set(username="satchel_worker", password=WORKER_PW))
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def app_engine(migrated: str, worker_engine: Engine) -> Iterator[Engine]:
    """satchel_app, paired with worker_engine so admin_session(engine=app_engine) uses
    satchel_worker."""
    url = make_url(migrated).set(username="satchel_app", password=APP_PW)
    eng = pair_admin_engine(create_engine(url), worker_engine)
    yield eng
    eng.dispose()


@pytest.fixture
def two_workspaces(app_engine: Engine) -> dict[str, uuid.UUID]:
    """Seed two workspaces; user u1/u2 in A, each with a private + shared item."""
    from satchel.store.models import AppUser, ContextItem, Workspace

    ids = {k: uuid.uuid4() for k in ("wa", "wb", "u1", "u2", "ub")}
    sfx = ids["wa"].hex[:8]
    with admin_session(engine=app_engine) as s:
        s.add_all(
            [
                Workspace(id=ids["wa"], slug=f"a-{sfx}", name="A"),
                Workspace(id=ids["wb"], slug=f"b-{sfx}", name="B"),
            ]
        )
        s.flush()
        s.add_all(
            [
                AppUser(id=ids["u1"], workspace_id=ids["wa"], role="analyst"),
                AppUser(id=ids["u2"], workspace_id=ids["wa"], role="analyst"),
                AppUser(id=ids["ub"], workspace_id=ids["wb"], role="analyst"),
            ]
        )
        s.flush()
        s.add_all(
            [
                ContextItem(workspace_id=ids["wa"], type="guide", key=f"a-shared-{sfx}"),
                ContextItem(workspace_id=ids["wb"], type="guide", key=f"b-shared-{sfx}"),
                ContextItem(
                    workspace_id=ids["wa"],
                    type="guide",
                    key=f"a-u1-{sfx}",
                    scope_kind="user",
                    scope_id=ids["u1"],
                ),
            ]
        )
    return ids
