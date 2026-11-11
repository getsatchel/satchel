from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from satchel.llm.embeddings import HashEmbedder
from satchel.store.db import admin_session
from satchel.store.models import (
    Asset,
    ContextChunk,
    ContextEntityLink,
    ContextItem,
    ContextVersion,
    ReleaseItem,
)
from satchel.sync import ImportValidationError, import_context
from satchel.sync.sqlrefs import extract_tables
from tests.helpers import copy_example, unique_slug

EMB = HashEmbedder()


def test_import_is_idempotent_and_versions(app_engine: Engine, tmp_path: Path) -> None:
    src = copy_example(tmp_path)
    slug = unique_slug()
    r1 = import_context(src, slug, embedder=EMB, engine=app_engine)
    assert r1.counts() == {"created": 24, "updated": 0, "unchanged": 0, "deprecated": 0}
    assert r1.release_seq == 1 and r1.release_created

    r2 = import_context(src, slug, embedder=EMB, engine=app_engine)
    assert r2.counts() == {"created": 0, "updated": 0, "unchanged": 24, "deprecated": 0}
    assert not r2.release_created and r2.release_seq == 1 and r2.chunks_embedded == 0

    guide = src / "guides" / "refunds-and-returns.md"
    guide.write_text(guide.read_text() + "\n## Extra\n\nGoodwill refunds need manager approval.\n")
    r3 = import_context(src, slug, embedder=EMB, engine=app_engine)
    assert r3.updated == ["guides/refunds-and-returns.md"]
    assert r3.counts()["unchanged"] == 23 and r3.release_seq == 2 and r3.release_created

    (src / "caveats" / "guest-checkout-customers.md").unlink()
    r4 = import_context(src, slug, embedder=EMB, engine=app_engine)
    assert r4.deprecated == ["caveats/guest-checkout-customers.md"]
    assert r4.release_seq == 3

    with admin_session(engine=app_engine) as s:
        item = s.scalars(
            select(ContextItem).where(ContextItem.key == "guides/refunds-and-returns.md",
                                      ContextItem.workspace_id == r1.workspace_id)
        ).one()
        seqs = [
            v.seq
            for v in s.scalars(select(ContextVersion).where(ContextVersion.item_id == item.id))
        ]
        assert sorted(seqs) == [1, 2]
        assert item.managed_by == "git" and item.state == "published"
        dep = s.scalars(
            select(ContextItem).where(
                ContextItem.key == "caveats/guest-checkout-customers.md",
                ContextItem.workspace_id == r1.workspace_id,
            )
        ).one()
        assert dep.state == "deprecated" and dep.published_version_id is None
        in_r3 = s.scalars(
            select(ReleaseItem).where(ReleaseItem.release_id == r4.release_id)
        ).all()
        assert dep.id not in {ri.item_id for ri in in_r3}
        assert len(in_r3) == 23


def test_validation_errors_abort_import(app_engine: Engine, tmp_path: Path) -> None:
    src = copy_example(tmp_path)
    (src / "guides" / "bad.md").write_text("no frontmatter here")
    with pytest.raises(ImportValidationError) as e:
        import_context(src, unique_slug(), embedder=EMB, engine=app_engine)
    assert "guides/bad.md" in e.value.errors[0]


def test_entity_links(app_engine: Engine, tmp_path: Path) -> None:
    src = copy_example(tmp_path)
    r = import_context(src, unique_slug(), embedder=EMB, engine=app_engine)
    with admin_session(engine=app_engine) as s:
        assets = {
            a.fqn: a for a in s.scalars(select(Asset).where(Asset.workspace_id == r.workspace_id))
        }
        assert {"analytics.orders", "analytics.orders.status", "analytics.payments"} <= set(assets)
        assert assets["analytics.orders"].connection_id is not None
        assert assets["analytics.orders.status"].parent_id == assets["analytics.orders"].id

        def links(key: str) -> set[tuple[str, str]]:
            rows = s.execute(
                select(Asset.fqn, ContextEntityLink.relation)
                .join(ContextEntityLink, ContextEntityLink.asset_id == Asset.id)
                .join(ContextVersion, ContextVersion.id == ContextEntityLink.version_id)
                .join(ContextItem, ContextItem.current_version_id == ContextVersion.id)
                .where(ContextItem.key == key, ContextItem.workspace_id == r.workspace_id)
            )
            return {(a, rel) for a, rel in rows}

        assert ("analytics.orders", "defines") in links("assets/warehouse/analytics/orders.yaml")
        assert ("analytics.orders.amount", "defines") in links(
            "assets/warehouse/analytics/orders.yaml"
        )
        assert links("queries/refund-rate.yaml") == {
            ("analytics.orders", "uses"),
            ("analytics.payments", "uses"),
        }
        assert ("analytics.customers", "applies_to") in links("caveats/guest-checkout-customers.md")
        assert ("analytics.payments", "mentions") in links(
            "glossary/business-terms.yaml#refund"
        )
        # Markdown mentions of known tables are linked too.
        assert ("analytics.orders", "mentions") in links("guides/revenue-reporting.md")


def test_chunks_embedded_with_model(app_engine: Engine, tmp_path: Path) -> None:
    r = import_context(copy_example(tmp_path), unique_slug(), embedder=EMB, engine=app_engine)
    with admin_session(engine=app_engine) as s:
        rows = s.execute(
            select(ContextChunk.embedding_model, ContextChunk.embedding)
            .join(ContextVersion, ContextVersion.id == ContextChunk.version_id)
            .join(ContextItem, ContextItem.id == ContextVersion.item_id)
            .where(ContextItem.workspace_id == r.workspace_id)
        ).all()
    assert len(rows) == r.chunks_embedded >= 23
    assert {m for m, _ in rows} == {EMB.model_id}
    assert all(e is not None and len(e) == 1024 for _, e in rows)


def test_extract_tables_ignores_ctes() -> None:
    sql = """
    with r as (select * from analytics.orders), x as (select * from r)
    select * from x join payments p on true join other.schema_t using (id)
    """
    assert set(extract_tables(sql, "duckdb")) == {"analytics.orders", "payments", "other.schema_t"}
    assert extract_tables("select from from (", None) == []
