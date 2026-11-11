"""MCP Apps (extension ``io.modelcontextprotocol/ui``, spec 2026-01-26): the result widget.

Tools declare ``_meta.ui.resourceUri`` pointing at a ``ui://`` resource served as
``text/html;profile=mcp-app``. The host renders it in a sandboxed iframe and pushes the tool
result (``structuredContent``) into it over postMessage JSON-RPC. The app calls server tools
(``rate_result``, ``get_result``) back through the host. Clients without the extension just get
the complete text content.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any

EXTENSION_ID = "io.modelcontextprotocol/ui"
MIME_TYPE = "text/html;profile=mcp-app"
RESULT_URI = "ui://satchel/result"


@lru_cache(maxsize=1)
def result_widget_html() -> str:
    return (
        resources.files("satchel.resources").joinpath("widgets/result.html").read_text("utf-8")
    )


def ui_negotiation(capabilities: Any) -> bool | None:
    """Did the client advertise the UI extension? None = unknown (no ``initialize`` seen)."""
    if capabilities is None:
        return None
    extra = getattr(capabilities, "model_extra", None) or {}
    ext = extra.get("extensions")
    if not isinstance(ext, dict):
        return False
    entry = ext.get(EXTENSION_ID)
    if not isinstance(entry, dict):
        return False
    mime = entry.get("mimeTypes")
    return isinstance(mime, list) and MIME_TYPE in mime


def tool_meta(widget: bool, app_only: bool) -> dict[str, Any] | None:
    if app_only:
        return {"ui": {"resourceUri": RESULT_URI, "visibility": ["app"]}}
    if widget:
        # The flat key is the deprecated spelling; older hosts still read it.
        return {"ui": {"resourceUri": RESULT_URI}, "ui/resourceUri": RESULT_URI}
    return None


def resource_meta() -> dict[str, Any]:
    # No csp: the widget is fully inline, so the default (no external origins) is what we want.
    return {"ui": {"prefersBorder": True}}
