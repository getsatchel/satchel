"""Connection records: add/update, resolve, and build a live Connector."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session as OrmSession

from satchel import audit
from satchel.core.crypto import DecryptionError, EncryptionUnavailable, Keyring
from satchel.data.connectors import Connector, make_connector, resolve_secret
from satchel.data.gateway import DIALECTS
from satchel.secrets import connection_aad, is_sealed
from satchel.settings import Settings, get_settings
from satchel.store.db import admin_session
from satchel.store.models import Connection, Workspace

KINDS = ("duckdb", "postgres", "bigquery", "snowflake")


class ConnectionError_(Exception):
    """A user-fixable connection problem (message shown to the agent)."""


def add_connection(
    workspace_slug: str,
    name: str,
    kind: str,
    config: dict[str, Any],
    *,
    secret_ref: str | None = None,
    engine: Engine | None = None,
) -> Connection:
    """Create or update a connection. ``secret_ref`` is ``env:NAME``; raw secrets are refused."""
    if kind not in KINDS:
        raise ValueError(f"unsupported kind {kind!r}; one of {', '.join(KINDS)}")
    if secret_ref is not None:
        resolve_secret_syntax(secret_ref)
    with admin_session(engine=engine) as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == workspace_slug)).first()
        if ws is None:
            raise LookupError(f"unknown workspace: {workspace_slug}")
        conn = s.scalars(
            select(Connection).where(Connection.workspace_id == ws.id, Connection.name == name)
        ).first()
        if conn is None:
            first = (s.scalar(select(func.count()).select_from(Connection).where(
                Connection.workspace_id == ws.id)) or 0) == 0
            conn = Connection(workspace_id=ws.id, name=name, kind=kind, config=config,
                              secret_ref=secret_ref, default_for_ai=first)
            s.add(conn)
            created = True
        else:
            conn.kind, conn.config = kind, config
            if secret_ref is not None:
                conn.secret_ref = secret_ref
            created = False
        s.flush()
        # Config can name hosts and users; the secret itself is only ever an env reference.
        audit.record(s, ws.id, None, "connection.add" if created else "connection.update",
                     {"connection": name, "kind": kind, "secret_ref": secret_ref})
        s.expunge(conn)
        return conn


def resolve_secret_syntax(ref: str) -> None:
    scheme, _, name = ref.partition(":")
    if scheme != "env" or not name:
        raise ValueError("secret_ref must look like env:NAME (secrets are never stored)")


def workspace_connections(s: OrmSession) -> list[Connection]:
    return list(s.scalars(select(Connection).order_by(Connection.name)))


def pick_connection(s: OrmSession, name: str | None) -> Connection:
    conns = workspace_connections(s)
    if not conns:
        raise ConnectionError_("No data connection is configured for this workspace.")
    if name:
        for c in conns:
            if c.name == name:
                return c
        raise ConnectionError_(
            f"Unknown connection {name!r}. Available: {', '.join(c.name for c in conns)}."
        )
    if len(conns) == 1:
        return conns[0]
    default = [c for c in conns if c.default_for_ai]
    if len(default) == 1:
        return default[0]
    raise ConnectionError_(
        f"Several connections exist ({', '.join(c.name for c in conns)}); pass connection=<name>."
    )


def connection_secret(conn: Connection, settings: Settings | None = None) -> str | None:
    """The connection's secret: an ``env:NAME`` reference (CLI) or a value sealed with
    SATCHEL_ENCRYPTION_KEY (entered in Settings → Connections)."""
    ref = conn.secret_ref
    if is_sealed(ref):
        assert ref is not None
        try:
            keyring = Keyring.from_settings(settings or get_settings())
        except EncryptionUnavailable as e:
            raise ValueError(f"its password is encrypted but {e}") from None
        try:
            return keyring.decrypt(ref, connection_aad(conn.workspace_id, conn.id))
        except DecryptionError as e:
            raise ValueError(f"its stored password cannot be decrypted ({e})") from None
    return resolve_secret(ref)


def open_connector(conn: Connection, settings: Settings | None = None) -> Connector:
    try:
        secret = connection_secret(conn, settings)
        return make_connector(conn.kind, dict(conn.config), secret)
    except (ValueError, FileNotFoundError, NotImplementedError) as e:
        raise ConnectionError_(f"Connection {conn.name!r} is unavailable: {e}") from None


def sqlglot_dialect(kind: str) -> str | None:
    return DIALECTS.get(kind)
