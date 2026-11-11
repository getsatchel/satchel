"""MCP server (design §6): adapts the tool registry to the official ``mcp`` SDK low-level
``Server``. Transport-agnostic: the caller supplies a ``PrincipalResolver``.

``validate_input=False`` is deliberate: the registry validates arguments itself so a missing
``session_id`` / ``question`` gets a corrective message (spike C) instead of a generic schema
error. The JSON schema still marks them required.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from pydantic import AnyUrl
from sqlalchemy import Engine, select

from satchel.llm.embeddings import EmbeddingProvider
from satchel.mcp import apps
from satchel.mcp.auth import Principal
from satchel.store.db import workspace_session
from satchel.store.models import ContextItem, ContextVersion
from satchel.tools import InvokeContext, ToolUserError, invoke_tool_ex, visible_tools
from satchel.tools.text import INSTRUCTIONS, WORKFLOW_PROMPT

logger = logging.getLogger("satchel.mcp")

RULES_URI = "satchel://workspace-rules"
# Resolves the caller from the current MCP request (HTTP: the auth middleware's result;
# stdio: the token resolved at startup).
PrincipalResolver = Callable[[Any], Principal]


def parse_user_agent(ua: str | None) -> tuple[str | None, str | None]:
    """First product token of a User-Agent: 'claude-code/2.1.311 (cli)' -> (name, version)."""
    if not ua or not ua.strip():
        return None, None
    product = ua.strip().split()[0]
    name, _, version = product.partition("/")
    return name[:100] or None, version[:50] or None


def build_server(
    engine: Engine, embedder: EmbeddingProvider, resolve_principal: PrincipalResolver
) -> Server[Any, Any]:
    server: Server[Any, Any] = Server("satchel", instructions=INSTRUCTIONS)

    def ui_supported() -> bool | None:
        params = server.request_context.session.client_params
        return apps.ui_negotiation(params.capabilities) if params else None

    def invoke_ctx() -> InvokeContext:
        rc = server.request_context
        params = rc.session.client_params
        info = params.clientInfo if params else None
        request = rc.request
        headers = request.headers if request is not None else {}
        name = info.name if info else None
        version = info.version if info else None
        if name is None:
            # Stateless HTTP: `initialize` is not shared across requests, so fall back to the
            # User-Agent ("claude-code/2.1.311").
            name, version = parse_user_agent(headers.get("user-agent"))
        return InvokeContext(
            engine=engine,
            embedder=embedder,
            principal=resolve_principal(rc),
            client_name=name,
            client_version=version,
            mcp_session_id=headers.get("mcp-session-id"),
            ui_supported=ui_supported(),
        )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        principal = resolve_principal(server.request_context)
        tools = await anyio.to_thread.run_sync(visible_tools, engine, principal)
        ui = ui_supported()
        # app-only tools are meaningless without a UI host; widget metadata is harmless
        # otherwise, so only a client that negotiated "no UI" loses it.
        return [
            types.Tool(
                name=t.name, description=t.description, inputSchema=t.input_schema(),
                _meta=apps.tool_meta(t.widget, t.app_only) if ui is not False else None,
            )
            for t in tools
            if not (t.app_only and ui is False)
        ]

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        ctx = invoke_ctx()
        try:
            out = await anyio.to_thread.run_sync(invoke_tool_ex, ctx, name, arguments)
        except ToolUserError as e:
            return types.CallToolResult(
                isError=True, content=[types.TextContent(type="text", text=str(e))]
            )
        except Exception:
            logger.exception("tool %s failed", name)
            return types.CallToolResult(
                isError=True,
                content=[types.TextContent(type="text", text=f"Internal error in {name}.")],
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=out.text)],
            structuredContent=out.structured,
        )

    @server.list_prompts()
    async def _list_prompts() -> list[types.Prompt]:
        return [
            types.Prompt(name="satchel_workflow", description="How to run a governed analysis"),
            types.Prompt(
                name="analyze",
                description="Answer a data question using the Satchel workflow",
                arguments=[types.PromptArgument(name="question", required=True)],
            ),
        ]

    @server.get_prompt()
    async def _get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
        if name == "satchel_workflow":
            body = WORKFLOW_PROMPT
        elif name == "analyze":
            q = (arguments or {}).get("question", "")
            body = f"{WORKFLOW_PROMPT}\n\nUser question: {q}"
        else:
            raise ValueError(f"unknown prompt: {name}")
        return types.GetPromptResult(
            messages=[
                types.PromptMessage(role="user", content=types.TextContent(type="text", text=body))
            ]
        )

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        widget = [] if ui_supported() is False else [
            types.Resource(
                uri=AnyUrl(apps.RESULT_URI),
                name="result-widget",
                description="Interactive result table, chart and feedback buttons.",
                mimeType=apps.MIME_TYPE,
                _meta=apps.resource_meta(),
            )
        ]
        return widget + [
            types.Resource(
                uri=AnyUrl(RULES_URI),
                name="workspace-rules",
                description="The workspace's always-on rules (published).",
                mimeType="text/markdown",
            )
        ]

    @server.read_resource()
    async def _read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
        if str(uri) == apps.RESULT_URI:
            html = await anyio.to_thread.run_sync(apps.result_widget_html)
            return [
                ReadResourceContents(
                    content=html, mime_type=apps.MIME_TYPE, meta=apps.resource_meta()
                )
            ]
        if str(uri) != RULES_URI:
            raise ValueError(f"unknown resource: {uri}")
        principal = resolve_principal(server.request_context)

        def load() -> str:
            with workspace_session(principal.workspace_id, principal.user_id, engine=engine) as s:
                rows = s.execute(
                    select(ContextItem.key, ContextVersion.body)
                    .join(ContextVersion, ContextVersion.id == ContextItem.published_version_id)
                    .where(ContextItem.type == "workspace_rule", ContextItem.state == "published")
                    .order_by(ContextItem.key)
                )
                return "\n\n".join(f"<!-- {k} -->\n{b.strip()}" for k, b in rows) or (
                    "(No workspace rules are published yet.)"
                )

        text = await anyio.to_thread.run_sync(load)
        return [ReadResourceContents(content=text, mime_type="text/markdown")]

    return server
