"""Warehouse connections managed from the UI (Settings → Connections): the form each kind
needs, validation, create / update / remove / test, with the password sealed at rest.

The CLI (``satchel connection add``) still works and may use ``env:NAME`` references; the UI
stores the secret itself, encrypted with SATCHEL_ENCRYPTION_KEY (``satchel.secrets``).
"""

from __future__ import annotations

import importlib.util
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from satchel.audit import record as record_audit
from satchel.core.crypto import EncryptionUnavailable, Keyring
from satchel.data.connections import KINDS, ConnectionError_, connection_secret
from satchel.data.connectors import make_connector
from satchel.data.connectors.bigquery_conn import DEFAULT_MAX_GIB_BILLED, parse_service_account
from satchel.data.connectors.bigquery_conn import check_config as check_bigquery
from satchel.data.connectors.snowflake_conn import check_config as check_snowflake
from satchel.data.connectors.snowflake_conn import normalize_private_key
from satchel.secrets import connection_aad
from satchel.settings import Settings
from satchel.store.models import Asset, Connection, ContextSource, QueryResult

FieldType = Literal["text", "number", "path"]


@dataclass(frozen=True)
class KindField:
    name: str
    label: str
    type: FieldType = "text"
    required: bool = False
    placeholder: str | None = None
    help: str | None = None
    default: Any = None
    min: int = 1  # number fields
    max: int = 65535


@dataclass(frozen=True)
class KindSpec:
    kind: str
    label: str
    available: bool
    fields: tuple[KindField, ...]
    secret_label: str | None = None
    secret_help: str | None = None
    note: str | None = None
    secret_multiline: bool = False  # a PEM / JSON key: a textarea, newlines kept
    passphrase_label: str | None = None  # decrypts the secret when it is saved; never stored
    requires: tuple[str, ...] = ()  # importable modules the connector needs
    extra: str | None = None  # the satchel extra that installs them


def missing_modules(spec: KindSpec) -> list[str]:
    out = []
    for mod in spec.requires:
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append(mod)
    return out


def kind_status(spec: KindSpec) -> KindSpec:
    """The spec as it applies to this install: a kind whose library is missing is unavailable,
    with the install hint as its note."""
    if spec.available and missing_modules(spec):
        return replace(spec, available=False,
                       note=f"Not installed: pip install 'satchel[{spec.extra or spec.kind}]'")
    return spec


def kinds() -> list[KindSpec]:
    return [kind_status(k) for k in SPECS.values()]


SPECS: dict[str, KindSpec] = {
    "postgres": KindSpec(
        "postgres", "PostgreSQL", True,
        (
            KindField("host", "Host", required=True, placeholder="warehouse.internal"),
            KindField("port", "Port", "number", default=5432),
            KindField("database", "Database", required=True, placeholder="analytics"),
            KindField("user", "User", required=True, placeholder="satchel_reader"),
            KindField("role", "Role (optional)", help="Run every query as this role "
                      "(SET ROLE). Use a role that can only read what agents may see."),
        ),
        secret_label="Password",
        secret_help="Or a full postgresql:// URL, which then overrides the fields above.",
        note="Sessions are read-only with a statement timeout. Grant the user SELECT only.",
    ),
    "duckdb": KindSpec(
        "duckdb", "DuckDB", True,
        (
            KindField("path", "Database file", "path", required=True,
                      placeholder="/data/warehouse.duckdb",
                      help="A DuckDB file on the server, opened read-only."),
        ),
        note="For local use and demos. Parquet-backed views are configured with the CLI.",
    ),
    "bigquery": KindSpec(
        "bigquery", "BigQuery", True,
        (
            KindField("project", "Project", required=True, placeholder="my-project",
                      help="Runs and bills the query jobs. The identity needs "
                      "roles/bigquery.jobUser here."),
            KindField("data_project", "Data project (optional)", placeholder="my-project",
                      help="Holds the datasets, when it is not the job project."),
            KindField("location", "Location (optional)", placeholder="US",
                      help="The datasets' location, e.g. US, EU or europe-west1."),
            KindField("datasets", "Datasets (optional)", placeholder="analytics, marts",
                      help="Comma-separated dataset IDs to catalog. Empty: every dataset the "
                      "identity can see. With exactly one, unqualified table names resolve "
                      "in it."),
            KindField("maximum_gib_billed", "Max GiB billed per query", "number",
                      default=DEFAULT_MAX_GIB_BILLED, max=1_000_000,
                      help="Cost guard: BigQuery refuses a query that would bill more."),
        ),
        secret_label="Service-account key (optional)",
        secret_help="Paste the JSON key file. Leave empty to use Application Default "
        "Credentials (workload identity on GKE / Cloud Run), the recommended setup.",
        secret_multiline=True,
        note="Read-only is enforced by the SQL guardrail and IAM: grant "
        "roles/bigquery.dataViewer on the data and roles/bigquery.jobUser on the project, "
        "nothing else.",
        requires=("google.cloud.bigquery",), extra="bigquery",
    ),
    "snowflake": KindSpec(
        "snowflake", "Snowflake", True,
        (
            KindField("account", "Account", required=True, placeholder="myorg-myaccount",
                      help="The account identifier (orgname-accountname), not the URL."),
            KindField("user", "User", required=True, placeholder="SATCHEL_READER"),
            KindField("role", "Role", placeholder="SATCHEL_READ",
                      help="A role with only USAGE and SELECT grants. Empty: the user's "
                      "default role."),
            KindField("warehouse", "Warehouse", placeholder="SATCHEL_WH"),
            KindField("database", "Database", required=True, placeholder="ANALYTICS"),
            KindField("schemas", "Schemas (optional)", placeholder="MARTS, STAGING",
                      help="Comma-separated schemas to catalog. Empty: every schema."),
        ),
        secret_label="Private key",
        secret_help="Key-pair auth: paste the PEM private key whose public key is set on the "
        "user (ALTER USER ... SET RSA_PUBLIC_KEY).",
        secret_multiline=True,
        passphrase_label="Key passphrase (optional)",
        note="Queries are tagged 'satchel' and time out with the SQL timeout limit. Use a "
        "role that can only read.",
        requires=("snowflake.connector",), extra="snowflake",
    ),
}
assert set(SPECS) == set(KINDS)


class ConnectionAdminError(ValueError):
    """Invalid input (the API maps it to 422)."""


def clean_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    spec = SPECS.get(kind)
    if spec is None:
        raise ConnectionAdminError(f"unknown kind {kind!r}")
    spec = kind_status(spec)
    if not spec.available:
        raise ConnectionAdminError(f"{spec.label} connections are not available: {spec.note}")
    known = {f.name for f in spec.fields}
    extra = sorted(set(config) - known)
    if extra:
        raise ConnectionAdminError(f"unknown field(s) for {spec.label}: {', '.join(extra)}")
    out: dict[str, Any] = {}
    for f in spec.fields:
        v = config.get(f.name, f.default)
        if isinstance(v, str):
            v = v.strip() or None
        if v is None:
            if f.required:
                raise ConnectionAdminError(f"{f.label} is required")
            continue
        if f.type == "number":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ConnectionAdminError(f"{f.label} must be a number") from None
            if not f.min <= v <= f.max:
                raise ConnectionAdminError(f"{f.label} is out of range ({f.min}-{f.max})")
        out[f.name] = v
    check = {"bigquery": check_bigquery, "snowflake": check_snowflake}.get(kind)
    if check is not None:
        try:
            check(out)
        except ValueError as e:
            raise ConnectionAdminError(str(e)) from None
    return out


def clean_secret(kind: str, secret: str | None, passphrase: str | None = None) -> str | None:
    """Validate a secret before it is sealed. A Snowflake key is decrypted with its passphrase
    and kept as unencrypted PKCS#8 (it is sealed with SATCHEL_ENCRYPTION_KEY, which would protect
    a stored passphrase just the same); a BigQuery key must be service-account JSON."""
    if not secret:
        if passphrase:
            raise ConnectionAdminError("a passphrase needs the private key it decrypts")
        return secret
    spec = SPECS.get(kind)
    if passphrase and (spec is None or spec.passphrase_label is None):
        raise ConnectionAdminError(f"{kind} connections take no passphrase")
    try:
        if kind == "snowflake":
            return normalize_private_key(secret, passphrase)
        if kind == "bigquery":
            parse_service_account(secret)
            return secret.strip()
    except ValueError as e:
        raise ConnectionAdminError(f"{spec.secret_label if spec else 'secret'}: {e}") from None
    return secret


def _name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 64 or not all(c.isalnum() or c in "-_." for c in name):
        raise ConnectionAdminError("name: 1-64 letters, digits, dash, underscore or dot")
    return name


def _seal(conn: Connection, keyring: Keyring | None, secret: str) -> None:
    if keyring is None:
        raise EncryptionUnavailable("SATCHEL_ENCRYPTION_KEY is not set, so the password "
                                    "cannot be stored")
    conn.secret_ref = keyring.encrypt(secret, connection_aad(conn.workspace_id, conn.id))


def _set_default(s: Session, conn: Connection) -> None:
    s.execute(update(Connection).where(
        Connection.workspace_id == conn.workspace_id, Connection.id != conn.id,
    ).values(default_for_ai=False))
    conn.default_for_ai = True


def create(
    s: Session, workspace_id: uuid.UUID, actor: uuid.UUID | None, keyring: Keyring | None, *,
    name: str, kind: str, config: dict[str, Any], secret: str | None = None,
    passphrase: str | None = None, default_for_ai: bool | None = None,
) -> Connection:
    name = _name(name)
    cfg = clean_config(kind, config)
    secret = clean_secret(kind, secret, passphrase)
    if s.scalars(select(Connection.id).where(Connection.workspace_id == workspace_id,
                                            Connection.name == name)).first():
        raise ConnectionAdminError(f"a connection named {name!r} already exists")
    first = (s.scalar(select(func.count()).select_from(Connection).where(
        Connection.workspace_id == workspace_id)) or 0) == 0
    conn = Connection(id=uuid.uuid4(), workspace_id=workspace_id, name=name, kind=kind,
                      config=cfg, default_for_ai=False)
    if secret:
        _seal(conn, keyring, secret)
    s.add(conn)
    s.flush()
    if default_for_ai or (default_for_ai is None and first):
        _set_default(s, conn)
    s.flush()
    record_audit(s, workspace_id, actor, "connection.add",
                 {"connection": name, "kind": kind, "secret": "stored" if secret else None,
                  "default_for_ai": conn.default_for_ai})
    return conn


_UNSET: Any = object()


def update_conn(
    s: Session, conn: Connection, actor: uuid.UUID | None, keyring: Keyring | None, *,
    name: Any = _UNSET, config: Any = _UNSET, secret: Any = _UNSET,
    passphrase: str | None = None, default_for_ai: Any = _UNSET,
) -> Connection:
    """Patch: left-out arguments keep their value; ``secret=None`` removes the secret.
    ``passphrase`` only decrypts a new ``secret``."""
    if passphrase and (secret is _UNSET or not secret):
        raise ConnectionAdminError("a passphrase needs the private key it decrypts")
    changed: list[str] = []
    old = conn.name
    if name is not _UNSET and name != conn.name:
        new = _name(name)
        if s.scalars(select(Connection.id).where(
                Connection.workspace_id == conn.workspace_id, Connection.name == new,
                Connection.id != conn.id)).first():
            raise ConnectionAdminError(f"a connection named {new!r} already exists")
        conn.name = new
        changed.append("name")
    if config is not _UNSET:
        conn.config = clean_config(conn.kind, config)
        changed.append("config")
    if secret is not _UNSET:
        if secret:
            _seal(conn, keyring, clean_secret(conn.kind, secret, passphrase) or "")
        else:
            conn.secret_ref = None
        changed.append("secret")
    if default_for_ai is not _UNSET and default_for_ai and not conn.default_for_ai:
        _set_default(s, conn)
        changed.append("default_for_ai")
    if changed:
        s.flush()
        record_audit(s, conn.workspace_id, actor, "connection.update",
                     {"connection": old, "renamed_to": conn.name if conn.name != old else None,
                      "fields": changed})
    return conn


def remove(s: Session, conn: Connection, actor: uuid.UUID | None) -> int:
    """Remove a connection. Its catalogued assets are marked dropped and detached (context that
    mentions them is flagged by the stale-reference check, history is kept); cached results
    forget it. Refused while a source uses it. Returns the number of assets dropped."""
    users = list(s.scalars(select(ContextSource.name).where(
        ContextSource.connection_id == conn.id)))
    if users:
        raise ConnectionAdminError(
            f"sources use this connection: {', '.join(sorted(users))}. Change or remove them "
            "first.")
    now = datetime.now(UTC)
    n = s.scalar(select(func.count()).select_from(Asset).where(
        Asset.connection_id == conn.id, Asset.dropped_at.is_(None))) or 0
    s.execute(update(Asset).where(Asset.connection_id == conn.id, Asset.dropped_at.is_(None))
              .values(dropped_at=now))
    s.execute(update(Asset).where(Asset.connection_id == conn.id).values(connection_id=None))
    s.execute(update(QueryResult).where(QueryResult.connection_id == conn.id)
              .values(connection_id=None))
    record_audit(s, conn.workspace_id, actor, "connection.remove",
                 {"connection": conn.name, "kind": conn.kind, "assets_dropped": n})
    s.delete(conn)
    s.flush()
    return int(n)


@dataclass
class TestResult:
    ok: bool
    latency_ms: int
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def test(
    kind: str, config: dict[str, Any], secret: str | None, passphrase: str | None = None,
) -> TestResult:
    """Open the connection and run ``select 1`` (10 s timeout). The secret is scrubbed from
    errors."""
    t0 = time.monotonic()
    try:
        cfg = clean_config(kind, config)
        connector = make_connector(kind, cfg, clean_secret(kind, secret, passphrase))
        try:
            connector.execute("select 1", limit=1, timeout_s=10.0)
        finally:
            connector.close()
    except Exception as e:  # noqa: BLE001 - every failure is reported to the admin
        msg = scrub(f"{type(e).__name__}: {e}", secret, passphrase)
        return TestResult(False, int((time.monotonic() - t0) * 1000), msg[:1000])
    return TestResult(True, int((time.monotonic() - t0) * 1000))


def scrub(msg: str, *secrets: str | None) -> str:
    """Remove secrets from an error message, including any line of a multi-line key (PEM lines
    are also split on the ``\\n`` escapes of a JSON key file)."""
    for secret in secrets:
        if not secret or len(secret) < 4:
            continue
        msg = msg.replace(secret, "***")
        for line in re.split(r"\\n|\n", secret):
            line = line.strip()
            if len(line) >= 16:
                msg = msg.replace(line, "***")
    return msg


def stored_secret(conn: Connection, settings: Settings) -> str | None:
    try:
        return connection_secret(conn, settings)
    except ValueError as e:
        raise ConnectionError_(str(e)) from None
