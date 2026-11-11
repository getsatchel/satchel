"""stdio transport: ``satchel mcp stdio`` with SATCHEL_TOKEN + SATCHEL_DATABASE_URL (or a config
file).

Used by Claude Desktop (``claude_desktop_config.json``) and Claude Code. The token is
resolved once at startup. (Proxying to a remote satchel via SATCHEL_URL is not implemented; use
``npx mcp-remote`` for that.)

If the database schema does not match this build, the server still starts and answers every
tool call with the fix ("run: satchel upgrade") so the agent can tell the user, instead of failing
each call with an UndefinedColumn error.
"""

from __future__ import annotations

import sys
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from satchel.llm.embeddings import make_embedder
from satchel.mcp.auth import verify_token
from satchel.mcp.server import build_server
from satchel.settings import get_settings
from satchel.store.db import get_engine
from satchel.store.schema import guard_message


def blocked_server(message: str) -> Server[Any, Any]:
    """A server whose only tool, and every tool call, reports ``message``."""
    full = f"Satchel is not ready: {message}"
    server: Server[Any, Any] = Server("satchel", instructions=full)

    @server.list_tools()
    async def _list() -> list[types.Tool]:
        return [
            types.Tool(
                name="satchel_status",
                description=f"Satchel is not ready. Call this and tell the user: {message}",
                inputSchema={"type": "object", "properties": {}},
            )
        ]

    @server.call_tool(validate_input=False)
    async def _call(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        return types.CallToolResult(
            isError=True, content=[types.TextContent(type="text", text=full)]
        )

    return server


def run_stdio() -> None:
    token = get_settings().token or ""
    if not token:
        sys.exit("SATCHEL_TOKEN is required (create one with `satchel token create`).")
    engine = get_engine()
    blocked = guard_message(engine)
    if blocked:
        print(f"satchel: {blocked}", file=sys.stderr)
        server = blocked_server(blocked)
    else:
        principal = verify_token(token, engine=engine)
        if principal is None:
            sys.exit("SATCHEL_TOKEN is invalid, expired or revoked.")
        server = build_server(engine, make_embedder(), lambda _rc: principal)

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)


__all__: list[Any] = ["run_stdio", "blocked_server"]
