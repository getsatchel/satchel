"""Models and migrations must not drift: `alembic upgrade head` on an empty database has to
produce exactly `Base.metadata` (alembic autogenerate reports no diffs)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from testcontainers.postgres import PostgresContainer

import satchel.store
from satchel.store.models import Base


def _cfg(url: str) -> Config:
    store = Path(satchel.store.__file__).parent
    cfg = Config(str(store / "alembic.ini"))
    cfg.set_main_option("script_location", str(store / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_head_matches_models_on_empty_db() -> None:
    with PostgresContainer("pgvector/pgvector:pg17", driver="psycopg") as pg:
        url = pg.get_connection_url()
        command.upgrade(_cfg(url), "head")
        eng = create_engine(url)
        try:
            with eng.connect() as conn:
                diffs = compare_metadata(
                    MigrationContext.configure(
                        conn, opts={"compare_type": True, "compare_server_default": True}
                    ),
                    Base.metadata,
                )
                tables = set(inspect(conn).get_table_names())
            assert diffs == []
            assert tables - {"alembic_version"} == set(Base.metadata.tables)
        finally:
            eng.dispose()
        # Also reversible down to empty.
        command.downgrade(_cfg(url), "base")
