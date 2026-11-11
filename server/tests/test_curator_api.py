"""M3a curator REST API: role enforcement, workbench lifecycle, proposals, observability,
evidence redaction, catalog, RLS across workspaces and the OpenAPI contract."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from satchel.api.app import create_app
from satchel.llm.embeddings import HashEmbedder
from satchel.mcp.auth import Principal, create_token
from satchel.oauth import sessions
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import (
    AppUser,
    Asset,
    AuditEvent,
    Connection,
    ContextItem,
    Feedback,
    Release,
    ServedContext,
    Signal,
    ToolCall,
)
from satchel.store.models import Session as SessionRow
from satchel.sync import import_context
from satchel.tools.registry import InvokeContext, invoke_tool
from tests.helpers import copy_example, unique_slug

EMB = HashEmbedder()
ROLES = ["admin", "curator", "analyst", "explorer"]


@dataclass
class Env:
    client: TestClient
    engine: Engine
    slug: str
    ws_id: uuid.UUID
    users: dict[str, uuid.UUID]

    def h(self, role: str) -> dict[str, str]:
        return {"X-Satchel-Workspace": self.slug, "X-Satchel-User": str(self.users[role])}


def _make_env(app_engine: Engine, tmp_path: Path, prefix: str = "cur") -> Env:
    slug = unique_slug(prefix)
    r = import_context(copy_example(tmp_path), slug, embedder=EMB, engine=app_engine)
    users = {role: uuid.uuid4() for role in [*ROLES, "analyst2"]}
    with admin_session(engine=app_engine) as s:
        for name, uid in users.items():
            s.add(AppUser(id=uid, workspace_id=r.workspace_id, role=name.rstrip("2"),
                          email=f"{name}@{slug}.test", display_name=name))
    client = TestClient(create_app(app_engine, EMB, settings=Settings(dev_auth=True)))
    return Env(client, app_engine, slug, r.workspace_id, users)


@pytest.fixture
def env(app_engine: Engine, tmp_path: Path) -> Env:
    return _make_env(app_engine, tmp_path)


def k(key: str) -> str:
    return quote(key, safe="/")


GUIDE = {
    "key": "guides/zanzibar.md",
    "type": "guide",
    "body": "# Zanzibar pricing\n\nThe zanzibar surcharge applies to island orders only.",
    "meta": {"name": "Zanzibar pricing", "description": "Island surcharge rules", "tags": []},
}


def create_guide(env: Env, role: str = "curator", **over: Any) -> dict[str, Any]:
    r = env.client.post("/v1/items", json={**GUIDE, **over}, headers=env.h(role))
    assert r.status_code == 201, r.text
    return r.json()


def search_keys(env: Env, path: str, q: str = "zanzibar surcharge") -> list[str]:
    r = env.client.post(path, json={"query": q, "k": 10}, headers=env.h("curator"))
    assert r.status_code == 200, r.text
    return [x["key"] for x in r.json()]


# ---- roles -----------------------------------------------------------------------------------

ID = "00000000-0000-4000-8000-0000000000aa"  # fixed: test ids must match across xdist workers
ENDPOINTS: list[tuple[str, str, dict[str, Any] | None, str]] = [
    # (method, path, json, minimum role)
    ("GET", "/v1/me", None, "explorer"),
    ("GET", "/v1/items", None, "explorer"),
    ("GET", "/v1/items/workspace.md", None, "explorer"),
    ("GET", "/v1/items/workspace.md/versions/1", None, "explorer"),
    ("GET", "/v1/items/workspace.md/diff?from=1&to=1", None, "explorer"),
    ("GET", "/v1/connections", None, "explorer"),
    ("GET", "/v1/assets", None, "explorer"),
    ("POST", "/v1/items", {**GUIDE, "key": "guides/role-probe.md"}, "curator"),
    ("PUT", "/v1/items/guides/nope.md", {"body": "x", "meta": {}}, "curator"),
    ("POST", "/v1/items/guides/nope.md/deprecate", None, "curator"),
    ("GET", "/v1/changes", None, "curator"),
    ("POST", "/v1/previews", {}, "curator"),
    ("GET", f"/v1/previews/{ID}", None, "curator"),
    ("POST", f"/v1/previews/{ID}/publish", None, "curator"),
    ("POST", f"/v1/previews/{ID}/search", {"query": "x"}, "curator"),
    ("POST", f"/v1/previews/{ID}/context-pack", {"question": "x"}, "curator"),
    ("GET", "/v1/releases", None, "curator"),
    ("POST", f"/v1/releases/{ID}/rollback", None, "curator"),
    ("GET", "/v1/proposals", None, "curator"),
    ("POST", "/v1/proposals/p/accept", {}, "curator"),
    ("POST", "/v1/proposals/p/reject", {"reason": "r"}, "curator"),
    ("GET", "/v1/sources", None, "curator"),
    ("GET", "/v1/source-kinds", None, "curator"),
    ("POST", f"/v1/sources/{ID}/sync", None, "curator"),
    ("PUT", "/v1/items/guides/nope.md/overlay", {"overlay": {}}, "curator"),
    ("GET", "/v1/observability/summary", None, "curator"),
    ("POST", f"/v1/sessions/{ID}/sensitive", {"sensitive": True}, "admin"),
    ("GET", "/v1/audit", None, "admin"),
]


@pytest.mark.parametrize("method,path,body,minimum", ENDPOINTS)
def test_role_enforcement(
    env: Env, method: str, path: str, body: dict[str, Any] | None, minimum: str
) -> None:
    rank = {"explorer": 0, "analyst": 1, "curator": 2, "admin": 3}
    assert env.client.request(method, path, json=body).status_code == 401
    for role in ROLES:
        r = env.client.request(method, path, json=body, headers=env.h(role))
        if rank[role] < rank[minimum]:
            assert r.status_code == 403, (role, r.text)
        else:
            assert r.status_code != 403, (role, r.text)
            assert r.status_code != 401


def test_session_list_scoped_for_low_roles(env: Env) -> None:
    for role in ("analyst", "explorer"):
        assert env.client.get("/v1/sessions", headers=env.h(role)).status_code == 200
        other = str(env.users["admin"])
        r = env.client.get("/v1/sessions", params={"user_id": other}, headers=env.h(role))
        assert r.status_code == 403


def test_me_and_cookie_csrf(env: Env, app_engine: Engine) -> None:
    me = env.client.get("/v1/me", headers=env.h("curator")).json()
    assert me["role"] == "curator" and me["workspace_slug"] == env.slug
    assert me["csrf_token"] is None
    cookie, csrf = sessions.create_session(
        app_engine, env.ws_id, env.users["curator"], Settings(dev_auth=False)
    )
    c = TestClient(create_app(app_engine, EMB, settings=Settings(dev_auth=False)))
    c.cookies.set(sessions.COOKIE, cookie)
    me = c.get("/v1/me").json()
    assert me["csrf_token"] == csrf and me["auth"] == "cookie" and me["role"] == "curator"
    body = {**GUIDE, "key": "guides/cookie.md"}
    assert c.post("/v1/items", json=body).status_code == 403  # no CSRF header
    assert c.post("/v1/items", json=body, headers={sessions.CSRF_HEADER: csrf}).status_code == 201


def test_bearer_token_role(env: Env, app_engine: Engine) -> None:
    raw, _ = create_token(env.slug, "pat@x.test", "t", role="analyst", engine=app_engine)
    c = TestClient(create_app(app_engine, EMB, settings=Settings(dev_auth=False)))
    h = {"Authorization": f"Bearer {raw}"}
    assert c.get("/v1/items", headers=h).status_code == 200
    assert c.get("/v1/changes", headers=h).status_code == 403


# ---- library -----------------------------------------------------------------------------------


def test_library_filters_pagination_detail_diff(env: Env) -> None:
    h = env.h("explorer")
    page = env.client.get("/v1/items", params={"limit": 5, "offset": 0}, headers=h).json()
    assert page["total"] == 24 and len(page["items"]) == 5
    nxt = env.client.get("/v1/items", params={"limit": 5, "offset": 5}, headers=h).json()
    assert {i["key"] for i in nxt["items"]}.isdisjoint({i["key"] for i in page["items"]})

    def total(**p: Any) -> int:
        return int(env.client.get("/v1/items", params=p, headers=h).json()["total"])

    assert total(type="golden_query") == 5
    assert total(managed_by="git") == 24 and total(managed_by="ui") == 0
    assert total(state="published") == 24 and total(state="draft") == 0
    assert total(scope="workspace") > 0
    assert total(q="refund") >= 1
    assert total(q="no-such-text-xyzzy") == 0
    assert total(q="100%_") == 0  # wildcards are escaped

    d = env.client.get("/v1/items/workspace.md", headers=h).json()
    assert d["current_seq"] == d["published_seq"] == 1 and d["has_changes"] is False
    assert [v["seq"] for v in d["versions"]] == [1] and d["versions"][0]["is_published"]
    v = env.client.get("/v1/items/workspace.md/versions/1", headers=h).json()
    assert v["body"] == d["body"] and v["key"] == "workspace.md"
    assert env.client.get("/v1/items/workspace.md/versions/9", headers=h).status_code == 404
    assert env.client.get("/v1/items/nope.md/versions/1", headers=h).status_code == 404
    # keys with '#' and '/' work through the greedy path routes
    e = env.client.get(f"/v1/items/{k('glossary/business-terms.yaml#revenue')}/versions/1",
                       headers=h)
    assert e.status_code == 200


def test_diff_between_versions(env: Env) -> None:
    create_guide(env)
    new_body = GUIDE["body"].replace("island orders only", "island and ferry orders")
    r = env.client.put(f"/v1/items/{k(GUIDE['key'])}",
                       json={"body": new_body, "meta": GUIDE["meta"]}, headers=env.h("curator"))
    assert r.status_code == 200 and r.json()["current_seq"] == 2
    d = env.client.get(f"/v1/items/{k(GUIDE['key'])}/diff", params={"from": 1, "to": 2},
                       headers=env.h("explorer")).json()
    assert d["from_seq"] == 1 and d["to_seq"] == 2
    assert d["additions"] == 1 and d["deletions"] == 1
    assert "-The zanzibar surcharge applies to island orders only." in d["unified"]
    assert "+The zanzibar surcharge applies to island and ferry orders." in d["unified"]
    # defaults: published (none yet) -> current
    d2 = env.client.get(f"/v1/items/{k(GUIDE['key'])}/diff", headers=env.h("explorer")).json()
    assert d2["from_seq"] is None and d2["to_seq"] == 2


def seed_session(
    env: Env,
    user: str,
    *,
    client: str | None = "claude-code",
    question: str = "what is revenue?",
    sql: str | None = None,
    served: list[str] | None = None,
    rating: int | None = None,
    comment: str | None = None,
    signals: list[str] | None = None,
    sensitive: bool = False,
    days_ago: float = 0,
) -> uuid.UUID:
    when = datetime.now(UTC) - timedelta(days=days_ago)
    with admin_session(engine=env.engine) as s:
        sess = SessionRow(workspace_id=env.ws_id, user_id=env.users[user], client=client,
                          question=question, started_at=when, sensitive=sensitive)
        s.add(sess)
        s.flush()
        tcs: list[ToolCall] = []
        if sql:
            tc = ToolCall(
                session_id=sess.id, tool="run_sql", args={"sql": sql, "question": question},
                latency_ms=40, created_at=when,
                result_meta={"rows": 3, "query": "secret q"},
            )
            s.add(tc)
            tcs.append(tc)
        ctx = ToolCall(session_id=sess.id, tool="get_context_pack",
                       args={"question": question}, latency_ms=10, created_at=when,
                       error="boom 'literal'" if rating == -2 else None)
        s.add(ctx)
        s.flush()
        for rank, key in enumerate(served or [], start=1):
            item = s.scalars(select(ContextItem).where(
                ContextItem.key == key, ContextItem.workspace_id == env.ws_id)).one()
            assert item.current_version_id
            s.add(ServedContext(tool_call_id=ctx.id, version_id=item.current_version_id,
                                rank=rank, score=0.5))
        if rating in (1, -1):
            s.add(Feedback(session_id=sess.id, tool_call_id=ctx.id, user_id=env.users[user],
                           rating=rating, comment=comment, created_at=when))
        for kind in signals or []:
            s.add(Signal(workspace_id=env.ws_id, session_id=sess.id, user_id=env.users[user],
                         kind=kind, source="implicit", created_at=when,
                         payload={"question": question, "detail": "x" * 300}))
        return sess.id


def test_item_usage_stats(env: Env) -> None:
    seed_session(env, "analyst", served=["workspace.md"], rating=1)
    seed_session(env, "analyst", served=["workspace.md"], rating=-1)
    seed_session(env, "analyst", served=["workspace.md"], days_ago=45)  # outside the window
    d = env.client.get("/v1/items/workspace.md", headers=env.h("explorer")).json()
    assert d["usage"] == {"served_30d": 2, "thumbs_up": 1, "thumbs_down": 1}
    listed = env.client.get("/v1/items", params={"q": "workspace.md"}, headers=env.h("explorer"))
    row = next(i for i in listed.json()["items"] if i["key"] == "workspace.md")
    assert row["usage"]["served_30d"] == 2


# ---- workbench -----------------------------------------------------------------------------------


def test_git_items_are_read_only(env: Env) -> None:
    r = env.client.put("/v1/items/workspace.md", json={"body": "x", "meta": {}},
                       headers=env.h("admin"))
    assert r.status_code == 409 and "managed by git" in r.json()["detail"]
    r = env.client.post("/v1/items/workspace.md/deprecate", headers=env.h("admin"))
    assert r.status_code == 409 and "managed by git" in r.json()["detail"]
    assert env.client.get("/v1/changes", headers=env.h("curator")).json()["changes"] == []


def test_create_validation(env: Env) -> None:
    c, h = env.client, env.h("curator")
    bad = c.post("/v1/items", json={**GUIDE, "meta": {}}, headers=h)  # guide needs name
    assert bad.status_code == 422
    assert c.post("/v1/items", json={**GUIDE, "type": "nope"}, headers=h).status_code == 422
    assert c.post("/v1/items", json={**GUIDE, "key": "/abs.md"}, headers=h).status_code == 422
    assert c.post("/v1/items", json={**GUIDE, "scope_kind": "domain"}, headers=h).status_code == 422
    create_guide(env)
    assert c.post("/v1/items", json=GUIDE, headers=h).status_code == 409  # duplicate key
    gl = c.post("/v1/items", json={"key": "glossary/x.yaml", "type": "glossary_term",
                                   "body": "term: x\n"}, headers=h)
    assert gl.status_code == 422  # missing definition
    yml = c.post("/v1/items", json={"key": "glossary/y.yaml", "type": "glossary_term",
                                    "body": "term: yak\ndefinition: a hairy ox\n"}, headers=h)
    assert yml.status_code == 201
    upd = c.put(f"/v1/items/{k(GUIDE['key'])}", json={"body": "", "meta": {}}, headers=h)
    assert upd.status_code == 422


def test_draft_preview_publish_rollback(env: Env) -> None:
    c, cur = env.client, env.h("curator")
    first = env.client.get("/v1/releases", headers=cur).json()
    assert first["total"] == 1 and first["releases"][0]["is_current"]
    base_release = first["releases"][0]
    base_items = base_release["item_count"]

    d = create_guide(env)
    assert d["state"] == "draft" and d["managed_by"] == "ui" and d["published_seq"] is None
    ch = c.get("/v1/changes", headers=cur).json()["changes"]
    assert [(x["key"], x["kind"]) for x in ch] == [(GUIDE["key"], "create")]
    assert ch[0]["diff"]["additions"] > 0 and ch[0]["diff"]["from_seq"] is None
    # not visible to agents until published
    assert GUIDE["key"] not in search_keys(env, "/v1/search")

    # unknown selection
    assert c.post("/v1/previews", json={"keys": ["nope"]}, headers=cur).status_code == 422
    pv = c.post("/v1/previews", json={"keys": [GUIDE["key"]], "note": "try it"}, headers=cur)
    assert pv.status_code == 201, pv.text
    p = pv.json()
    assert p["kind"] == "preview" and p["removed_keys"] == []
    assert p["changed_keys"] == [GUIDE["key"]]
    assert p["item_count"] == base_items + 1
    assert p["parent_release_id"] == base_release["id"]

    # the draft is retrievable from the preview but not from the published release
    assert GUIDE["key"] in search_keys(env, f"/v1/previews/{p['id']}/search")
    assert GUIDE["key"] not in search_keys(env, "/v1/search")
    pack = c.post(f"/v1/previews/{p['id']}/context-pack",
                  json={"question": "zanzibar surcharge", "budget_tokens": 3000},
                  headers=cur).json()
    assert pack["release_id"] == p["id"]
    assert any(e["key"] == GUIDE["key"] for e in pack["entries"])
    assert c.post(f"/v1/previews/{uuid.uuid4()}/search", json={"query": "x"},
                  headers=cur).status_code == 404

    pub = c.post(f"/v1/previews/{p['id']}/publish", headers=cur)
    assert pub.status_code == 200, pub.text
    rel = pub.json()
    assert rel["kind"] == "published" and rel["seq"] == 2 and rel["id"] == p["id"]
    assert rel["is_current"] and rel["item_count"] == base_items + 1
    assert GUIDE["key"] in search_keys(env, "/v1/search")
    item = c.get(f"/v1/items/{k(GUIDE['key'])}", headers=cur).json()
    assert item["state"] == "published" and item["published_seq"] == 1
    assert c.get("/v1/changes", headers=cur).json()["changes"] == []
    assert c.post(f"/v1/previews/{p['id']}/publish", headers=cur).status_code == 409

    # second change: edit -> preview -> publish
    c.put(f"/v1/items/{k(GUIDE['key'])}",
          json={"body": GUIDE["body"] + "\n\nAlso ferries.", "meta": GUIDE["meta"]}, headers=cur)
    p2 = c.post("/v1/previews", json={}, headers=cur).json()
    assert p2["seq"] == 1 and p2["changed_keys"] == [GUIDE["key"]]  # previews number separately
    # a stale preview cannot be published after another release lands
    p3 = c.post("/v1/previews", json={}, headers=cur).json()
    assert c.post(f"/v1/previews/{p2['id']}/publish", headers=cur).status_code == 200
    stale = c.post(f"/v1/previews/{p3['id']}/publish", headers=cur)
    assert stale.status_code == 409 and "create a new preview" in stale.json()["detail"]
    assert c.get(f"/v1/items/{k(GUIDE['key'])}", headers=cur).json()["published_seq"] == 2

    hist = c.get("/v1/releases", headers=cur).json()
    assert [r["seq"] for r in hist["releases"]] == [3, 2, 1]
    assert [r["is_current"] for r in hist["releases"]] == [True, False, False]

    # rollback to release 1: the guide leaves the live context and becomes a draft again
    rb = c.post(f"/v1/releases/{base_release['id']}/rollback", headers=cur)
    assert rb.status_code == 201, rb.text
    r4 = rb.json()
    assert r4["seq"] == 4 and r4["item_count"] == base_items and r4["is_current"]
    assert "rollback to release #1" in r4["note"]
    assert GUIDE["key"] not in search_keys(env, "/v1/search")
    item = c.get(f"/v1/items/{k(GUIDE['key'])}", headers=cur).json()
    assert item["state"] == "draft" and item["published_seq"] is None and item["has_changes"]
    assert c.post(f"/v1/releases/{r4['id']}/rollback", headers=cur).status_code == 409
    assert c.post(f"/v1/releases/{p['id']}/rollback", headers=cur).status_code == 201
    assert GUIDE["key"] in search_keys(env, "/v1/search")

    with admin_session(engine=env.engine) as s:
        actions = list(s.scalars(select(AuditEvent.action).where(
            AuditEvent.workspace_id == env.ws_id).order_by(AuditEvent.id)))
        assert actions.count("release.publish") == 2 and actions.count("release.rollback") == 2
        assert "item.create" in actions and "item.update" in actions
        n_rel = s.scalars(select(Release).where(Release.workspace_id == env.ws_id,
                                                Release.kind == "published")).all()
        assert len(n_rel) == 5


def test_deprecate_flows_through_publish(env: Env) -> None:
    c, cur = env.client, env.h("curator")
    create_guide(env)
    pid = c.post("/v1/previews", json={}, headers=cur).json()["id"]
    c.post(f"/v1/previews/{pid}/publish", headers=cur)
    dep = c.post(f"/v1/items/{k(GUIDE['key'])}/deprecate", headers=cur)
    assert dep.status_code == 200 and dep.json()["state"] == "deprecated"
    assert c.post(f"/v1/items/{k(GUIDE['key'])}/deprecate", headers=cur).status_code == 409
    assert c.put(f"/v1/items/{k(GUIDE['key'])}", json={"body": "x", "meta": {}},
                 headers=cur).status_code == 409
    # still live until a release removes it
    assert GUIDE["key"] in search_keys(env, "/v1/search")
    ch = c.get("/v1/changes", headers=cur).json()["changes"]
    assert [(x["key"], x["kind"]) for x in ch] == [(GUIDE["key"], "deprecate")]
    p = c.post("/v1/previews", json={}, headers=cur).json()
    assert p["removed_keys"] == [GUIDE["key"]]
    c.post(f"/v1/previews/{p['id']}/publish", headers=cur)
    assert GUIDE["key"] not in search_keys(env, "/v1/search")
    assert c.get(f"/v1/items/{k(GUIDE['key'])}", headers=cur).json()["published_seq"] is None
    assert c.get("/v1/changes", headers=cur).json()["changes"] == []
    assert c.post("/v1/previews", json={}, headers=cur).status_code == 422  # nothing left
    with admin_session(engine=env.engine) as s:
        assert "item.deprecate" in list(s.scalars(select(AuditEvent.action)))


# ---- proposals -----------------------------------------------------------------------------------


def propose(env: Env, **args: Any) -> str:
    ctx = InvokeContext(env.engine, EMB,
                        Principal(env.ws_id, env.users["analyst"], "analyst"), client_name="t")
    started = json.loads(
        invoke_tool(ctx, "start_analysis", {"question": "how do we price zanzibar?"})
    )
    out = invoke_tool(ctx, "propose_context_change",
                      {"session_id": started["session_id"], "rationale": "seen twice", **args})
    return str(json.loads(out)["proposal_key"])


def test_proposals_accept_and_reject(env: Env) -> None:
    c, cur = env.client, env.h("curator")
    new_key = propose(env, op="create_item", type="guide", body=GUIDE["body"],
                      meta=GUIDE["meta"])
    upd_key = propose(env, op="update_item", type="glossary_term",
                      body="term: revenue\ndefinition: net of refunds, in AUD cents\n",
                      target="glossary/business-terms.yaml#revenue")
    ui_target = create_guide(env, key="guides/target.md")
    ui_key = propose(env, op="update_item", type="guide", body="Updated by agent.",
                     meta=GUIDE["meta"], target=ui_target["key"])
    rej_key = propose(env, op="create_item", type="guide", body="meh", meta=GUIDE["meta"])

    props = c.get("/v1/proposals", headers=cur).json()["proposals"]
    assert {p["key"] for p in props} == {new_key, upd_key, ui_key, rej_key}
    first = next(p for p in props if p["key"] == new_key)
    assert first["op"] == "create_item" and first["rationale"] == "seen twice"
    assert first["session_id"] and first["session_question"] == "how do we price zanzibar?"
    assert first["proposed_by_label"] == "analyst"
    upd = next(p for p in props if p["key"] == ui_key)
    assert upd["target"] == ui_target["key"] and "Updated by agent." in upd["diff"]["unified"]
    assert c.get("/v1/proposals", headers=env.h("analyst")).status_code == 403
    # proposals are not live context
    assert new_key not in search_keys(env, "/v1/search", "zanzibar")

    # create_item -> draft under a chosen key
    r = c.post(f"/v1/proposals/{k(new_key)}/accept", json={"key": "guides/accepted.md"},
               headers=cur)
    assert r.status_code == 200, r.text
    assert r.json()["item_key"] == "guides/accepted.md" and r.json()["item_seq"] == 1
    got = c.get("/v1/items/guides/accepted.md", headers=cur).json()
    assert got["state"] == "draft" and got["versions"][0]["provenance"]["source"] == "agent"
    assert c.get("/v1/items/guides/accepted.md", headers=cur).status_code == 200
    ch = {x["key"] for x in c.get("/v1/changes", headers=cur).json()["changes"]}
    assert "guides/accepted.md" in ch
    assert c.post(f"/v1/proposals/{k(new_key)}/accept", json={}, headers=cur).status_code == 404

    # update_item on a ui item -> new draft version on the target
    r = c.post(f"/v1/proposals/{k(ui_key)}/accept", json={}, headers=cur)
    assert r.status_code == 200 and r.json()["item_key"] == ui_target["key"]
    assert r.json()["item_seq"] == 2
    t = c.get(f"/v1/items/{k(ui_target['key'])}", headers=cur).json()
    assert t["current_seq"] == 2 and t["versions"][0]["provenance"]["source"] == "proposal"

    # update_item on a git-managed target is refused (PRs come later); proposal stays open
    r = c.post(f"/v1/proposals/{k(upd_key)}/accept", json={}, headers=cur)
    assert r.status_code == 409 and "managed by git" in r.json()["detail"]

    # reject needs a reason
    assert c.post(f"/v1/proposals/{k(rej_key)}/reject", json={}, headers=cur).status_code == 422
    r = c.post(f"/v1/proposals/{k(rej_key)}/reject", json={"reason": "duplicate"}, headers=cur)
    assert r.status_code == 200 and r.json()["decision"] == "rejected"
    left = {p["key"] for p in c.get("/v1/proposals", headers=cur).json()["proposals"]}
    assert left == {upd_key}
    assert c.post(f"/v1/proposals/{k(rej_key)}/reject", json={"reason": "x"},
                  headers=cur).status_code == 409
    with admin_session(engine=env.engine) as s:
        evs = list(s.scalars(select(AuditEvent).where(AuditEvent.action.like("proposal.%"))
                             .where(AuditEvent.workspace_id == env.ws_id)))
        assert sorted(e.action for e in evs) == ["proposal.accept", "proposal.accept",
                                                 "proposal.reject"]
        assert any(e.target.get("reason") == "duplicate" for e in evs)


# ---- observability + evidence visibility --------------------------------------------------

SQL = "select * from orders where customer = 'Alice Smith' and note = 'it''s x' and total > 100"
LONG_Q = "Why is revenue for Alice Smith so odd? " + "padding " * 40


def test_observability_summary(env: Env) -> None:
    seed_session(env, "analyst", client="claude-code", served=["workspace.md"], rating=1,
                 signals=["sql_error", "sql_error"], sql=SQL)
    seed_session(env, "analyst2", client="cursor",
                 served=["workspace.md", "queries/refund-rate.yaml"], rating=-1,
                 signals=["gap_flagged"])
    seed_session(env, "admin", client="claude-code", days_ago=20)
    seed_session(env, "admin", client="claude-code", days_ago=60)
    cur = env.h("curator")
    s7 = env.client.get("/v1/observability/summary", params={"days": 7}, headers=cur).json()
    assert s7["days"] == 7 and s7["total_sessions"] == 2
    assert sorted((d["client"], d["sessions"]) for d in s7["sessions_per_day"]) == [
        ("claude-code", 1), ("cursor", 1)]
    assert {t["tool"]: t["calls"] for t in s7["top_tools"]} == {"get_context_pack": 2, "run_sql": 1}
    assert {s["kind"]: s["count"] for s in s7["signals_by_kind"]} == {
        "sql_error": 2, "gap_flagged": 1}
    assert s7["feedback"] == {
        "positive": 1, "negative": 1, "human_positive": 0, "human_negative": 0,
        "agent_positive": 1, "agent_negative": 1}
    top = s7["most_served_items"][0]
    assert top["key"] == "workspace.md" and top["served"] == 2
    assert top["thumbs_up"] == 1 and top["thumbs_down"] == 1
    assert env.client.get("/v1/observability/summary", params={"days": 30},
                          headers=cur).json()["total_sessions"] == 3
    assert env.client.get("/v1/observability/summary", params={"days": 90},
                          headers=cur).json()["total_sessions"] == 4
    assert env.client.get("/v1/observability/summary", params={"days": 5},
                          headers=cur).status_code == 422
    f = env.client.get("/v1/observability/summary",
                       params={"client": "cursor", "days": 7}, headers=cur).json()
    assert f["total_sessions"] == 1 and [s["kind"] for s in f["signals_by_kind"]] == ["gap_flagged"]
    f = env.client.get("/v1/observability/summary",
                       params={"user": str(env.users["analyst"])}, headers=cur).json()
    fb = f["feedback"]
    assert f["total_sessions"] == 1 and (fb["positive"], fb["negative"]) == (1, 0)
    f = env.client.get("/v1/observability/summary", params={"signal_kind": "sql_error"},
                       headers=cur).json()
    assert f["total_sessions"] == 1 and f["signals_by_kind"] == [{"kind": "sql_error", "count": 2}]

    lst = env.client.get("/v1/sessions", params={"client": "claude-code", "days": 7},
                         headers=cur).json()
    assert lst["total"] == 1
    row = lst["sessions"][0]
    assert row["signal_count"] == 2 and row["thumbs_up"] == 1 and row["tool_call_count"] == 2
    assert row["user_label"] == "analyst" and row["client"] == "claude-code"


def test_session_inspector_redaction_by_role(env: Env) -> None:
    sid = seed_session(env, "analyst", question=LONG_Q, sql=SQL, served=["workspace.md"],
                       rating=-1, comment="c" * 300, signals=["sql_error"])
    url = f"/v1/sessions/{sid}"

    # admin: raw
    a = env.client.get(url, headers=env.h("admin")).json()
    assert a["redaction"] == "none" and a["question"] == LONG_Q
    run = next(t for t in a["timeline"] if t["tool"] == "run_sql")
    assert run["args"]["sql"] == SQL and run["result_meta"]["query"] == "secret q"
    assert a["feedback"][0]["comment"] == "c" * 300

    # curator: SQL literals -> '?', questions truncated to 120
    cu = env.client.get(url, headers=env.h("curator")).json()
    assert cu["redaction"] == "redacted"
    assert len(cu["question"]) <= 120 and cu["question"] != LONG_Q
    run = next(t for t in cu["timeline"] if t["tool"] == "run_sql")
    sql = run["args"]["sql"]
    assert "'?'" in sql and "Alice Smith" not in sql and "x'" not in sql
    assert "100" not in sql and "total > ?" in sql  # numeric literals are scrubbed too
    assert len(run["args"]["question"]) <= 120
    assert run["result_meta"]["rows"] == 3 and run["result_meta"]["query"] == "secret q"[:120]
    assert len(cu["feedback"][0]["comment"]) <= 120
    assert len(cu["signals"][0]["payload"]["detail"]) <= 120
    # the served context + timeline structure is intact
    ctx = next(t for t in cu["timeline"] if t["tool"] == "get_context_pack")
    assert ctx["served"][0]["item_key"] == "workspace.md" and ctx["served"][0]["rank"] == 1

    # owner sees their own raw data
    o = env.client.get(url, headers=env.h("analyst")).json()
    assert o["redaction"] == "none" and o["question"] == LONG_Q
    # other analysts / explorers: 403
    for role in ("analyst2", "explorer"):
        assert env.client.get(url, headers=env.h(role)).status_code == 403
    assert env.client.get(f"/v1/sessions/{uuid.uuid4()}", headers=env.h("admin")).status_code == 404


def test_sensitive_sessions_are_metadata_only(env: Env) -> None:
    sid = seed_session(env, "analyst", question="secret merger numbers", sql=SQL,
                       served=["workspace.md"], rating=-1, comment="private", signals=["sql_error"])
    url = f"/v1/sessions/{sid}"
    assert env.client.post(f"{url}/sensitive", json={"sensitive": True},
                           headers=env.h("curator")).status_code == 403
    r = env.client.post(f"{url}/sensitive", json={"sensitive": True}, headers=env.h("admin"))
    assert r.status_code == 200 and r.json()["sensitive"] is True

    for role in ("curator", "analyst"):  # even the owner
        d = env.client.get(url, headers=env.h(role)).json()
        assert d["redaction"] == "metadata_only" and d["question"] is None
        run = next(t for t in d["timeline"] if t["tool"] == "run_sql")
        assert run["args"] is None and run["error"] is None
        assert run["tool"] == "run_sql" and run["latency_ms"] == 40
        assert run["result_meta"] == {"rows": 3}  # counts only, no strings
        assert d["signals"][0]["payload"] is None and d["signals"][0]["kind"] == "sql_error"
        assert d["feedback"][0]["comment"] is None and d["feedback"][0]["rating"] == -1
        assert d["timeline"][1]["served"][0]["item_key"] == "workspace.md"
    lst = env.client.get("/v1/sessions", headers=env.h("curator")).json()["sessions"]
    assert next(s for s in lst if s["id"] == str(sid))["question"] is None
    d = env.client.get(url, headers=env.h("admin")).json()
    assert d["redaction"] == "none" and d["question"] == "secret merger numbers"
    assert d["timeline"][0]["args"]["sql"] == SQL
    # list question for admin raw, for curator truncated
    seed_session(env, "analyst", question=LONG_Q)
    qs = {s["question"] for s in env.client.get("/v1/sessions", headers=env.h("curator"))
          .json()["sessions"] if s["question"]}
    assert LONG_Q not in qs and any(len(q) <= 120 for q in qs)


def test_redaction_helpers() -> None:
    from satchel.curation.redact import redact_sql, truncate

    assert redact_sql("select 1 from t where a = 'x' and b = 'it''s'") == (
        "SELECT ? FROM t WHERE a = '?' AND b = '?'")
    # numbers are PII (ids, phone numbers); LIMIT/OFFSET, INTERVAL and ordinals stay readable
    assert redact_sql(
        "select a, count(*) from c where id = 12345 and phone = 0412345678 and p > 1.5e3 "
        "and q in (7, 8) group by 1 order by 1 limit 10 offset 20"
    ) == ("SELECT a, COUNT(*) FROM c WHERE id = ? AND phone = ? AND p > ? AND q IN (?, ?) "
          "GROUP BY 1 ORDER BY 1 LIMIT 10 OFFSET 20")
    assert redact_sql("select * from t where d > now() - interval '30' day and id = 99") == (
        "SELECT * FROM t WHERE d > NOW() - INTERVAL '30' DAY AND id = ?")
    assert redact_sql('select t1.a from t1 where "col2" = 5') == (
        'SELECT t1.a FROM t1 WHERE "col2" = ?')
    broken = "select ((( from t where a = 'secret' and id = 55 and n = 4.5 limit 5 offset 10"
    assert redact_sql(broken) == (
        "select ((( from t where a = '?' and id = ? and n = ? limit 5 offset 10")
    assert redact_sql(
        "select ((( from t where x > interval '7' day and \"t2\" = 8 and c_1 = 9"
    ) == "select ((( from t where x > interval '7' day and \"t2\" = ? and c_1 = ?"
    assert truncate("a" * 200) == "a" * 119 + "…" and truncate("short") == "short"


# ---- catalog -----------------------------------------------------------------------------------


def test_catalog(env: Env) -> None:
    h = env.h("explorer")
    with admin_session(engine=env.engine) as s:
        conn = Connection(workspace_id=env.ws_id, kind="duckdb", name="extra",
                          config={"path": "/secret/path"}, secret_ref="env:X")
        s.add(conn)
        s.flush()
        s.add(Asset(workspace_id=env.ws_id, connection_id=conn.id, kind="table",
                    fqn="main.ghost", dropped_at=datetime.now(UTC)))
    conns = env.client.get("/v1/connections", headers=h).json()
    extra = next(c for c in conns if c["name"] == "extra")
    assert extra["has_secret"] and extra["asset_count"] == 0
    assert extra.get("config") is None and "secret_ref" not in extra  # admins only
    page = env.client.get("/v1/assets", headers=h).json()
    by_fqn = {a["fqn"]: a for a in page["assets"]}
    documented = [a for a in page["assets"] if a["has_doc"]]
    assert documented and documented[0]["doc_item_key"].startswith("assets/")
    assert any(a["children"] for a in documented)
    assert by_fqn["main.ghost"]["dropped"] is True and not by_fqn["main.ghost"]["has_doc"]
    f = env.client.get("/v1/assets", params={"connection": "extra"}, headers=h).json()
    assert [a["fqn"] for a in f["assets"]] == ["main.ghost"]
    q = env.client.get("/v1/assets", params={"q": "ghost"}, headers=h).json()
    assert q["total"] == 1
    assert env.client.get("/v1/assets", params={"connection": "zzz"}, headers=h).status_code == 404
    one = env.client.get(f"/v1/assets/{documented[0]['id']}", headers=h).json()
    assert one["linked_items"] and one["linked_items"][0]["relation"] in {"defines", "mentions",
                                                                           "uses", "applies_to"}
    assert env.client.get(f"/v1/assets/{uuid.uuid4()}", headers=h).status_code == 404


def test_endorsement_flags(env: Env) -> None:
    c, cur = env.client, env.h("curator")
    tbl = next(a for a in c.get("/v1/assets", headers=cur).json()["assets"] if a["has_doc"])
    body = f"asset: {tbl['fqn']}\nendorsed: true\nexcluded_from_ai: true\n"
    assert c.post("/v1/items", json={"key": "endorse/x.yaml", "type": "endorsement", "body": body},
                  headers=cur).status_code == 201
    pid = c.post("/v1/previews", json={}, headers=cur).json()["id"]
    c.post(f"/v1/previews/{pid}/publish", headers=cur)
    assets = c.get("/v1/assets", headers=cur).json()["assets"]
    after = next(a for a in assets if a["id"] == tbl["id"])
    assert after["endorsed"] and after["excluded_from_ai"]


# ---- RLS across workspaces + contract ------------------------------------------------------------


def test_workspaces_are_isolated(env: Env, app_engine: Engine, tmp_path: Path) -> None:
    other = _make_env(app_engine, tmp_path / "b", "other")
    create_guide(env)
    sid = seed_session(env, "analyst", served=["workspace.md"])
    pid = env.client.post("/v1/previews", json={}, headers=env.h("curator")).json()["id"]
    oh = other.h("admin")
    assert env.client.get("/v1/items/guides/zanzibar.md", headers=oh).status_code == 404
    assert other.client.get("/v1/items/guides/zanzibar.md", headers=oh).status_code == 404
    assert other.client.get(f"/v1/sessions/{sid}", headers=oh).status_code == 404
    assert other.client.get(f"/v1/previews/{pid}", headers=oh).status_code == 404
    assert other.client.post(f"/v1/previews/{pid}/publish", headers=oh).status_code == 404
    assert other.client.post(f"/v1/releases/{pid}/rollback", headers=oh).status_code == 404
    assert other.client.get("/v1/changes", headers=oh).json()["changes"] == []
    assert other.client.get("/v1/sessions", headers=oh).json()["total"] == 0
    # same key can exist independently in both workspaces
    assert other.client.post("/v1/items", json=GUIDE, headers=oh).status_code == 201
    # a user of workspace A cannot be used against workspace B's slug
    cross = {"X-Satchel-Workspace": other.slug, "X-Satchel-User": str(env.users["admin"])}
    assert other.client.get("/v1/items", headers=cross).status_code == 403


def test_openapi_contract(env: Env, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from satchel.cli.main import app as cli

    doc = env.client.get("/openapi.json").json()
    op_ids = [o["operationId"] for p in doc["paths"].values() for o in p.values()]
    assert len(op_ids) == len(set(op_ids))
    assert all("_v1_" not in i for i in op_ids), op_ids
    assert "/v1/proposals/{key}/accept" in doc["paths"]
    res = CliRunner().invoke(cli, ["openapi"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["paths"].keys() == doc["paths"].keys()
    committed = Path(__file__).resolve().parents[2] / "ui" / "openapi.json"
    assert committed.read_text() == res.output, "run `satchel openapi -o ../ui/openapi.json`"
