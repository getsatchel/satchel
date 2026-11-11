"""REST API (design §2, §14 M1/M3a): library, workbench, proposals, observability, catalog.

Everything is under ``/v1`` with role enforcement per design §9 (admin > curator > analyst >
explorer). The OAuth authorization server and OIDC login routes and ``/mcp`` are mounted here
too. Auth lives in :mod:`satchel.api.auth`; routers in the sibling modules. ``/openapi.json`` is
the contract the web UI generates its TypeScript types from (``satchel openapi``).
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.routing import APIRoute
from sqlalchemy import Engine, select

from satchel.api import (
    audit,
    catalog,
    connections,
    evals,
    hooks,
    library,
    llm,
    observability,
    proposals,
    sources,
    suggestions,
    workbench,
    workspace_settings,
)
from satchel.api.auth import ApiContext, Auth, Caller
from satchel.api.health import mount_readyz
from satchel.api.schemas import (
    MeOut,
    PackEntryOut,
    PackOut,
    PackRequest,
    SearchHitOut,
    SearchRequest,
)
from satchel.llm.chat import ChatProvider
from satchel.llm.embeddings import EmbeddingProvider, make_embedder
from satchel.oauth.clients import CimdFetcher
from satchel.oauth.oidc import OidcProvider
from satchel.oauth.routes import OAuthApp, mount_oauth
from satchel.retrieval import build_context_pack, search_context
from satchel.runtime import TraceMiddleware
from satchel.settings import Settings, get_settings
from satchel.store.db import admin_session, get_engine, workspace_session
from satchel.store.models import AppUser, Workspace

__all__ = ["Caller", "create_app"]


# Paths the SPA fallback must never answer (they are APIs, so a miss is a real 404).
_API_PREFIXES = (
    "v1", "auth", "oauth", "mcp", ".well-known", "openapi.json", "docs", "redoc", "healthz",
    "readyz",
)
_DEFAULT_UI_DIR = Path(__file__).resolve().parents[3] / "ui" / "dist"  # repo checkout
_PACKAGED_UI_DIR = Path(__file__).resolve().parents[1] / "resources" / "ui"  # built into the wheel

_NO_UI_PAGE = """<!doctype html><meta charset="utf-8"><title>satchel</title>
<body style="font:15px/1.5 system-ui;max-width:640px;margin:48px auto;padding:0 16px">
<h1>Satchel is running</h1>
<p>The API and MCP endpoint are up, but this install has no built curator UI.</p>
<p>The PyPI package includes it: <code>uv tool install --force satchel-server</code>.
From a satchel repo checkout, build it and point satchel at it:</p>
<pre>cd ui &amp;&amp; npm ci &amp;&amp; npm run build
SATCHEL_UI_DIR=$PWD/dist satchel up</pre>
<p>Or reinstall from the checkout after building, so the UI is bundled:
<code>uv tool install --force ./server</code></p>
<p>MCP: <code>/mcp</code> &middot; API docs: <a href="/docs">/docs</a></p></body>"""


def _resolve_ui_dir(configured: str | None) -> Path | None:
    """Configured dir, else the wheel's bundled UI, the repo checkout, or ./ui/dist."""
    if configured:
        return Path(configured).expanduser()
    for cand in (_PACKAGED_UI_DIR, _DEFAULT_UI_DIR, Path.cwd() / "ui" / "dist"):
        if (cand / "index.html").is_file():
            return cand
    return None


def _mount_ui(app: FastAPI, ui_dir: Path) -> bool:
    """Serve the built curator UI at ``/`` with an SPA fallback (registered last)."""
    root = ui_dir.resolve()
    index = root / "index.html"
    if not index.is_file():
        return False

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        head = path.split("/", 1)[0]
        if head in _API_PREFIXES:
            raise HTTPException(404, "Not Found")
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return True


def _operation_id(route: APIRoute) -> str:
    return route.name  # stable, readable names for the generated TS client


def create_app(
    engine: Engine | None = None, embedder: EmbeddingProvider | None = None, *,
    mcp: bool = True, settings: Settings | None = None, oidc: OidcProvider | None = None,
    cimd_fetcher: CimdFetcher | None = None, chat: ChatProvider | None = None,
) -> FastAPI:
    app = FastAPI(title="satchel", version="0.1.0", generate_unique_id_function=_operation_id)
    app.add_middleware(TraceMiddleware)
    eng = engine or get_engine()
    emb = embedder or make_embedder()
    st = settings or get_settings()
    if mcp:
        from satchel.mcp.http import McpHttp, mount_mcp

        mount_mcp(app, McpHttp(eng, emb, st))  # Streamable HTTP at /mcp, bearer auth
    mount_oauth(app, OAuthApp(eng, st, oidc, cimd_fetcher))  # /oauth/*, /auth/*, /.well-known/*

    auth = Auth(ApiContext(eng, emb, st, chat))
    Who = Annotated[Caller, Depends(auth.require("explorer"))]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    mount_readyz(app, eng)

    me = APIRouter(tags=["me"])

    @me.get("/v1/me", response_model=MeOut)
    def get_me(who: Who) -> MeOut:
        with admin_session(engine=eng) as s:
            ws = s.scalars(select(Workspace).where(Workspace.id == who.workspace_id)).one()
            u = s.get(AppUser, who.user_id) if who.user_id else None
            return MeOut(
                user_id=who.user_id, email=u.email if u else None,
                name=u.display_name if u else None, role=who.effective_role,
                workspace_id=ws.id, workspace_slug=ws.slug, workspace_name=ws.name,
                csrf_token=who.csrf, auth=who.auth,
            )

    app.include_router(me)
    app.include_router(library.make_router(auth))
    app.include_router(workbench.make_router(auth))
    app.include_router(proposals.make_router(auth))
    app.include_router(suggestions.make_router(auth))
    app.include_router(observability.make_router(auth))
    app.include_router(catalog.make_router(auth))
    app.include_router(evals.make_router(auth))
    app.include_router(sources.make_router(auth))
    app.include_router(hooks.make_router(auth.ctx))  # unauthenticated push webhooks
    app.include_router(audit.make_router(auth))
    app.include_router(llm.make_router(auth))
    app.include_router(connections.make_router(auth))
    app.include_router(workspace_settings.make_router(auth))

    search_r = APIRouter(tags=["retrieval"])

    @search_r.post("/v1/search", response_model=list[SearchHitOut])
    def search(req: SearchRequest, who: Who) -> list[SearchHitOut]:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            hits = search_context(s, req.query, types=req.types, k=req.k, embedder=emb)
            return [
                SearchHitOut(
                    key=h.key, type=h.type, seq=h.seq, tag=h.tag, score=h.score,
                    sources=h.sources, snippet=h.snippet,
                )
                for h in hits
            ]

    @search_r.post("/v1/context-pack", response_model=PackOut)
    def context_pack(req: PackRequest, who: Who) -> PackOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            pack = build_context_pack(
                s, req.question, who.user_id, embedder=emb, budget_tokens=req.budget_tokens
            )
            return PackOut(
                release_id=pack.release_id,
                budget_tokens=pack.budget_tokens,
                used_tokens=pack.used_tokens,
                text=pack.text,
                entries=[
                    PackEntryOut(
                        key=e.key, type=e.type, seq=e.seq, tag=e.tag, tokens=e.tokens,
                        score=e.score, text=e.text,
                    )
                    for e in pack.entries
                ],
            )

    app.include_router(search_r)
    ui = _resolve_ui_dir(st.ui_dir)
    if ui is None or not _mount_ui(app, ui):

        @app.get("/", include_in_schema=False)
        def no_ui() -> HTMLResponse:
            return HTMLResponse(_NO_UI_PAGE)

    return app
