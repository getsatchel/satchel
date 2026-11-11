"""Streamable HTTP transport at ``/mcp`` with bearer auth (PAT or OAuth access token).

A 401 carries ``WWW-Authenticate: Bearer resource_metadata="<public>/.well-known/oauth-
protected-resource/mcp"`` so OAuth-capable clients (Claude Desktop/Code) can discover satchel's
authorization server (RFC 9728, MCP authorization spec).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import anyio
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy import Engine
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from satchel.llm.embeddings import EmbeddingProvider, make_embedder
from satchel.mcp.auth import Principal, authenticate_bearer
from satchel.mcp.server import build_server
from satchel.oauth.common import issuer
from satchel.settings import Settings, get_settings
from satchel.store.db import get_engine

SCOPE_KEY = "satchel.principal"


def _resolve(rc: Any) -> Principal:
    request = rc.request
    principal = request.scope.get(SCOPE_KEY) if request is not None else None
    if not isinstance(principal, Principal):
        raise PermissionError("unauthenticated MCP request")
    return principal


class McpHttp:
    """ASGI app: bearer auth, then the SDK's session manager. Needs ``run()`` for lifespan."""

    def __init__(
        self, engine: Engine, embedder: EmbeddingProvider, settings: Settings | None = None
    ) -> None:
        self.engine = engine
        self.settings = settings or get_settings()
        self.server = build_server(engine, embedder, _resolve)
        # Stateless: sessions live in Postgres (our own session_id), so any replica can serve any
        # request with no sticky routing. json_response avoids long-lived SSE streams.
        self.manager = StreamableHTTPSessionManager(
            app=self.server, stateless=True, json_response=True
        )

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        async with self.manager.run():
            yield

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        header = dict(scope["headers"]).get(b"authorization", b"").decode() or None
        principal = await anyio.to_thread.run_sync(
            lambda: authenticate_bearer(header, engine=self.engine, settings=self.settings)
        )
        if principal is None:
            await _unauthorized(self.settings, bool(header))(scope, receive, send)
            return
        scope[SCOPE_KEY] = principal
        await self.manager.handle_request(scope, receive, send)


def _unauthorized(settings: Settings, had_credentials: bool) -> Response:
    prm = f"{issuer(settings)}/.well-known/oauth-protected-resource/mcp"
    challenge = 'Bearer realm="satchel"'
    if had_credentials:  # RFC 6750 3.1: only name an error when credentials were presented
        challenge += ', error="invalid_token"'
    challenge += f', resource_metadata="{prm}", scope="mcp"'
    return JSONResponse(
        {"error": "invalid_token", "error_description": "Missing, invalid, expired or revoked "
         "token. Authenticate with OAuth (see resource_metadata) or 'Authorization: Bearer "
         "satchel_pat_...'."},
        status_code=401,
        headers={"WWW-Authenticate": challenge},
    )


def mount_mcp(app: Any, mcp: McpHttp) -> None:
    """Add ``/mcp`` to a Starlette/FastAPI app and run the manager in its lifespan."""
    app.router.routes.append(Route("/mcp", endpoint=mcp, methods=["GET", "POST", "DELETE"]))
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: Any) -> AsyncIterator[Any]:
        async with mcp.run(), inner(a) as state:
            yield state

    app.router.lifespan_context = lifespan


def create_standalone_app(
    engine: Engine | None = None, embedder: EmbeddingProvider | None = None,
    settings: Settings | None = None,
) -> Starlette:
    """``satchel serve mcp``: /mcp, /healthz and the OAuth/OIDC routes."""
    from satchel.oauth.routes import OAuthApp, mount_oauth

    eng, st = engine or get_engine(), settings or get_settings()
    mcp = McpHttp(eng, embedder or make_embedder(), st)

    async def healthz(_: Any) -> Response:
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[Route("/healthz", healthz)])
    mount_mcp(app, mcp)
    mount_oauth(app, OAuthApp(eng, st))
    return app
