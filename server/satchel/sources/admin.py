"""Sources created and edited from the UI (Sources page). Stricter than the CLI, because the
caller is a browser, not someone with a shell on the server:

- File inputs (a dbt manifest, a Cube model folder) are paths inside the source's git repository
  (``git_url``, see :mod:`satchel.sources.git`), ``s3://`` / ``gs://`` URIs read with the
  server's own cloud identity, or left empty and uploaded on each sync. Server paths are
  refused: they would let an admin read files from the server.
- ``*_env`` fields (an environment variable *name*) are refused: they could send a server
  secret to a URL the caller chose. Use the secret field instead.
- ``*_url`` fields must not point at link-local or cloud metadata addresses.
- A dbt source from git either reads a committed ``manifest`` or builds one from
  ``dbt_project_dir`` with ``dbt parse`` (:mod:`satchel.sources.dbt_build`); exactly one applies.
- Secret fields (``*token*``, ``*secret*``, ``*password*``, ``*api_key*``) are sealed with
  SATCHEL_ENCRYPTION_KEY and never returned (the API shows ``***``).
- ``sync_interval_hours`` (scheduled sync) and the push webhook need a stored location for every
  required file input. The webhook secret is generated here (:func:`set_webhook`), never taken
  from a config, and kept when the config is replaced.
"""

from __future__ import annotations

import secrets as pysecrets
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from satchel.audit import record as record_audit
from satchel.core.crypto import EncryptionUnavailable, Keyring
from satchel.data.objstore import is_object_uri
from satchel.llm.connections import LlmConnectionError, validate_base_url
from satchel.secrets import is_sealed, is_secret_key, source_aad
from satchel.sources import git
from satchel.sources.config import (
    HOST_KEYS,
    WEBHOOK_KEY,
    SourceConfigError,
    check_automatable,
    config_with_defaults,
)
from satchel.sources.registry import AdapterEntry, get_entry
from satchel.store.models import Connection, ContextItem, ContextSource

MASK = "***"


class SourceAdminError(ValueError):
    """Invalid input (the API maps it to 422)."""


def _check_git(config: dict[str, Any]) -> bool:
    """Validate the git fields; True when the source reads from git."""
    try:
        if not config.get("git_url"):
            for k in ("git_ref", "git_username"):
                if config.get(k):
                    raise SourceAdminError(f"{k} needs git_url")
            return False
        config["git_url"] = git.validate_url(str(config["git_url"]))
        config["git_ref"] = git.validate_ref(config.get("git_ref"))
        config["git_username"] = git.validate_username(config.get("git_username"))
    except git.GitSourceError as e:
        raise SourceAdminError(str(e)) from None
    return True


def _check_fields(entry: AdapterEntry, config: dict[str, Any], from_git: bool) -> None:
    files = {f.name for f in entry.info.file_inputs}
    for k, v in config.items():
        if v is None or v == "":
            continue
        if k in files:
            if not isinstance(v, str):
                raise SourceAdminError(f"{k}: give a path")
            if from_git:
                if is_object_uri(v):
                    raise SourceAdminError(
                        f"{k}: with a git repository, give a path inside it (or remove git_url "
                        "to read from a bucket)")
                try:
                    git.validate_path(v)
                except git.GitSourceError as e:
                    raise SourceAdminError(f"{k}: {e}") from None
            elif not is_object_uri(v):
                raise SourceAdminError(
                    f"{k}: give a path in the git repository, an s3:// or gs:// location, or "
                    "leave it empty and upload the file when syncing (server paths can only be "
                    "set with the CLI)")
        elif k.endswith("_env"):
            raise SourceAdminError(
                f"{k} names a server environment variable and can only be set with the CLI; "
                "use the matching secret field instead")
        elif k.endswith("_url"):
            try:
                validate_base_url(str(v))
            except LlmConnectionError as e:
                raise SourceAdminError(f"{k}: {str(e).replace('base_url', 'URL')}") from None


def _connection_id(s: Session, ws_id: uuid.UUID, name: str | None) -> uuid.UUID | None:
    if not name:
        return None
    cid = s.scalars(select(Connection.id).where(Connection.workspace_id == ws_id,
                                                Connection.name == name)).first()
    if cid is None:
        raise SourceAdminError(f"unknown connection: {name}")
    return cid


def _seal_secrets(src: ContextSource, cfg: dict[str, Any], previous: dict[str, Any],
                  keyring: Keyring | None) -> dict[str, Any]:
    """Seal new secret values; ``***`` or a missing key keeps the stored one."""
    out = dict(cfg)
    for k in {*cfg, *previous}:
        if not is_secret_key(k):
            continue
        v = cfg.get(k, MASK)
        if v == MASK:
            if k in previous:
                out[k] = previous[k]
            else:
                out.pop(k, None)
        elif v in (None, ""):
            out.pop(k, None)
        elif not is_sealed(v):
            if keyring is None:
                raise EncryptionUnavailable(
                    f"SATCHEL_ENCRYPTION_KEY is not set, so {k} cannot be stored")
            out[k] = keyring.encrypt(str(v), source_aad(src.workspace_id, src.id, k))
    return out


def _check_automation(entry: AdapterEntry, stored: dict[str, Any], has_webhook: bool) -> None:
    try:
        if stored.get("sync_interval_hours"):
            check_automatable(entry, stored, "sync_interval_hours (scheduled sync)")
        if has_webhook:
            check_automatable(entry, stored, "the push webhook (remove it first)")
    except SourceConfigError as e:
        raise SourceAdminError(str(e)) from None


def _validated(entry: AdapterEntry, config: dict[str, Any]) -> dict[str, Any]:
    if config.get(WEBHOOK_KEY) not in (None, "", MASK):
        raise SourceAdminError(f"{WEBHOOK_KEY} is generated by the server: use the source's "
                               "Push webhook (POST /v1/sources/{id}/webhook)")
    config = {k: v for k, v in config.items() if k != WEBHOOK_KEY}  # always kept as stored
    plain = {k: (None if is_secret_key(k) and v == MASK else v) for k, v in config.items()}
    from_git = _check_git(plain)
    _check_fields(entry, {k: v for k, v in plain.items() if k not in HOST_KEYS}, from_git)
    try:
        stored = config_with_defaults(entry, plain)
    except SourceConfigError as e:
        raise SourceAdminError(str(e)) from None
    # Keep a masked secret as a marker so _seal_secrets keeps the stored value; an explicit
    # null or "" removes it.
    for k, v in config.items():
        if is_secret_key(k) and v == MASK:
            stored[k] = MASK
        elif is_secret_key(k) and v in (None, ""):
            stored[k] = None
    if not from_git:
        stored["git_token"] = None  # no repository, no repository token
    elif entry.kind == "dbt" and not stored.get("manifest") and not stored.get("dbt_project_dir"):
        raise SourceAdminError(
            "with a git repository, set either the manifest path (a committed "
            "target/manifest.json) or dbt_project_dir (Satchel builds the manifest with dbt parse)")
    if stored.get("dbt_project_dir"):
        stored["dbt_project_dir"] = git.validate_path(str(stored["dbt_project_dir"]))
    return stored


def create(
    s: Session, workspace_id: uuid.UUID, actor: uuid.UUID | None, keyring: Keyring | None, *,
    kind: str, name: str, config: dict[str, Any],
) -> ContextSource:
    name = name.strip()
    if not name or len(name) > 64 or not all(c.isalnum() or c in "-_." for c in name):
        raise SourceAdminError("name: 1-64 letters, digits, dash, underscore or dot")
    try:
        entry = get_entry(kind)
    except LookupError as e:
        raise SourceAdminError(str(e)) from None
    if entry.adapter is None or not entry.available:
        raise SourceAdminError(f"the {kind} adapter is not available on this server")
    if s.scalars(select(ContextSource.id).where(ContextSource.workspace_id == workspace_id,
                                                ContextSource.name == name)).first():
        raise SourceAdminError(f"a source named {name!r} already exists")
    stored = _validated(entry, config)
    _check_automation(entry, stored, has_webhook=False)
    src = ContextSource(id=uuid.uuid4(), workspace_id=workspace_id, kind=kind, name=name,
                        config={}, connection_id=_connection_id(s, workspace_id,
                                                                stored.get("connection")))
    src.config = _seal_secrets(src, stored, {}, keyring)
    s.add(src)
    s.flush()
    record_audit(s, workspace_id, actor, "source.add",
                 {"source": name, "kind": kind, "connection": stored.get("connection")})
    return src


def update_src(
    s: Session, src: ContextSource, actor: uuid.UUID | None, keyring: Keyring | None, *,
    config: dict[str, Any],
) -> ContextSource:
    """Replace the config. Secret fields sent as ``***`` (or left out) keep their value."""
    entry = get_entry(src.kind)
    previous = dict(src.config or {})
    # A server path set with the CLI stays valid when the UI sends it back unchanged.
    files = {f.name for f in entry.info.file_inputs}
    check = {k: (None if k in files and v == previous.get(k) else v) for k, v in config.items()}
    stored = _validated(entry, check)
    for k in files:
        if k in config and config[k] == previous.get(k) and config[k]:
            stored[k] = config[k]
    _check_automation(entry, stored, has_webhook=bool(previous.get(WEBHOOK_KEY)))
    src.connection_id = _connection_id(s, src.workspace_id, stored.get("connection"))
    src.config = _seal_secrets(src, stored, previous, keyring)
    s.flush()
    changed = sorted(k for k in {*previous, *src.config}
                     if previous.get(k) != src.config.get(k))
    record_audit(s, src.workspace_id, actor, "source.update",
                 {"source": src.name, "kind": src.kind, "fields": changed})
    return src


def set_webhook(s: Session, src: ContextSource, actor: uuid.UUID | None,
                keyring: Keyring | None) -> tuple[str, bool]:
    """Generate (or rotate) the source's push-webhook secret, sealed in its config. Returns the
    plain secret (shown to the admin once) and whether an old one was replaced."""
    entry = get_entry(src.kind)
    try:
        check_automatable(entry, dict(src.config or {}), "a push webhook")
    except SourceConfigError as e:
        raise SourceAdminError(str(e)) from None
    if keyring is None:
        raise EncryptionUnavailable(
            "SATCHEL_ENCRYPTION_KEY is not set, so a webhook secret cannot be stored")
    secret = pysecrets.token_urlsafe(32)
    rotated = bool((src.config or {}).get(WEBHOOK_KEY))
    src.config = {**(src.config or {}),
                  WEBHOOK_KEY: keyring.encrypt(secret, source_aad(src.workspace_id, src.id,
                                                                   WEBHOOK_KEY))}
    s.flush()
    record_audit(s, src.workspace_id, actor,
                 "source.webhook.rotate" if rotated else "source.webhook.create",
                 {"source": src.name, "kind": src.kind})
    return secret, rotated


def remove_webhook(s: Session, src: ContextSource, actor: uuid.UUID | None) -> bool:
    """Drop the webhook secret; deliveries are refused from then on. False when there was none."""
    if not (src.config or {}).get(WEBHOOK_KEY):
        return False
    src.config = {k: v for k, v in src.config.items() if k != WEBHOOK_KEY}
    s.flush()
    record_audit(s, src.workspace_id, actor, "source.webhook.remove",
                 {"source": src.name, "kind": src.kind})
    return True


def remove(s: Session, src: ContextSource, actor: uuid.UUID | None) -> int:
    """Its items are deprecated and detached (nothing is deleted); returns how many."""
    n = 0
    for item in s.scalars(select(ContextItem).where(ContextItem.source_id == src.id)):
        item.state = "deprecated"
        item.managed_by = "ui"
        item.source_id = None
        n += 1
    s.flush()
    record_audit(s, src.workspace_id, actor, "source.remove",
                 {"source": src.name, "kind": src.kind, "items_detached": n})
    s.delete(src)
    s.flush()
    return n


def masked(config: dict[str, Any]) -> dict[str, Any]:
    """Config for responses: secrets (sealed or not) as ``***``."""
    return {k: (MASK if (is_secret_key(k) or is_sealed(v)) and v else v)
            for k, v in config.items()}
