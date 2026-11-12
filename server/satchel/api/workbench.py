"""Workbench: draft items, changes, previews, publish, releases and rollback (design §7.3, §8.1).

State model: an item with ``current_version_id != published_version_id`` has an unpublished
change. ``deprecate`` flags the item and takes effect (the item leaves the published release)
when the next release is published. Items with ``managed_by='git'`` are read-only here.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from satchel.api.auth import Auth, Caller
from satchel.api.common import (
    audit,
    get_item,
    has_changes,
    make_diff,
    require_ui_managed,
)
from satchel.api.eval_schemas import PublishRequest
from satchel.api.library import item_detail
from satchel.api.schemas import (
    ChangeOut,
    ChangesOut,
    CreateItemRequest,
    ItemDetail,
    PackEntryOut,
    PackOut,
    PackRequest,
    PreviewOut,
    PreviewRequest,
    ReleaseOut,
    ReleasePage,
    SearchHitOut,
    SearchRequest,
    UpdateItemRequest,
    UpdateOverlayRequest,
)
from satchel.curation.drafts import (
    ItemValidationError,
    add_version,
    content_hash,
    ensure_chunks,
    normalized_meta,
    validate_item,
)
from satchel.curation.releases import (
    create_release,
    latest_published,
    next_seq,
    release_map,
)
from satchel.evals.gate import GateState, evaluate_gate
from satchel.retrieval import build_context_pack, search_context
from satchel.store.db import workspace_session
from satchel.store.models import (
    Asset,
    Connection,
    ContextItem,
    ContextVersion,
    Domain,
    Release,
    ReleaseItem,
)

Change = tuple[ContextItem, str, ContextVersion | None, ContextVersion | None]


def compute_changes(s: Session) -> list[Change]:
    """Unpublished changes: (item, kind, current version, published version)."""
    items = s.scalars(
        select(ContextItem)
        .where(
            ContextItem.state.in_(["draft", "published", "deprecated"]),
            or_(
                ContextItem.current_version_id.is_distinct_from(ContextItem.published_version_id),
                and_(
                    ContextItem.state == "deprecated",
                    ContextItem.published_version_id.is_not(None),
                ),
            ),
        )
        .order_by(ContextItem.key)
    )
    out: list[Change] = []
    for i in items:
        if not has_changes(i):
            continue
        cur = s.get(ContextVersion, i.current_version_id) if i.current_version_id else None
        pub = s.get(ContextVersion, i.published_version_id) if i.published_version_id else None
        kind = "deprecate" if i.state == "deprecated" else ("create" if pub is None else "update")
        out.append((i, kind, cur, pub))
    return out


def _release_out(s: Session, rel: Release, current_id: uuid.UUID | None) -> ReleaseOut:
    n = s.scalar(
        select(func.count()).select_from(ReleaseItem).where(ReleaseItem.release_id == rel.id)
    )
    return ReleaseOut(
        id=rel.id, seq=rel.seq, kind=rel.kind, note=rel.note, created_by=rel.created_by,
        created_at=rel.created_at, parent_release_id=rel.parent_release_id,
        item_count=n or 0, is_current=rel.id == current_id, ref=rel.ref,
    )


def _keys(s: Session, ids: list[uuid.UUID]) -> list[str]:
    if not ids:
        return []
    return sorted(s.scalars(select(ContextItem.key).where(ContextItem.id.in_(ids))))


def _preview_out(s: Session, rel: Release) -> PreviewOut:
    parent = release_map(s, rel.parent_release_id)
    mine = release_map(s, rel.id)
    changed = [i for i, v in mine.items() if parent.get(i) != v]
    removed = [i for i in parent if i not in mine]
    cur = latest_published(s, rel.workspace_id)
    base = _release_out(s, rel, cur.id if cur else None)
    return PreviewOut(**base.model_dump(), changed_keys=_keys(s, changed),
                      removed_keys=_keys(s, removed))


def _get_release(s: Session, release_id: uuid.UUID) -> Release:
    rel = s.get(Release, release_id)
    if rel is None:
        raise HTTPException(404, f"no such release: {release_id}")
    return rel


def _check_scope(s: Session, body: CreateItemRequest) -> None:
    if body.scope_kind == "workspace":
        if body.scope_id is not None:
            raise HTTPException(422, "scope_id must be omitted for workspace scope")
        return
    model = {"domain": Domain, "connection": Connection, "asset": Asset}[body.scope_kind]
    if body.scope_id is None or s.get(model, body.scope_id) is None:
        raise HTTPException(422, f"scope_id must be an existing {body.scope_kind} id")


def _validate(item_type: str, body: str, meta: dict[str, object]) -> None:
    try:
        validate_item(item_type, body, dict(meta))
    except ItemValidationError as e:
        raise HTTPException(422, str(e)) from None


def _gate_message(g: GateState) -> str:
    """One line for the 409: why the eval gate blocks publishing."""
    bits: list[str] = []
    for sg in g.suites:
        if sg.regressions:
            ms = ", ".join(
                f"{m['measure']} {m['a']:.0%} -> "
                f"{'n/a' if m['b'] is None else format(m['b'], '.0%')}"
                for m in sg.regressions
            )
            bits.append(f"{sg.suite_key}: {ms}")
        elif sg.status != "completed":
            bits.append(f"{sg.suite_key}: {sg.message or sg.status}")
    prefix = "eval gate " + ("is running" if g.status == "running" else
                              "has no results" if g.status == "pending" else "blocked publish")
    return f"{prefix} ({'; '.join(bits)}). Run evals on the preview, or ask an admin to override."


def apply_rollback_pointers(
    s: Session, workspace_id: uuid.UUID, mapping: dict[uuid.UUID, uuid.UUID]
) -> None:
    """Point every item at its version in ``mapping``; items absent from it are unpublished
    (back to draft, keeping their current version as a pending change)."""
    for it in s.scalars(select(ContextItem).where(ContextItem.workspace_id == workspace_id)):
        vid = mapping.get(it.id)
        if vid is not None:
            it.published_version_id = vid
            if it.state in ("draft", "deprecated"):
                it.state = "published"
        elif it.published_version_id is not None:
            it.published_version_id = None
            if it.state == "published":
                it.state = "draft"
    s.flush()


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["workbench"])
    Cur = Annotated[Caller, Depends(auth.require("curator"))]
    eng, emb = auth.ctx.engine, auth.ctx.embedder

    # ---- drafts ----------------------------------------------------------------------------

    @r.post("/v1/items", response_model=ItemDetail, status_code=201)
    def create_item(body: CreateItemRequest, who: Cur) -> ItemDetail:
        key = body.key.strip()
        if key.startswith("/") or "//" in key or not key:
            raise HTTPException(422, "key must be a relative path without empty segments")
        _validate(body.type, body.body, body.meta)
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            if s.scalars(select(ContextItem.id).where(ContextItem.key == key)).first():
                raise HTTPException(409, f"an item with key {key!r} already exists")
            _check_scope(s, body)
            item = ContextItem(
                workspace_id=who.workspace_id, type=body.type, key=key,
                scope_kind=body.scope_kind, scope_id=body.scope_id, state="draft",
                managed_by="ui", owner_user_id=who.user_id,
            )
            s.add(item)
            s.flush()
            add_version(
                s, item, body.body, body.meta, author=who.user_id,
                provenance={"source": "ui", "ref": str(who.user_id) if who.user_id else None},
                embedder=emb,
            )
            audit(s, who, "item.create", {"key": key, "type": body.type})
            s.refresh(item)
            return item_detail(s, item)

    @r.put("/v1/items/{key:path}/overlay", response_model=ItemDetail)
    def update_overlay(key: str, body: UpdateOverlayRequest, who: Cur) -> ItemDetail:
        """Edit the analyst overlay of a source-managed item. The source block is carried over
        untouched; the new version is a draft that goes through preview and publish."""
        from pydantic import ValidationError

        from satchel.sources.items import clean_overlay, rebuild

        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            item = get_item(s, key)
            if item.managed_by != "source":
                raise HTTPException(409, f"{key} is not synced from a source; edit it directly")
            if item.state == "deprecated":
                raise HTTPException(409, f"{key} is deprecated and cannot be edited")
            cur = s.get(ContextVersion, item.current_version_id) if item.current_version_id \
                else None
            if cur is None or not isinstance(cur.meta.get("source"), dict):
                raise HTTPException(409, f"{key} has no source block to build on")
            try:
                overlay = clean_overlay(item.type, body.overlay)
            except ValidationError as e:
                raise HTTPException(422, str(e)) from None
            new_body, new_meta = rebuild(
                item.type, cur.meta["source"], overlay, cur.meta.get("connection")
            )
            new_hash = content_hash(new_body, normalized_meta(item.type, new_body, new_meta))
            if cur.content_hash != new_hash:
                add_version(
                    s, item, new_body, new_meta, author=who.user_id,
                    provenance={"source": "ui", "ref": str(who.user_id) if who.user_id else None,
                                "overlay": True},
                    embedder=emb,
                )
                audit(s, who, "item.overlay", {"key": key})
                s.refresh(item)
            return item_detail(s, item)

    @r.put("/v1/items/{key:path}", response_model=ItemDetail)
    def update_item(key: str, body: UpdateItemRequest, who: Cur) -> ItemDetail:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            item = get_item(s, key)
            require_ui_managed(item)
            if item.state == "deprecated":
                raise HTTPException(409, f"{key} is deprecated and cannot be edited")
            if item.state == "proposed":
                raise HTTPException(
                    409, f"{key} is an agent proposal; accept or reject it from /v1/proposals"
                )
            _validate(item.type, body.body, body.meta)
            enriched = normalized_meta(item.type, body.body, body.meta)
            cur = (
                s.get(ContextVersion, item.current_version_id) if item.current_version_id else None
            )
            if cur is None or cur.content_hash != content_hash(body.body, enriched):
                add_version(
                    s, item, body.body, body.meta, author=who.user_id,
                    provenance={"source": "ui", "ref": str(who.user_id) if who.user_id else None},
                    embedder=emb,
                )
                audit(s, who, "item.update", {"key": key})
                s.refresh(item)
            return item_detail(s, item)

    @r.post("/v1/items/{key:path}/deprecate", response_model=ItemDetail)
    def deprecate_item(key: str, who: Cur) -> ItemDetail:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            item = get_item(s, key)
            require_ui_managed(item)
            if item.state == "deprecated":
                raise HTTPException(409, f"{key} is already deprecated")
            prior = item.state
            item.state = "deprecated"
            item.updated_at = func.now()
            audit(s, who, "item.deprecate", {"key": key, "prior_state": prior})
            s.flush()
            s.refresh(item)
            return item_detail(s, item)

    # ---- changes / previews ----------------------------------------------------------------

    @r.get("/v1/changes", response_model=ChangesOut)
    def list_changes(who: Cur) -> ChangesOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            return ChangesOut(
                changes=[
                    ChangeOut(
                        key=i.key, type=i.type, kind=kind,
                        managed_by=i.managed_by,
                        from_seq=pub.seq if pub else None,
                        to_seq=None if kind == "deprecate" else (cur.seq if cur else None),
                        diff=make_diff(i.key, pub, None if kind == "deprecate" else cur),
                    )
                    for i, kind, cur, pub in compute_changes(s)
                ]
            )

    @r.post("/v1/previews", response_model=PreviewOut, status_code=201)
    def create_preview(body: PreviewRequest, who: Cur) -> PreviewOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            changes = compute_changes(s)
            if body.keys is not None:
                unknown = sorted(set(body.keys) - {c[0].key for c in changes})
                if unknown:
                    raise HTTPException(
                        422, f"no unpublished change for: {', '.join(unknown)}"
                    )
                changes = [c for c in changes if c[0].key in set(body.keys)]
            if not changes:
                raise HTTPException(422, "there are no draft changes to preview")
            base = latest_published(s, who.workspace_id)
            mapping = dict(release_map(s, base.id if base else None))
            for item, kind, cur, _ in changes:
                if kind == "deprecate":
                    mapping.pop(item.id, None)
                elif cur is not None:
                    ensure_chunks(s, cur, item.type, emb)
                    mapping[item.id] = cur.id
            rel = create_release(
                s, who.workspace_id, kind="preview", mapping=mapping,
                parent_id=base.id if base else None, note=body.note, created_by=who.user_id,
            )
            return _preview_out(s, rel)

    @r.get("/v1/previews/{release_id}", response_model=PreviewOut)
    def get_preview(release_id: uuid.UUID, who: Cur) -> PreviewOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            return _preview_out(s, _get_release(s, release_id))

    @r.post("/v1/previews/{release_id}/publish", response_model=ReleaseOut)
    def publish_preview(
        release_id: uuid.UUID, who: Cur, body: PublishRequest | None = None
    ) -> ReleaseOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            rel = _get_release(s, release_id)
            if rel.kind != "preview":
                raise HTTPException(409, "this release is already published")
            if rel.ref is not None:
                raise HTTPException(
                    409, f"this preview was built from {rel.ref}; it is published when the pull "
                    "request merges (the GitHub Action imports the default branch)")
            head = latest_published(s, who.workspace_id)
            if rel.parent_release_id != (head.id if head else None):
                raise HTTPException(
                    409,
                    "another release was published since this preview was created; "
                    "create a new preview",
                )
            gate = evaluate_gate(s, who.workspace_id, rel)
            override = (body.override_reason or "").strip() if body else ""
            if gate.status not in ("none", "pass"):
                if not override:
                    raise HTTPException(409, _gate_message(gate))
                if not who.has_role("admin"):
                    raise HTTPException(403, "only an admin can override the eval gate")
                audit(
                    s, who, "release.publish_override",
                    {"preview_id": rel.id, "reason": override, "gate": gate.status,
                     "details": _gate_message(gate)},
                )
            elif override:
                raise HTTPException(422, "override_reason given but the eval gate is not blocking")
            parent = release_map(s, rel.parent_release_id)
            mine = release_map(s, rel.id)
            rel.seq = next_seq(s, who.workspace_id, "published")
            rel.kind = "published"
            s.flush()
            items = {
                i.id: i
                for i in s.scalars(
                    select(ContextItem).where(
                        ContextItem.id.in_([*mine.keys(), *parent.keys()])
                    )
                )
            }
            changed: list[uuid.UUID] = []
            for item_id, vid in mine.items():
                if parent.get(item_id) != vid and item_id in items:
                    it = items[item_id]
                    it.published_version_id = vid
                    if it.state == "draft":
                        it.state = "published"
                    changed.append(item_id)
            removed = [i for i in parent if i not in mine and i in items]
            for item_id in removed:
                items[item_id].published_version_id = None
            s.flush()
            audit(
                s, who, "release.publish",
                {"release_id": rel.id, "seq": rel.seq, "changed": _keys(s, changed),
                 "removed": _keys(s, removed)},
            )
            return _release_out(s, rel, rel.id)

    # ---- releases ----------------------------------------------------------------------------

    @r.get("/v1/releases", response_model=ReleasePage)
    def list_releases(
        who: Cur,
        kind: Annotated[str, Query(pattern="^(published|preview)$")] = "published",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ReleasePage:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            head = latest_published(s, who.workspace_id)
            total = s.scalar(
                select(func.count()).select_from(Release).where(Release.kind == kind)
            ) or 0
            rels = s.scalars(
                select(Release)
                .where(Release.kind == kind)
                .order_by(Release.seq.desc())
                .limit(limit)
                .offset(offset)
            )
            return ReleasePage(
                total=total, limit=limit, offset=offset,
                releases=[_release_out(s, x, head.id if head else None) for x in rels],
            )

    @r.post("/v1/releases/{release_id}/rollback", response_model=ReleaseOut, status_code=201)
    def rollback_release(release_id: uuid.UUID, who: Cur) -> ReleaseOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            target = _get_release(s, release_id)
            if target.kind != "published":
                raise HTTPException(409, "only published releases can be rolled back to")
            head = latest_published(s, who.workspace_id)
            assert head is not None
            if head.id == target.id:
                raise HTTPException(409, "this is already the current release")
            mapping = release_map(s, target.id)
            rel = create_release(
                s, who.workspace_id, kind="published", mapping=mapping, parent_id=head.id,
                note=f"rollback to release #{target.seq}", created_by=who.user_id,
            )
            apply_rollback_pointers(s, who.workspace_id, mapping)
            audit(
                s, who, "release.rollback",
                {"release_id": rel.id, "seq": rel.seq, "to_release_id": target.id,
                 "to_seq": target.seq, "from_seq": head.seq},
            )
            return _release_out(s, rel, rel.id)

    # ---- test a draft: retrieval against a preview ---------------------------------------------

    @r.post("/v1/previews/{release_id}/search", response_model=list[SearchHitOut])
    def preview_search(release_id: uuid.UUID, req: SearchRequest, who: Cur) -> list[SearchHitOut]:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            _get_release(s, release_id)
            hits = search_context(
                s, req.query, types=req.types, k=req.k, embedder=emb, release_id=release_id
            )
            return [
                SearchHitOut(
                    key=h.key, type=h.type, seq=h.seq, tag=h.tag, score=h.score,
                    sources=h.sources, snippet=h.snippet,
                )
                for h in hits
            ]

    @r.post("/v1/previews/{release_id}/context-pack", response_model=PackOut)
    def preview_pack(release_id: uuid.UUID, req: PackRequest, who: Cur) -> PackOut:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            _get_release(s, release_id)
            pack = build_context_pack(
                s, req.question, embedder=emb, release_id=release_id,
                budget_tokens=req.budget_tokens,
            )
            return PackOut(
                release_id=pack.release_id, budget_tokens=pack.budget_tokens,
                used_tokens=pack.used_tokens, text=pack.text,
                entries=[
                    PackEntryOut(
                        key=e.key, type=e.type, seq=e.seq, tag=e.tag, tokens=e.tokens,
                        score=e.score, text=e.text,
                    )
                    for e in pack.entries
                ],
            )

    return r
