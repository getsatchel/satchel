"""Security headers on every response, and a strict Content-Security-Policy for the curator UI.

Every response gets, unless the route set its own:

- ``X-Content-Type-Options: nosniff``
- ``Referrer-Policy: strict-origin-when-cross-origin``
- ``X-Frame-Options: DENY`` (nothing Satchel serves is meant to be framed; the MCP result widget
  is delivered as an MCP resource, not by URL)
- ``Strict-Transport-Security`` when ``SATCHEL_PUBLIC_URL`` is https

The UI page itself (``index.html``) gets :func:`ui_csp`: scripts only from this origin plus the
page's own inline theme script by hash, no plugins, no framing. FastAPI's ``/docs`` loads from
a CDN, so it is not given that policy.

Pure ASGI (not ``BaseHTTPMiddleware``): it must not buffer ``/mcp``'s streamed responses.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

HSTS = "max-age=31536000"


class SecurityHeadersMiddleware:
    def __init__(self, app: App, *, hsts: bool) -> None:
        self.app = app
        defaults = [
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"x-frame-options", b"DENY"),
        ]
        if hsts:
            defaults.append((b"strict-transport-security", HSTS.encode()))
        self.defaults = defaults

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in self.defaults if k not in present]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def ui_csp(index_html: str) -> str:
    """The UI's policy. Inline scripts in ``index.html`` (the theme snippet) are allowed by
    their sha256; everything the bundle loads (scripts, Monaco's workers, styles, fonts) is
    same-origin. Inline style attributes stay allowed: React and Monaco set them."""
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(m.encode()).digest()).decode() + "'"
        for m in _INLINE_SCRIPT.findall(index_html)
    )
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self'{' ' + hashes if hashes else ''}",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "worker-src 'self' blob:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])
