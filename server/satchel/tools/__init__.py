"""Transport-neutral tool registry (design §6.1). Used by MCP now, REST later."""

from satchel.tools.registry import (
    InvokeContext,
    ToolDef,
    ToolUserError,
    get_registry,
    invoke_tool,
    invoke_tool_ex,
    visible_tools,
)

__all__ = [
    "InvokeContext", "ToolDef", "ToolUserError", "get_registry", "invoke_tool", "invoke_tool_ex",
    "visible_tools",
]
