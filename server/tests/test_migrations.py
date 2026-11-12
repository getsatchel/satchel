"""Models and migrations must not drift: `alembic upgrade head` on an empty database has to
produce exactly `Base.metadata` (alembic autogenerate reports no diffs)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
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


def test_0014_deletes_user_memory_items() -> None:
    with PostgresContainer("pgvector/pgvector:pg17", driver="psycopg") as pg:
        url = pg.get_connection_url()
        command.upgrade(_cfg(url), "0013")
        eng = create_engine(url)
        try:
            with eng.begin() as conn:
                q = lambda sql, **kw: conn.execute(text(sql), kw).scalar()  # noqa: E731
                ws = q("insert into workspace (slug, name) values ('m', 'M') returning id")
                user = q("insert into app_user (workspace_id, role) values (:w, 'analyst') "
                         "returning id", w=ws)
                rel = q("insert into release (workspace_id, seq, kind) values (:w, 1, 'published') "
                        "returning id", w=ws)
                for typ, key, scope, sid in (("user_memory", "memory/u", "user", user),
                                             ("guide", "guides/a.md", "workspace", None)):
                    item = q("insert into context_item (workspace_id, type, key, scope_kind, "
                             "scope_id) values (:w, :t, :k, :sk, :sid) returning id",
                             w=ws, t=typ, k=key, sk=scope, sid=sid)
                    ver = q("insert into context_version (item_id, seq, body, content_hash) "
                            "values (:i, 1, 'x', 'h') returning id", i=item)
                    conn.execute(text("update context_item set current_version_id = :v, "
                                      "published_version_id = :v where id = :i"),
                                 {"v": ver, "i": item})
                    conn.execute(text("insert into context_chunk (version_id, ord, text) "
                                      "values (:v, 0, 'x')"), {"v": ver})
                    conn.execute(text("insert into release_item (release_id, item_id, "
                                      "version_id) values (:r, :i, :v)"),
                                 {"r": rel, "i": item, "v": ver})
            command.upgrade(_cfg(url), "0014")
            with eng.connect() as conn:
                types = conn.execute(text("select type from context_item")).scalars().all()
                counts = [conn.execute(text(f"select count(*) from {t}")).scalar()
                          for t in ("context_version", "context_chunk", "release_item")]
            assert types == ["guide"] and counts == [1, 1, 1]
        finally:
            eng.dispose()
