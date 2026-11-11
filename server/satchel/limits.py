"""Workspace limits and retention (Settings → Limits).

The deployment's environment sets the defaults (``SATCHEL_SQL_ROW_CAP``, ``SATCHEL_SQL_TIMEOUT_S``,
``SATCHEL_RESULT_TTL_DAYS``, ``SATCHEL_SESSION_RETENTION_DAYS``); an admin can override each one
for their workspace, within fixed bounds. Overrides live in ``workspace.settings["limits"]``;
removing one goes back to the deployment value.

``purge_old_sessions`` is the retention job the worker runs after each pass: sessions older than
the workspace's retention are deleted with their tool calls and served context. Signals, eval
results and cached results that point at them are kept and detached. Sessions with feedback are
kept, since feedback is evidence for open and past suggestions.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, delete, exists, select, update
from sqlalchemy.orm import Session

from satchel.audit import record as record_audit
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import (
    EvalCaseResult,
    Feedback,
    QueryResult,
    ServedContext,
    Signal,
    ToolCall,
    Workspace,
)
from satchel.store.models import Session as AgentSession

log = logging.getLogger("satchel.limits")


@dataclass(frozen=True)
class LimitSpec:
    name: str
    label: str
    unit: str
    min: int
    max: int
    help: str
    nullable: bool = False  # None = no limit (keep forever)
    null_label: str | None = None


SPECS: tuple[LimitSpec, ...] = (
    LimitSpec("sql_row_cap", "SQL row cap", "rows", 1, 100_000,
              "Most rows one query returns to an agent. Larger results are truncated."),
    LimitSpec("sql_timeout_s", "SQL timeout", "seconds", 1, 600,
              "Queries running longer are cancelled in the warehouse."),
    LimitSpec("result_ttl_days", "Result cache", "days", 1, 365,
              "How long query results stay available to page through and download."),
    LimitSpec("session_retention_days", "Session retention", "days", 7, 3650,
              "Agent sessions and their tool calls older than this are deleted. Sessions with "
              "feedback are kept.", nullable=True, null_label="Keep forever"),
)
BY_NAME = {s.name: s for s in SPECS}


class LimitsError(ValueError):
    """Invalid limit (the API maps it to 422)."""


@dataclass(frozen=True)
class Limits:
    sql_row_cap: int
    sql_timeout_s: float
    result_ttl_days: int
    session_retention_days: int | None


def deployment_value(settings: Settings, name: str) -> Any:
    v = getattr(settings, name)
    return int(v) if isinstance(v, float) and v.is_integer() else v


def overrides(ws_settings: dict[str, Any] | None) -> dict[str, Any]:
    raw = (ws_settings or {}).get("limits") or {}
    return {k: v for k, v in raw.items() if k in BY_NAME}


def resolve(ws_settings: dict[str, Any] | None, settings: Settings) -> Limits:
    o = overrides(ws_settings)
    v = {s.name: o[s.name] if s.name in o else deployment_value(settings, s.name) for s in SPECS}
    return Limits(sql_row_cap=int(v["sql_row_cap"]), sql_timeout_s=float(v["sql_timeout_s"]),
                  result_ttl_days=int(v["result_ttl_days"]),
                  session_retention_days=(None if v["session_retention_days"] is None
                                          else int(v["session_retention_days"])))


def for_workspace(s: Session, workspace_id: uuid.UUID, settings: Settings) -> Limits:
    ws = s.get(Workspace, workspace_id)
    return resolve(ws.settings if ws else None, settings)


def _check(spec: LimitSpec, v: Any) -> int | None:
    if v is None:
        if not spec.nullable:
            raise LimitsError(f"{spec.label} needs a value")
        return None
    if isinstance(v, bool) or not isinstance(v, int | float) or int(v) != v:
        raise LimitsError(f"{spec.label} must be a whole number")
    n = int(v)
    if not spec.min <= n <= spec.max:
        raise LimitsError(f"{spec.label} must be between {spec.min} and {spec.max} {spec.unit}")
    return n


_DEFAULT: Any = object()


def set_limits(
    s: Session, workspace_id: uuid.UUID, actor: uuid.UUID | None, changes: dict[str, Any],
) -> dict[str, Any]:
    """Apply ``{name: value}``; the value ``"default"`` removes the override (back to the
    deployment value). ``None`` means no limit, for limits that allow it. Returns the new
    overrides."""
    unknown = sorted(set(changes) - set(BY_NAME))
    if unknown:
        raise LimitsError(f"unknown limit(s): {', '.join(unknown)}")
    ws = s.get(Workspace, workspace_id)
    if ws is None:
        raise LimitsError("no such workspace")
    cur = overrides(ws.settings)
    new = dict(cur)
    for name, v in changes.items():
        if v == "default":
            new.pop(name, None)
        else:
            new[name] = _check(BY_NAME[name], v)
    if new != cur:
        ws.settings = {**(ws.settings or {}), "limits": new}
        s.flush()
        record_audit(s, workspace_id, actor, "workspace.limits",
                     {"changed": sorted(k for k in set(cur) | set(new)
                                        if cur.get(k, _DEFAULT) != new.get(k, _DEFAULT)),
                      "limits": new})
    return new


# ----------------------------------------------------------------------------- retention

PURGE_BATCH = 500


@dataclass
class PurgeReport:
    sessions: int = 0
    tool_calls: int = 0
    workspaces: int = 0

    def line(self) -> str:
        return (f"session retention: deleted {self.sessions} session(s) and {self.tool_calls} "
                f"tool call(s) in {self.workspaces} workspace(s)")


def _purge_workspace(s: Session, ws: Workspace, days: int, now: datetime,
                     batch: int) -> tuple[int, int]:
    cutoff = now - timedelta(days=days)
    ids = list(s.scalars(
        select(AgentSession.id).where(
            AgentSession.workspace_id == ws.id,
            AgentSession.started_at < cutoff,
            ~exists().where(Feedback.session_id == AgentSession.id),
        ).order_by(AgentSession.started_at).limit(batch)
    ))
    if not ids:
        return 0, 0
    calls = select(ToolCall.id).where(ToolCall.session_id.in_(ids)).scalar_subquery()
    s.execute(delete(ServedContext).where(ServedContext.tool_call_id.in_(calls)))
    s.execute(update(QueryResult).where(QueryResult.session_id.in_(ids))
              .values(session_id=None, tool_call_id=None))
    s.execute(update(QueryResult).where(QueryResult.tool_call_id.in_(calls))
              .values(tool_call_id=None))
    s.execute(update(Signal).where(Signal.session_id.in_(ids)).values(session_id=None))
    s.execute(update(EvalCaseResult).where(EvalCaseResult.session_id.in_(ids))
              .values(session_id=None))
    n_calls = len(list(s.scalars(select(ToolCall.id).where(ToolCall.session_id.in_(ids)))))
    s.execute(delete(ToolCall).where(ToolCall.session_id.in_(ids)))
    s.execute(delete(AgentSession).where(AgentSession.id.in_(ids)))
    record_audit(s, ws.id, None, "retention.purge",
                 {"sessions": len(ids), "tool_calls": n_calls, "older_than_days": days})
    return len(ids), n_calls


def purge_old_sessions(
    settings: Settings, *, engine: Engine | None = None, now: datetime | None = None,
    batch: int = PURGE_BATCH, workspace_id: uuid.UUID | None = None,
) -> PurgeReport:
    """Delete sessions past each workspace's retention, at most ``batch`` per workspace per
    call (the worker runs it every pass, so a backlog drains over a few passes)."""
    now = now or datetime.now(UTC)
    rep = PurgeReport()
    with admin_session(engine=engine) as s:
        q = select(Workspace)
        if workspace_id is not None:
            q = q.where(Workspace.id == workspace_id)
        workspaces = list(s.scalars(q))
        for ws in workspaces:
            days = resolve(ws.settings, settings).session_retention_days
            if days is None:
                continue
            n, calls = _purge_workspace(s, ws, days, now, batch)
            if n:
                rep.sessions += n
                rep.tool_calls += calls
                rep.workspaces += 1
    return rep
