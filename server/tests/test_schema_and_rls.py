import uuid

import pytest
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import DBAPIError

from satchel.store.db import workspace_session
from satchel.store.models import Base, ContextItem, Workspace


def test_tables_and_extension_exist(admin_engine: Engine) -> None:
    names = set(inspect(admin_engine).get_table_names())
    assert set(Base.metadata.tables) <= names
    with admin_engine.connect() as c:
        assert c.execute(text("select 1 from pg_extension where extname='vector'")).scalar() == 1
        idx = {
            r[0]
            for r in c.execute(
                text("select indexname from pg_indexes where tablename='context_chunk'")
            )
        }
    assert {"context_chunk_embedding_hnsw", "context_chunk_tsv_gin"} <= idx


def test_rls_isolates_workspaces(app_engine: Engine, two_workspaces: dict[str, uuid.UUID]) -> None:
    ids = two_workspaces
    with workspace_session(ids["wa"], ids["u2"], engine=app_engine) as s:
        keys = [i.key for i in s.scalars(select(ContextItem))]
        assert keys and all(k.startswith("a-") for k in keys)
        assert [w.id for w in s.scalars(select(Workspace))] == [ids["wa"]]
    with workspace_session(ids["wb"], ids["ub"], engine=app_engine) as s:
        assert all(i.key.startswith("b-") for i in s.scalars(select(ContextItem)))


def test_rls_blocks_cross_workspace_write(
    app_engine: Engine, two_workspaces: dict[str, uuid.UUID]
) -> None:
    ids = two_workspaces
    with pytest.raises(DBAPIError), workspace_session(ids["wa"], ids["u1"], engine=app_engine) as s:
        s.add(ContextItem(workspace_id=ids["wb"], type="guide", key=f"evil-{uuid.uuid4().hex}"))
        s.flush()


def test_no_context_sees_nothing(app_engine: Engine, two_workspaces: dict[str, uuid.UUID]) -> None:
    with app_engine.begin() as c:
        assert c.execute(text("select count(*) from context_item")).scalar() == 0


def test_user_scoped_items_private(
    app_engine: Engine, two_workspaces: dict[str, uuid.UUID]
) -> None:
    ids = two_workspaces
    with workspace_session(ids["wa"], ids["u1"], engine=app_engine) as s:
        assert any(i.scope_kind == "user" for i in s.scalars(select(ContextItem)))
    with workspace_session(ids["wa"], ids["u2"], engine=app_engine) as s:
        assert not any(i.scope_kind == "user" for i in s.scalars(select(ContextItem)))
