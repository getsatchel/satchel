"""MCP Apps (extension ``io.modelcontextprotocol/ui``, spec 2026-01-26): the result widget.

Tools declare ``_meta.ui.resourceUri`` pointing at a ``ui://`` resource served as
``text/html;profile=mcp-app``. The host renders it in a sandboxed iframe and pushes the tool
result (``structuredContent``) into it over postMessage JSON-RPC. The app calls server tools
(``rate_result``, ``get_result``) back through the host. Clients without the extension just get
the complete text content.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib import resources
from typing import Any

EXTENSION_ID = "io.modelcontextprotocol/ui"
MIME_TYPE = "text/html;profile=mcp-app"
RESULT_BASE = "ui://satchel/result"


@lru_cache(maxsize=1)
def result_widget_html() -> str:
    return (
        resources.files("satchel.resources").joinpath("widgets/result.html").read_text("utf-8")
    )


@lru_cache(maxsize=1)
def result_uri() -> str:
    """The widget's URI, versioned by its content. Hosts cache a ``ui://`` resource by URI (Claude
    kept 0.4.0's widget after an upgrade and could not draw 0.5.0's answer card), so each build
    gets its own URI and a host that sees a new one fetches the new widget."""
    digest = hashlib.sha256(result_widget_html().encode("utf-8")).hexdigest()[:12]
    return f"{RESULT_BASE}/{digest}"


def is_result_uri(uri: str) -> bool:
    """Any version of the widget, or the unversioned URI older tool lists named: all are served
    the current widget, which draws every payload this server sends."""
    return uri == RESULT_BASE or uri.startswith(RESULT_BASE + "/")


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
        return {"ui": {"resourceUri": result_uri(), "visibility": ["app"]}}
    if widget:
        # The flat key is the deprecated spelling; older hosts still read it.
        return {"ui": {"resourceUri": result_uri()}, "ui/resourceUri": result_uri()}
    return None


def resource_meta() -> dict[str, Any]:
    # No csp: the widget is fully inline, so the default (no external origins) is what we want.
    return {"ui": {"prefersBorder": True}}
