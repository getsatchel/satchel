"""Reference agent harness (design §10): a minimal tool-use loop over a ``ToolChatProvider``.

The agent calls satchel's tools **in-process** through ``invoke_tool`` (the same path MCP uses),
with the invoke context pinned to the target release and tagged with the eval run, so the
session it records is a normal session carrying ``release_id`` and ``eval_run_id``. The system
prompt is the MCP ``instructions`` workflow text.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select

from satchel.llm.chat import AgentMessage, ChatError, ModelTurn, ToolChatProvider, ToolSpec, Usage
from satchel.llm.embeddings import EmbeddingProvider
from satchel.mcp.auth import Principal
from satchel.store.db import workspace_session
from satchel.store.models import ContextItem, ContextVersion, QueryResult, ServedContext, ToolCall
from satchel.store.models import Session as SessionRow
from satchel.tools import InvokeContext, ToolUserError, invoke_tool, visible_tools
from satchel.tools.text import INSTRUCTIONS

log = logging.getLogger("satchel.evals")

DEFAULT_MAX_STEPS = 12
RESULT_CHARS = 6000  # a tool result is truncated to this many characters in the transcript
EXCLUDED_TOOLS = {"propose_context_change"}  # an eval agent never writes context
CLIENT_NAME = "satchel-eval"


@dataclass
class TraceStep:
    tool: str
    args: dict[str, Any]
    result: str
    is_error: bool


@dataclass
class AgentRun:
    session_id: uuid.UUID | None = None
    answer: str = ""
    steps: int = 0
    trace: list[TraceStep] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    hit_step_limit: bool = False
    error: str | None = None

    def cost(self) -> dict[str, Any]:
        return {"steps": self.steps, "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "tool_calls": len(self.trace)}


def tool_specs(engine: Engine, principal: Principal) -> list[ToolSpec]:
    return [
        ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema())
        for t in visible_tools(engine, principal)
        if t.name not in EXCLUDED_TOOLS
    ]


_SID = re.compile(r'"session_id":\s*"([0-9a-f-]{36})"')


def run_agent(
    engine: Engine,
    embedder: EmbeddingProvider,
    principal: Principal,
    provider: ToolChatProvider,
    prompt: str,
    *,
    release_id: uuid.UUID,
    eval_run_id: uuid.UUID,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> AgentRun:
    """Run one attempt: the agent answers ``prompt`` against ``release_id``."""
    ctx = InvokeContext(
        engine=engine, embedder=embedder, principal=principal, client_name=CLIENT_NAME,
        release_id=release_id, eval_run_id=eval_run_id,
    )
    specs = tool_specs(engine, principal)
    run = AgentRun()
    messages: list[AgentMessage] = [{"role": "user", "content": prompt}]
    for _ in range(max_steps):
        try:
            turn: ModelTurn = provider.complete_with_tools(INSTRUCTIONS, messages, specs)
        except ChatError as e:
            run.error = f"model call failed: {e}"
            break
        run.steps += 1
        run.usage.add(turn.usage)
        if not turn.tool_uses:
            run.answer = turn.text.strip()
            break
        messages.append({
            "role": "assistant", "text": turn.text,
            "tool_uses": [{"id": t.id, "name": t.name, "input": t.input} for t in turn.tool_uses],
        })
        results: list[dict[str, Any]] = []
        for use in turn.tool_uses:
            args = dict(use.input)
            # Concurrent evals share one user: never let the session-recovery fallback
            # (latest session of the user) pick another case's session.
            if (run.session_id is not None and not args.get("session_id")
                    and use.name != "start_analysis"):
                args["session_id"] = str(run.session_id)
            is_error = False
            try:
                out = invoke_tool(ctx, use.name, args)
            except ToolUserError as e:
                out, is_error = str(e), True
            except Exception as e:  # a tool crash is data for the agent and the grader
                log.exception("eval tool %s failed", use.name)
                out, is_error = f"Internal error in {use.name}: {type(e).__name__}", True
            if use.name == "start_analysis" and not is_error and run.session_id is None:
                m = _SID.search(out)
                if m:
                    run.session_id = uuid.UUID(m.group(1))
            run.trace.append(TraceStep(use.name, args, out[:RESULT_CHARS], is_error))
            results.append({"id": use.id, "name": use.name, "content": out[:RESULT_CHARS],
                            "is_error": is_error})
        messages.append({"role": "tool", "results": results})
    else:
        run.hit_step_limit = True
        run.error = f"step limit ({max_steps}) reached without a final answer"
    if run.session_id is not None:
        with workspace_session(principal.workspace_id, principal.user_id, engine=engine) as s:
            sess = s.get(SessionRow, run.session_id)
            if sess is not None:
                sess.ended_at = datetime.now(UTC)
                if run.error and sess.status == "active":
                    sess.status = "abandoned"
    return run


@dataclass
class SessionEvidence:
    """What a judge/grader may look at for a finished eval session."""

    final_sql: str | None
    final_connection_id: uuid.UUID | None
    served: list[str]  # [ctx:key@vN] tags, in served order


def session_evidence(
    engine: Engine, principal: Principal, session_id: uuid.UUID | None
) -> SessionEvidence:
    if session_id is None:
        return SessionEvidence(None, None, [])
    with workspace_session(principal.workspace_id, principal.user_id, engine=engine) as s:
        qr = s.scalars(
            select(QueryResult).where(QueryResult.session_id == session_id)
            .order_by(QueryResult.created_at.desc())
        ).first()
        rows = s.execute(
            select(ContextItem.key, ContextVersion.seq)
            .select_from(ServedContext)
            .join(ToolCall, ToolCall.id == ServedContext.tool_call_id)
            .join(ContextVersion, ContextVersion.id == ServedContext.version_id)
            .join(ContextItem, ContextItem.id == ContextVersion.item_id)
            .where(ToolCall.session_id == session_id)
            .order_by(ToolCall.id, ServedContext.rank)
        ).all()
        final_sql = qr.sql if qr else None
        final_conn = qr.connection_id if qr else None
    tags: list[str] = []
    for key, seq in rows:
        tag = f"[ctx:{key}@v{seq}]"
        if tag not in tags:
            tags.append(tag)
    return SessionEvidence(final_sql, final_conn, tags)


def trace_text(run: AgentRun, limit: int = 12000) -> str:
    parts = []
    for i, st in enumerate(run.trace, start=1):
        args = json.dumps(st.args, default=str)[:1500]
        flag = "ERROR: " if st.is_error else ""
        parts.append(f"[{i}] {st.tool}({args})\n-> {flag}{st.result[:2000]}")
    return "\n\n".join(parts)[:limit]
