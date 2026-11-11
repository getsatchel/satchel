"""Tool registry and the one invocation path (auth role check, session resolution,
``tool_call`` logging). Transport adapters (MCP now, REST later) call ``invoke_tool``."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as OrmSession

from satchel.llm.embeddings import EmbeddingProvider
from satchel.mcp.auth import Principal
from satchel.store.db import workspace_session
from satchel.store.models import Session, ToolCall
from satchel.tools import text

log = logging.getLogger("satchel.tools")


class ToolUserError(Exception):
    """A fixable mistake in the call. The message is shown to the agent verbatim.

    ``persist=True`` (handlers only) keeps the handler's writes (signals, result rows) and
    records the error on the ``tool_call`` instead of rolling the transaction back.
    """

    def __init__(self, message: str, *, persist: bool = False) -> None:
        super().__init__(message)
        self.persist = persist
        self.persisted = False


@dataclass
class InvokeContext:
    engine: Engine
    embedder: EmbeddingProvider
    principal: Principal
    client_name: str | None = None
    client_version: str | None = None
    mcp_session_id: str | None = None
    # Evals: pin the tools to this release (a preview or an older published one) instead of
    # the latest published, and tag the sessions they start (design §10).
    release_id: uuid.UUID | None = None
    eval_run_id: uuid.UUID | None = None
    # MCP Apps: False when the client negotiated initialize without the UI extension. None =
    # unknown (stateless HTTP does not share ``initialize``); the structured widget payload is
    # only built when this is not False.
    ui_supported: bool | None = None


@dataclass
class Call:
    """What a handler gets: an RLS session, the caller, validated args, and log hooks."""

    s: OrmSession
    ctx: InvokeContext
    args: Any
    session: Session | None = None
    tool_call_id: int | None = None
    result_meta: dict[str, Any] = field(default_factory=dict)
    # Data for the result widget (MCP Apps ``structuredContent``); text stays the full answer.
    structured: dict[str, Any] | None = None
    _tool: str = ""
    _raw: dict[str, Any] = field(default_factory=dict)

    def ensure_tool_call(self) -> int:
        """Insert the ``tool_call`` row (once) and return its id."""
        if self.tool_call_id is None:
            if self.session is None:
                raise RuntimeError("no session to attach the tool_call to")
            tc = ToolCall(session_id=self.session.id, tool=self._tool, args=self._raw)
            self.s.add(tc)
            self.s.flush()
            self.tool_call_id = tc.id
        return self.tool_call_id


Handler = Callable[[Call], "str | dict[str, Any]"]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Handler
    min_role: str = "explorer"
    requires_session: bool = True
    requires_question: bool = False
    requires_connection: bool = False  # data-plane tools: only offered with a connection
    # MCP Apps: renders in the ui://satchel/result widget; app_only tools are callable by the
    # widget but hidden from the model (``_meta.ui.visibility = ["app"]``).
    widget: bool = False
    app_only: bool = False

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()


@lru_cache(maxsize=1)
def get_registry() -> dict[str, ToolDef]:
    from satchel.tools.definitions import build_tools

    return {t.name: t for t in build_tools()}


def workspace_has_connection(
    engine: Engine, workspace_id: uuid.UUID, user_id: uuid.UUID | None = None
) -> bool:
    from satchel.store.models import Connection

    with workspace_session(workspace_id, user_id, engine=engine) as s:
        return s.scalar(select(Connection.id).limit(1)) is not None


def visible_tools(ctx_engine: Engine, principal: Principal) -> list[ToolDef]:
    """Registry tools this caller's workspace can use (data tools need a connection)."""
    tools = list(get_registry().values())
    if any(t.requires_connection for t in tools) and not workspace_has_connection(
        ctx_engine, principal.workspace_id, principal.user_id
    ):
        tools = [t for t in tools if not t.requires_connection]
    return tools


def _blank(v: object) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _uuid_or_none(v: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v))
    except ValueError:
        return None


def _validation_message(tool: ToolDef, e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "arguments"
        parts.append(f"{loc}: {err['msg']}")
    return (
        f"Invalid arguments for {tool.name}: "
        + "; ".join(parts)
        + f". Fix the call and retry (see the {tool.name} input schema)."
    )


def _lookup_session(s: OrmSession, ctx: InvokeContext, raw_sid: object) -> Session | None:
    sid = _uuid_or_none(raw_sid)
    if sid is None:
        return None
    return s.scalars(
        select(Session).where(Session.id == sid, Session.user_id == ctx.principal.user_id)
    ).first()


_SESSION_ALIASES = ("analysis_id", "sessionId", "analysisId")
_SESSION_FALLBACK_WINDOW = timedelta(hours=2)


def _recover_session_id(ctx: InvokeContext, tool: ToolDef, raw: dict[str, Any]) -> None:
    """Fill a missing session_id in place, or raise the corrective error.

    Some clients drop the argument in transit (Claude Desktop's local agent mode delivers
    ``question`` but not ``session_id``), so accept common aliases and otherwise fall back
    to the caller's most recent session from the last two hours.
    """
    for alias in _SESSION_ALIASES:
        if not _blank(raw.get(alias)):
            raw["session_id"] = raw[alias]
            return
    log.warning("%s called without session_id; argument keys: %s", tool.name, sorted(raw))
    with workspace_session(
        ctx.principal.workspace_id, ctx.principal.user_id, engine=ctx.engine
    ) as s:
        latest = s.scalars(
            select(Session.id)
            .where(
                Session.user_id == ctx.principal.user_id,
                Session.started_at >= datetime.now(UTC) - _SESSION_FALLBACK_WINDOW,
            )
            .order_by(Session.started_at.desc())
            .limit(1)
        ).first()
    if latest is None:
        raise ToolUserError(text.ERR_NO_SESSION)
    raw["session_id"] = str(latest)


def _log_error(ctx: InvokeContext, tool: ToolDef, raw: dict[str, Any], message: str) -> None:
    """Best effort: record a failed call when its session is known."""
    if not raw.get("session_id"):
        return
    try:
        with workspace_session(
            ctx.principal.workspace_id, ctx.principal.user_id, engine=ctx.engine
        ) as s:
            sess = _lookup_session(s, ctx, raw.get("session_id"))
            if sess is not None:
                s.add(
                    ToolCall(
                        session_id=sess.id,
                        tool=tool.name,
                        args=json.loads(json.dumps(raw, default=str)),
                        error=message[:2000],
                        latency_ms=0,
                    )
                )
    except Exception:  # logging must never mask the real error
        log.exception("failed to log tool error")


@dataclass
class ToolOutput:
    text: str
    structured: dict[str, Any] | None = None


def invoke_tool(ctx: InvokeContext, name: str, raw_args: dict[str, Any] | None) -> str:
    """Run a tool. Returns text for the agent; raises ``ToolUserError`` on a bad call."""
    return invoke_tool_ex(ctx, name, raw_args).text


def invoke_tool_ex(
    ctx: InvokeContext, name: str, raw_args: dict[str, Any] | None
) -> ToolOutput:
    """Like :func:`invoke_tool`, plus the widget payload (MCP structuredContent) if any."""
    tool = get_registry().get(name)
    if tool is None:
        raise ToolUserError(f"Unknown tool {name!r}. Available: {', '.join(get_registry())}.")
    raw = dict(raw_args or {})
    try:
        return _invoke(ctx, tool, raw)
    except ToolUserError as e:
        if not e.persisted:
            _log_error(ctx, tool, raw, str(e))
        raise


def _invoke(ctx: InvokeContext, tool: ToolDef, raw: dict[str, Any]) -> ToolOutput:
    if not ctx.principal.has_role(tool.min_role):
        raise ToolUserError(
            f"{tool.name} requires the {tool.min_role} role; your role is {ctx.principal.role}."
        )
    if tool.requires_connection and not workspace_has_connection(
        ctx.engine, ctx.principal.workspace_id, ctx.principal.user_id
    ):
        raise ToolUserError(
            f"{tool.name} is unavailable: this workspace has no data connection. "
            "Use the context tools only, and tell the user an admin must add a connection."
        )
    if tool.requires_session and _blank(raw.get("session_id")):
        _recover_session_id(ctx, tool, raw)
    if tool.requires_question and _blank(raw.get("question")):
        raise ToolUserError(text.ERR_NO_QUESTION)
    try:
        args = tool.input_model.model_validate(raw)
    except ValidationError as e:
        raise ToolUserError(_validation_message(tool, e)) from None

    stored = json.loads(json.dumps(args.model_dump(mode="json"), default=str))
    t0 = time.perf_counter()
    with workspace_session(
        ctx.principal.workspace_id, ctx.principal.user_id, engine=ctx.engine
    ) as s:
        call = Call(s=s, ctx=ctx, args=args, _tool=tool.name, _raw=stored)
        if tool.requires_session:
            call.session = _lookup_session(s, ctx, raw.get("session_id"))
            if call.session is None:
                raise ToolUserError(text.ERR_BAD_SESSION.format(sid=raw.get("session_id")))
            call.ensure_tool_call()
        deferred: ToolUserError | None = None
        try:
            result = tool.handler(call)
        except ToolUserError as e:
            if not (e.persist and call.session is not None):
                raise
            deferred, result = e, ""
        tcid = call.ensure_tool_call() if call.session is not None else None
        if tcid is not None:
            tc = s.get(ToolCall, tcid)
            assert tc is not None
            tc.latency_ms = int((time.perf_counter() - t0) * 1000)
            tc.result_meta = call.result_meta
            if deferred is not None:
                tc.error = str(deferred)[:2000]
                deferred.persisted = True
    if deferred is not None:
        raise deferred
    text_out = result if isinstance(result, str) else json.dumps(result, indent=2, default=str)
    return ToolOutput(text_out, call.structured)
