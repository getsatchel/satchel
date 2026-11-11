"""Every secret satchel stores in Postgres, in one place: where it lives, its associated data,
and key rotation (``satchel secrets rotate``) and the ``satchel doctor`` check over all of them.

Sealed values (``satchel.core.crypto``) live in:

- ``llm_connection.api_key_enc`` / ``extra_headers_enc`` (Settings → Models)
- ``connection.secret_ref`` when it starts with ``enc:`` (a warehouse password or URL entered in
  the UI; ``env:NAME`` references are still accepted from the CLI)
- ``context_source.config[<key>]`` for adapter fields that hold a secret (a Cube API token)
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from satchel.core.crypto import PREFIX, Keyring
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import Connection, ContextSource, LlmConnection

# Adapter config keys whose values are secrets (same words the API masks).
SECRET_WORDS = ("token", "secret", "password", "api_key")


def is_sealed(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def is_secret_key(key: str) -> bool:
    k = key.lower()
    return any(w in k for w in SECRET_WORDS) and not k.endswith("_env")


def connection_aad(ws_id: uuid.UUID, conn_id: uuid.UUID) -> str:
    return f"connection:{ws_id}:{conn_id}:secret"


def source_aad(ws_id: uuid.UUID, src_id: uuid.UUID, key: str) -> str:
    return f"context_source:{ws_id}:{src_id}:{key}"


def llm_aad(ws_id: uuid.UUID, conn_id: uuid.UUID, column: str) -> str:
    return f"llm_connection:{ws_id}:{conn_id}:{column}"


def unseal_source_config(src: ContextSource, keyring: Keyring | None) -> dict[str, Any]:
    """The source config with sealed values decrypted (for the adapter, never for responses)."""
    cfg = dict(src.config or {})
    for k, v in cfg.items():
        if is_sealed(v):
            if keyring is None:
                raise ValueError(f"source {src.name!r} has an encrypted {k} but "
                                 "SATCHEL_ENCRYPTION_KEY is not set")
            cfg[k] = keyring.decrypt(v, source_aad(src.workspace_id, src.id, k))
    return cfg


@dataclass
class Sealed:
    label: str
    token: str
    aad: str
    set: Callable[[str], None]


def iter_sealed(s: Session, workspace_id: uuid.UUID | None = None) -> Iterator[Sealed]:
    def scoped(model: Any) -> Any:
        q = select(model)
        return q.where(model.workspace_id == workspace_id) if workspace_id else q

    def attr_setter(obj: Any, attr: str) -> Callable[[str], None]:
        return lambda v: setattr(obj, attr, v)

    for row in s.scalars(scoped(LlmConnection).order_by(LlmConnection.name)):
        for col in ("api_key", "extra_headers"):
            tok = getattr(row, f"{col}_enc")
            if tok:
                yield Sealed(f"{row.workspace_id}/llm/{row.name}.{col}", tok,
                             llm_aad(row.workspace_id, row.id, col),
                             attr_setter(row, f"{col}_enc"))
    for conn in s.scalars(scoped(Connection).order_by(Connection.name)):
        if is_sealed(conn.secret_ref):
            yield Sealed(f"{conn.workspace_id}/connection/{conn.name}", conn.secret_ref or "",
                         connection_aad(conn.workspace_id, conn.id),
                         attr_setter(conn, "secret_ref"))
    for src in s.scalars(scoped(ContextSource).order_by(ContextSource.name)):
        for k, v in (src.config or {}).items():
            if is_sealed(v):
                def put(new: str, x: ContextSource = src, key: str = k) -> None:
                    x.config = {**x.config, key: new}
                yield Sealed(f"{src.workspace_id}/source/{src.name}.{k}", v,
                             source_aad(src.workspace_id, src.id, k), put)


@dataclass
class RotateReport:
    rotated: int = 0
    current: int = 0
    failed: list[str] = field(default_factory=list)


def rotate_secrets(st: Settings, engine: Engine | None = None, *, dry_run: bool = False,
                   workspace_id: uuid.UUID | None = None) -> RotateReport:
    """Re-encrypt every stored secret (of one workspace, or all) with the current key."""
    keyring = Keyring.from_settings(st)
    rep = RotateReport()
    with admin_session(engine=engine) as s:
        for x in list(iter_sealed(s, workspace_id)):
            if keyring.is_current(x.token):
                rep.current += 1
                continue
            try:
                plain = keyring.decrypt(x.token, x.aad)
            except Exception as e:  # noqa: BLE001 - report every value
                rep.failed.append(f"{x.label}: {e}")
                continue
            if not dry_run:
                x.set(keyring.encrypt(plain, x.aad))
            rep.rotated += 1
    return rep


def check_secrets(keyring: Keyring, engine: Engine | None = None) -> tuple[int, int, int]:
    """(ok, on a previous key, undecryptable) over every stored secret."""
    ok = stale = bad = 0
    with admin_session(engine=engine) as s:
        for x in iter_sealed(s):
            try:
                keyring.decrypt(x.token, x.aad)
            except Exception:  # noqa: BLE001
                bad += 1
                continue
            if keyring.is_current(x.token):
                ok += 1
            else:
                stale += 1
    return ok, stale, bad
