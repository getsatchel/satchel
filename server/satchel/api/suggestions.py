"""Suggestions: the curation inbox (design §7.3) and the curation trigger.

A suggestion is a cluster of signals plus proposed changes. Accepting a change creates a draft
(new version or new draft item) exactly like accepting an agent proposal; nothing publishes
here. A suggestion completes automatically once every change is decided. Evidence is redacted
by role (design §9): admins see raw values, curators the redacted view, and sensitive sessions
are metadata only for everyone but admins. All routes need the curator role, except
``POST /v1/curate/run`` (admin).
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from satchel.api.auth import Auth, Caller
from satchel.api.common import audit, diff_bodies
from satchel.api.observability import evidence_level
from satchel.api.schemas import (
    AcceptChangeRequest,
    ChangeDecision,
    CurateRunOut,
    CurateRunRequest,
    DismissRequest,
    EvidenceSession,
    EvidenceSignal,
    RejectChangeRequest,
    SignalStat,
    SuggestionChangeOut,
    SuggestionDetail,
    SuggestionList,
    SuggestionSummary,
)
from satchel.curation.apply import ApplyError, apply_change
from satchel.curation.redact import view_obj, view_text
from satchel.curation.run import run_curation
from satchel.curation.signal_text import signal_text
from satchel.store.db import workspace_session
from satchel.store.models import (
    AppUser,
    Asset,
    ContextItem,
    ContextVersion,
    Signal,
    Suggestion,
    SuggestionChange,
    SuggestionSignal,
)
from satchel.store.models import Session as SessionRow

MAX_EVIDENCE_SESSIONS = 50


def _now() -> datetime:
    return datetime.now(UTC)


def _counts(s: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
    if not ids:
        return {}
    return {
        sid: (n, int(p))
        for sid, n, p in s.execute(
            select(
                SuggestionChange.suggestion_id, func.count(),
                func.sum(case((SuggestionChange.status == "pending", 1), else_=0)),
            )
            .where(SuggestionChange.suggestion_id.in_(ids))
            .group_by(SuggestionChange.suggestion_id)
        )
    }


def _summary(sug: Suggestion, counts: tuple[int, int]) -> SuggestionSummary:
    return SuggestionSummary(
        id=sug.id, title=sug.title, summary=sug.summary, status=sug.status,
        source=sug.source,
        evidence_count=sug.evidence_count, distinct_users=sug.distinct_users,
        distinct_sessions=sug.distinct_sessions, change_count=counts[0],
        pending_changes=counts[1], drafted=sug.drafted_at is not None,
        draft_note=sug.draft_note, created_at=sug.created_at, updated_at=sug.updated_at,
    )


def _change_out(s: Session, c: SuggestionChange) -> SuggestionChangeOut:
    target = s.get(ContextItem, c.target_item_id) if c.target_item_id else None
    cur: ContextVersion | None = None
    if target is not None and target.current_version_id:
        cur = s.get(ContextVersion, target.current_version_id)
    asset = s.get(Asset, c.target_asset_id) if c.target_asset_id else None
    key = target.key if target else (c.proposed_key or "(new item)")
    proposed = "" if c.op == "deprecate_item" else c.proposed_body
    diff = diff_bodies(
        key, cur.body if cur else "", proposed,
        f"{key}@v{cur.seq}" if cur else "/dev/null",
        f"{key}@suggested" if proposed or not cur else "/dev/null",
        from_seq=cur.seq if cur else None, to_seq=None,
        meta_changed=bool(cur and c.op != "deprecate_item" and cur.meta != c.proposed_meta),
    )
    return SuggestionChangeOut(
        id=c.id, ord=c.ord, op=c.op, item_type=c.item_type or (target.type if target else None),
        target_key=target.key if target else None,
        target_managed_by=target.managed_by if target else None,
        target_asset=asset.fqn if asset else None, proposed_key=c.proposed_key,
        proposed_body=c.proposed_body, proposed_meta=c.proposed_meta,
        current_body=cur.body if cur else None, current_meta=dict(cur.meta) if cur else None,
        rationale=c.rationale, status=c.status,
        decided_by=c.decided_by, decided_at=c.decided_at, result=c.result, diff=diff,
    )


def _evidence(
    s: Session, who: Caller, sug: Suggestion
) -> tuple[list[EvidenceSession], list[SignalStat], bool]:
    sigs = list(s.scalars(
        select(Signal).join(SuggestionSignal, SuggestionSignal.signal_id == Signal.id)
        .where(SuggestionSignal.suggestion_id == sug.id).order_by(Signal.id)
    ))
    kinds: dict[str, int] = {}
    for sg in sigs:
        kinds[sg.kind] = kinds.get(sg.kind, 0) + 1
    stats = [SignalStat(kind=k, count=n)
             for k, n in sorted(kinds.items(), key=lambda t: (-t[1], t[0]))]
    sess_ids = {sg.session_id for sg in sigs if sg.session_id}
    sessions = {x.id: x for x in s.scalars(select(SessionRow).where(SessionRow.id.in_(sess_ids)))} \
        if sess_ids else {}
    labels: dict[uuid.UUID, str] = {}
    uids = {x.user_id for x in sessions.values() if x.user_id}
    if uids:
        for uid, name, email in s.execute(
            select(AppUser.id, AppUser.display_name, AppUser.email).where(AppUser.id.in_(uids))
        ):
            labels[uid] = name or email or str(uid)[:8]
    groups: dict[uuid.UUID | None, list[Signal]] = {}
    for sg in sigs:
        groups.setdefault(sg.session_id, []).append(sg)
    ordered = sorted(
        groups,
        key=lambda sid: sessions[sid].started_at if sid in sessions else datetime.min.replace(
            tzinfo=UTC),
        reverse=True,
    )
    out: list[EvidenceSession] = []
    for sid in ordered[:MAX_EVIDENCE_SESSIONS]:
        sess = sessions.get(sid) if sid else None
        level = evidence_level(who, sess.user_id if sess else None,
                               sess.sensitive if sess else False)
        signals = []
        for sg in groups[sid]:
            payload = view_obj(level, sg.payload)
            signals.append(EvidenceSignal(
                id=sg.id, kind=sg.kind, source=sg.source, created_at=sg.created_at,
                message=signal_text(sg.kind, payload) if payload is not None else None,
            ))
        out.append(EvidenceSession(
            session_id=sid, user_label=labels.get(sess.user_id) if sess and sess.user_id else None,
            client=sess.client if sess else None,
            question=view_text(level, sess.question) if sess else None,
            sensitive=sess.sensitive if sess else False, redaction=level,
            started_at=sess.started_at if sess else None, signals=signals,
        ))
    return out, stats, len(ordered) > MAX_EVIDENCE_SESSIONS


def _auto_complete(s: Session, who: Caller, sug: Suggestion) -> None:
    pending, total = s.execute(
        select(func.sum(case((SuggestionChange.status == "pending", 1), else_=0)), func.count())
        .where(SuggestionChange.suggestion_id == sug.id)
    ).one()
    if total and not pending and sug.status == "open":
        sug.status = "completed"
        sug.resolved_by, sug.resolved_at, sug.updated_at = who.user_id, _now(), _now()
        audit(s, who, "suggestion.complete", {"suggestion": sug.id, "auto": True})


def _close_pending(s: Session, who: Caller, sug: Suggestion, reason: str) -> None:
    for c in s.scalars(select(SuggestionChange).where(
        SuggestionChange.suggestion_id == sug.id, SuggestionChange.status == "pending")):
        c.status, c.decided_by, c.decided_at = "rejected", who.user_id, _now()
        c.result = {"reason": reason}


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["suggestions"])
    Cur = Annotated[Caller, Depends(auth.require("curator"))]
    Adm = Annotated[Caller, Depends(auth.require("admin"))]
    eng, emb = auth.ctx.engine, auth.ctx.embedder

    def get_open(s: Session, sid: uuid.UUID, *, must_be_open: bool = True) -> Suggestion:
        sug = s.get(Suggestion, sid)
        if sug is None:
            raise HTTPException(404, "no such suggestion")
        if must_be_open and sug.status != "open":
            raise HTTPException(409, f"this suggestion is already {sug.status}")
        return sug

    def get_change(s: Session, sug: Suggestion, cid: uuid.UUID) -> SuggestionChange:
        c = s.get(SuggestionChange, cid)
        if c is None or c.suggestion_id != sug.id:
            raise HTTPException(404, "no such change on this suggestion")
        if c.status != "pending":
            raise HTTPException(409, f"this change was already {c.status}")
        return c

    def summary_of(s: Session, sug: Suggestion) -> SuggestionSummary:
        return _summary(sug, _counts(s, [sug.id]).get(sug.id, (0, 0)))

    @r.get("/v1/suggestions", response_model=SuggestionList)
    def list_suggestions(
        who: Cur,
        status: Annotated[Literal["open", "completed", "dismissed", "all"], Query()] = "open",
        source: Annotated[Literal["signals", "hygiene", "golden"] | None, Query()] = None,
        sort: Annotated[Literal["evidence", "recent"], Query()] = "evidence",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> SuggestionList:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            conds = []
            if status != "all":
                conds.append(Suggestion.status == status)
            if source:
                conds.append(Suggestion.source == source)
            order = (
                [Suggestion.evidence_count.desc(), Suggestion.updated_at.desc()]
                if sort == "evidence" else [Suggestion.updated_at.desc()]
            )
            total = s.scalar(select(func.count()).select_from(Suggestion).where(*conds)) or 0
            rows = list(s.scalars(
                select(Suggestion).where(*conds).order_by(*order, Suggestion.id)
                .limit(limit).offset(offset)
            ))
            counts = _counts(s, [x.id for x in rows])
            return SuggestionList(
                total=total, limit=limit, offset=offset,
                suggestions=[_summary(x, counts.get(x.id, (0, 0))) for x in rows],
            )

    @r.get("/v1/suggestions/{suggestion_id}", response_model=SuggestionDetail)
    def get_suggestion(suggestion_id: uuid.UUID, who: Cur) -> SuggestionDetail:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            sug = get_open(s, suggestion_id, must_be_open=False)
            evidence, kinds, truncated = _evidence(s, who, sug)
            changes = list(s.scalars(
                select(SuggestionChange).where(SuggestionChange.suggestion_id == sug.id)
                .order_by(SuggestionChange.ord, SuggestionChange.created_at)
            ))
            return SuggestionDetail(
                **summary_of(s, sug).model_dump(), dismiss_reason=sug.dismiss_reason,
                resolved_by=sug.resolved_by, resolved_at=sug.resolved_at,
                draft_model=sug.draft_model, signal_kinds=kinds, evidence=evidence,
                evidence_truncated=truncated, changes=[_change_out(s, c) for c in changes],
            )

    @r.post("/v1/suggestions/{suggestion_id}/changes/{change_id}/accept",
            response_model=ChangeDecision)
    def accept_change(
        suggestion_id: uuid.UUID, change_id: uuid.UUID, req: AcceptChangeRequest, who: Cur
    ) -> ChangeDecision:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            sug = get_open(s, suggestion_id)
            c = get_change(s, sug, change_id)
            try:
                result = apply_change(
                    s, workspace_id=who.workspace_id, user_id=who.user_id, sug=sug, change=c,
                    embedder=emb, body=req.body, meta=req.meta, key=req.key,
                )
            except ApplyError as e:
                raise HTTPException(e.status, e.message) from None
            manual = bool(result.get("manual"))
            c.status = "applied" if manual else "accepted"
            c.decided_by, c.decided_at, c.result = who.user_id, _now(), result
            s.flush()
            audit(s, who, "suggestion.change.accept", {
                "suggestion": sug.id, "change": c.id, "op": c.op, "item": result.get("item_key"),
                "seq": result.get("seq"), "manual": manual, "edited": req.body is not None,
            })
            _auto_complete(s, who, sug)
            s.flush()
            return ChangeDecision(
                change=_change_out(s, c), suggestion=summary_of(s, sug),
                item_key=result.get("item_key"), item_seq=result.get("seq"),
                edit_in_git=manual, message=result.get("message"),
            )

    @r.post("/v1/suggestions/{suggestion_id}/changes/{change_id}/reject",
            response_model=ChangeDecision)
    def reject_change(
        suggestion_id: uuid.UUID, change_id: uuid.UUID, req: RejectChangeRequest, who: Cur
    ) -> ChangeDecision:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            sug = get_open(s, suggestion_id)
            c = get_change(s, sug, change_id)
            c.status, c.decided_by, c.decided_at = "rejected", who.user_id, _now()
            c.result = {"reason": req.reason} if req.reason else {}
            s.flush()
            audit(s, who, "suggestion.change.reject",
                  {"suggestion": sug.id, "change": c.id, "op": c.op, "reason": req.reason})
            _auto_complete(s, who, sug)
            s.flush()
            return ChangeDecision(
                change=_change_out(s, c), suggestion=summary_of(s, sug), item_key=None,
                item_seq=None, edit_in_git=False, message=None,
            )

    @r.post("/v1/suggestions/{suggestion_id}/dismiss", response_model=SuggestionSummary)
    def dismiss(suggestion_id: uuid.UUID, req: DismissRequest, who: Cur) -> SuggestionSummary:
        reason = req.reason.strip()
        if not reason:
            raise HTTPException(422, "a reason is required: it is fed back into future drafts")
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            sug = get_open(s, suggestion_id)
            _close_pending(s, who, sug, "suggestion dismissed")
            sug.status, sug.dismiss_reason = "dismissed", reason
            sug.resolved_by, sug.resolved_at, sug.updated_at = who.user_id, _now(), _now()
            s.flush()
            audit(s, who, "suggestion.dismiss", {"suggestion": sug.id, "reason": reason})
            return summary_of(s, sug)

    @r.post("/v1/suggestions/{suggestion_id}/complete", response_model=SuggestionSummary)
    def complete(suggestion_id: uuid.UUID, who: Cur) -> SuggestionSummary:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            sug = get_open(s, suggestion_id)
            _close_pending(s, who, sug, "closed without action")
            sug.status = "completed"
            sug.resolved_by, sug.resolved_at, sug.updated_at = who.user_id, _now(), _now()
            s.flush()
            audit(s, who, "suggestion.complete", {"suggestion": sug.id, "auto": False})
            return summary_of(s, sug)

    @r.post("/v1/curate/run", response_model=CurateRunOut)
    def curate_run(who: Adm, req: CurateRunRequest | None = None) -> CurateRunOut:
        use_llm = True if req is None else req.use_llm
        report = run_curation(
            who.workspace_id, engine=eng, embedder=emb, chat=auth.ctx.chat,
            settings=auth.ctx.settings, use_llm=use_llm,
        )
        if report is None:
            raise HTTPException(409, "a curation run for this workspace is already in progress")
        drafted = [d for d in report.drafts if d.status == "drafted"]
        out = CurateRunOut(
            workspace=report.workspace, llm=report.llm, embedded=report.embedded,
            hygiene=report.hygiene.emitted, hygiene_notes=report.hygiene.notes,
            golden_candidates=report.golden_candidates,
            signals_considered=report.gaps.signals_considered,
            clusters_found=report.gaps.clusters_found,
            below_threshold=report.gaps.below_threshold,
            suggestions_created=len(report.gaps.created),
            suggestions_extended=len(report.gaps.attached),
            drafted=len(drafted), changes_proposed=sum(d.changes for d in drafted),
            invalid_dropped=sum(d.dropped for d in drafted),
            draft_failed=sum(1 for d in report.drafts if d.status == "failed"),
            undrafted_left=report.undrafted_left, errors=report.errors,
        )
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            audit(s, who, "curate.run", {**out.model_dump(exclude={"errors"}), "use_llm": use_llm})
        return out

    return r

