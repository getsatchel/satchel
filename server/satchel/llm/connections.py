"""LLM connections: which model provider curation and evals call, per workspace (design §13).

Two places configure a provider:

- **Deployment** (``SATCHEL_CHAT_PROVIDER`` / ``_MODEL`` / ``_BASE_URL`` / ``_API_KEY``). When set
  it wins for every workspace and the UI shows it read-only. GitOps deployments pin it here.
- **Workspace** (``llm_connection`` rows an admin adds in Settings → Models). The API key and the
  extra header values are AES-256-GCM ciphertext (:mod:`satchel.core.crypto`), write-only over
  the API; the UI sees a hint (``…abcd``) and the header names. One row may be the default.

:func:`resolve_target` picks what a workspace uses; :func:`build_chat` turns it into a client.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from satchel.audit import record as record_audit
from satchel.core.crypto import GENERATE_HINT, EncryptionUnavailable, Keyring, secret_hint
from satchel.llm.chat import (
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_ANTHROPIC_URL,
    DEFAULT_OPENAI_URL,
    AnthropicChat,
    ChatError,
    ChatProvider,
    OpenAICompatChat,
)
from satchel.secrets import llm_aad
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import LlmConnection

log = logging.getLogger("satchel.llm.connections")

Provider = Literal["anthropic", "openai_compat"]
PROVIDERS: tuple[Provider, ...] = ("anthropic", "openai_compat")
DEPLOYMENT_NAME = "deployment"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# Headers the client sets itself or that must not be overridden.
_RESERVED_HEADERS = {
    "host", "content-length", "content-type", "transfer-encoding", "connection", "upgrade",
    "te", "trailer", "proxy-authorization", "keep-alive",
}
MAX_HEADERS = 20
MAX_SECRET = 8192
# Cloud metadata endpoints: never a model provider, always an SSRF target.
_BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}


class LlmConnectionError(ValueError):
    """Invalid input for a connection (the API maps it to 422)."""


@dataclass(frozen=True)
class ChatTarget:
    """A resolved provider: everything needed to build a client, secrets in plaintext."""

    name: str
    provider: Provider
    base_url: str | None
    model: str | None
    api_key: str | None = field(repr=False)
    extra_headers: dict[str, str] = field(default_factory=dict, repr=False)
    managed_by: Literal["deployment", "workspace"] = "workspace"
    connection_id: uuid.UUID | None = None


def _aad(ws_id: uuid.UUID, conn_id: uuid.UUID, column: str) -> str:
    return llm_aad(ws_id, conn_id, column)


# --- resolution -----------------------------------------------------------------------------


def deployment_target(st: Settings) -> ChatTarget | None:
    """The ``SATCHEL_CHAT_*`` provider, or ``None`` when ``SATCHEL_CHAT_PROVIDER`` is unset."""
    if st.chat_provider is None:
        return None
    key = st.chat_api_key
    if key is None:
        if st.chat_provider == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY")
        elif (st.chat_base_url or DEFAULT_OPENAI_URL) == DEFAULT_OPENAI_URL:
            key = os.environ.get("OPENAI_API_KEY")
    return ChatTarget(
        name=DEPLOYMENT_NAME, provider=st.chat_provider, base_url=st.chat_base_url,
        model=st.chat_model, api_key=key, managed_by="deployment",
    )


def target_of(row: LlmConnection, keyring: Keyring | None) -> ChatTarget:
    """Decrypt a row. ``keyring`` may be ``None`` only when the row holds no secrets."""
    key: str | None = None
    headers: dict[str, str] = {}
    if row.api_key_enc or row.extra_headers_enc:
        if keyring is None:
            raise LlmConnectionError(
                f"LLM connection {row.name!r} has stored secrets but SATCHEL_ENCRYPTION_KEY is "
                "not set"
            )
        if row.api_key_enc:
            key = keyring.decrypt(row.api_key_enc, _aad(row.workspace_id, row.id, "api_key"))
        if row.extra_headers_enc:
            headers = json.loads(keyring.decrypt(
                row.extra_headers_enc, _aad(row.workspace_id, row.id, "extra_headers")))
    return ChatTarget(
        name=row.name, provider=row.provider, base_url=row.base_url,  # type: ignore[arg-type]
        model=row.default_model, api_key=key, extra_headers=headers, connection_id=row.id,
    )


def _keyring_or_none(st: Settings) -> Keyring | None:
    try:
        return Keyring.from_settings(st)
    except EncryptionUnavailable:
        return None


def resolve_target(
    s: Session, workspace_id: uuid.UUID, st: Settings, provider: str | None = None,
) -> ChatTarget | None:
    """What ``workspace_id`` uses: the deployment provider when configured, else the workspace
    default connection. With ``provider`` (an eval suite pinned to one), the connection must be
    of that provider: the default if it matches, else the first such connection by name."""
    dep = deployment_target(st)
    if dep is not None and (provider is None or dep.provider == provider):
        return dep
    rows = list(s.scalars(
        select(LlmConnection).where(LlmConnection.workspace_id == workspace_id)
        .order_by(LlmConnection.is_default.desc(), LlmConnection.name)
    ))
    if provider is None:
        rows = [r for r in rows if r.is_default]
    else:
        rows = [r for r in rows if r.provider == provider]
    if not rows:
        return None
    return target_of(rows[0], _keyring_or_none(st))


def build_chat(
    target: ChatTarget, st: Settings, model: str | None = None, *,
    timeout_s: float | None = None, max_retries: int | None = None,
) -> ChatProvider | None:
    """A client for ``target`` (``model`` overrides its default), or ``None`` with a warning when
    it cannot work (no key for Anthropic / api.openai.com, no model for OpenAI-compatible)."""
    t = st.chat_timeout_s if timeout_s is None else timeout_s
    r = st.chat_max_retries if max_retries is None else max_retries
    where = f"LLM connection {target.name!r}"
    if target.provider == "anthropic":
        if not target.api_key and not target.base_url:
            log.warning("%s (anthropic) has no API key", where)
            return None
        return AnthropicChat(
            target.api_key or "", model or target.model or DEFAULT_ANTHROPIC_MODEL,
            target.base_url or DEFAULT_ANTHROPIC_URL, timeout_s=t, max_retries=r,
            extra_headers=target.extra_headers,
        )
    name = model or target.model
    if not name:
        log.warning("%s (openai_compat) has no model", where)
        return None
    base = target.base_url or DEFAULT_OPENAI_URL
    if base == DEFAULT_OPENAI_URL and not target.api_key:
        log.warning("%s targets api.openai.com without an API key", where)
        return None
    return OpenAICompatChat(name, base, target.api_key, timeout_s=t, max_retries=r,
                            extra_headers=target.extra_headers)


def chat_for_workspace(
    workspace_id: uuid.UUID, st: Settings, engine: Engine | None = None,
) -> ChatProvider | None:
    """The workspace's chat client for server-internal jobs (curation). Never raises: a broken
    connection (undecryptable, invalid) logs and returns ``None`` so the job degrades."""
    try:
        with admin_session(engine=engine) as s:
            target = resolve_target(s, workspace_id, st)
    except Exception as e:  # noqa: BLE001 - degrade, never fail the curation pass
        log.error("LLM connection for workspace %s is unusable: %s", workspace_id, e)
        return None
    return build_chat(target, st) if target else None


# --- validation -----------------------------------------------------------------------------


def validate_name(name: str) -> str:
    name = name.strip()
    if not _NAME.match(name):
        raise LlmConnectionError(
            "name: 1-64 characters, letters, digits, space, dot, dash or underscore, starting "
            "with a letter or digit"
        )
    if name.lower() == DEPLOYMENT_NAME:
        raise LlmConnectionError(f"name {name!r} is reserved for the deployment provider")
    return name


def validate_base_url(url: str | None) -> str | None:
    if url is None or not url.strip():
        return None
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LlmConnectionError("base_url must be an http(s) URL with a host")
    if parts.username or parts.password:
        raise LlmConnectionError("base_url must not embed credentials; use the API key field")
    host = parts.hostname.lower()
    if host in _BLOCKED_HOSTS:
        raise LlmConnectionError(f"base_url host {host} is a cloud metadata endpoint")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or str(ip) == "fd00:ec2::254":
        raise LlmConnectionError(f"base_url host {host} is link-local / a metadata endpoint")
    return url


def validate_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    if len(headers) > MAX_HEADERS:
        raise LlmConnectionError(f"at most {MAX_HEADERS} extra headers")
    out: dict[str, str] = {}
    for k, v in headers.items():
        if not _HEADER.match(k):
            raise LlmConnectionError(f"header name {k!r}: letters, digits and dashes only")
        if k.lower() in _RESERVED_HEADERS:
            raise LlmConnectionError(f"header {k} is set by satchel and cannot be overridden")
        if any(c in v for c in "\r\n") or len(v) > MAX_SECRET:
            raise LlmConnectionError(f"header {k}: invalid value")
        out[k] = v
    return out


def _check_usable(provider: str, base_url: str | None, model: str | None,
                  has_key: bool) -> None:
    if provider not in PROVIDERS:
        raise LlmConnectionError(f"provider must be one of {', '.join(PROVIDERS)}")
    if provider == "anthropic" and not has_key and not base_url:
        raise LlmConnectionError("an Anthropic connection needs an API key")
    if provider == "openai_compat":
        if not model:
            raise LlmConnectionError("an OpenAI-compatible connection needs a default model")
        if (base_url or DEFAULT_OPENAI_URL) == DEFAULT_OPENAI_URL and not has_key:
            raise LlmConnectionError("api.openai.com needs an API key")


# --- writes (admin) -------------------------------------------------------------------------

_UNSET: Any = object()


def _need(keyring: Keyring | None) -> Keyring:
    if keyring is None:
        raise EncryptionUnavailable(
            f"SATCHEL_ENCRYPTION_KEY is not set, so secrets cannot be stored; {GENERATE_HINT}")
    return keyring


def _seal(row: LlmConnection, keyring: Keyring | None, api_key: Any,
          headers: Any) -> list[str]:
    """Apply secret changes (``_UNSET`` = keep); returns the changed field names (never values)
    for the audit. Only storing a non-empty secret needs the key."""
    changed: list[str] = []
    if api_key is not _UNSET:
        if api_key:
            if len(api_key) > MAX_SECRET:
                raise LlmConnectionError("api_key is too long")
            row.api_key_enc = _need(keyring).encrypt(
                api_key, _aad(row.workspace_id, row.id, "api_key"))
            row.api_key_hint = secret_hint(api_key)
        else:
            row.api_key_enc = row.api_key_hint = None
        changed.append("api_key")
    if headers is not _UNSET:
        clean = validate_headers(headers)
        row.extra_headers_enc = _need(keyring).encrypt(
            json.dumps(clean), _aad(row.workspace_id, row.id, "extra_headers")) if clean else None
        row.extra_header_names = sorted(clean)
        changed.append("extra_headers")
    return changed


def _make_default(s: Session, row: LlmConnection) -> None:
    s.execute(update(LlmConnection).where(
        LlmConnection.workspace_id == row.workspace_id, LlmConnection.id != row.id,
        LlmConnection.is_default.is_(True)).values(is_default=False))
    s.flush()
    row.is_default = True


def create_connection(
    s: Session, workspace_id: uuid.UUID, actor: uuid.UUID | None, keyring: Keyring | None, *,
    name: str, provider: str, base_url: str | None = None, default_model: str | None = None,
    api_key: str | None = None, extra_headers: dict[str, str] | None = None,
    is_default: bool | None = None,
) -> LlmConnection:
    name = validate_name(name)
    base = validate_base_url(base_url)
    model = (default_model or "").strip() or None
    _check_usable(provider, base, model, bool(api_key))
    if s.scalars(select(LlmConnection.id).where(
            LlmConnection.workspace_id == workspace_id, LlmConnection.name == name)).first():
        raise LlmConnectionError(f"an LLM connection named {name!r} already exists")
    first = s.scalars(select(LlmConnection.id).where(
        LlmConnection.workspace_id == workspace_id)).first() is None
    row = LlmConnection(id=uuid.uuid4(), workspace_id=workspace_id, name=name, provider=provider,
                        base_url=base, default_model=model, created_by=actor,
                        extra_header_names=[])
    _seal(row, keyring, api_key or None, extra_headers or {})
    s.add(row)
    s.flush()
    if is_default or (is_default is None and first):
        _make_default(s, row)
    s.flush()
    record_audit(s, workspace_id, actor, "llm_connection.create", {
        "connection": name, "provider": provider, "base_url": base, "model": model,
        "default": row.is_default, "secrets": [f for f in ("api_key", "extra_headers")
                                                if (f == "api_key" and api_key)
                                                or (f == "extra_headers" and extra_headers)],
    })
    return row


def update_connection(
    s: Session, row: LlmConnection, actor: uuid.UUID | None, keyring: Keyring | None, *,
    name: Any = _UNSET, base_url: Any = _UNSET, default_model: Any = _UNSET,
    api_key: Any = _UNSET, extra_headers: Any = _UNSET, is_default: Any = _UNSET,
) -> LlmConnection:
    """Patch semantics: an argument left out keeps the stored value; ``api_key=None`` (or "")
    clears the key; ``extra_headers`` replaces the whole map."""
    changed: list[str] = []
    old_name = row.name
    if name is not _UNSET and name != row.name:
        new = validate_name(name)
        if s.scalars(select(LlmConnection.id).where(
                LlmConnection.workspace_id == row.workspace_id, LlmConnection.name == new,
                LlmConnection.id != row.id)).first():
            raise LlmConnectionError(f"an LLM connection named {new!r} already exists")
        row.name = new
        changed.append("name")
    if base_url is not _UNSET:
        row.base_url = validate_base_url(base_url)
        changed.append("base_url")
    if default_model is not _UNSET:
        row.default_model = (default_model or "").strip() or None
        changed.append("default_model")
    changed += _seal(row, keyring, _UNSET if api_key is _UNSET else (api_key or None),
                     extra_headers)
    _check_usable(row.provider, row.base_url, row.default_model, row.api_key_enc is not None)
    if is_default is not _UNSET:
        if is_default and not row.is_default:
            _make_default(s, row)
            changed.append("default")
        elif not is_default and row.is_default:
            row.is_default = False
            changed.append("default")
    if not changed:
        return row
    row.updated_at = datetime.now(UTC)
    s.flush()
    record_audit(s, row.workspace_id, actor, "llm_connection.update",
                 {"connection": old_name, "renamed_to": row.name if row.name != old_name
                  else None, "fields": changed})
    return row


def delete_connection(s: Session, row: LlmConnection, actor: uuid.UUID | None) -> None:
    record_audit(s, row.workspace_id, actor, "llm_connection.delete",
                 {"connection": row.name, "was_default": row.is_default})
    s.delete(row)
    s.flush()


# --- test ------------------------------------------------------------------------

TEST_SCHEMA = {
    "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
}


@dataclass
class TestResult:
    ok: bool
    model_id: str | None
    latency_ms: int | None
    error: str | None = None


def test_target(target: ChatTarget, st: Settings, model: str | None = None) -> TestResult:
    """One tiny structured call, no retries, 20 s timeout. Secrets are scrubbed from errors."""
    chat = build_chat(target, st, model, timeout_s=20.0, max_retries=0)
    if chat is None:
        return TestResult(False, None, None, "the connection is incomplete (key or model)")
    t0 = time.monotonic()
    try:
        chat.complete_json("Reply with ok=true.", [{"role": "user", "content": "ping"}],
                           TEST_SCHEMA, max_tokens=64)
    except ChatError as e:
        msg = str(e)
        for secret in (target.api_key, *target.extra_headers.values()):
            if secret and len(secret) >= 4:
                msg = msg.replace(secret, "***")
        return TestResult(False, chat.model_id, int((time.monotonic() - t0) * 1000), msg)
    return TestResult(True, chat.model_id, int((time.monotonic() - t0) * 1000))
