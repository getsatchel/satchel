"""Settings → Members: who can use the workspace, and with which role (admin only).

* ``GET /v1/members``: every member with their status and last activity, plus the sign-in policy.
* ``POST /v1/members``: invite by email. An invite is a member with an email and no identity
  provider subject yet; their first OIDC sign-in with that (verified) email links the account.
* ``PATCH /v1/members/{id}``: change the role, deactivate or reactivate. A deactivated member's
  tokens, sessions and sign-ins are refused until they are reactivated.
* ``DELETE /v1/members/{id}``: cancel an invite nobody has used yet.
* ``PUT /v1/members/sign-in``: whether people who were not invited may sign in (as explorers),
  optionally only from some email domains. The same setting as ``satchel workspace auth``.

Guards: you cannot change your own role or deactivate yourself, and the workspace always keeps
at least one active admin. Every change is audited (``user.invite``, ``user.role_change``,
``user.disable``, ``user.enable``, ``user.uninvite``, ``workspace.auth.update``).
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from satchel.api.auth import Auth, Caller
from satchel.api.common import audit
from satchel.oauth.directory import directory_for
from satchel.oauth.routes import provider_name
from satchel.store.db import workspace_session
from satchel.store.models import ApiToken, AppUser, WebSession, Workspace

Role = Literal["admin", "curator", "analyst", "explorer"]
Status = Literal["active", "invited", "deactivated"]
EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
MAX_INVITES = 50


class MemberOut(BaseModel):
    id: uuid.UUID
    email: str | None
    name: str | None
    role: Role
    status: Status = Field(
        description="invited: never signed in or used a token; deactivated: refused everywhere."
    )
    created_at: datetime
    last_active_at: datetime | None
    is_you: bool


class SignInPolicy(BaseModel):
    provision: Literal["reject", "explorer"] = Field(
        description="reject: only members may sign in. explorer: anyone else with a verified "
        "email (in allowed_email_domains, if set) joins as an explorer on first sign-in.")
    allowed_email_domains: list[str] = Field(default_factory=list)


class MembersOut(BaseModel):
    members: list[MemberOut]
    sign_in: SignInPolicy
    oidc_configured: bool = Field(description="Whether the server has an identity provider.")
    provider: str | None = Field(None, description="The identity provider's name, if known.")
    creates_accounts: bool = Field(
        False, description="Inviting someone also creates their account at the identity "
        "provider, which emails them (SATCHEL_OIDC_CREATE_USERS).")


class InviteIn(BaseModel):
    emails: list[str] = Field(min_length=1, max_length=MAX_INVITES)
    role: Role = "analyst"


class AccountOut(BaseModel):
    email: str
    status: Literal["created", "exists", "failed"] = Field(
        description="created: the provider emailed them; exists: they already had an account.")
    detail: str | None = None


class InviteOut(BaseModel):
    invited: list[MemberOut]
    existing: list[str] = Field(description="Addresses that are already members (unchanged).")
    accounts: list[AccountOut] = Field(
        default_factory=list, description="Identity provider accounts created for the invited "
        "addresses (and for earlier invites nobody has used), when the server does that.")


class MemberPatch(BaseModel):
    role: Role | None = None
    active: bool | None = None


def _last_active(s: Session) -> dict[uuid.UUID, datetime]:
    seen = union_all(
        select(WebSession.user_id.label("u"),
               func.coalesce(WebSession.last_seen_at, WebSession.created_at).label("t")),
        select(ApiToken.user_id.label("u"), ApiToken.last_used_at.label("t"))
        .where(ApiToken.last_used_at.is_not(None)),
    ).subquery()
    rows = s.execute(select(seen.c.u, func.max(seen.c.t)).group_by(seen.c.u)).all()
    return {u: t for u, t in rows}


def _out(u: AppUser, last: datetime | None, who: Caller) -> MemberOut:
    status: Status = (
        "deactivated" if u.disabled_at is not None
        else "invited" if u.external_sub is None and last is None
        else "active"
    )
    return MemberOut(
        id=u.id, email=u.email, name=u.display_name if u.display_name != u.email else None,
        role=u.role, status=status, created_at=u.created_at,
        last_active_at=last, is_you=u.id == who.user_id,
    )


def _policy(ws: Workspace) -> SignInPolicy:
    cfg = (ws.settings or {}).get("auth", {})
    return SignInPolicy(
        provision="explorer" if cfg.get("provision") == "explorer" else "reject",
        allowed_email_domains=list(cfg.get("allowed_email_domains", [])),
    )


def _norm_email(raw: str) -> str:
    e = raw.strip().lower()
    if not EMAIL.match(e):
        raise HTTPException(422, f"not an email address: {raw.strip() or '(empty)'}")
    return e


def _norm_domain(raw: str) -> str:
    d = raw.strip().lower().lstrip("@")
    if not re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", d):
        raise HTTPException(422, f"not a domain: {raw.strip() or '(empty)'}")
    return d


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["members"])
    Adm = Annotated[Caller, Depends(auth.require("admin"))]
    eng = auth.ctx.engine

    def get_member(s: Session, member_id: uuid.UUID) -> AppUser:
        u = s.get(AppUser, member_id)
        if u is None:
            raise HTTPException(404, "no such member")
        return u

    @r.get("/v1/members", response_model=MembersOut)
    def list_members(who: Adm) -> MembersOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            last = _last_active(s)
            users = s.scalars(select(AppUser).order_by(
                func.lower(func.coalesce(AppUser.email, AppUser.display_name, "")))).all()
            ws = s.get(Workspace, who.workspace_id)
            assert ws is not None
            return MembersOut(
                members=[_out(u, last.get(u.id), who) for u in users],
                sign_in=_policy(ws),
                oidc_configured=bool(auth.ctx.settings.oidc_issuer),
                provider=provider_name(auth.ctx.settings),
                creates_accounts=directory_for(auth.ctx.settings) is not None,
            )

    @r.post("/v1/members", response_model=InviteOut, status_code=201)
    def invite(req: InviteIn, who: Adm) -> InviteOut:
        emails = list(dict.fromkeys(_norm_email(e) for e in req.emails))
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            found = s.scalars(select(AppUser).where(func.lower(AppUser.email).in_(emails))).all()
            taken = {(u.email or "").lower() for u in found}
            last = _last_active(s)
            # Re-inviting someone who never signed in retries their provider account.
            pending = [(u.email or "").lower() for u in found
                       if _out(u, last.get(u.id), who).status == "invited"]
            new = [e for e in emails if e not in taken]
            users = [AppUser(workspace_id=who.workspace_id, email=e, display_name=e,
                             role=req.role) for e in new]
            s.add_all(users)
            s.flush()
            for u in users:
                audit(s, who, "user.invite", {"user": u.id, "email": u.email, "role": u.role})
            out = InviteOut(invited=[_out(u, None, who) for u in users],
                            existing=[e for e in emails if e in taken])
        # After the commit: a provider failure never undoes the invite.
        directory = directory_for(auth.ctx.settings)
        if directory is not None:
            for e in [*new, *pending]:
                res = directory.create_user(e)
                out.accounts.append(
                    AccountOut(email=res.email, status=res.status, detail=res.detail))
            with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
                for acc in out.accounts:
                    audit(s, who, "user.idp_account", {"email": acc.email, "status": acc.status,
                                                       "provider": directory.name})
        return out

    @r.patch("/v1/members/{member_id}", response_model=MemberOut)
    def update_member(member_id: uuid.UUID, req: MemberPatch, who: Adm) -> MemberOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            # Lock the admins first so two admins demoting each other cannot both succeed.
            admins = s.scalars(select(AppUser).where(
                AppUser.role == "admin", AppUser.disabled_at.is_(None)).with_for_update()).all()
            u = get_member(s, member_id)
            role = req.role or u.role
            active = u.disabled_at is None if req.active is None else req.active
            if u.id == who.user_id and (role != u.role or not active):
                raise HTTPException(409, "You can't change your own role or deactivate yourself.")
            if (u.role == "admin" and u.disabled_at is None and (role != "admin" or not active)
                    and not [a for a in admins if a.id != u.id]):
                raise HTTPException(409, "The workspace needs at least one active admin.")
            if role != u.role:
                audit(s, who, "user.role_change", {"user": u.id, "email": u.email, "from": u.role,
                                            "to": role})
                u.role = role
            if active != (u.disabled_at is None):
                u.disabled_at = None if active else datetime.now(UTC)
                audit(s, who, "user.enable" if active else "user.disable",
                      {"user": u.id, "email": u.email})
            s.flush()
            return _out(u, _last_active(s).get(u.id), who)

    @r.delete("/v1/members/{member_id}", status_code=204)
    def cancel_invite(member_id: uuid.UUID, who: Adm) -> Response:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            u = get_member(s, member_id)
            if _out(u, _last_active(s).get(u.id), who).status != "invited":
                raise HTTPException(409, "Only an unused invite can be removed. Deactivate the "
                                         "member instead.")
            email = u.email
            try:
                with s.begin_nested():
                    s.delete(u)
                    s.flush()
            except IntegrityError:
                # Referenced elsewhere (a token, an authored item): keep the row for history.
                raise HTTPException(
                    409, "This invite has history. Deactivate it instead.") from None
            audit(s, who, "user.uninvite", {"user": member_id, "email": email})
        return Response(status_code=204)

    @r.put("/v1/members/sign-in", response_model=SignInPolicy)
    def set_sign_in(req: SignInPolicy, who: Adm) -> SignInPolicy:
        domains = list(dict.fromkeys(_norm_domain(d) for d in req.allowed_email_domains))
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            ws = s.get(Workspace, who.workspace_id)
            assert ws is not None
            settings = dict(ws.settings or {})
            settings["auth"] = {"provision": req.provision, "allowed_email_domains": domains}
            ws.settings = settings
            flag_modified(ws, "settings")
            audit(s, who, "workspace.auth.update", {"auth": settings["auth"], "via": "ui"})
            return _policy(ws)

    return r
