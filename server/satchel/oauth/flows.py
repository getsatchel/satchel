"""Interactive flows: OAuth authorize -> IdP -> workspace -> consent -> code, and UI login.

State machine of an ``auth_flow`` row::

    idp --callback--> workspace --(pick / auto)--> consent --approve--> code --token--> redeemed
                                         \\--> done   (kind = web: session cookie issued)

A per-flow cookie (hash stored on the row) binds every step to the browser that started it
(login-CSRF protection); the picker and consent forms also carry the row's CSRF token.
"""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from satchel import audit
from satchel.oauth import sessions
from satchel.oauth.clients import CimdFetcher, ClientInfo, get_client, normalize_scopes
from satchel.oauth.common import (
    allowed_resources,
    issuer,
    mcp_resource,
    normalize_resource,
    pkce_s256,
    random_token,
    safe_next,
    sha256_hex,
    valid_challenge,
)
from satchel.oauth.oidc import Identity, OidcError, OidcProvider
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import AppUser, AuthFlow, Workspace

PURGE_AFTER = timedelta(days=1)


class PageError(Exception):
    """Shown to the user as an error page; never redirected (unsafe redirect target or no
    trustworthy client to send the user back to)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message, self.status = message, status


class RedirectError(Exception):
    """OAuth error returned to the (validated) client redirect_uri."""

    def __init__(self, redirect_uri: str, error: str, description: str, state: str | None,
                 iss: str) -> None:
        super().__init__(error)
        params = {"error": error, "error_description": description, "iss": iss}
        if state is not None:
            params["state"] = state
        self.url = with_params(redirect_uri, params)


@dataclass
class Outcome:
    """What the route should do next: redirect, set and clear cookies."""

    redirect: str
    set_cookies: list[tuple[str, str, int]] = field(default_factory=list)
    clear_cookies: list[str] = field(default_factory=list)
    # (workspace_id, user_id) of a completed web login; finish_web_login mints the session.
    pending_session: tuple[uuid.UUID, uuid.UUID] | None = None


@dataclass
class PickerData:
    flow_id: uuid.UUID
    csrf: str
    workspaces: list[tuple[str, str]]  # (slug, name)


@dataclass
class ConsentData:
    flow_id: uuid.UUID
    csrf: str
    client_name: str
    client_id: str
    redirect_host: str
    scopes: list[str]
    workspace_name: str
    user_label: str


def with_params(uri: str, params: dict[str, str]) -> str:
    p = urlsplit(uri)
    q = parse_qsl(p.query, keep_blank_values=True) + list(params.items())
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def flow_cookie_name(flow_id: uuid.UUID) -> str:
    return f"lf_{flow_id.hex[:16]}"


class FlowService:
    def __init__(
        self, engine: Engine | None, settings: Settings, oidc: OidcProvider | None,
        fetcher: CimdFetcher | None = None,
    ) -> None:
        self.engine, self.settings, self.oidc, self.fetcher = engine, settings, oidc, fetcher

    @property
    def callback_url(self) -> str:
        return issuer(self.settings) + "/auth/callback"

    # ---- starting a flow ------------------------------------------------------------------

    def _new_flow(self, s: OrmSession, kind: str, **fields: Any) -> tuple[AuthFlow, str, str]:
        """Insert a flow in state ``idp``; returns (row, idp_authorize_url, browser secret)."""
        if self.oidc is None:
            raise PageError("Login is not configured on this server (SATCHEL_OIDC_* unset).", 503)
        now = datetime.now(UTC)
        s.execute(delete(AuthFlow).where(AuthFlow.expires_at < now - PURGE_AFTER))
        state, nonce = random_token(24), random_token(24)
        verifier, browser = random_token(48), random_token(24)
        flow = AuthFlow(
            kind=kind, status="idp", state_hash=sha256_hex(state), browser_hash=sha256_hex(browser),
            nonce=nonce, code_verifier=verifier, csrf=random_token(24),
            expires_at=now + timedelta(seconds=self.settings.oauth_flow_ttl_s), **fields,
        )
        s.add(flow)
        s.flush()
        try:
            url = self.oidc.authorize_url(
                state=state, nonce=nonce, code_challenge=pkce_s256(verifier),
                redirect_uri=self.callback_url,
            )
        except OidcError as e:
            raise PageError(f"The identity provider is unavailable: {e}", 502) from e
        return flow, url, browser

    def _outcome(self, flow: AuthFlow, url: str, browser: str) -> Outcome:
        return Outcome(url, [(flow_cookie_name(flow.id), browser, self.settings.oauth_flow_ttl_s)])

    def start_authorize(self, q: dict[str, str]) -> Outcome:
        st = self.settings
        iss = issuer(st)
        client = get_client(q.get("client_id", ""), st, engine=self.engine, fetcher=self.fetcher)
        if client is None:
            raise PageError("Unknown or invalid client_id.")
        redirect_uri = q.get("redirect_uri")
        if redirect_uri is None and len(client.redirect_uris) == 1:
            redirect_uri = client.redirect_uris[0]
        # Exact string match only; anything else is an error page, never a redirect.
        if redirect_uri is None or redirect_uri not in client.redirect_uris:
            raise PageError("redirect_uri does not match a URI registered for this client.")
        state = q.get("state")

        def fail(error: str, desc: str) -> RedirectError:
            return RedirectError(redirect_uri, error, desc, state, iss)

        if q.get("response_type") != "code":
            raise fail("unsupported_response_type", "response_type must be 'code'")
        challenge = q.get("code_challenge", "")
        if q.get("code_challenge_method") != "S256" or not valid_challenge(challenge):
            raise fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
        resource = normalize_resource(q.get("resource") or mcp_resource(st))
        if resource not in allowed_resources(st):
            raise fail("invalid_target", "resource is not this server")
        scopes = normalize_scopes(q.get("scope"))
        with admin_session(engine=self.engine) as s:
            flow, url, browser = self._new_flow(
                s, "mcp", client_id=client.client_id, redirect_uri=redirect_uri,
                code_challenge=challenge, scope=" ".join(scopes), resource=resource,
                client_state=state, requested_workspace=q.get("workspace") or None,
            )
            return self._outcome(flow, url, browser)

    def start_web_login(self, next_url: str | None, workspace: str | None) -> Outcome:
        with admin_session(engine=self.engine) as s:
            flow, url, browser = self._new_flow(
                s, "web", next_url=safe_next(next_url), requested_workspace=workspace or None
            )
            return self._outcome(flow, url, browser)

    # ---- IdP callback ---------------------------------------------------------------------

    def _verify_browser(self, flow: AuthFlow, cookies: dict[str, str]) -> None:
        got = cookies.get(flow_cookie_name(flow.id), "")
        if not got or not hmac.compare_digest(sha256_hex(got), flow.browser_hash):
            raise PageError("This login was not started in this browser. Please start again.", 403)

    def handle_callback(self, q: dict[str, str], cookies: dict[str, str]) -> Outcome:
        state = q.get("state")
        if not state:
            raise PageError("Missing state.")
        now = datetime.now(UTC)
        with admin_session(engine=self.engine) as s:
            flow = s.scalars(
                select(AuthFlow).where(AuthFlow.state_hash == sha256_hex(state)).with_for_update()
            ).first()
            if flow is None or flow.status != "idp" or flow.expires_at <= now:
                raise PageError("This login link has expired or was already used.")
            self._verify_browser(flow, cookies)
            flow_id, kind = flow.id, flow.kind
            nonce, verifier = flow.nonce, flow.code_verifier
            redirect_uri, client_state = flow.redirect_uri, flow.client_state
            if "error" in q or "code" not in q:
                flow.status = "denied"
                flow.expires_at = now
                err = q.get("error", "access_denied")
            else:
                flow.status = "workspace"  # the state is now consumed: no callback replay
                err = None
        if err is not None:
            clear = [flow_cookie_name(flow_id)]
            if kind == "mcp" and redirect_uri:
                url = RedirectError(redirect_uri, "access_denied", "login was not completed",
                                    client_state, issuer(self.settings)).url
                return Outcome(url, clear_cookies=clear)
            raise PageError("The identity provider did not complete the login.")
        assert self.oidc is not None
        try:
            ident = self.oidc.exchange(
                code=q["code"], code_verifier=verifier, nonce=nonce, redirect_uri=self.callback_url
            )
        except OidcError as e:
            raise PageError(f"Login failed: {e}", 502) from e
        return self._after_identity(flow_id, ident)

    def _after_identity(self, flow_id: uuid.UUID, ident: Identity) -> Outcome:
        with admin_session(engine=self.engine) as s:
            flow = s.get(AuthFlow, flow_id, with_for_update=True)
            assert flow is not None
            flow.idp_subject, flow.idp_email = ident.subject, ident.email
            flow.idp_email_verified, flow.idp_name = ident.email_verified, ident.name
            cands = self._candidates(s, flow, ident)
            if not cands:
                flow.status = "denied"
                raise_denied = True
            else:
                raise_denied = False
                if len(cands) == 1:
                    return self._select(s, flow, cands[0], ident)
        if raise_denied:
            raise PageError(
                "Your account does not have access to this satchel workspace. "
                "Ask a workspace admin to add you.", 403)
        return Outcome(f"/auth/workspace?flow={flow_id}")

    # ---- user mapping & provisioning ------------------------------------------------------

    def _email_trusted(self, ident: Identity) -> bool:
        return bool(ident.email) and (
            ident.email_verified or not self.settings.oidc_require_verified_email
        )

    def _match(self, s: OrmSession, ws: Workspace, ident: Identity) -> tuple[str, AppUser | None]:
        """('user', u) existing user; ('provision', None) may create one; ('deny', None)."""
        by_sub = s.scalars(select(AppUser).where(
            AppUser.workspace_id == ws.id, AppUser.external_sub == ident.subject)).first()
        if by_sub is not None:
            return "user", by_sub
        if self._email_trusted(ident):
            assert ident.email
            by_email = s.scalars(select(AppUser).where(
                AppUser.workspace_id == ws.id,
                func.lower(AppUser.email) == ident.email.lower())).first()
            if by_email is not None:
                # Never re-link an account already bound to a different IdP subject.
                if by_email.external_sub not in (None, ident.subject):
                    return "deny", None
                return "user", by_email
        auth = (ws.settings or {}).get("auth", {})
        if auth.get("provision") == "explorer" and self._email_trusted(ident):
            domains = [d.lower() for d in auth.get("allowed_email_domains", []) if d]
            assert ident.email
            if not domains or ident.email.lower().rpartition("@")[2] in domains:
                return "provision", None
        return "deny", None

    def _candidates(self, s: OrmSession, flow: AuthFlow, ident: Identity) -> list[Workspace]:
        stmt = select(Workspace).order_by(Workspace.slug)
        if flow.requested_workspace:
            stmt = stmt.where(Workspace.slug == flow.requested_workspace)
        return [w for w in s.scalars(stmt) if self._match(s, w, ident)[0] != "deny"]

    def _select(self, s: OrmSession, flow: AuthFlow, ws: Workspace, ident: Identity) -> Outcome:
        verdict, user = self._match(s, ws, ident)
        if verdict == "deny":
            raise PageError("Your account does not have access to this workspace.", 403)
        if user is None:
            user = AppUser(workspace_id=ws.id, external_sub=ident.subject, email=ident.email,
                           display_name=ident.name or ident.email, role="explorer")
            s.add(user)
            try:
                s.flush()
            except IntegrityError as e:
                raise PageError("Could not provision your account; try again.", 409) from e
            audit.record(s, ws.id, user.id, "user.provision",
                         {"user": user.id, "email": ident.email, "role": "explorer",
                          "via": "oidc"})
        elif user.external_sub is None:
            user.external_sub = ident.subject
        flow.workspace_id, flow.user_id = ws.id, user.id
        audit.record(s, ws.id, user.id, "auth.login",
                     {"kind": flow.kind, "client_id": flow.client_id})
        clear = [flow_cookie_name(flow.id)]
        if flow.kind == "mcp":
            flow.status = "consent"
            return Outcome(f"/oauth/consent?flow={flow.id}")
        flow.status = "done"
        next_url = safe_next(flow.next_url)
        # The session row is written after this transaction commits (see finish_web_login).
        return Outcome(next_url, clear_cookies=clear, pending_session=(ws.id, user.id))

    def finish_web_login(self, outcome: Outcome) -> Outcome:
        """Create the browser session for a completed web login and attach its cookie."""
        pending = outcome.pending_session
        if pending is None:
            return outcome
        raw, _csrf = sessions.create_session(self.engine, pending[0], pending[1], self.settings)
        outcome.set_cookies.append((sessions.COOKIE, raw, self.settings.session_ttl_s))
        return outcome

    # ---- workspace picker -----------------------------------------------------------------

    def _load(
        self, s: OrmSession, flow_id: uuid.UUID, cookies: dict[str, str], status: str
    ) -> AuthFlow:
        flow = s.get(AuthFlow, flow_id, with_for_update=True)
        if flow is None or flow.expires_at <= datetime.now(UTC) or flow.status != status:
            raise PageError("This step has expired or was already completed.")
        self._verify_browser(flow, cookies)
        return flow

    def _ident(self, flow: AuthFlow) -> Identity:
        assert flow.idp_subject
        return Identity(flow.idp_subject, flow.idp_email, flow.idp_email_verified, flow.idp_name)

    def picker(self, flow_id: uuid.UUID, cookies: dict[str, str]) -> PickerData:
        with admin_session(engine=self.engine) as s:
            flow = self._load(s, flow_id, cookies, "workspace")
            cands = self._candidates(s, flow, self._ident(flow))
            return PickerData(flow.id, flow.csrf, [(w.slug, w.name) for w in cands])

    def pick(
        self, flow_id: uuid.UUID, slug: str, csrf: str, cookies: dict[str, str]
    ) -> Outcome:
        with admin_session(engine=self.engine) as s:
            flow = self._load(s, flow_id, cookies, "workspace")
            if not hmac.compare_digest(flow.csrf, csrf):
                raise PageError("Invalid form token.", 403)
            ident = self._ident(flow)
            chosen = [w for w in self._candidates(s, flow, ident) if w.slug == slug]
            if not chosen:
                raise PageError("You do not have access to that workspace.", 403)
            return self._select(s, flow, chosen[0], ident)

    # ---- consent --------------------------------------------------------------------------

    def consent_data(self, flow_id: uuid.UUID, cookies: dict[str, str]) -> ConsentData:
        with admin_session(engine=self.engine) as s:
            flow = self._load(s, flow_id, cookies, "consent")
            assert flow.client_id and flow.redirect_uri and flow.workspace_id and flow.user_id
            client = get_client(flow.client_id, self.settings, engine=self.engine,
                                fetcher=self.fetcher)
            ws = s.get(Workspace, flow.workspace_id)
            user = s.get(AppUser, flow.user_id)
            assert ws is not None and user is not None
            return ConsentData(
                flow.id, flow.csrf, client.client_name if client else flow.client_id,
                flow.client_id, urlsplit(flow.redirect_uri).netloc,
                (flow.scope or "mcp").split(), ws.name, user.email or user.display_name or "you",
            )

    def decide(
        self, flow_id: uuid.UUID, decision: str, csrf: str, cookies: dict[str, str]
    ) -> Outcome:
        iss = issuer(self.settings)
        with admin_session(engine=self.engine) as s:
            flow = self._load(s, flow_id, cookies, "consent")
            if not hmac.compare_digest(flow.csrf, csrf):
                raise PageError("Invalid form token.", 403)
            assert flow.redirect_uri
            clear = [flow_cookie_name(flow.id)]
            consent = {"client_id": flow.client_id, "scope": flow.scope, "decision": decision,
                       "redirect_host": urlsplit(flow.redirect_uri).netloc}
            if flow.workspace_id is not None:
                audit.record(s, flow.workspace_id, flow.user_id,
                             "oauth.consent.approve" if decision == "approve"
                             else "oauth.consent.deny", consent)
            if decision != "approve":
                flow.status = "denied"
                url = RedirectError(flow.redirect_uri, "access_denied", "the user denied access",
                                    flow.client_state, iss).url
                return Outcome(url, clear_cookies=clear)
            code = random_token(32)
            flow.code_hash = sha256_hex(code)
            flow.code_expires_at = datetime.now(UTC) + timedelta(
                seconds=self.settings.oauth_code_ttl_s)
            flow.status = "code"
            params = {"code": code, "iss": iss}
            if flow.client_state is not None:
                params["state"] = flow.client_state
            return Outcome(with_params(flow.redirect_uri, params), clear_cookies=clear)


def client_summary(c: ClientInfo) -> str:
    return f"{c.client_name} ({c.kind})"
