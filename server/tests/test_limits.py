"""Settings → Limits (per-workspace overrides of the deployment defaults), session retention,
and the read-only Settings → Deployment view."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select

from satchel import limits
from satchel.api.app import create_app
from satchel.core.crypto import generate_key
from satchel.data.result_store import LocalResultStore
from satchel.data.results import store_result
from satchel.llm.embeddings import HashEmbedder
from satchel.settings import Settings
from satchel.store.db import admin_session, workspace_session
from satchel.store.models import (
    AppUser,
    AuditEvent,
    Feedback,
    Signal,
    ToolCall,
    Workspace,
)
from satchel.store.models import Session as AgentSession
from satchel.sync.importer import get_or_create_workspace
from tests.helpers import unique_slug

KEY = generate_key()


def st(**kw: Any) -> Settings:
    return Settings(dev_auth=True, encryption_key=SecretStr(KEY), **kw)


class Env:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.slug = unique_slug("lim")
        self.users = {r: uuid.uuid4() for r in ("admin", "curator", "explorer")}
        with admin_session(engine=engine) as s:
            self.ws_id = get_or_create_workspace(s, self.slug).id
            for r, uid in self.users.items():
                s.add(AppUser(id=uid, workspace_id=self.ws_id, role=r,
                              email=f"{r}@{self.slug}.test"))
        self.client = TestClient(create_app(engine, HashEmbedder(), mcp=False,
                                            settings=settings))

    def h(self, role: str = "admin") -> dict[str, str]:
        return {"X-Satchel-Workspace": self.slug, "X-Satchel-User": str(self.users[role])}

    def get(self, path: str, role: str = "admin") -> Any:
        return self.client.get(path, headers=self.h(role))

    def put(self, values: dict[str, Any], role: str = "admin") -> Any:
        return self.client.put("/v1/settings/limits", json={"values": values},
                               headers=self.h(role))


@pytest.fixture
def env(app_engine: Engine) -> Env:
    return Env(app_engine, st(sql_row_cap=250))


def by_name(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["name"]: r for r in rows}


def test_limits_default_to_the_deployment_and_can_be_overridden(env: Env) -> None:
    r = env.get("/v1/settings/limits", role="curator")
    assert r.status_code == 200
    rows = by_name(r.json())
    assert set(rows) == {s.name for s in limits.SPECS}
    cap = rows["sql_row_cap"]
    assert (cap["value"], cap["deployment"], cap["overridden"]) == (250, 250, False)
    assert cap["env"] == "SATCHEL_SQL_ROW_CAP"
    assert rows["sql_timeout_s"]["value"] == 30  # float setting shown as a whole number
    assert rows["session_retention_days"]["value"] is None  # keep forever

    r = env.put({"sql_row_cap": 5000, "session_retention_days": 90})
    assert r.status_code == 200, r.text
    rows = by_name(r.json())
    assert (rows["sql_row_cap"]["value"], rows["sql_row_cap"]["deployment"]) == (5000, 250)
    assert rows["sql_row_cap"]["overridden"] is True
    assert rows["session_retention_days"]["value"] == 90
    with admin_session(engine=env.engine) as s:
        eff = limits.for_workspace(s, env.ws_id, st(sql_row_cap=250))
        ws = s.get(Workspace, env.ws_id)
        assert ws is not None
        ws.settings = {**ws.settings, "publish": {"gate": "warn"}}
    assert eff.sql_row_cap == 5000 and eff.session_retention_days == 90

    # "default" removes the override; other workspace settings are untouched.
    rows = by_name(env.put({"sql_row_cap": "default"}).json())
    assert (rows["sql_row_cap"]["value"], rows["sql_row_cap"]["overridden"]) == (250, False)
    assert rows["session_retention_days"]["value"] == 90
    with admin_session(engine=env.engine) as s:
        ws = s.get(Workspace, env.ws_id)
        assert ws is not None and ws.settings["publish"] == {"gate": "warn"}

    # Back to "keep forever" while the deployment default would also be forever: still an
    # explicit override, so a later deployment default does not apply to this workspace.
    rows = by_name(env.put({"session_retention_days": None}).json())
    assert rows["session_retention_days"]["overridden"] is True
    assert rows["session_retention_days"]["value"] is None

    with admin_session(engine=env.engine) as s:
        events = list(s.scalars(select(AuditEvent).where(
            AuditEvent.workspace_id == env.ws_id, AuditEvent.action == "workspace.limits")
            .order_by(AuditEvent.id)))
        assert [e.target["changed"] for e in events] == [
            ["session_retention_days", "sql_row_cap"], ["sql_row_cap"],
            ["session_retention_days"]]


@pytest.mark.parametrize("values, msg", [
    ({"sql_row_cap": 0}, "between 1 and 100000"),
    ({"sql_timeout_s": 601}, "between 1 and 600"),
    ({"result_ttl_days": 1.5}, "whole number"),
    ({"sql_row_cap": None}, "needs a value"),
    ({"session_retention_days": 3}, "between 7 and 3650"),
    ({"nope": 1}, "unknown limit"),
])
def test_limits_are_validated(env: Env, values: dict[str, Any], msg: str) -> None:
    r = env.put(values)
    assert r.status_code == 422 and msg in r.text, r.text


def test_only_admins_change_limits_or_see_the_deployment(env: Env) -> None:
    assert env.put({"sql_row_cap": 10}, role="curator").status_code == 403
    assert env.get("/v1/settings/limits", role="explorer").status_code == 403
    assert env.get("/v1/settings/deployment", role="curator").status_code == 403


def test_result_ttl_override_sets_expiry(env: Env, tmp_path: Path) -> None:
    env.put({"result_ttl_days": 2})
    with workspace_session(env.ws_id, env.users["admin"], engine=env.engine) as s:
        row = store_result(s, workspace_id=env.ws_id, session_id=None, tool_call_id=None,
                           connection_id=None, sql="select 1", table=pa.table({"a": [1]}),
                           truncated=False, store=LocalResultStore(tmp_path))
        left = row.expires_at - datetime.now(UTC)
    assert timedelta(days=1, hours=23) < left <= timedelta(days=2)


def test_deployment_view_hides_secrets(app_engine: Engine) -> None:
    settings = st(oidc_client_secret="oidc-shh-123", chat_api_key="sk-chat-456",
                  database_url="postgresql+psycopg://satchel_app:db-pw-789@db:5432/satchel")
    e = Env(app_engine, settings)
    # The app runs on app_engine; the view reports the configured URL, password hidden.
    r = e.get("/v1/settings/deployment")
    assert r.status_code == 200, r.text
    body = r.text
    for secret in ("oidc-shh-123", "sk-chat-456", "db-pw-789", KEY):
        assert secret not in body
    rows = {(g["title"], x["label"]): x for g in r.json()["groups"] for x in g["rows"]}
    assert rows[("Database", "App connection")]["value"] == (
        "postgresql+psycopg://satchel_app:***@db:5432/satchel")
    assert rows[("Sign-in", "OIDC client secret")]["value"] == "set"
    assert rows[("Encryption", "Encryption key")] == {
        "label": "Encryption key", "env": "SATCHEL_ENCRYPTION_KEY", "value": "set",
        "source": "env", "secret": True}
    assert rows[("Server", "Log format")]["source"] == "default"
    assert rows[("Sign-in", "Refresh token lifetime")]["value"] == "30 days"
    assert rows[("Data", "SQL timeout (default)")]["value"] == "30 seconds"
    assert rows[("Data", "Session retention (default)")]["value"] == "keep forever"
    assert rows[("Database", "Schema revision")]["value"]


# ----------------------------------------------------------------------------- retention


def _session(s: Any, ws: uuid.UUID, age_days: int, **kw: Any) -> uuid.UUID:
    sid = uuid.uuid4()
    s.add(AgentSession(id=sid, workspace_id=ws,
                       started_at=datetime.now(UTC) - timedelta(days=age_days), **kw))
    s.flush()
    s.add(ToolCall(session_id=sid, tool="search_context"))
    return sid


def test_session_retention_purges_old_sessions(app_engine: Engine) -> None:
    keep_env = Env(app_engine, st())  # no retention: untouched
    e = Env(app_engine, st())
    e.put({"session_retention_days": 30})
    with admin_session(engine=app_engine) as s:
        old = _session(s, e.ws_id, 40)
        old_fb = _session(s, e.ws_id, 40)
        new = _session(s, e.ws_id, 5)
        other = _session(s, keep_env.ws_id, 400)
        s.add(Feedback(session_id=old_fb, user_id=e.users["admin"], rating=1))
        s.add(Signal(workspace_id=e.ws_id, session_id=old, kind="no_results", source="agent"))
    for ws in (e.ws_id, keep_env.ws_id):
        limits.purge_old_sessions(st(), engine=app_engine, workspace_id=ws)
    with admin_session(engine=app_engine) as s:
        left = set(s.scalars(select(AgentSession.id).where(
            AgentSession.id.in_([old, old_fb, new, other]))))
        assert left == {old_fb, new, other}
        assert not list(s.scalars(select(ToolCall.id).where(ToolCall.session_id == old)))
        sig = s.scalars(select(Signal).where(Signal.workspace_id == e.ws_id)).one()
        assert sig.session_id is None  # kept, detached
        ev = s.scalars(select(AuditEvent).where(AuditEvent.workspace_id == e.ws_id,
                                                AuditEvent.action == "retention.purge")).one()
        assert ev.target == {"sessions": 1, "tool_calls": 1, "older_than_days": 30}
    # Nothing left to do on the next pass.
    again = limits.purge_old_sessions(st(), engine=app_engine, workspace_id=e.ws_id)
    assert (again.sessions, again.workspaces) == (0, 0)


def test_deployment_default_retention_applies_to_workspaces_without_override(
    app_engine: Engine,
) -> None:
    e = Env(app_engine, st())
    with admin_session(engine=app_engine) as s:
        old = _session(s, e.ws_id, 100)
    rep = limits.purge_old_sessions(st(session_retention_days=60), engine=app_engine,
                                    workspace_id=e.ws_id)
    assert (rep.sessions, rep.tool_calls, rep.workspaces) == (1, 1, 1)
    with admin_session(engine=app_engine) as s:
        assert s.get(AgentSession, old) is None
