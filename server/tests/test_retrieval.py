import uuid
from pathlib import Path

import pytest
from sqlalchemy import Engine, Select, func, insert, select, text
from sqlalchemy.orm import Session as OrmSession

from satchel.evals.retrieval import load_cases, run_retrieval_eval
from satchel.llm.embeddings import HashEmbedder
from satchel.retrieval import build_context_pack, search_context
from satchel.retrieval.search import (
    CANDIDATES,
    HNSW_EF_SEARCH,
    HNSW_MAX_SCAN_TUPLES,
    _pgvector_ver,
    _vector,
    tune_vector_scan,
)
from satchel.store.db import admin_session, workspace_session
from satchel.store.models import (
    AppUser,
    ContextChunk,
    ContextItem,
    ContextVersion,
    Feedback,
    Release,
    ReleaseItem,
    ServedContext,
    Session,
    ToolCall,
    Workspace,
)
from satchel.sync import import_context
from tests.helpers import EXAMPLE, copy_example, unique_slug

EMB = HashEmbedder()


@pytest.fixture(scope="module")
def jaffle(app_engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> uuid.UUID:
    src = copy_example(tmp_path_factory.mktemp("jaffle"))
    return import_context(src, unique_slug("jaffle"), embedder=EMB, engine=app_engine).workspace_id


def test_search_revenue_top3(app_engine: Engine, jaffle: uuid.UUID) -> None:
    with workspace_session(jaffle, engine=app_engine) as s:
        hits = search_context(s, "revenue last month", k=5, embedder=EMB)
    top3 = [h.key for h in hits[:3]]
    assert "glossary/business-terms.yaml#revenue" in top3
    assert "queries/monthly-revenue.yaml" in top3
    assert hits[0].tag.startswith("[ctx:") and hits[0].tag.endswith("@v1]")


def test_search_type_filter_and_exact_synonym(app_engine: Engine, jaffle: uuid.UUID) -> None:
    with workspace_session(jaffle, engine=app_engine) as s:
        hits = search_context(s, "what is AOV", types=["glossary_term"], k=3, embedder=EMB)
    assert hits[0].key == "glossary/business-terms.yaml#average-order-value"
    assert "glossary" in hits[0].sources
    assert {h.type for h in hits} == {"glossary_term"}


def test_asset_expansion_pulls_docs_and_caveats(app_engine: Engine, jaffle: uuid.UUID) -> None:
    with workspace_session(jaffle, engine=app_engine) as s:
        hits = search_context(s, "anything about the payments table", k=10, embedder=EMB)
        # Leading-character typo ('xustomers' vs 'customers', difflib ratio 0.889 >= 0.85)
        # still matches via the suffix slice in _asset_matches.
        typo_hits = search_context(s, "how many xustomers do we have", k=10, embedder=EMB)
    by_key = {h.key: h for h in hits}
    assert "asset" in by_key["assets/warehouse/analytics/payments.yaml"].sources
    assert "asset" in by_key["caveats/refunds-pre-2023.md"].sources
    typo_by_key = {h.key: h for h in typo_hits}
    assert "asset" in typo_by_key["assets/warehouse/analytics/customers.yaml"].sources


def test_retrieval_eval_hash_baseline(app_engine: Engine, jaffle: uuid.UUID) -> None:
    cases = load_cases(EXAMPLE / "retrieval_eval.yaml")
    assert len(cases) >= 20
    with workspace_session(jaffle, engine=app_engine) as s:
        res = run_retrieval_eval(s, cases, EMB, k=5)
    assert res.recall_at_k >= 0.9


def test_pack_budget_and_order(app_engine: Engine, jaffle: uuid.UUID) -> None:
    order = ["workspace_rule", "user_memory", "caveat", "glossary_term", "golden_query",
             "asset_doc", "guide", "semantic_model"]
    with workspace_session(jaffle, engine=app_engine) as s:
        big = build_context_pack(s, "revenue last month", None, embedder=EMB, budget_tokens=4000)
        small = build_context_pack(s, "revenue last month", None, embedder=EMB, budget_tokens=500)
    assert big.used_tokens <= 4000 and small.used_tokens <= 500
    assert len(small.entries) < len(big.entries)
    for pack in (big, small):
        idx = [order.index(e.type) for e in pack.entries]
        assert idx == sorted(idx)
        assert pack.entries[0].type == "workspace_rule"
        assert all(e.tag in pack.text for e in pack.entries)
    assert "[ctx:glossary/business-terms.yaml#revenue@v1]" in big.text


def test_rls_isolates_search_between_workspaces(app_engine: Engine, tmp_path: Path) -> None:
    def mk(slug_marker: str) -> uuid.UUID:
        d = tmp_path / slug_marker
        (d / "guides").mkdir(parents=True)
        (d / "guides" / f"{slug_marker}.md").write_text(
            f"---\nname: {slug_marker}\ndescription: secret {slug_marker} handbook\n---\n"
            f"The {slug_marker} zanzibar procedure is confidential.\n"
        )
        slug = unique_slug(slug_marker)
        return import_context(d, slug, embedder=EMB, engine=app_engine).workspace_id

    wa, wb = mk("alpha"), mk("bravo")
    with workspace_session(wa, engine=app_engine) as s:
        keys = {h.key for h in search_context(s, "zanzibar procedure", k=10, embedder=EMB)}
        pack = build_context_pack(s, "zanzibar procedure", None, embedder=EMB)
    assert keys == {"guides/alpha.md"}
    assert "bravo" not in pack.text
    with workspace_session(wb, engine=app_engine) as s:
        assert {h.key for h in search_context(s, "zanzibar", k=10, embedder=EMB)} == {
            "guides/bravo.md"
        }


def test_user_memory_private_in_pack(app_engine: Engine, jaffle: uuid.UUID) -> None:
    u1, u2 = uuid.uuid4(), uuid.uuid4()
    with admin_session(engine=app_engine) as s:
        s.add_all([AppUser(id=u, workspace_id=jaffle, role="analyst") for u in (u1, u2)])
        s.flush()
        item = ContextItem(
            workspace_id=jaffle, type="user_memory", key=f"memory/{u1}", scope_kind="user",
            scope_id=u1, state="published", managed_by="ui",
        )
        s.add(item)
        s.flush()
        v = ContextVersion(item_id=item.id, seq=1, body="I always report in USD thousands.",
                           content_hash="x")
        s.add(v)
        s.flush()
        item.current_version_id = item.published_version_id = v.id
    with workspace_session(jaffle, u1, engine=app_engine) as s:
        p1 = build_context_pack(s, "revenue", u1, embedder=EMB)
    with workspace_session(jaffle, u2, engine=app_engine) as s:
        p2 = build_context_pack(s, "revenue", u2, embedder=EMB)
        # Even if u2 asks for u1's memory, RLS hides it.
        p3 = build_context_pack(s, "revenue", u1, embedder=EMB)
    assert "USD thousands" in p1.text
    assert "USD thousands" not in p2.text and "USD thousands" not in p3.text


def test_served_context_logged(app_engine: Engine, jaffle: uuid.UUID) -> None:
    with admin_session(engine=app_engine) as s:
        sess = Session(workspace_id=jaffle, question="q")
        s.add(sess)
        s.flush()
        tc = ToolCall(session_id=sess.id, tool="get_context_pack")
        s.add(tc)
        s.flush()
        tcid = tc.id
    with workspace_session(jaffle, engine=app_engine) as s:
        pack = build_context_pack(s, "refund rate", None, embedder=EMB, tool_call_id=tcid)
    with workspace_session(jaffle, engine=app_engine) as s:
        rows = s.execute(
            select(ServedContext.version_id, ServedContext.rank)
            .where(ServedContext.tool_call_id == tcid)
            .order_by(ServedContext.rank)
        ).all()
    assert [r[0] for r in rows] == [e.version_id for e in pack.entries]
    assert [r[1] for r in rows] == list(range(1, len(rows) + 1))


def test_release_pinning(app_engine: Engine, tmp_path: Path) -> None:
    src = copy_example(tmp_path)
    slug = unique_slug()
    r1 = import_context(src, slug, embedder=EMB, engine=app_engine)
    g = src / "guides" / "payments-reconciliation.md"
    g.write_text(g.read_text() + "\n## Disputes\n\nChargebacks arrive as quokka notices.\n")
    r2 = import_context(src, slug, embedder=EMB, engine=app_engine)
    assert r2.release_id != r1.release_id
    with workspace_session(r1.workspace_id, engine=app_engine) as s:
        assert (
            s.scalar(select(func.count()).select_from(Release).where(Release.kind == "published"))
            == 2
        )
        old = search_context(s, "quokka", k=3, embedder=EMB, release_id=r1.release_id)
        new = search_context(s, "quokka", k=3, embedder=EMB)
    assert all(h.seq == 1 for h in old if h.key == "guides/payments-reconciliation.md")
    pay = "guides/payments-reconciliation.md"
    assert not any(h.key == pay and "bm25" in h.sources for h in old)
    assert any(h.key == pay and "bm25" in h.sources for h in new)


def test_human_feedback_moves_the_helpfulness_prior(app_engine: Engine, jaffle: uuid.UUID) -> None:
    """A person's 👍/👎 on a result (result widget) votes on the context served earlier in that
    session, weighing more than agent-relayed feedback."""
    q = "revenue last month"
    with workspace_session(jaffle, engine=app_engine) as s:
        base = {h.key: h.score for h in search_context(s, q, k=10, embedder=EMB)}
    with admin_session(engine=app_engine) as s:
        uid = AppUser(workspace_id=jaffle, role="analyst", email=f"{uuid.uuid4()}@x.test")
        s.add(uid)
        sess = Session(workspace_id=jaffle, question=q, user_id=None)
        s.add(sess)
        s.flush()
        pack_call = ToolCall(session_id=sess.id, tool="get_context_pack")
        s.add(pack_call)
        s.flush()
        sess_id, pack_id, user_id = sess.id, pack_call.id, uid.id
    with workspace_session(jaffle, engine=app_engine) as s:
        build_context_pack(s, q, None, embedder=EMB, tool_call_id=pack_id)
    with admin_session(engine=app_engine) as s:
        sql_call = ToolCall(session_id=sess_id, tool="run_sql")
        s.add(sql_call)
        s.flush()
        s.add(Feedback(session_id=sess_id, tool_call_id=sql_call.id, user_id=user_id,
                       rating=-1, source="human"))
    with workspace_session(jaffle, engine=app_engine) as s:
        after = {h.key: h.score for h in search_context(s, q, k=10, embedder=EMB)}
    served = [k for k in base if k in after]
    assert served and all(after[k] < base[k] for k in served[:3])


# ---- vector candidate generation on a large, filtered library ---------------------------------


@pytest.fixture(scope="module")
def big_library(app_engine: Engine) -> tuple[uuid.UUID, uuid.UUID, list[float]]:
    """200 items x 25 random-vector chunks (5000). Release 1 holds items 0-149, release 2 holds
    items 100-199: a filter on either keeps ~75% of the chunks, so the planner favours the HNSW
    index, and the filter still drops some of what the index returns."""
    import numpy as np

    rng = np.random.default_rng(7)
    dim = int(ContextChunk.embedding.type.dim)  # type: ignore[attr-defined]
    wid = uuid.uuid4()
    with admin_session(engine=app_engine) as s:
        s.add(Workspace(id=wid, slug=unique_slug(), name="big"))
        s.flush()
        rels = [Release(workspace_id=wid, seq=n, kind="published") for n in (1, 2)]
        s.add_all(rels)
        s.flush()
        for n in range(200):
            item = ContextItem(workspace_id=wid, type="guide", key=f"guides/g{n}")
            s.add(item)
            s.flush()
            ver = ContextVersion(item_id=item.id, seq=1, body=str(n), content_hash=str(n))
            s.add(ver)
            s.flush()
            for rel, lo, hi in ((rels[0], 0, 150), (rels[1], 100, 200)):
                if lo <= n < hi:
                    s.add(ReleaseItem(release_id=rel.id, item_id=item.id, version_id=ver.id))
            vecs = rng.normal(size=(25, dim)).astype("float32")
            s.execute(
                insert(ContextChunk),
                [
                    {"version_id": ver.id, "ord": i, "text": f"g{n} chunk {i}",
                     "embedding": v.tolist(), "embedding_model": EMB.model_id}
                    for i, v in enumerate(vecs)
                ],
            )
        for table in ("context_chunk", "release_item", "context_version", "context_item"):
            s.execute(text(f"analyze {table}"))
        rid = rels[0].id
    return wid, rid, rng.normal(size=dim).astype("float32").tolist()


def _force_hnsw(s: OrmSession) -> None:
    """Planner statistics depend on whatever else the shared test database holds; with seq scans
    and explicit sorts off the only way to satisfy ORDER BY ... LIMIT is the HNSW index."""
    s.execute(text("set local enable_seqscan = off"))
    s.execute(text("set local enable_sort = off"))


def _vector_stmt(rid: uuid.UUID, q: list[float]) -> Select[tuple[int]]:
    return (
        select(ContextChunk.id)
        .join(ReleaseItem, ReleaseItem.version_id == ContextChunk.version_id)
        .where(ReleaseItem.release_id == rid, ContextChunk.embedding_model == EMB.model_id)
        .order_by(ContextChunk.embedding.cosine_distance(q), ContextChunk.id)
        .limit(CANDIDATES)
    )


def test_vector_returns_full_candidate_list_for_filtered_release(
    app_engine: Engine, big_library: tuple[uuid.UUID, uuid.UUID, list[float]]
) -> None:
    wid, rid, q = big_library
    with workspace_session(wid, engine=app_engine) as s:
        _force_hnsw(s)
        # Without the tuning the index scan stops at ef_search=40 rows before the release filter.
        unfixed = len(s.execute(_vector_stmt(rid, q)).all())
        got = _vector(s, rid, q, EMB.model_id, None)
        assert len(got) == CANDIDATES
        assert unfixed < CANDIDATES
        assert s.scalar(text("show hnsw.ef_search")) == str(HNSW_EF_SEARCH)
        if _pgvector_ver(s)[:2] >= (0, 8):
            assert s.scalar(text("show hnsw.iterative_scan")) == "relaxed_order"
            assert s.scalar(text("show hnsw.max_scan_tuples")) == str(HNSW_MAX_SCAN_TUPLES)
    with workspace_session(wid, engine=app_engine) as s:  # SET LOCAL does not outlive the txn
        assert s.scalar(text("show hnsw.ef_search")) == "40"


def test_vector_query_still_plans_an_hnsw_index_scan(
    app_engine: Engine, big_library: tuple[uuid.UUID, uuid.UUID, list[float]]
) -> None:
    wid, rid, q = big_library
    with workspace_session(wid, engine=app_engine) as s:
        _force_hnsw(s)
        tune_vector_scan(s)
        plan = "\n".join(
            str(r[0])
            for r in s.execute(
                text(
                    "explain select cc.id from context_chunk cc "
                    "join release_item ri on ri.version_id = cc.version_id "
                    "where ri.release_id = :rid and cc.embedding_model = :m "
                    "order by cc.embedding <=> cast(:q as vector) limit 50"
                ),
                {"rid": rid, "m": EMB.model_id, "q": str(q)},
            )
        )
        import re as _re

        plan = _re.sub(r"'\[[^\]]*\]'", "'[...]'", plan)
        assert "context_chunk_embedding_hnsw" in plan, plan

