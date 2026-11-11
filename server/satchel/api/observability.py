"""Observability: usage summary, session list and the session inspector (design §9).

Evidence visibility: admins see raw ``tool_call.args``; curators see them redacted (SQL string
literals -> ``'?'``, free text truncated to 120 characters); analysts and explorers only see
their own sessions (403 on others'); a session flagged ``sensitive`` is metadata only for
everyone but admins.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import ColumnElement, and_, case, distinct, exists, func, select
from sqlalchemy.orm import Session

from satchel.api.auth import Auth, Caller
from satchel.api.common import audit
from satchel.api.schemas import (
    DayClientCount,
    FeedbackCounts,
    FeedbackOut,
    ObservabilitySummary,
    Redaction,
    SensitiveRequest,
    ServedItemStat,
    ServedOut,
    SessionDetail,
    SessionPage,
    SessionRowOut,
    SignalOut,
    SignalStat,
    ToolCallOut,
    ToolStat,
)
from satchel.curation.redact import metadata_only, redact_json, truncate
from satchel.store.db import workspace_session
from satchel.store.models import (
    AppUser,
    ContextItem,
    ContextVersion,
    Feedback,
    Release,
    ServedContext,
    Signal,
    ToolCall,
)
from satchel.store.models import Session as SessionRow
from satchel.store.scopes import session_not_eval, signal_not_eval


def evidence_level(who: Caller, owner_id: uuid.UUID | None, sensitive: bool) -> Redaction:
    if who.has_role("admin"):
        return "none"
    if sensitive:
        return "metadata_only"
    if who.user_id is not None and owner_id == who.user_id:
        return "none"
    return "redacted"


def _text(level: Redaction, value: str | None) -> str | None:
    if value is None or level == "none":
        return value
    return truncate(value) if level == "redacted" else None


def _obj(level: Redaction, value: dict[str, Any]) -> dict[str, Any] | None:
    if level == "none":
        return value
    return redact_json(value) if level == "redacted" else None


def _session_conds(
    since: datetime, client: str | None, user_id: uuid.UUID | None, signal_kind: str | None
) -> list[ColumnElement[bool]]:
    conds: list[ColumnElement[bool]] = [SessionRow.started_at >= since, session_not_eval()]
    if client:
        conds.append(SessionRow.client == client)
    if user_id:
        conds.append(SessionRow.user_id == user_id)
    if signal_kind:
        conds.append(
            exists().where(Signal.session_id == SessionRow.id, Signal.kind == signal_kind)
        )
    return conds


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["observability"])
    Cur = Annotated[Caller, Depends(auth.require("curator"))]
    Any_ = Annotated[Caller, Depends(auth.require("explorer"))]
    Adm = Annotated[Caller, Depends(auth.require("admin"))]
    eng = auth.ctx.engine

    @r.get("/v1/observability/summary", response_model=ObservabilitySummary)
    def summary(
        who: Cur,
        days: Annotated[int, Query(json_schema_extra={"enum": [7, 30, 90]})] = 7,
        client: Annotated[str | None, Query()] = None,
        user: Annotated[uuid.UUID | None, Query()] = None,
        signal_kind: Annotated[str | None, Query()] = None,
    ) -> ObservabilitySummary:
        if days not in (7, 30, 90):
            raise HTTPException(422, "days must be 7, 30 or 90")
        since = datetime.now(UTC) - timedelta(days=days)
        conds = _session_conds(since, client, user, signal_kind)
        sess_ids = select(SessionRow.id).where(*conds)
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            total = s.scalar(select(func.count()).select_from(SessionRow).where(*conds)) or 0
            day = func.date(SessionRow.started_at)
            cl = func.coalesce(SessionRow.client, "unknown")
            per_day = [
                DayClientCount(day=d, client=c, sessions=n)
                for d, c, n in s.execute(
                    select(day, cl, func.count()).where(*conds).group_by(day, cl).order_by(day, cl)
                )
            ]
            tools = [
                ToolStat(tool=t, calls=n, errors=e or 0,
                         avg_latency_ms=float(a) if a is not None else None)
                for t, n, e, a in s.execute(
                    select(
                        ToolCall.tool,
                        func.count(),
                        func.sum(case((ToolCall.error.is_not(None), 1), else_=0)),
                        func.avg(ToolCall.latency_ms),
                    )
                    .where(ToolCall.session_id.in_(sess_ids), ToolCall.created_at >= since)
                    .group_by(ToolCall.tool)
                    .order_by(func.count().desc(), ToolCall.tool)
                    .limit(10)
                )
            ]
            sig_conds: list[ColumnElement[bool]] = [Signal.created_at >= since, signal_not_eval()]
            if signal_kind:
                sig_conds.append(Signal.kind == signal_kind)
            if client or user:
                sig_conds.append(Signal.session_id.in_(sess_ids))
            signals = [
                SignalStat(kind=k, count=n)
                for k, n in s.execute(
                    select(Signal.kind, func.count())
                    .where(*sig_conds)
                    .group_by(Signal.kind)
                    .order_by(func.count().desc(), Signal.kind)
                )
            ]
            hu, hd, au, ad = s.execute(
                select(
                    *(
                        func.coalesce(
                            func.sum(case(
                                (and_(Feedback.source == src, sign), 1), else_=0
                            )), 0)
                        for src in ("human", "agent")
                        for sign in (Feedback.rating > 0, Feedback.rating < 0)
                    )
                ).where(Feedback.session_id.in_(sess_ids), Feedback.created_at >= since)
            ).one()
            up, down = hu + au, hd + ad
            served = list(
                s.execute(
                    select(ContextItem.id, ContextItem.key, ContextItem.type, func.count())
                    .select_from(ServedContext)
                    .join(ToolCall, ToolCall.id == ServedContext.tool_call_id)
                    .join(ContextVersion, ContextVersion.id == ServedContext.version_id)
                    .join(ContextItem, ContextItem.id == ContextVersion.item_id)
                    .where(ToolCall.session_id.in_(sess_ids), ToolCall.created_at >= since)
                    .group_by(ContextItem.id, ContextItem.key, ContextItem.type)
                    .order_by(func.count().desc(), ContextItem.key)
                    .limit(10)
                )
            )
            thumbs: dict[uuid.UUID, tuple[int, int]] = {}
            if served:
                for item_id, tu, td in s.execute(
                    select(
                        ContextVersion.item_id,
                        func.count(distinct(case((Feedback.rating > 0, Feedback.id)))),
                        func.count(distinct(case((Feedback.rating < 0, Feedback.id)))),
                    )
                    .select_from(Feedback)
                    .join(ServedContext, ServedContext.tool_call_id == Feedback.tool_call_id)
                    .join(ContextVersion, ContextVersion.id == ServedContext.version_id)
                    .where(
                        ContextVersion.item_id.in_([x[0] for x in served]),
                        Feedback.session_id.in_(sess_ids),
                        Feedback.created_at >= since,
                    )
                    .group_by(ContextVersion.item_id)
                ):
                    thumbs[item_id] = (tu, td)
            return ObservabilitySummary(
                days=days, total_sessions=total, sessions_per_day=per_day, top_tools=tools,
                signals_by_kind=signals,
                feedback=FeedbackCounts(
                    positive=int(up), negative=int(down),
                    human_positive=int(hu), human_negative=int(hd),
                    agent_positive=int(au), agent_negative=int(ad),
                ),
                most_served_items=[
                    ServedItemStat(key=k, type=t, served=n, thumbs_up=thumbs.get(i, (0, 0))[0],
                                   thumbs_down=thumbs.get(i, (0, 0))[1])
                    for i, k, t, n in served
                ],
            )

    # ---- sessions ------------------------------------------------------------------------------

    def rows(
        s: Session, who: Caller, conds: list[ColumnElement[bool]], limit: int, offset: int
    ) -> tuple[int, list[SessionRowOut]]:
        total = s.scalar(select(func.count()).select_from(SessionRow).where(*conds)) or 0
        tc = (
            select(func.count()).where(ToolCall.session_id == SessionRow.id).scalar_subquery()
        )
        sg = select(func.count()).where(Signal.session_id == SessionRow.id).scalar_subquery()
        fu = (
            select(func.count())
            .where(Feedback.session_id == SessionRow.id, Feedback.rating > 0)
            .scalar_subquery()
        )
        fd = (
            select(func.count())
            .where(Feedback.session_id == SessionRow.id, Feedback.rating < 0)
            .scalar_subquery()
        )
        q = (
            select(SessionRow, AppUser.display_name, AppUser.email, tc, sg, fu, fd)
            .outerjoin(AppUser, AppUser.id == SessionRow.user_id)
            .where(*conds)
            .order_by(SessionRow.started_at.desc(), SessionRow.id)
            .limit(limit)
            .offset(offset)
        )
        out: list[SessionRowOut] = []
        for sess, name, email, ntc, nsg, nfu, nfd in s.execute(q):
            level = evidence_level(who, sess.user_id, sess.sensitive)
            out.append(
                SessionRowOut(
                    id=sess.id, user_id=sess.user_id, user_label=name or email,
                    client=sess.client, status=sess.status, started_at=sess.started_at,
                    ended_at=sess.ended_at, question=_text(level, sess.question),
                    sensitive=sess.sensitive, tool_call_count=ntc, signal_count=nsg,
                    thumbs_up=nfu, thumbs_down=nfd,
                )
            )
        return total, out

    @r.get("/v1/sessions", response_model=SessionPage)
    def list_sessions(
        who: Any_,
        client: Annotated[str | None, Query()] = None,
        user_id: Annotated[uuid.UUID | None, Query()] = None,
        status: Annotated[str | None, Query()] = None,
        days: Annotated[int | None, Query(ge=1, le=365)] = None,
        include_evals: Annotated[bool, Query()] = False,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> SessionPage:
        conds: list[ColumnElement[bool]] = [] if include_evals else [session_not_eval()]
        if not who.has_role("curator"):
            if who.user_id is None or (user_id is not None and user_id != who.user_id):
                raise HTTPException(403, "you can only list your own sessions")
            user_id = who.user_id
        if client:
            conds.append(SessionRow.client == client)
        if user_id:
            conds.append(SessionRow.user_id == user_id)
        if status:
            conds.append(SessionRow.status == status)
        if days:
            conds.append(SessionRow.started_at >= datetime.now(UTC) - timedelta(days=days))
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            total, items = rows(s, who, conds, limit, offset)
            return SessionPage(total=total, limit=limit, offset=offset, sessions=items)

    @r.get("/v1/sessions/{session_id}", response_model=SessionDetail)
    def get_session(session_id: uuid.UUID, who: Any_) -> SessionDetail:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            sess = s.get(SessionRow, session_id)
            if sess is None:
                raise HTTPException(404, "no such session")
            if not who.has_role("curator") and (
                who.user_id is None or sess.user_id != who.user_id
            ):
                raise HTTPException(403, "you can only inspect your own sessions")
            level = evidence_level(who, sess.user_id, sess.sensitive)
            _, listed = rows(s, who, [SessionRow.id == session_id], 1, 0)
            head = listed[0]
            calls = list(
                s.scalars(
                    select(ToolCall).where(ToolCall.session_id == session_id).order_by(ToolCall.id)
                )
            )
            served_by_call: dict[int, list[ServedOut]] = {}
            if calls:
                for tcid, key, typ, seq, rank, score in s.execute(
                    select(
                        ServedContext.tool_call_id, ContextItem.key, ContextItem.type,
                        ContextVersion.seq, ServedContext.rank, ServedContext.score,
                    )
                    .join(ContextVersion, ContextVersion.id == ServedContext.version_id)
                    .join(ContextItem, ContextItem.id == ContextVersion.item_id)
                    .where(ServedContext.tool_call_id.in_([c.id for c in calls]))
                    .order_by(ServedContext.tool_call_id, ServedContext.rank)
                ):
                    served_by_call.setdefault(tcid, []).append(
                        ServedOut(item_key=key, item_type=typ, seq=seq, rank=rank, score=score)
                    )
            timeline = [
                ToolCallOut(
                    id=c.id, tool=c.tool, created_at=c.created_at, latency_ms=c.latency_ms,
                    has_error=c.error is not None, error=_text(level, c.error),
                    args=_obj(level, c.args),
                    result_meta=(
                        metadata_only(c.result_meta) if level == "metadata_only"
                        else _obj(level, c.result_meta) or {}
                    ),
                    served=served_by_call.get(c.id, []),
                )
                for c in calls
            ]
            sigs = list(
                s.scalars(
                    select(Signal).where(Signal.session_id == session_id).order_by(Signal.id)
                )
            )
            item_ids = {i for sg in sigs for i in sg.item_ids}
            keys = (
                dict(s.execute(select(ContextItem.id, ContextItem.key)
                               .where(ContextItem.id.in_(item_ids))).tuples().all())
                if item_ids else {}
            )
            signals = [
                SignalOut(
                    id=sg.id, kind=sg.kind, source=sg.source, created_at=sg.created_at,
                    payload=_obj(level, sg.payload),
                    item_keys=sorted(keys[i] for i in sg.item_ids if i in keys),
                )
                for sg in sigs
            ]
            fbs = s.scalars(
                select(Feedback).where(Feedback.session_id == session_id).order_by(Feedback.id)
            )
            feedback = [
                FeedbackOut(
                    id=f.id, tool_call_id=f.tool_call_id, user_id=f.user_id, rating=f.rating,
                    comment=_text(level, f.comment), created_at=f.created_at,
                    source="human" if f.source == "human" else "agent", client=f.client,
                )
                for f in fbs
            ]
            rel_seq = s.scalar(select(Release.seq).where(Release.id == sess.release_id)) \
                if sess.release_id else None
            return SessionDetail(
                **head.model_dump(), redaction=level, release_seq=rel_seq, timeline=timeline,
                signals=signals, feedback=feedback,
            )

    @r.post("/v1/sessions/{session_id}/sensitive", response_model=SessionRowOut)
    def set_sensitive(session_id: uuid.UUID, req: SensitiveRequest, who: Adm) -> SessionRowOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            sess = s.get(SessionRow, session_id)
            if sess is None:
                raise HTTPException(404, "no such session")
            sess.sensitive = req.sensitive
            s.flush()
            audit(s, who, "session.sensitive",
                  {"session_id": session_id, "sensitive": req.sensitive})
            _, listed = rows(s, who, [and_(SessionRow.id == session_id)], 1, 0)
            return listed[0]

    return r
