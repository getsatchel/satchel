"""Snowflake connector (``pip install 'satchel[snowflake]'``), key-pair auth only.

Password auth is deliberately not offered: Snowflake is phasing out single-factor password
sign-in (service users, ``TYPE = SERVICE``, cannot use one at all), and key-pair is its
recommended auth for programs. The stored secret is an unencrypted PKCS#8 PEM; an encrypted
key is decrypted with its passphrase once, when it is saved, and then sealed with
SATCHEL_ENCRYPTION_KEY (``normalize_private_key``).

Sessions carry ``STATEMENT_TIMEOUT_IN_SECONDS`` (the gateway timeout) and
``QUERY_TAG = 'satchel'``. Snowflake has no read-only session: reads are enforced by the gateway
guardrail and by a role that only has USAGE / SELECT (docs/integrations/warehouses.md).
"""

from __future__ import annotations

import importlib.util
import math
import re
from typing import Any

import pyarrow as pa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from satchel.data.connectors.base import AssetSpec, SqlTimeout, split_list

MISSING = ("Snowflake needs the snowflake-connector-python package: "
           "pip install 'satchel[snowflake]'")
QUERY_TAG = "satchel"
APPLICATION = "Satchel"
INTROSPECT_TIMEOUT_S = 120
_TIMEOUT_ERRNO = {604, 630}  # canceled; reached its statement / warehouse timeout
_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def normalize_private_key(pem: str, passphrase: str | None = None) -> str:
    """Load a PEM private key (decrypting it with ``passphrase``) and return it as an
    unencrypted PKCS#8 PEM. Raises ``ValueError`` with an admin-facing message."""
    text = pem.strip().replace("\r\n", "\n")
    if "-----BEGIN" not in text:
        raise ValueError("the private key must be a PEM file (-----BEGIN PRIVATE KEY-----)")
    try:
        key = serialization.load_pem_private_key(
            text.encode(), password=passphrase.encode() if passphrase else None)
    except TypeError:
        if passphrase:
            raise ValueError("this private key is not encrypted; leave the passphrase "
                             "empty") from None
        raise ValueError("this private key is encrypted: enter its passphrase") from None
    except ValueError:
        raise ValueError("cannot read the private key: wrong passphrase, or not a valid PEM "
                         "(paste the whole file, including the BEGIN / END lines)") from None
    if not isinstance(key, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
        raise ValueError("Snowflake key-pair auth needs an RSA (or EC) private key")
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()


def private_key_der(pem: str) -> bytes:
    key = serialization.load_pem_private_key(normalize_private_key(pem).encode(), None)
    return key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def ident(name: str) -> str:
    """A Snowflake identifier for SQL: plain names stay unquoted (Snowflake upper-cases them),
    anything else is double-quoted."""
    return name if _PLAIN.match(name) else '"' + name.replace('"', '""') + '"'


def check_config(config: dict[str, Any]) -> None:
    acct = config.get("account")
    if acct and (not _ACCOUNT.match(str(acct)) or "snowflakecomputing.com" in str(acct)):
        raise ValueError("account: the account identifier (orgname-accountname, or the "
                         "account locator), not the URL")


def session_parameters(timeout_s: float) -> dict[str, Any]:
    return {"STATEMENT_TIMEOUT_IN_SECONDS": max(1, math.ceil(timeout_s)),
            "QUERY_TAG": QUERY_TAG}


def _default_connect(**kwargs: Any) -> Any:
    try:
        import snowflake.connector
    except ImportError:
        raise ValueError(MISSING) from None
    return snowflake.connector.connect(**kwargs)


class SnowflakeConnector:
    kind = "snowflake"
    dialect = "snowflake"

    def __init__(
        self, config: dict[str, Any], secret: str | None = None, *, connect: Any = None
    ) -> None:
        for key in ("account", "user", "database"):
            if not config.get(key):
                raise ValueError(f"snowflake connection needs config.{key}")
        check_config(config)
        if not secret:
            raise ValueError("snowflake connection needs its private key (key-pair auth)")
        self.config = config
        self.schemas = split_list(config.get("schemas"))
        self._key = private_key_der(secret)
        self._connect_fn = connect or _default_connect
        if connect is None and importlib.util.find_spec("snowflake.connector") is None:
            raise ValueError(MISSING)

    def connect_kwargs(self, timeout_s: float) -> dict[str, Any]:
        c = self.config
        kw: dict[str, Any] = {
            "account": c["account"], "user": c["user"], "private_key": self._key,
            "authenticator": "SNOWFLAKE_JWT", "database": c["database"],
            "session_parameters": session_parameters(timeout_s),
            "application": APPLICATION, "login_timeout": 30,
            "client_session_keep_alive": False,
        }
        for opt in ("role", "warehouse"):
            if c.get(opt):
                kw[opt] = c[opt]
        return kw

    def _query(self, sql: str, params: list[Any] | None, *, limit: int | None,
               timeout_s: float) -> tuple[list[str], list[tuple[Any, ...]]]:
        conn = self._connect_fn(**self.connect_kwargs(timeout_s))
        try:
            cur = conn.cursor()
            try:
                if params:
                    cur.execute(sql, params)
                else:
                    cur.execute(sql)
                names = [d[0] for d in (cur.description or [])]
                rows = cur.fetchmany(limit) if limit is not None else cur.fetchall()
            finally:
                cur.close()
        except Exception as e:
            if _is_timeout(e):
                raise SqlTimeout(f"statement exceeded {timeout_s:g}s timeout") from e
            raise
        finally:
            conn.close()
        return names, [tuple(r) for r in rows]

    def execute(self, sql: str, *, limit: int, timeout_s: float) -> pa.Table:
        names, rows = self._query(sql, None, limit=max(limit, 1), timeout_s=timeout_s)
        return pa.table({n: [r[i] for r in rows] for i, n in enumerate(names)})

    def introspect_sql(self) -> tuple[str, list[Any]]:
        db = ident(str(self.config["database"]))
        where = "c.table_schema <> 'INFORMATION_SCHEMA'"
        params: list[Any] = []
        if self.schemas:
            where += " AND UPPER(c.table_schema) IN (" + ", ".join(
                "UPPER(%s)" for _ in self.schemas) + ")"
            params = list(self.schemas)
        sql = (
            "SELECT CURRENT_DATABASE(), c.table_schema, c.table_name, c.column_name, "
            "c.data_type, c.comment, t.comment "
            f"FROM {db}.INFORMATION_SCHEMA.COLUMNS c "
            f"JOIN {db}.INFORMATION_SCHEMA.TABLES t ON t.table_schema = c.table_schema "
            "AND t.table_name = c.table_name "
            f"WHERE {where} ORDER BY c.table_schema, c.table_name, c.ordinal_position"
        )
        return sql, params

    def introspect(self) -> list[AssetSpec]:
        sql, params = self.introspect_sql()
        _names, rows = self._query(sql, params, limit=None, timeout_s=INTROSPECT_TIMEOUT_S)
        return column_specs(str(self.config["database"]), rows)

    def close(self) -> None:
        return None


def column_specs(database: str, rows: list[tuple[Any, ...]]) -> list[AssetSpec]:
    """Rows of (current_database, schema, table, column, data_type, column_comment,
    table_comment) ordered by schema, table, position -> catalog assets."""
    db = str(rows[0][0]) if rows and rows[0][0] else database
    out: list[AssetSpec] = [AssetSpec("database", db)]
    schemas: set[str] = set()
    tables: set[str] = set()
    for _db, sch, tbl, col, dtype, ccom, tcom in rows:
        t = f"{sch}.{tbl}"
        if sch not in schemas:
            schemas.add(sch)
            out.append(AssetSpec("schema", sch, db))
        if t not in tables:
            tables.add(t)
            out.append(AssetSpec("table", t, sch, comment=tcom or None))
        out.append(AssetSpec("column", f"{t}.{col}", t, dtype, ccom or None))
    return out


def _is_timeout(e: Exception) -> bool:
    return getattr(e, "errno", None) in _TIMEOUT_ERRNO or getattr(e, "sqlstate", None) == "57014"
