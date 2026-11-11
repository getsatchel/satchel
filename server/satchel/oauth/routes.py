"""Starlette routes: discovery metadata, /oauth/*, /auth/*. Mounted on the REST app and on the
standalone MCP app via ``mount_oauth``."""

from __future__ import annotations

import html
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import Engine, select
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from satchel.oauth import sessions, tokens
from satchel.oauth.clients import (
    CimdFetcher,
    ClientMetadataError,
    register_client,
)
from satchel.oauth.common import SUPPORTED_SCOPES, cookie_secure, issuer, mcp_resource
from satchel.oauth.flows import FlowService, Outcome, PageError, RedirectError
from satchel.oauth.oidc import OidcProvider, make_provider
from satchel.settings import Settings, get_settings
from satchel.store.db import admin_session, get_engine
from satchel.store.models import AppUser, Workspace

MAX_REGISTER_BYTES = 16 * 1024
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                               "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Max-Age": "600"}

Handler = Callable[[Request], Awaitable[Response]]

_CSS = ("body{font:16px system-ui,sans-serif;max-width:30rem;margin:12vh auto;padding:0 1rem;"
        "color:#1f2937}button{font:inherit;padding:.6rem 1.1rem;border-radius:.4rem;"
        "border:1px solid #9ca3af;background:#fff;cursor:pointer}"
        "button.primary{background:#1d4ed8;color:#fff;border-color:#1d4ed8}"
        "code{background:#f3f4f6;padding:.1rem .3rem;border-radius:.2rem}li{margin:.3rem 0}")


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
           f"<title>{html.escape(title)}</title><style>{_CSS}</style>"
           f"<h1>{html.escape(title)}</h1>{body}")
    return HTMLResponse(doc, status_code=status, headers=PAGE_HEADERS)


def error_page(e: PageError) -> HTMLResponse:
    return page("Something went wrong", f"<p>{html.escape(e.message)}</p>", e.status)


def _oauth_json(body: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache", **CORS})


def _err(error: str, description: str, status: int = 400) -> JSONResponse:
    return _oauth_json({"error": error, "error_description": description}, status)


def _flow_id(raw: str | None) -> uuid.UUID:
    try:
        return uuid.UUID(raw or "")
    except ValueError:
        raise PageError("Invalid flow.") from None


class OAuthApp:
    def __init__(
        self, engine: Engine | None = None, settings: Settings | None = None,
        oidc: OidcProvider | None = None, fetcher: CimdFetcher | None = None,
    ) -> None:
        self.engine = engine or get_engine()
        self.settings = settings or get_settings()
        self.oidc = oidc if oidc is not None else make_provider(self.settings)
        self.flows = FlowService(self.engine, self.settings, self.oidc, fetcher)
        self.fetcher = fetcher

    # ---- helpers --------------------------------------------------------------------------

    def _respond(self, o: Outcome) -> Response:
        r = RedirectResponse(o.redirect, status_code=302, headers={"Cache-Control": "no-store"})
        for name, value, max_age in o.set_cookies:
            r.set_cookie(name, value, max_age=max_age, httponly=True,
                         secure=cookie_secure(self.settings), samesite="lax", path="/")
        for name in o.clear_cookies:
            r.delete_cookie(name, path="/")
        return r

    async def _flow_step(self, fn: Callable[[], Outcome]) -> Response:
        try:
            return self._respond(await run_in_threadpool(fn))
        except RedirectError as e:
            return RedirectResponse(e.url, status_code=302, headers={"Cache-Control": "no-store"})
        except PageError as e:
            return error_page(e)

    # ---- discovery ------------------------------------------------------------------------

    def _prm(self, resource: str) -> JSONResponse:
        return JSONResponse({
            "resource": resource,
            "authorization_servers": [issuer(self.settings)],
            "scopes_supported": list(SUPPORTED_SCOPES),
            "bearer_methods_supported": ["header"],
            "resource_name": "satchel",
        }, headers=CORS)

    async def prm_mcp(self, request: Request) -> Response:
        return self._prm(mcp_resource(self.settings))

    async def prm_root(self, request: Request) -> Response:
        # Clients that probe the root get the only resource tokens can be issued for.
        return self._prm(mcp_resource(self.settings))

    async def as_metadata(self, request: Request) -> Response:
        iss = issuer(self.settings)
        return JSONResponse({
            "issuer": iss,
            "authorization_endpoint": f"{iss}/oauth/authorize",
            "token_endpoint": f"{iss}/oauth/token",
            "registration_endpoint": f"{iss}/oauth/register",
            "revocation_endpoint": f"{iss}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(SUPPORTED_SCOPES),
            "authorization_response_iss_parameter_supported": True,
            "client_id_metadata_document_supported": self.settings.oauth_cimd_enabled,
            "service_documentation": "https://github.com/satchel-dev/satchel",
        }, headers=CORS)

    # ---- OAuth endpoints ------------------------------------------------------------------

    async def register(self, request: Request) -> Response:
        if int(request.headers.get("content-length") or 0) > MAX_REGISTER_BYTES:
            return _err("invalid_client_metadata", "request too large", 413)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return _err("invalid_client_metadata", "body must be a JSON object")
        try:
            doc = await run_in_threadpool(register_client, body, engine=self.engine)
        except ClientMetadataError as e:
            return _err("invalid_client_metadata", str(e))
        return _oauth_json(doc, 201)

    async def authorize(self, request: Request) -> Response:
        q = dict(request.query_params)
        return await self._flow_step(lambda: self.flows.start_authorize(q))

    async def token(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        client_id = form.get("client_id")
        if not client_id:
            return _err("invalid_request", "client_id is required (public clients only)")
        grant = form.get("grant_type")
        try:
            if grant == "authorization_code":
                if not form.get("code"):
                    return _err("invalid_request", "code is required")
                res = await run_in_threadpool(
                    lambda: tokens.exchange_code(
                        code=form["code"], client_id=client_id,
                        redirect_uri=form.get("redirect_uri"),
                        code_verifier=form.get("code_verifier"), resource=form.get("resource"),
                        settings=self.settings, engine=self.engine))
            elif grant == "refresh_token":
                if not form.get("refresh_token"):
                    return _err("invalid_request", "refresh_token is required")
                res = await run_in_threadpool(
                    lambda: tokens.refresh(
                        refresh_token=form["refresh_token"], client_id=client_id,
                        resource=form.get("resource"), settings=self.settings,
                        engine=self.engine))
            else:
                return _err("unsupported_grant_type", "use authorization_code or refresh_token")
        except tokens.OAuthError as e:
            return _err(e.error, e.description, e.status)
        return _oauth_json(res)

    async def revoke(self, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        if form.get("token"):
            await run_in_threadpool(
                lambda: tokens.revoke(token=form["token"], client_id=form.get("client_id"),
                                      engine=self.engine))
        return _oauth_json({})  # RFC 7009: 200 regardless of token validity

    # ---- interactive pages ----------------------------------------------------------------

    async def callback(self, request: Request) -> Response:
        q, cookies = dict(request.query_params), dict(request.cookies)

        def run() -> Outcome:
            return self.flows.finish_web_login(self.flows.handle_callback(q, cookies))

        return await self._flow_step(run)

    async def login(self, request: Request) -> Response:
        q = request.query_params
        return await self._flow_step(
            lambda: self.flows.start_web_login(q.get("next"), q.get("workspace")))

    async def workspace_get(self, request: Request) -> Response:
        try:
            data = await run_in_threadpool(
                self.flows.picker, _flow_id(request.query_params.get("flow")),
                dict(request.cookies))
        except PageError as e:
            return error_page(e)
        items = "".join(
            f"<li><button name=workspace value='{html.escape(slug, quote=True)}'>"
            f"{html.escape(name)}</button></li>" for slug, name in data.workspaces)
        return page("Choose a workspace", (
            f"<form method=post action=/auth/workspace>"
            f"<input type=hidden name=flow value='{data.flow_id}'>"
            f"<input type=hidden name=csrf value='{html.escape(data.csrf, quote=True)}'>"
            f"<ul style='list-style:none;padding:0'>{items}</ul></form>"))

    async def workspace_post(self, request: Request) -> Response:
        f = {k: str(v) for k, v in (await request.form()).items()}

        def run() -> Outcome:
            return self.flows.finish_web_login(self.flows.pick(
                _flow_id(f.get("flow")), f.get("workspace", ""), f.get("csrf", ""),
                dict(request.cookies)))

        return await self._flow_step(run)

    async def consent_get(self, request: Request) -> Response:
        try:
            d = await run_in_threadpool(
                self.flows.consent_data, _flow_id(request.query_params.get("flow")),
                dict(request.cookies))
        except PageError as err:
            return error_page(err)
        e = html.escape
        scopes = "".join(f"<li><code>{e(s)}</code></li>" for s in d.scopes)
        return page(f"Authorize {d.client_name}", (
            f"<p><b>{e(d.client_name)}</b> wants to access the <b>{e(d.workspace_name)}</b> "
            f"satchel workspace as <b>{e(d.user_label)}</b>.</p>"
            f"<p>Requested access:</p><ul>{scopes}</ul>"
            f"<p style='color:#6b7280;font-size:.9rem'>Client ID: <code>{e(d.client_id)}</code>"
            f"<br>After you decide you will be sent to <code>{e(d.redirect_host)}</code>. "
            f"Only approve if you recognise this application.</p>"
            f"<form method=post action=/oauth/consent>"
            f"<input type=hidden name=flow value='{d.flow_id}'>"
            f"<input type=hidden name=csrf value='{e(d.csrf, quote=True)}'>"
            f"<button class=primary name=decision value=approve>Approve</button> "
            f"<button name=decision value=deny>Deny</button></form>"))

    async def consent_post(self, request: Request) -> Response:
        f = {k: str(v) for k, v in (await request.form()).items()}
        return await self._flow_step(lambda: self.flows.decide(
            _flow_id(f.get("flow")), f.get("decision", ""), f.get("csrf", ""),
            dict(request.cookies)))

    # ---- UI session -----------------------------------------------------------------------

    async def me(self, request: Request) -> Response:
        info = await run_in_threadpool(
            sessions.load_session, request.cookies.get(sessions.COOKIE), engine=self.engine)
        if info is None:
            return JSONResponse({"error": "not_authenticated"}, status_code=401,
                                headers={"Cache-Control": "no-store"})

        def details() -> dict[str, Any]:
            with admin_session(engine=self.engine) as s:
                ws = s.scalars(select(Workspace).where(Workspace.id == info.workspace_id)).one()
                u = s.scalars(select(AppUser).where(AppUser.id == info.user_id)).one()
                return {"workspace": ws.slug, "user_id": str(u.id), "email": u.email,
                        "name": u.display_name, "role": u.role, "csrf_token": info.csrf}

        return JSONResponse(await run_in_threadpool(details), headers={"Cache-Control": "no-store"})

    async def logout(self, request: Request) -> Response:
        raw = request.cookies.get(sessions.COOKIE)
        info = await run_in_threadpool(sessions.load_session, raw, engine=self.engine)
        if info is None:
            return JSONResponse({"error": "not_authenticated"}, status_code=401)
        if not sessions.csrf_ok(info, request.headers.get(sessions.CSRF_HEADER)):
            return JSONResponse({"error": "csrf"}, status_code=403)
        await run_in_threadpool(sessions.revoke_session, raw, engine=self.engine)
        r = JSONResponse({"status": "logged_out"})
        r.delete_cookie(sessions.COOKIE, path="/")
        return r

    # ---- wiring ---------------------------------------------------------------------------

    def routes(self) -> list[Route]:
        async def preflight(_: Request) -> Response:
            return Response(status_code=204, headers=CORS)

        def cors_route(path: str, h: Handler, methods: list[str]) -> list[Route]:
            return [Route(path, h, methods=methods),
                    Route(path, preflight, methods=["OPTIONS"])]

        out: list[Route] = []
        out += cors_route("/.well-known/oauth-protected-resource/mcp", self.prm_mcp, ["GET"])
        out += cors_route("/.well-known/oauth-protected-resource", self.prm_root, ["GET"])
        out += cors_route("/.well-known/oauth-authorization-server", self.as_metadata, ["GET"])
        out += cors_route("/oauth/register", self.register, ["POST"])
        out += cors_route("/oauth/token", self.token, ["POST"])
        out += cors_route("/oauth/revoke", self.revoke, ["POST"])
        out += [
            Route("/oauth/authorize", self.authorize, methods=["GET"]),
            Route("/oauth/consent", self.consent_get, methods=["GET"]),
            Route("/oauth/consent", self.consent_post, methods=["POST"]),
            Route("/auth/login", self.login, methods=["GET"]),
            Route("/auth/callback", self.callback, methods=["GET"]),
            Route("/auth/workspace", self.workspace_get, methods=["GET"]),
            Route("/auth/workspace", self.workspace_post, methods=["POST"]),
            Route("/auth/me", self.me, methods=["GET"]),
            Route("/auth/logout", self.logout, methods=["POST"]),
        ]
        return out


def mount_oauth(app: Any, oauth: OAuthApp) -> None:
    """Add the OAuth/OIDC routes to a Starlette/FastAPI app."""
    app.router.routes.extend(oauth.routes())
