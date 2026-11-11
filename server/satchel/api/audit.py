"""``GET /v1/audit``: the audit trail for SIEM pulls (admin, RLS-scoped, keyset paginated).

Events are ordered by ``(created_at, id)``. Poll with ``since`` and follow ``next_cursor``
until it is null; remember the last event's ``created_at`` as the next ``since``. For bulk
export to files or object storage use ``satchel audit export``.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from satchel import audit as audit_lib
from satchel.api.auth import Auth, Caller
from satchel.store.db import workspace_session


class AuditEventOut(BaseModel):
    id: int
    workspace: str
    actor_user_id: str | None
    actor_email: str | None
    action: str
    target: dict[str, Any]
    created_at: datetime


class AuditPage(BaseModel):
    events: list[AuditEventOut]
    next_cursor: str | None = Field(
        default=None, description="Pass as `cursor` for the next page; null on the last page."
    )


def _when(name: str, value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return audit_lib.parse_when(value)
    except ValueError as e:
        raise HTTPException(422, f"{name}: {e}") from None


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["audit"])
    Adm = Annotated[Caller, Depends(auth.require("admin"))]
    eng = auth.ctx.engine

    @r.get("/v1/audit", response_model=AuditPage)
    def list_audit(
        who: Adm,
        since: Annotated[str | None, Query(description="ISO-8601, inclusive")] = None,
        until: Annotated[str | None, Query(description="ISO-8601, exclusive")] = None,
        cursor: Annotated[str | None, Query(description="From a previous page")] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    ) -> AuditPage:
        after = None
        if cursor is not None:
            try:
                after = audit_lib.decode_cursor(cursor)
            except ValueError:
                raise HTTPException(422, "invalid cursor") from None
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            rows, nxt = audit_lib.fetch_page(
                s, since=_when("since", since), until=_when("until", until), after=after,
                limit=limit, workspace_id=who.workspace_id,
            )
            events = [
                AuditEventOut(
                    id=x.id, workspace=x.workspace,
                    actor_user_id=str(x.actor_user_id) if x.actor_user_id else None,
                    actor_email=x.actor_email, action=x.action, target=x.target,
                    created_at=x.created_at,
                )
                for x in rows
            ]
        return AuditPage(events=events, next_cursor=audit_lib.encode_cursor(nxt) if nxt else None)

    return r
