"""Push webhooks (``POST /v1/hooks/sources/{id}``): a git host tells satchel a source changed.

Unauthenticated by design (GitHub and GitLab cannot send a satchel token); each delivery is
verified against the source's webhook secret (``satchel.sources.admin.set_webhook``):

- GitHub: ``X-Hub-Signature-256: sha256=<HMAC-SHA256 of the raw body>``.
- GitLab (or any CI): ``X-Gitlab-Token: <secret>``.

Both are compared in constant time. An unknown source, a source without a webhook and a bad
signature all get the same 401, so the endpoint does not reveal which source ids exist. Bodies
over 1 MiB are refused (413) before the source is looked up.

For a git source, a push to another branch than the one the source follows (``git_ref``, else
the repository's default branch from the payload) is acknowledged and ignored (200), as are
other events; GitHub's ``ping`` gets 200. A matching push gets 202 and the sync runs after the
response (GitHub gives up after 10 s), under the source's advisory lock, waiting up to
10 minutes for a running sync to finish; ``satchel.sources.automation.run_sync`` skips the work
when the ref still points at the last synced commit. Deliveries coalesce per source in the
process: while a webhook sync of a source is queued or waiting, further deliveries get 202
without adding another (the pending one resolves the ref when it runs, so it gets the newest
commit).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from starlette.datastructures import Headers

from satchel.api.auth import ApiContext
from satchel.core.crypto import Keyring, configured
from satchel.secrets import source_aad
from satchel.sources.automation import run_sync
from satchel.sources.config import WEBHOOK_KEY
from satchel.store.db import admin_session
from satchel.store.models import ContextSource

log = logging.getLogger("satchel.sources")

MAX_BODY = 1 << 20
WAIT_FOR_RUNNING_S = 600.0
# Sources with a webhook sync queued, waiting for the lock or running in this process.
_pending: set[uuid.UUID] = set()
_pending_lock = threading.Lock()


def claim(source_id: uuid.UUID) -> bool:
    """Mark a webhook sync of the source pending; False when one already is."""
    with _pending_lock:
        if source_id in _pending:
            return False
        _pending.add(source_id)
        return True


def release(source_id: uuid.UUID) -> None:
    with _pending_lock:
        _pending.discard(source_id)
_UNVERIFIED = "missing or invalid webhook signature"
_DUMMY_KEY = b"satchel-no-such-webhook"  # keeps the unknown-source path doing the same work
_SHA = re.compile(r"[0-9a-f]{7,40}")
_PUSH_EVENTS = {"github": {"push"}, "gitlab": {"Push Hook", "Tag Push Hook"}}


class HookOut(BaseModel):
    status: Literal["queued", "ignored", "pong"]
    reason: str | None = None


@dataclass(frozen=True)
class Target:
    id: uuid.UUID
    secret: str
    git_url: str | None
    git_ref: str | None


def verify(headers: Headers, body: bytes, secret: str | None) -> str | None:
    """The provider (``github`` / ``gitlab``) whose signature checks out, else None."""
    key = secret.encode() if secret else _DUMMY_KEY
    sig = headers.get("x-hub-signature-256")
    token = headers.get("x-gitlab-token")
    if sig is not None:
        want = b"sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest().encode()
        ok, provider = hmac.compare_digest(want, sig.strip().encode()), "github"
    elif token is not None:
        ok, provider = hmac.compare_digest(key, token.encode()), "gitlab"
    else:
        return None
    return provider if ok and secret else None


def payload_of(body: bytes, content_type: str) -> dict[str, Any]:
    """The JSON payload (``application/json``, or GitHub's form-encoded ``payload=``)."""
    try:
        raw = body.decode()
        if content_type.startswith("application/x-www-form-urlencoded"):
            raw = (parse_qs(raw).get("payload") or ["{}"])[0]
        data = json.loads(raw or "{}")
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def ignored_because(t: Target, payload: dict[str, Any]) -> str | None:
    """Why a push does not concern the source, or None to sync. Without a ref in the payload
    (a CI call, a non-git source) it always syncs; the unchanged-commit check keeps that cheap."""
    ref = payload.get("ref")
    if not t.git_url or not isinstance(ref, str) or not ref:
        return None
    if payload.get("deleted") is True or payload.get("after") == "0" * 40:
        return f"{ref} was deleted"
    want = t.git_ref
    if want:
        if _SHA.fullmatch(want):
            return f"the source is pinned to commit {want[:10]}"
        if ref == want or ref in (f"refs/heads/{want}", f"refs/tags/{want}"):
            return None
        return f"push to {ref}; the source follows {want}"
    default = next((x.get("default_branch") for x in (payload.get("repository"),
                                                       payload.get("project"))
                    if isinstance(x, dict) and isinstance(x.get("default_branch"), str)), None)
    if default:
        if ref == f"refs/heads/{default}":
            return None
        return f"push to {ref}; the source follows the default branch ({default})"
    return None if ref.startswith("refs/heads/") else f"push to {ref}; the source follows a branch"


async def _read_body(request: Request) -> bytes:
    n = request.headers.get("content-length")
    if n and n.isdigit() and int(n) > MAX_BODY:
        raise HTTPException(413, f"payload larger than {MAX_BODY >> 20} MiB")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY:
            raise HTTPException(413, f"payload larger than {MAX_BODY >> 20} MiB")
    return bytes(buf)


def make_router(ctx: ApiContext) -> APIRouter:
    r = APIRouter(tags=["hooks"])

    def lookup(source_id: str) -> Target | None:
        try:
            sid = uuid.UUID(source_id)
        except ValueError:
            return None
        with admin_session(engine=ctx.engine) as s:  # no workspace context: server-internal
            src = s.get(ContextSource, sid)
            sealed = (src.config or {}).get(WEBHOOK_KEY) if src is not None else None
            if src is None or not sealed:
                return None
            if not configured(ctx.settings):
                log.error("source %s has a webhook but SATCHEL_ENCRYPTION_KEY is not set", sid)
                return None
            try:
                secret = Keyring.from_settings(ctx.settings).decrypt(
                    sealed, source_aad(src.workspace_id, src.id, WEBHOOK_KEY))
            except Exception:  # noqa: BLE001 - a key mismatch is an operator problem; log it
                log.exception("cannot decrypt the webhook secret of source %s", sid)
                return None
            cfg = src.config
            return Target(src.id, secret, cfg.get("git_url") or None, cfg.get("git_ref") or None)

    def run(source_id: uuid.UUID, detail: dict[str, Any]) -> None:
        try:
            res = run_sync(source_id, trigger="webhook", engine=ctx.engine,
                           embedder=ctx.embedder, settings=ctx.settings, detail=detail,
                           wait_s=WAIT_FOR_RUNNING_S)
        except Exception:
            log.exception("webhook sync of source %s failed", source_id)
            return
        finally:
            release(source_id)
        log.info("webhook: %s", res.line())

    @r.post(
        "/v1/hooks/sources/{source_id}", response_model=HookOut, status_code=202,
        responses={200: {"model": HookOut, "description": "ping, or an event that is ignored"},
                   401: {"description": _UNVERIFIED}, 413: {"description": "body over 1 MiB"}},
    )
    async def source_webhook(source_id: str, request: Request, response: Response,
                             background: BackgroundTasks) -> HookOut:
        """Push-event receiver for GitHub (``X-Hub-Signature-256``) and GitLab
        (``X-Gitlab-Token``). No satchel auth: the webhook secret is the credential."""
        body = await _read_body(request)
        target = await run_in_threadpool(lookup, source_id)
        provider = verify(request.headers, body, target.secret if target else None)
        if target is None or provider is None:
            raise HTTPException(401, _UNVERIFIED)
        h = request.headers
        event = h.get("x-github-event") if provider == "github" else h.get("x-gitlab-event")
        if provider == "github" and event == "ping":
            response.status_code = 200
            return HookOut(status="pong")
        if event is not None and event not in _PUSH_EVENTS[provider]:
            response.status_code = 200
            return HookOut(status="ignored", reason=f"{event} events do not trigger a sync")
        payload = payload_of(body, h.get("content-type", ""))
        reason = ignored_because(target, payload)
        if reason:
            response.status_code = 200
            return HookOut(status="ignored", reason=reason)
        ref = payload.get("ref")
        delivery = h.get("x-github-delivery") or h.get("x-gitlab-event-uuid")
        detail = {"provider": provider, "event": (event or "push")[:64],
                  **({"ref": ref[:200]} if isinstance(ref, str) else {}),
                  **({"delivery": delivery[:100]} if delivery else {})}
        if not claim(target.id):
            return HookOut(status="queued", reason="a sync of this source is already queued; "
                           "it will pick up the newest commit")
        try:
            background.add_task(run, target.id, detail)
        except BaseException:
            release(target.id)
            raise
        return HookOut(status="queued")

    return r
