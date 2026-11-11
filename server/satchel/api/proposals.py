"""Proposals: items in state ``proposed`` created by the agent's ``propose_context_change``.

Accepting turns a ``create_item`` proposal into a draft (optionally under a new key) or applies
an ``update_item`` proposal to its target as a new draft version. Rejecting needs a reason.
Resolved proposals stay in the library as ``deprecated`` with the decision in the version's
provenance and the audit log. Nothing is published here (design: nothing auto-publishes).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from satchel.api.auth import Auth, Caller
from satchel.api.common import audit, diff_bodies, get_item, require_ui_managed
from satchel.api.schemas import (
    AcceptProposalRequest,
    ProposalList,
    ProposalOut,
    ProposalResult,
    RejectProposalRequest,
)
from satchel.curation.drafts import ItemValidationError, add_version, ensure_chunks, validate_item
from satchel.curation.redact import truncate
from satchel.store.db import workspace_session
from satchel.store.models import AppUser, ContextItem, ContextVersion
from satchel.store.models import Session as SessionRow


def _uuid(v: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v))
    except ValueError:
        return None


def _proposal(s: Session, who: Caller, item: ContextItem) -> ProposalOut:
    ver = s.get(ContextVersion, item.current_version_id) if item.current_version_id else None
    assert ver is not None
    prov: dict[str, Any] = ver.provenance
    target_key = prov.get("target")
    target = (
        s.scalars(select(ContextItem).where(ContextItem.key == target_key)).first()
        if target_key else None
    )
    diff = None
    if target is not None and target.current_version_id:
        tv = s.get(ContextVersion, target.current_version_id)
        if tv is not None:
            diff = diff_bodies(
                target.key, tv.body, ver.body, f"{target.key}@v{tv.seq}", f"{target.key}@proposed",
                from_seq=tv.seq, to_seq=None, meta_changed=tv.meta != ver.meta,
            )
    sid = _uuid(prov.get("ref"))
    sess = s.get(SessionRow, sid) if sid else None
    question: str | None = None
    if sess is not None and sess.question and (who.has_role("admin") or not sess.sensitive):
        question = sess.question if who.has_role("admin") else truncate(sess.question)
    user = s.get(AppUser, item.owner_user_id) if item.owner_user_id else None
    return ProposalOut(
        key=item.key, type=item.type, op=str(prov.get("op") or "create_item"),
        target=target_key, rationale=prov.get("rationale"), body=ver.body, meta=ver.meta,
        proposed_by=item.owner_user_id,
        proposed_by_label=(user.display_name or user.email) if user else None,
        session_id=sess.id if sess else None, session_question=question,
        created_at=item.created_at,
        target_managed_by=target.managed_by if target else None, diff=diff,
    )


def _resolve(ver: ContextVersion, who: Caller, decision: str, **extra: Any) -> None:
    ver.provenance = {
        **ver.provenance,
        "resolution": {
            "decision": decision, "by": str(who.user_id) if who.user_id else None, **extra,
        },
    }


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["proposals"])
    Cur = Annotated[Caller, Depends(auth.require("curator"))]
    eng, emb = auth.ctx.engine, auth.ctx.embedder

    def get_proposal(s: Session, key: str) -> ContextItem:
        item = get_item(s, key)
        if item.state != "proposed":
            raise HTTPException(409, f"{key} is not an open proposal (state {item.state})")
        return item

    @r.get("/v1/proposals", response_model=ProposalList)
    def list_proposals(who: Cur) -> ProposalList:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            items = s.scalars(
                select(ContextItem)
                .where(ContextItem.state == "proposed")
                .order_by(ContextItem.created_at.desc())
            )
            return ProposalList(proposals=[_proposal(s, who, i) for i in items])

    @r.post("/v1/proposals/{key:path}/accept", response_model=ProposalResult)
    def accept_proposal(key: str, req: AcceptProposalRequest, who: Cur) -> ProposalResult:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            prop = get_proposal(s, key)
            ver = s.get(ContextVersion, prop.current_version_id)
            assert ver is not None
            body = req.body if req.body is not None else ver.body
            meta = req.meta if req.meta is not None else ver.meta
            try:
                validate_item(prop.type, body, dict(meta))
            except ItemValidationError as e:
                raise HTTPException(422, str(e)) from None
            op = str(ver.provenance.get("op") or "create_item")
            prov = {"source": "proposal", "ref": prop.key, "session": ver.provenance.get("ref"),
                    "rationale": ver.provenance.get("rationale")}
            if op == "update_item":
                target = get_item(s, str(ver.provenance.get("target") or ""))
                require_ui_managed(target)
                if target.state in ("deprecated", "proposed"):
                    raise HTTPException(409, f"target {target.key} is {target.state}")
                new = add_version(s, target, body, meta, author=who.user_id, provenance=prov,
                                  embedder=emb)
                _resolve(ver, who, "accepted", applied_to=target.key, seq=new.seq)
                prop.state = "deprecated"
                prop.updated_at = func.now()
                audit(s, who, "proposal.accept",
                      {"proposal": prop.key, "op": op, "item": target.key, "seq": new.seq})
                return ProposalResult(proposal_key=prop.key, decision="accepted",
                                      item_key=target.key, item_seq=new.seq)
            # create_item: the proposal item itself becomes a draft.
            new_key = req.key.strip() if req.key else None
            if new_key:
                if new_key.startswith("/") or "//" in new_key:
                    raise HTTPException(422, "key must be a relative path without empty segments")
                if s.scalars(select(ContextItem.id).where(ContextItem.key == new_key)).first():
                    raise HTTPException(409, f"an item with key {new_key!r} already exists")
                prop.key = new_key
            if req.body is not None or req.meta is not None:
                add_version(s, prop, body, meta, author=who.user_id,
                            provenance={**prov, "source": "ui"}, embedder=emb)
            cur = s.get(ContextVersion, prop.current_version_id)
            assert cur is not None
            ensure_chunks(s, cur, prop.type, emb)
            _resolve(ver, who, "accepted", applied_to=prop.key)
            prop.state = "draft"
            prop.updated_at = func.now()
            audit(s, who, "proposal.accept", {"proposal": key, "op": op, "item": prop.key})
            return ProposalResult(proposal_key=key, decision="accepted", item_key=prop.key,
                                  item_seq=cur.seq)

    @r.post("/v1/proposals/{key:path}/reject", response_model=ProposalResult)
    def reject_proposal(key: str, req: RejectProposalRequest, who: Cur) -> ProposalResult:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            prop = get_proposal(s, key)
            ver = s.get(ContextVersion, prop.current_version_id)
            assert ver is not None
            _resolve(ver, who, "rejected", reason=req.reason)
            prop.state = "deprecated"
            prop.updated_at = func.now()
            audit(s, who, "proposal.reject", {"proposal": key, "reason": req.reason})
            return ProposalResult(proposal_key=key, decision="rejected", item_key=None,
                                  item_seq=None)

    return r
