"""M4 curation loop: chat providers, signal embedding, gap detection thresholds and attaching,
hygiene checks, LLM drafting (valid/invalid/dismiss feedback), golden-query promotion, the
suggestion API (roles, redaction, accept -> draft -> preview -> publish, git targets) and
running with no LLM."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, update

from satchel.api.app import create_app
from satchel.curation.config import CurationConfig, load_config
from satchel.curation.gaps import detect_gaps
from satchel.curation.hygiene import run_hygiene
from satchel.curation.run import run_curation
from satchel.curation.signal_text import embed_pending_signals
from satchel.llm.chat import (
    AnthropicChat,
    ChatError,
    FakeChatProvider,
    OpenAICompatChat,
    make_chat_provider,
    parse_json_object,
)
from satchel.llm.embeddings import HashEmbedder
from satchel.settings import Settings
from satchel.store.db import admin_session, workspace_session
from satchel.store.models import (
    AppUser,
    Asset,
    AuditEvent,
    ContextEntityLink,
    ContextItem,
    Feedback,
    Job,
    Signal,
    Suggestion,
    SuggestionChange,
    ToolCall,
    Workspace,
)
from satchel.store.models import Session as SessionRow
from satchel.sync import import_context
from tests.helpers import copy_example, unique_slug

EMB = HashEmbedder()
ROLES = ["admin", "curator", "analyst", "explorer"]
CHURN = "there is no definition of customer churn so I could not report the churn rate"
REFUND = "refund rate should exclude pending returns but the guide never says so"


# ---- fixtures / seeding ----------------------------------------------------------------------


class Ws:
    """A bare workspace with users (no imported context)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.slug = unique_slug("cur")
        self.id = uuid.uuid4()
        self.users = {r: uuid.uuid4() for r in [*ROLES, "u2", "u3"]}
        with admin_session(engine=engine) as s:
            s.add(Workspace(id=self.id, slug=self.slug, name=self.slug))
            s.flush()
            for name, uid in self.users.items():
                s.add(AppUser(id=uid, workspace_id=self.id, role="analyst" if name in
                              ("u2", "u3") else name, email=f"{name}@{self.slug}.test",
                              display_name=name))

    def h(self, role: str) -> dict[str, str]:
        return {"X-Satchel-Workspace": self.slug, "X-Satchel-User": str(self.users[role])}

    def client(self, chat: FakeChatProvider | None = None) -> TestClient:
        return TestClient(create_app(self.engine, EMB, settings=Settings(dev_auth=True),
                                     chat=chat, mcp=False))

    def session(self, user: str, question: str, *, sensitive: bool = False,
                age_days: float = 0) -> uuid.UUID:
        sid = uuid.uuid4()
        with admin_session(engine=self.engine) as s:
            s.add(SessionRow(id=sid, workspace_id=self.id, user_id=self.users[user],
                             client="claude-desktop", question=question, sensitive=sensitive,
                             started_at=datetime.now(UTC) - timedelta(days=age_days)))
        return sid

    def signal(self, user: str | None, sid: uuid.UUID | None, kind: str, payload: dict[str, Any],
               *, source: str = "agent", embed: bool = True, age_days: float = 0,
               assets: list[uuid.UUID] | None = None) -> int:
        with admin_session(engine=self.engine) as s:
            sg = Signal(workspace_id=self.id, session_id=sid,
                        user_id=self.users[user] if user else None, kind=kind, source=source,
                        payload=payload, asset_ids=assets or [],
                        created_at=datetime.now(UTC) - timedelta(days=age_days))
            s.add(sg)
            s.flush()
            if embed:
                sg.embedding = EMB.embed([json.dumps(payload)])[0]
            return sg.id

    def gap(self, user: str, text: str, **kw: Any) -> int:
        sid = self.session(user, "what is churn?")
        return self.signal(user, sid, "gap_flagged", {"what_was_missing": text, "why": "x"}, **kw)

    def churn_cluster(self, users: tuple[str, ...] = ("analyst", "u2", "u3")) -> None:
        for u in users:
            self.gap(u, CHURN)

    def detect(self, cfg: CurationConfig | None = None) -> Any:
        with workspace_session(self.id, engine=self.engine) as s:
            return detect_gaps(s, self.id, cfg or CurationConfig())

    def suggestions(self) -> list[Suggestion]:
        with workspace_session(self.id, engine=self.engine) as s:
            rows = list(s.scalars(select(Suggestion).order_by(Suggestion.created_at)))
            s.expunge_all()
            return rows


@pytest.fixture
def ws(app_engine: Engine) -> Ws:
    return Ws(app_engine)


@pytest.fixture
def jaffle(app_engine: Engine, tmp_path: Path) -> Ws:
    """A workspace with the Jaffle Shop context imported (all items git-managed)."""
    w = Ws(app_engine)
    import_context(copy_example(tmp_path), w.slug, embedder=EMB, engine=app_engine)
    with admin_session(engine=app_engine) as s:  # the import created its own workspace by slug
        imported = s.scalars(select(Workspace).where(Workspace.slug == w.slug)).one()
        assert imported.id == w.id  # get_or_create_workspace reused ours
    return w


# ---- chat providers ----------------------------------------------------------------------------


def _anthropic(handler: Any, **kw: Any) -> AnthropicChat:
    return AnthropicChat("k", client=httpx.Client(transport=httpx.MockTransport(handler)),
                         sleep=lambda _s: None, **kw)


def test_anthropic_tool_use_and_headers() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["h"], seen["body"] = req.headers, json.loads(req.content)
        return httpx.Response(200, json={"content": [
            {"type": "tool_use", "name": "respond", "input": {"title": "t"}}]})

    out = _anthropic(handler).complete_json("sys", [{"role": "user", "content": "hi"}],
                                            {"type": "object"})
    assert out == {"title": "t"}
    assert seen["h"]["x-api-key"] == "k" and seen["h"]["anthropic-version"]
    assert seen["body"]["model"] == "claude-sonnet-5-5"
    assert seen["body"]["tool_choice"] == {"type": "tool", "name": "respond"}
    assert seen["body"]["system"] == "sys"


def test_retries_on_429_and_5xx_then_gives_up() -> None:
    calls = {"n": 0}

    def flaky(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429 if calls["n"] == 1 else 503, headers={"retry-after": "0"})
        return httpx.Response(200, json={"content": [{"type": "tool_use", "input": {"a": 1}}]})

    assert _anthropic(flaky).complete_json("s", [], {}) == {"a": 1}
    assert calls["n"] == 3

    calls["n"] = 0
    with pytest.raises(ChatError, match="after 3 attempts"):
        _anthropic(lambda _r: httpx.Response(500), max_retries=2).complete_json("s", [], {})
    # 4xx other than 429 is not retried
    n = {"n": 0}

    def bad(_r: httpx.Request) -> httpx.Response:
        n["n"] += 1
        return httpx.Response(400, text="bad request")

    with pytest.raises(ChatError, match="400"):
        _anthropic(bad).complete_json("s", [], {})
    assert n["n"] == 1


def test_openai_compat_json_schema_and_fences() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "```json\n{\"ok\": true}\n```"}}]})

    c = OpenAICompatChat("llama3", "http://ollama:11434/v1", None,
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.complete_json("sys", [{"role": "user", "content": "x"}], {"type": "object"}) == {
        "ok": True}
    assert seen["url"] == "http://ollama:11434/v1/chat/completions"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["response_format"]["type"] == "json_schema"
    with pytest.raises(ChatError):
        parse_json_object("no json here")


def test_make_chat_provider_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert make_chat_provider(Settings()) is None
    assert make_chat_provider(Settings(chat_provider="anthropic")) is None  # no key
    p = make_chat_provider(Settings(chat_provider="anthropic", chat_api_key="k"))
    assert p is not None and p.model_id == "anthropic:claude-sonnet-5-5"
    p = make_chat_provider(Settings(chat_provider="openai_compat", chat_model="m",
                                    chat_base_url="http://localhost:8000/v1"))
    assert p is not None and p.model_id == "openai_compat:m"
    fake = FakeChatProvider([{"a": 1}, ChatError("boom")])
    assert fake.complete_json("s", [], {}) == {"a": 1}
    with pytest.raises(ChatError):
        fake.complete_json("s", [], {})


# ---- signal embeddings -------------------------------------------------------------------------


def test_embed_pending_signals(ws: Ws) -> None:
    ws.signal("analyst", None, "sql_error", {"message": "column x does not exist"}, embed=False)
    ws.signal("analyst", None, "gap_flagged", {"what_was_missing": "churn"}, embed=False)
    with workspace_session(ws.id, engine=ws.engine) as s:
        assert embed_pending_signals(s, ws.id, EMB) == 2
        assert embed_pending_signals(s, ws.id, EMB) == 0
        assert all(sg.embedding is not None for sg in s.scalars(select(Signal)))


# ---- gap detection -----------------------------------------------------------------------------


def test_cluster_over_thresholds_creates_one_suggestion(ws: Ws) -> None:
    ws.churn_cluster()
    ws.gap("analyst", REFUND)  # unrelated, single signal: below thresholds
    rep = ws.detect()
    assert len(rep.created) == 1 and rep.below_threshold == 1
    (sug,) = ws.suggestions()
    assert (sug.evidence_count, sug.distinct_users, sug.distinct_sessions) == (3, 3, 3)
    assert sug.source == "signals" and sug.status == "open" and "churn" in sug.title.lower()
    with workspace_session(ws.id, engine=ws.engine) as s:
        clustered = {sg.kind: sg.clustered_into for sg in s.scalars(select(Signal))}
        assert sum(1 for sg in s.scalars(select(Signal)) if sg.clustered_into == sug.id) == 3
        assert clustered  # the refund signal stays unclustered
        assert sum(1 for sg in s.scalars(select(Signal)) if sg.clustered_into is None) == 1


@pytest.mark.parametrize("users,sessions_per_user,expect", [
    (("analyst", "u2"), 1, 0),  # 2 signals < 3
    (("analyst",), 3, 0),  # 1 user
    (("analyst", "u2"), 2, 1),  # 4 signals, 2 users, 4 sessions
])
def test_thresholds(ws: Ws, users: tuple[str, ...], sessions_per_user: int, expect: int) -> None:
    for u in users:
        for _ in range(sessions_per_user):
            ws.gap(u, CHURN)
    assert len(ws.detect().created) == expect


def test_thresholds_are_configurable_per_workspace(ws: Ws) -> None:
    ws.gap("analyst", CHURN)
    ws.gap("u2", CHURN)
    assert ws.detect().created == []
    with admin_session(engine=ws.engine) as s:
        w = s.get(Workspace, ws.id)
        assert w is not None
        w.settings = {"curation": {"min_signals": 2, "min_users": 2, "min_sessions": 2}}
        cfg = load_config(w.settings)
    assert (cfg.min_signals, cfg.min_users) == (2, 2)
    assert len(ws.detect(cfg).created) == 1
    assert load_config({"curation": {"min_users": 0}}).min_users == 2  # invalid -> defaults


def test_human_signal_counts_double_toward_the_signal_threshold(ws: Ws) -> None:
    ws.gap("analyst", CHURN)
    sid = ws.session("u2", "what is churn?")
    ws.signal("u2", sid, "user_doubt", {"sentiment": "negative", "comment": CHURN,
                                        "via": "widget"}, source="human")
    # 2 users, 2 sessions, 2 signals: only a person's 👎 (weight 2) gets it to min_signals=3
    assert len(ws.detect().created) == 1


def test_same_session_signals_do_not_meet_session_threshold(ws: Ws) -> None:
    sid = ws.session("analyst", "q")
    for _ in range(3):
        ws.signal("analyst", sid, "gap_flagged", {"what_was_missing": CHURN})
    assert ws.detect().created == []


def test_old_signals_outside_window_are_ignored(ws: Ws) -> None:
    for u in ("analyst", "u2", "u3"):
        ws.gap(u, CHURN, age_days=45)
    assert ws.detect().signals_considered == 0


def test_cluster_attaches_to_open_suggestion_and_new_topic_creates_new(ws: Ws) -> None:
    ws.churn_cluster()
    first = ws.detect().created[0]
    ws.gap("curator", CHURN)  # a single new signal on the same topic, below thresholds alone
    rep = ws.detect()
    assert rep.created == [] and rep.attached == [first]
    (sug,) = ws.suggestions()
    assert sug.evidence_count == 4 and sug.distinct_users == 4
    ws.gap("analyst", REFUND)
    ws.gap("u2", REFUND)
    ws.gap("u3", REFUND)
    rep = ws.detect()
    assert len(rep.created) == 1 and rep.created[0] != first
    assert len(ws.suggestions()) == 2


def test_dismissed_suggestion_signals_stay_clustered(ws: Ws) -> None:
    ws.churn_cluster()
    assert len(ws.detect().created) == 1
    assert ws.detect().signals_considered == 0  # already clustered


def test_asset_overlap_pulls_borderline_signals_together(ws: Ws) -> None:
    with admin_session(engine=ws.engine) as s:
        a = Asset(workspace_id=ws.id, kind="table", fqn="analytics.orders")
        s.add(a)
        s.flush()
        aid = a.id
    # Related but lexically different wording: below 0.72 alone, above it with the asset bonus.
    base = "customer churn rate definition missing from orders documentation"
    variants = [base, base + " please", base + " thanks"]
    for u, v in zip(("analyst", "u2", "u3"), variants, strict=True):
        ws.signal(u, ws.session(u, "q"), "gap_flagged", {"what_was_missing": v}, assets=[aid])
    assert len(ws.detect().created) == 1


# ---- hygiene -----------------------------------------------------------------------------------


def _hygiene(ws: Ws, chat: FakeChatProvider | None = None, cfg: CurationConfig | None = None
             ) -> Any:
    with admin_session(engine=ws.engine) as s:
        return run_hygiene(s, ws.id, cfg or CurationConfig(), EMB, chat)


def _hygiene_signals(ws: Ws, kind: str) -> list[Signal]:
    with admin_session(engine=ws.engine) as s:
        rows = list(s.scalars(select(Signal).where(Signal.workspace_id == ws.id,
                                                   Signal.kind == kind)))
        s.expunge_all()
        return rows


def _add_item(ws: Ws, key: str, item_type: str, body: str, meta: dict[str, Any] | None = None,
              *, state: str = "published",
              scope: tuple[str, uuid.UUID | None] = ("workspace", None),
              age_days: float = 0) -> uuid.UUID:
    from satchel.curation.drafts import add_version

    with admin_session(engine=ws.engine) as s:
        item = ContextItem(workspace_id=ws.id, type=item_type, key=key, state="draft",
                           scope_kind=scope[0], scope_id=scope[1], managed_by="ui",
                           created_at=datetime.now(UTC) - timedelta(days=age_days))
        s.add(item)
        s.flush()
        ver = add_version(s, item, body, meta or {}, author=None, provenance={}, embedder=EMB)
        if state == "published":
            item.published_version_id = ver.id
            item.state = "published"
        return item.id


GUIDE_META = {"name": "G", "description": "d", "tags": []}


def test_hygiene_stale_reference(ws: Ws) -> None:
    with admin_session(engine=ws.engine) as s:
        a = Asset(workspace_id=ws.id, kind="table", fqn="analytics.old_orders",
                  dropped_at=datetime.now(UTC))
        s.add(a)
        s.flush()
        aid = a.id
    iid = _add_item(ws, "guides/old.md", "guide", "# Old\nUses old_orders.", GUIDE_META)
    with admin_session(engine=ws.engine) as s:
        vid = s.get(ContextItem, iid).current_version_id  # type: ignore[union-attr]
        s.add(ContextEntityLink(version_id=vid, asset_id=aid, relation="mentions"))
    rep = _hygiene(ws)
    assert rep.emitted == {"stale_reference": 1}
    (sg,) = _hygiene_signals(ws, "stale_reference")
    assert sg.source == "hygiene" and sg.payload["target"] == "guides/old.md"
    assert sg.item_ids == [iid] and sg.session_id is None and sg.user_id is None
    assert _hygiene(ws).emitted == {}  # not re-emitted


def test_hygiene_dead_item_respects_age_and_serving(ws: Ws) -> None:
    _add_item(ws, "guides/dead.md", "guide", "# Dead\nnobody reads this", GUIDE_META, age_days=90)
    served = _add_item(ws, "guides/served.md", "guide", "# Served\nread often", GUIDE_META,
                       age_days=90)
    _add_item(ws, "guides/new.md", "guide", "# New\nrecent", GUIDE_META, age_days=1)
    _add_item(ws, "rules/always.md", "workspace_rule", "Always on rule text", age_days=90)
    sid = ws.session("analyst", "q")
    with admin_session(engine=ws.engine) as s:
        from satchel.store.models import ServedContext

        tc = ToolCall(session_id=sid, tool="get_context_pack")
        s.add(tc)
        s.flush()
        vid = s.get(ContextItem, served).current_version_id  # type: ignore[union-attr]
        s.add(ServedContext(tool_call_id=tc.id, version_id=vid, rank=1))
    _hygiene(ws)
    assert [sg.payload["target"] for sg in _hygiene_signals(ws, "dead_item")] == [
        "guides/dead.md"]


def test_hygiene_duplicates_and_contradictions(ws: Ws) -> None:
    text = "# Revenue\n\nRevenue is the sum of order amount excluding returned orders in AUD."
    a = _add_item(ws, "guides/a.md", "guide", text, GUIDE_META)
    _add_item(ws, "guides/b.md", "guide", text, GUIDE_META)
    similar = text.replace("excluding returned", "including returned").replace("sum", "total")
    _add_item(ws, "guides/c.md", "guide", similar, GUIDE_META)
    chat = FakeChatProvider(
        lambda *_a: {"conflict": True, "explanation": "A excludes, C includes"})
    cfg = CurationConfig(contradiction_pairs=2, contradiction_min_similarity=0.5)
    rep = _hygiene(ws, chat, cfg)
    assert rep.emitted["duplicate"] == 1
    (dup,) = _hygiene_signals(ws, "duplicate")
    assert dup.payload["keys"] == ["guides/a.md", "guides/b.md"] and a in dup.item_ids
    # the identical pair is a duplicate, not a contradiction; cap of 2 pairs respected
    assert len(chat.calls) == 2 and rep.emitted["contradiction"] == 2
    assert "A excludes" in _hygiene_signals(ws, "contradiction")[0].payload["explanation"]
    # same versions are not checked again
    again = _hygiene(ws, chat, cfg)
    assert len(chat.calls) == 2 and "contradiction" not in again.emitted


def test_hygiene_contradiction_needs_a_chat_provider(ws: Ws) -> None:
    t = "# Revenue\n\nRevenue is the sum of order amount."
    _add_item(ws, "guides/a.md", "guide", t, GUIDE_META)
    _add_item(ws, "guides/b.md", "guide", t + " Net of refunds.", GUIDE_META)
    rep = _hygiene(ws, None, CurationConfig(contradiction_min_similarity=0.3))
    assert "contradiction" not in rep.emitted
    assert any("no chat provider" in n for n in rep.notes)


def test_hygiene_is_workspace_scoped(app_engine: Engine) -> None:
    a, b = Ws(app_engine), Ws(app_engine)
    _add_item(a, "guides/dead.md", "guide", "# Dead\ntext", GUIDE_META, age_days=90)
    _hygiene(b)
    assert _hygiene_signals(b, "dead_item") == []
    _hygiene(a)
    assert len(_hygiene_signals(a, "dead_item")) == 1


def test_hygiene_signal_bypasses_thresholds(ws: Ws) -> None:
    _add_item(ws, "guides/dead.md", "guide", "# Dead\ntext", GUIDE_META, age_days=90)
    _hygiene(ws)
    rep = ws.detect()
    assert len(rep.created) == 1
    (sug,) = ws.suggestions()
    assert sug.source == "hygiene" and sug.evidence_count == 1 and sug.distinct_users == 0
    assert "guides/dead.md" in sug.title


# ---- drafting ----------------------------------------------------------------------------------


def _changes(ws: Ws) -> list[SuggestionChange]:
    with workspace_session(ws.id, engine=ws.engine) as s:
        rows = list(s.scalars(select(SuggestionChange).order_by(SuggestionChange.ord)))
        s.expunge_all()
        return rows


def _draft_reply(update_key: str) -> dict[str, Any]:
    return {
        "title": "Define customer churn",
        "summary": "Agents cannot answer churn questions because no definition exists.",
        "changes": [
            {"op": "create_item", "item_type": "guide", "key": "guides/churn.md",
             "proposed_body": "# Churn\n\nWe have no churn guide.",
             "proposed_meta": {"name": "Churn"}, "rationale": "invalid: no description"},
            {"op": "create_item", "item_type": "glossary_term", "key": "glossary/churn.yaml",
             "proposed_body": "term: churn\nsynonyms: [attrition]\ndefinition: Customers with "
                              "no order in 90 days who previously ordered.\n",
             "proposed_meta": {}, "rationale": "A glossary term is the smallest fix."},
            {"op": "update_item", "target_key": update_key,
             "proposed_body": "# Customer notes\n\nChurn: no order in 90 days.",
             "proposed_meta": GUIDE_META, "rationale": "Mention churn."},
            {"op": "update_item", "target_key": "guides/does-not-exist.md",
             "proposed_body": "x", "proposed_meta": {}, "rationale": "bad target"},
            {"op": "frobnicate", "proposed_body": "", "rationale": ""},
        ],
    }


def test_drafting_stores_valid_changes_drops_invalid_and_orders_by_preference(ws: Ws) -> None:
    _add_item(ws, "guides/customers.md", "guide", "# Customer notes\n\nOld text.", GUIDE_META)
    ws.churn_cluster()
    chat = FakeChatProvider([_draft_reply("guides/customers.md")])
    rep = run_curation(ws.id, engine=ws.engine, embedder=EMB, chat=chat)
    assert rep is not None and rep.errors == []
    (d,) = rep.drafts
    assert (d.status, d.changes, d.dropped) == ("drafted", 2, 3)
    (sug,) = ws.suggestions()
    assert sug.title == "Define customer churn" and sug.drafted_at is not None
    assert sug.draft_model == "fake:scripted" and "dropped 3" in (sug.draft_note or "")
    cs = _changes(ws)
    # update of an existing item first, then the new glossary term
    assert [(c.op, c.item_type) for c in cs] == [("update_item", "guide"),
                                                  ("create_item", "glossary_term")]
    assert cs[0].target_item_id is not None and cs[1].proposed_key == "glossary/churn.yaml"
    # the prompt carried the evidence, the existing keys and the instructions section
    user_msg = chat.calls[0]["messages"][0]["content"]
    assert "customer churn" in user_msg and "guides/customers.md" in user_msg
    assert "Workspace curation instructions" in user_msg
    assert chat.calls[0]["schema"]["required"] == ["title", "summary", "changes"]
    # drafted suggestions are not drafted again
    rep2 = run_curation(ws.id, engine=ws.engine, embedder=EMB, chat=chat)
    assert rep2 is not None and rep2.drafts == [] and len(chat.calls) == 1


def test_drafting_new_key_that_exists_becomes_update_and_noop_is_dropped(ws: Ws) -> None:
    body = "# Customer notes\n\nOld text."
    _add_item(ws, "guides/customers.md", "guide", body, GUIDE_META)
    ws.churn_cluster()
    reply = {"title": "t", "summary": "s", "changes": [
        {"op": "create_item", "item_type": "guide", "key": "guides/customers.md",
         "proposed_body": "# Customer notes\n\nNew text.", "proposed_meta": GUIDE_META,
         "rationale": "r"},
        {"op": "update_item", "target_key": "guides/customers.md", "proposed_body": body,
         "proposed_meta": GUIDE_META, "rationale": "unchanged"},
    ]}
    rep = run_curation(ws.id, engine=ws.engine, embedder=EMB, chat=FakeChatProvider([reply]))
    assert rep is not None
    (c,) = _changes(ws)
    assert c.op == "update_item" and c.target_item_id is not None


def test_dismiss_reasons_are_fed_back_into_the_prompt(ws: Ws) -> None:
    ws.churn_cluster()
    run_curation(ws.id, engine=ws.engine, embedder=EMB, use_llm=False)
    (first,) = ws.suggestions()
    c = ws.client()
    r = c.post(f"/v1/suggestions/{first.id}/dismiss", json={"reason": ""}, headers=ws.h("curator"))
    assert r.status_code == 422
    r = c.post(f"/v1/suggestions/{first.id}/dismiss",
               json={"reason": "Churn is owned by the finance team; do not add it here"},
               headers=ws.h("curator"))
    assert r.status_code == 200 and r.json()["status"] == "dismissed"
    for u in ("analyst", "u2", "u3"):
        ws.gap(u, REFUND)
    chat = FakeChatProvider([{"title": "Refunds", "summary": "s", "changes": []}])
    run_curation(ws.id, engine=ws.engine, embedder=EMB, chat=chat)
    msg = chat.calls[0]["messages"][0]["content"]
    assert "owned by the finance team" in msg
    (second,) = [s for s in ws.suggestions() if s.id != first.id]
    assert second.title == "Refunds" and second.draft_note == "the model proposed no changes"


def test_chat_failure_leaves_suggestion_to_retry_next_run(ws: Ws) -> None:
    ws.churn_cluster()
    rep = run_curation(ws.id, engine=ws.engine, embedder=EMB,
                       chat=FakeChatProvider([ChatError("rate limited")]))
    assert rep is not None and rep.drafts[0].status == "failed"
    assert ws.suggestions()[0].drafted_at is None
    ok = FakeChatProvider([{"title": "T", "summary": "S", "changes": []}])
    rep = run_curation(ws.id, engine=ws.engine, embedder=EMB, chat=ok)
    assert rep is not None and rep.drafts[0].status == "drafted"


def test_prompt_redacts_sql_truncates_and_withholds_sensitive_sessions(ws: Ws) -> None:
    secret = "alice@example.com"
    long = "x" * 900
    for i, u in enumerate(("analyst", "u2", "u3")):
        sid = ws.session(u, f"churn question {i} {long}", sensitive=(i == 2))
        with admin_session(engine=ws.engine) as s:
            s.add(ToolCall(session_id=sid, tool="run_sql", args={
                "sql": f"select * from customers where email = '{secret}'", "question": "q"}))
        ws.signal(u, sid, "gap_flagged", {"what_was_missing": CHURN + f" #{i}"})
    chat = FakeChatProvider([{"title": "T", "summary": "S", "changes": []}])
    run_curation(ws.id, engine=ws.engine, embedder=EMB, chat=chat)
    msg = chat.calls[0]["messages"][0]["content"]
    assert secret not in msg  # SQL string literals are masked
    assert "email = '?'" in msg and long not in msg
    assert "sensitive session: details withheld" in msg
    assert "analyst@" not in msg and "user A" in msg  # identities are anonymised


# ---- golden queries ----------------------------------------------------------------------------

GOOD_SQL = (
    "select date_trunc('month', order_date) m, sum(amount)/100.0 "
    "from analytics.orders group by 1"
)


def _good_session(ws: Ws, user: str = "analyst", *, rating: int = 1, confidence: str = "high",
                  sql: str = GOOD_SQL, sensitive: bool = False) -> uuid.UUID:
    sid = ws.session(user, "What was revenue by month?", sensitive=sensitive)
    with admin_session(engine=ws.engine) as s:
        s.add_all([
            ToolCall(session_id=sid, tool="run_sql", args={"sql": "select bad", "question": "q"},
                     error="boom"),
            ToolCall(session_id=sid, tool="run_sql", args={"sql": sql, "question": "q"},
                     result_meta={"connection": "warehouse"}),
            ToolCall(session_id=sid, tool="report_answer", args={"confidence": confidence}),
            Feedback(session_id=sid, user_id=ws.users[user], rating=rating, comment="great"),
        ])
    return sid


def test_golden_query_promotion(ws: Ws) -> None:
    _good_session(ws)
    _good_session(ws, "u2", rating=-1)
    _good_session(ws, "u3", confidence="medium")
    _good_session(ws, "curator", sensitive=True)
    rep = run_curation(ws.id, engine=ws.engine, embedder=EMB, use_llm=False)
    assert rep is not None and rep.golden_candidates == 1 and rep.errors == []
    (sug,) = ws.suggestions()
    assert sug.source == "golden" and sug.drafted_at is not None and sug.draft_model is None
    assert sug.title.startswith("Promote to a golden query") and sug.distinct_sessions == 1
    (c,) = _changes(ws)
    assert c.op == "promote_golden_query" and c.item_type == "golden_query"
    assert c.proposed_key == "queries/what-was-revenue-by-month.yaml"
    assert "select date_trunc" in c.proposed_body and "connection: warehouse" in c.proposed_body
    assert "select bad" not in c.proposed_body  # the final *successful* query
    # idempotent, and an existing golden query with the same SQL suppresses new candidates
    rep = run_curation(ws.id, engine=ws.engine, embedder=EMB, use_llm=False)
    assert rep is not None and rep.golden_candidates == 0


def test_golden_candidate_skipped_when_query_already_exists(ws: Ws) -> None:
    _add_item(ws, "queries/rev.yaml", "golden_query",
              f"question: Revenue by month\nsql: {json.dumps(GOOD_SQL)}\n")
    _good_session(ws)
    rep = run_curation(ws.id, engine=ws.engine, embedder=EMB, use_llm=False)
    assert rep is not None and rep.golden_candidates == 0


# ---- running without an LLM --------------------------------------------------------------------


def test_run_without_llm_still_clusters(ws: Ws, caplog: pytest.LogCaptureFixture) -> None:
    ws.churn_cluster()
    with caplog.at_level(logging.WARNING, logger="satchel.curation"):
        rep = run_curation(ws.id, engine=ws.engine, embedder=EMB, settings=Settings())
    assert rep is not None and rep.llm == "none" and rep.errors == []
    assert "no LLM configured" in caplog.text
    (sug,) = ws.suggestions()
    assert sug.drafted_at is None and rep.undrafted_left == 1
    assert "No draft yet" in sug.summary
    r = run_curation(ws.id, engine=ws.engine, embedder=EMB, use_llm=False)
    assert r is not None and r.gaps.signals_considered == 0


def test_concurrent_run_is_refused(ws: Ws) -> None:
    from satchel.curation.run import run_lock

    with run_lock(ws.engine, ws.id) as got:
        assert got
        assert run_curation(ws.id, engine=ws.engine, embedder=EMB, use_llm=False) is None


# ---- API ---------------------------------------------------------------------------------------

ID = "00000000-0000-4000-8000-0000000000aa"  # fixed: test ids must match across xdist workers
ENDPOINTS: list[tuple[str, str, dict[str, Any] | None, str]] = [
    ("GET", "/v1/suggestions", None, "curator"),
    ("GET", f"/v1/suggestions/{ID}", None, "curator"),
    ("POST", f"/v1/suggestions/{ID}/changes/{ID}/accept", {}, "curator"),
    ("POST", f"/v1/suggestions/{ID}/changes/{ID}/reject", {}, "curator"),
    ("POST", f"/v1/suggestions/{ID}/dismiss", {"reason": "r"}, "curator"),
    ("POST", f"/v1/suggestions/{ID}/complete", None, "curator"),
    ("POST", "/v1/curate/run", {"use_llm": False}, "admin"),
]


@pytest.mark.parametrize("method,path,body,minimum", ENDPOINTS)
def test_suggestion_roles(ws: Ws, method: str, path: str, body: dict[str, Any] | None,
                          minimum: str) -> None:
    rank = {"explorer": 0, "analyst": 1, "curator": 2, "admin": 3}
    c = ws.client()
    assert c.request(method, path, json=body).status_code == 401
    for role in ROLES:
        r = c.request(method, path, json=body, headers=ws.h(role))
        if rank[role] < rank[minimum]:
            assert r.status_code == 403, (role, r.text)
        else:
            assert r.status_code not in (401, 403), (role, r.text)


def _drafted_suggestion(ws: Ws, reply: dict[str, Any]) -> tuple[TestClient, dict[str, Any]]:
    ws.churn_cluster()
    chat = FakeChatProvider([reply])
    c = ws.client(chat)
    r = c.post("/v1/curate/run?wait=true", json={}, headers=ws.h("admin"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["suggestions_created"] == 1 and out["drafted"] == 1
    sid = c.get("/v1/suggestions", headers=ws.h("curator")).json()["suggestions"][0]["id"]
    return c, c.get(f"/v1/suggestions/{sid}", headers=ws.h("curator")).json()


def test_accept_creates_draft_that_previews_and_publishes(ws: Ws) -> None:
    _add_item(ws, "guides/customers.md", "guide", "# Customer notes\n\nOld text.", GUIDE_META)
    c, detail = _drafted_suggestion(ws, _draft_reply("guides/customers.md"))
    h = ws.h("curator")
    assert detail["status"] == "open" and detail["change_count"] == 2
    assert detail["evidence_count"] == 3 and detail["distinct_users"] == 3
    upd, new = detail["changes"]
    assert upd["op"] == "update_item" and upd["target_key"] == "guides/customers.md"
    assert "+Churn: no order in 90 days." in upd["diff"]["unified"]
    assert upd["current_body"].startswith("# Customer notes")
    assert new["proposed_key"] == "glossary/churn.yaml" and new["current_body"] is None

    base = f"/v1/suggestions/{detail['id']}/changes"
    r = c.post(f"{base}/{upd['id']}/accept", json={}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["item_key"] == "guides/customers.md" and out["item_seq"] == 2
    assert out["edit_in_git"] is False and out["suggestion"]["status"] == "open"
    # accepting twice is a conflict
    assert c.post(f"{base}/{upd['id']}/accept", json={}, headers=h).status_code == 409
    # edit-and-accept the new glossary term under a different key
    edited = new["proposed_body"].replace("attrition", "attrition, lapse")
    r = c.post(f"{base}/{new['id']}/accept", json={"body": edited, "key": "glossary/churn-v2.yaml"},
               headers=h)
    assert r.status_code == 200 and r.json()["item_key"] == "glossary/churn-v2.yaml"
    assert r.json()["suggestion"]["status"] == "completed"  # every change decided

    keys = {x["key"]: x for x in c.get("/v1/changes", headers=h).json()["changes"]}
    assert set(keys) == {"guides/customers.md", "glossary/churn-v2.yaml"}
    prev = c.post("/v1/previews", json={}, headers=h)
    assert prev.status_code == 201, prev.text
    pub = c.post(f"/v1/previews/{prev.json()['id']}/publish", headers=h)
    assert pub.status_code == 200, pub.text
    item = c.get("/v1/items/glossary/churn-v2.yaml", headers=h).json()
    assert item["state"] == "published" and "lapse" in item["body"]
    version = c.get("/v1/items/glossary/churn-v2.yaml/versions/1", headers=h).json()
    assert version["provenance"]["source"] == "suggestion"
    assert version["provenance"]["ref"] == detail["id"]
    assert c.get("/v1/changes", headers=h).json()["changes"] == []
    actions = {a for (a,) in _audit(ws)}
    assert {"suggestion.change.accept", "suggestion.complete", "curate.run"} <= actions


def _audit(ws: Ws) -> list[tuple[str]]:
    with admin_session(engine=ws.engine) as s:
        return [(a,) for a in s.scalars(select(AuditEvent.action)
                                        .where(AuditEvent.workspace_id == ws.id))]


def test_reject_and_dismiss_and_complete_flow(ws: Ws) -> None:
    _add_item(ws, "guides/customers.md", "guide", "# Customer notes\n\nOld text.", GUIDE_META)
    c, detail = _drafted_suggestion(ws, _draft_reply("guides/customers.md"))
    h = ws.h("curator")
    base = f"/v1/suggestions/{detail['id']}"
    ids = [x["id"] for x in detail["changes"]]
    r = c.post(f"{base}/changes/{ids[0]}/reject", json={"reason": "wrong place"}, headers=h)
    assert r.status_code == 200 and r.json()["change"]["status"] == "rejected"
    assert r.json()["suggestion"]["pending_changes"] == 1
    # complete the rest manually: pending changes are closed, nothing is created
    r = c.post(f"{base}/complete", headers=h)
    assert r.json()["status"] == "completed" and r.json()["pending_changes"] == 0
    assert c.post(f"{base}/dismiss", json={"reason": "x"}, headers=h).status_code == 409
    assert c.get("/v1/changes", headers=h).json()["changes"] == []
    listed = c.get("/v1/suggestions", params={"status": "all"}, headers=h).json()
    assert listed["total"] == 1
    assert c.get("/v1/suggestions", headers=h).json()["total"] == 0  # default: open only


def test_list_sorting_and_source_filter(ws: Ws) -> None:
    ws.churn_cluster()  # 3 signals
    for u in ("analyst", "u2", "u3", "curator"):
        ws.gap(u, REFUND)  # 4 signals
    run_curation(ws.id, engine=ws.engine, embedder=EMB, use_llm=False)
    c = ws.client()
    h = ws.h("curator")
    by_ev = c.get("/v1/suggestions", params={"sort": "evidence"}, headers=h).json()
    assert [s["evidence_count"] for s in by_ev["suggestions"]] == [4, 3]
    recent = c.get("/v1/suggestions", params={"sort": "recent"}, headers=h).json()
    assert recent["total"] == 2
    assert c.get("/v1/suggestions", params={"source": "hygiene"}, headers=h).json()["total"] == 0


def test_git_managed_target_returns_edit_in_git(jaffle: Ws) -> None:
    ws = jaffle
    reply = {"title": "Revenue note", "summary": "s", "changes": [
        {"op": "update_item", "target_key": "guides/revenue-reporting.md",
         "proposed_body": "# Revenue reporting\n\nNew paragraph about churn.",
         "proposed_meta": {"name": "Revenue reporting", "description": "How to report revenue",
                           "tags": []}, "rationale": "r"}]}
    c, detail = _drafted_suggestion(ws, reply)
    (ch,) = detail["changes"]
    assert ch["target_managed_by"] == "git"
    r = c.post(f"/v1/suggestions/{detail['id']}/changes/{ch['id']}/accept", json={},
               headers=ws.h("curator"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["edit_in_git"] is True and "git" in out["message"]
    assert out["change"]["status"] == "applied" and out["change"]["result"]["manual"] is True
    assert out["change"]["result"]["proposed_body"].startswith("# Revenue reporting")
    assert out["suggestion"]["status"] == "completed"
    # no draft was created
    assert c.get("/v1/changes", headers=ws.h("curator")).json()["changes"] == []


def test_accept_validation_and_key_conflicts(ws: Ws) -> None:
    c, detail = _drafted_suggestion(ws, _draft_reply("guides/none.md"))
    (new,) = detail["changes"]  # only the glossary term survives
    h = ws.h("curator")
    base = f"/v1/suggestions/{detail['id']}/changes/{new['id']}/accept"
    assert c.post(base, json={"body": "term: [unclosed"}, headers=h).status_code == 422
    assert c.post(base, json={"key": "../etc/passwd"}, headers=h).status_code == 422
    assert c.post("/v1/items", json={"key": "glossary/churn.yaml", "type": "glossary_term",
                                     "body": "term: x\ndefinition: y\n", "meta": {}},
                  headers=h).status_code == 201
    assert c.post(base, json={}, headers=h).status_code == 409  # key now taken
    assert c.post(base, json={"key": "glossary/churn-3.yaml"}, headers=h).status_code == 200


def test_deprecate_change_applies_to_ui_item(ws: Ws) -> None:
    _add_item(ws, "guides/old.md", "guide", "# Old\n\nstale", GUIDE_META)
    reply = {"title": "Retire", "summary": "s", "changes": [
        {"op": "deprecate_item", "target_key": "guides/old.md", "proposed_body": "",
         "rationale": "dead"}]}
    c, detail = _drafted_suggestion(ws, reply)
    (ch,) = detail["changes"]
    r = c.post(f"/v1/suggestions/{detail['id']}/changes/{ch['id']}/accept", json={},
               headers=ws.h("curator"))
    assert r.status_code == 200 and r.json()["suggestion"]["status"] == "completed"
    changes = c.get("/v1/changes", headers=ws.h("curator")).json()["changes"]
    assert [(x["key"], x["kind"]) for x in changes] == [("guides/old.md", "deprecate")]


# ---- evidence redaction ------------------------------------------------------------------------


def test_evidence_redaction_by_role(ws: Ws) -> None:
    long = "L" * 300
    for u in ("analyst", "u2"):
        sid = ws.session(u, f"churn? {long}")
        ws.signal(u, sid, "gap_flagged", {"what_was_missing": CHURN, "why": long})
    sid = ws.session("u3", "secret churn question", sensitive=True)
    ws.signal("u3", sid, "gap_flagged", {"what_was_missing": CHURN, "why": "SECRET-DETAIL"})
    run_curation(ws.id, engine=ws.engine, embedder=EMB, use_llm=False, settings=Settings())
    c = ws.client()
    listed = c.get("/v1/suggestions", headers=ws.h("curator")).json()["suggestions"]
    assert listed, "expected a suggestion"
    sug = listed[0]["id"]

    def blob(role: str) -> dict[str, Any]:
        return c.get(f"/v1/suggestions/{sug}", headers=ws.h(role)).json()

    cur, adm = blob("curator"), blob("admin")
    cur_text, adm_text = json.dumps(cur["evidence"]), json.dumps(adm["evidence"])
    assert long not in cur_text and long in adm_text  # curators: truncated; admins: raw
    assert "SECRET-DETAIL" not in cur_text and "SECRET-DETAIL" in adm_text
    levels = {e["sensitive"]: e["redaction"] for e in cur["evidence"]}
    assert levels[True] == "metadata_only" and levels[False] == "redacted"
    assert {e["redaction"] for e in adm["evidence"]} == {"none"}
    sens = next(e for e in cur["evidence"] if e["sensitive"])
    assert sens["question"] is None and all(sg["message"] is None for sg in sens["signals"])
    assert [k["kind"] for k in cur["signal_kinds"]] == ["gap_flagged"]


def test_suggestions_isolated_between_workspaces(app_engine: Engine) -> None:
    a, b = Ws(app_engine), Ws(app_engine)
    a.churn_cluster()
    run_curation(a.id, engine=app_engine, embedder=EMB, use_llm=False)
    (sug,) = a.suggestions()
    cb = b.client()
    assert cb.get("/v1/suggestions", headers=b.h("curator")).json()["total"] == 0
    assert cb.get(f"/v1/suggestions/{sug.id}", headers=b.h("curator")).status_code == 404


def test_openapi_documents_suggestion_routes(ws: Ws) -> None:
    paths = ws.client().get("/openapi.json").json()["paths"]
    assert "/v1/suggestions/{suggestion_id}/changes/{change_id}/accept" in paths
    assert "/v1/curate/run" in paths


def test_golden_pins_to_the_call_a_person_thumbed_up(ws: Ws) -> None:
    sid = ws.session("analyst", "What was revenue by month?")
    other = "select 1 as other"
    with admin_session(engine=ws.engine) as s:
        a = ToolCall(session_id=sid, tool="run_sql", args={"sql": GOOD_SQL, "question": "q"},
                     result_meta={"connection": "warehouse"})
        b = ToolCall(session_id=sid, tool="run_sql", args={"sql": other, "question": "q"},
                     result_meta={"connection": "warehouse"})
        s.add_all([a, b, ToolCall(session_id=sid, tool="report_answer",
                                  args={"confidence": "high"})])
        s.flush()
        s.add(Feedback(session_id=sid, tool_call_id=a.id, user_id=ws.users["analyst"],
                       rating=1, source="human", client="claude-desktop"))
    rep = run_curation(ws.id, engine=ws.engine, embedder=EMB, use_llm=False)
    assert rep is not None and rep.golden_candidates == 1
    (c,) = _changes(ws)
    assert "select date_trunc" in c.proposed_body and other not in c.proposed_body
    with workspace_session(ws.id, engine=ws.engine) as s:
        sig = s.scalars(select(Signal).where(Signal.kind == "golden_candidate")).one()
        assert sig.source == "human" and sig.payload["rated_by"] == "human"


def test_a_human_thumbs_down_blocks_golden_promotion(ws: Ws) -> None:
    sid = _good_session(ws)
    with admin_session(engine=ws.engine) as s:
        s.add(Feedback(session_id=sid, user_id=ws.users["u2"], rating=-1, source="human"))
    rep = run_curation(ws.id, engine=ws.engine, embedder=EMB, use_llm=False)
    assert rep is not None and rep.golden_candidates == 0


# ---- curation as a job --------------------------------------------------------------------------


def _drain(ws: Ws, chat: FakeChatProvider | None = None) -> int:
    """Run queued curation passes. Passes other tests queued (the role checks) are finished
    first, so they never run with this test's chat provider."""
    from satchel.jobs.runner import JobRunner, Runtime

    with admin_session(engine=ws.engine) as s:
        s.execute(update(Job).where(Job.kind == "curation.run", Job.workspace_id != ws.id,
                                    Job.status.in_(["queued", "running"]))
                  .values(status="done", finished_at=func.now()))
    return JobRunner(Runtime(ws.engine, Settings(), EMB, chat), kinds={"curation.run"},
                     threads=0).drain()


def test_run_curation_is_queued_and_its_counts_kept_on_the_job(ws: Ws) -> None:
    ws.churn_cluster()
    chat = FakeChatProvider([_draft_reply("guides/customers.md")])
    c = ws.client(chat)
    q = c.post("/v1/curate/run", json={}, headers=ws.h("admin"))
    assert q.status_code == 202 and q.json()["kind"] == "curation.run", q.text
    again = c.post("/v1/curate/run", json={"use_llm": False}, headers=ws.h("admin"))
    assert again.json()["id"] == q.json()["id"]  # one pass per workspace at a time
    assert _drain(ws, chat) == 1
    done = c.get(f"/v1/jobs/{q.json()['id']}", headers=ws.h("curator")).json()
    assert done["status"] == "done" and done["subject"] == "workspace"
    assert done["result"]["suggestions_created"] == 1 and done["result"]["drafted"] == 1
    with admin_session(engine=ws.engine) as s:
        ev = s.scalars(select(AuditEvent).where(AuditEvent.workspace_id == ws.id,
                                                AuditEvent.action == "curate.run")).one()
        assert ev.actor_user_id == ws.users["admin"] and ev.target["job_id"] == q.json()["id"]


def test_a_queued_pass_waits_for_a_run_holding_the_workspace(ws: Ws) -> None:
    from satchel.curation.jobs import queue_curation
    from satchel.curation.run import run_lock

    jid, _ = queue_curation(ws.engine, ws.id, use_llm=False)
    with run_lock(ws.engine, ws.id) as got:  # the CLI is running curation
        assert got
        _drain(ws)
    with admin_session(engine=ws.engine) as s:
        j = s.get(Job, jid)
        assert j is not None and j.status == "queued" and "in progress" in (j.error or "")
        s.execute(update(Job).where(Job.id == jid).values(run_after=func.now()))
    _drain(ws)
    with admin_session(engine=ws.engine) as s:
        assert s.get(Job, jid).status == "done"  # type: ignore[union-attr]


def test_the_worker_pass_queues_curation_and_once_runs_it(ws: Ws,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from satchel.jobs.runner import JobRunner, Runtime
    from satchel.worker import main as worker

    monkeypatch.setattr(worker, "get_engine", lambda: ws.engine)
    monkeypatch.setattr(worker, "purge_results", lambda: None)
    monkeypatch.setattr(worker, "purge_sessions", lambda: None)
    monkeypatch.setattr(worker, "purge_jobs", lambda: None)
    runner = JobRunner(Runtime(ws.engine, Settings(), EMB), kinds={"curation.run"}, threads=0)
    worker.run_forever(once=True, workspace=ws.slug, use_llm=False, jobs=runner,
                       tick_min=0, eval_tick_min=0)
    with admin_session(engine=ws.engine) as s:
        (j,) = s.scalars(select(Job).where(Job.workspace_id == ws.id)).all()
        assert j.kind == "curation.run" and j.status == "done" and j.payload["use_llm"] is False
    with pytest.raises(LookupError):
        worker.queue_curations(workspace="no-such-workspace", engine=ws.engine)
