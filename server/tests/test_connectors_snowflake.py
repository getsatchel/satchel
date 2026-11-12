"""Snowflake connector: key handling, session parameters (timeout, query tag), guardrail and
row cap, crawl mapping, the Settings API, and run_sql / query_metrics end to end.

The warehouse is ``tests.warehouse_fakes.FakeSnowflake`` (Snowflake SQL transpiled onto the
Jaffle DuckDB). fakesnow was considered but pins an older sqlglot than satchel needs."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.client.session import ClientSession
from snowflake.connector.errors import ProgrammingError
from sqlalchemy import Engine, select

from satchel.core.crypto import Keyring
from satchel.data import connection_admin as ca
from satchel.data.connections import add_connection, sqlglot_dialect
from satchel.data.connectors import SqlTimeout, make_connector
from satchel.data.connectors import snowflake_conn as sf
from satchel.data.crawl import crawl_connection
from satchel.data.gateway import GuardrailError, run_guarded
from satchel.data.results import read_page
from satchel.llm.embeddings import HashEmbedder
from satchel.mcp.auth import create_token, verify_token
from satchel.secrets import connection_aad
from satchel.store.db import admin_session
from satchel.store.models import Asset, AuditEvent, QueryResult
from satchel.sync import import_context
from tests.helpers import build_jaffle_db, copy_example, unique_slug
from tests.test_mcp import Env as McpEnv
from tests.test_mcp import call, text_of, with_client
from tests.test_settings_admin import Env, env, st  # noqa: F401 - the Settings API fixture
from tests.warehouse_fakes import FakeSnowflake, run_duck

CFG = {"account": "myorg-myaccount", "user": "SATCHEL_READER", "role": "SATCHEL_READ",
       "warehouse": "SATCHEL_WH", "database": "JAFFLE"}
PASS = "correct horse battery staple"


def _pem(key: rsa.RSAPrivateKey, passphrase: str | None = None,
         fmt: serialization.PrivateFormat = serialization.PrivateFormat.PKCS8) -> str:
    enc: serialization.KeySerializationEncryption = (
        serialization.BestAvailableEncryption(passphrase.encode()) if passphrase
        else serialization.NoEncryption())
    return key.private_bytes(serialization.Encoding.PEM, fmt, enc).decode()


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_jaffle_db(tmp_path_factory.mktemp("sf") / "jaffle.duckdb")


@pytest.fixture
def fake(warehouse: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSnowflake:
    f = FakeSnowflake(warehouse)
    monkeypatch.setattr(sf, "_default_connect", f)
    return f


# --------------------------------------------------------------------- keys and config


def test_private_key_normalization(key: rsa.RSAPrivateKey) -> None:
    plain = _pem(key)
    assert sf.normalize_private_key(plain) == plain
    # An encrypted (or PKCS#1) key comes out as unencrypted PKCS#8, the form that is sealed.
    assert sf.normalize_private_key(_pem(key, PASS), PASS) == plain
    pkcs1 = _pem(key, fmt=serialization.PrivateFormat.TraditionalOpenSSL)
    assert sf.normalize_private_key(pkcs1 + "\r\n") == plain
    for pem, pw, msg in [
        (_pem(key, PASS), None, "enter its passphrase"),
        (_pem(key, PASS), "wrong", "wrong passphrase"),
        (plain, PASS, "not encrypted"),
        ("hunter2", None, "must be a PEM"),
        (plain[:200] + plain[-30:], None, "not a valid PEM"),  # truncated paste
    ]:
        with pytest.raises(ValueError, match=msg):
            sf.normalize_private_key(pem, pw)


def test_clean_config_and_secret(key: rsa.RSAPrivateKey) -> None:
    assert ca.clean_config("snowflake", CFG | {"schemas": " analytics "})["schemas"] == "analytics"
    for cfg, msg in [
        ({k: v for k, v in CFG.items() if k != "account"}, "Account is required"),
        (CFG | {"account": "myorg-myaccount.snowflakecomputing.com"}, "not the URL"),
        (CFG | {"password": "x"}, "unknown field"),
    ]:
        with pytest.raises(ca.ConnectionAdminError, match=msg):
            ca.clean_config("snowflake", cfg)
    assert ca.clean_secret("snowflake", _pem(key, PASS), PASS) == _pem(key)
    with pytest.raises(ca.ConnectionAdminError, match="Private key: .*passphrase"):
        ca.clean_secret("snowflake", _pem(key, PASS))
    with pytest.raises(ca.ConnectionAdminError, match="takes no passphrase|take no passphrase"):
        ca.clean_secret("postgres", "pw", "x")
    with pytest.raises(ca.ConnectionAdminError, match="needs the private key"):
        ca.clean_secret("snowflake", None, PASS)


def test_connect_parameters(fake: FakeSnowflake, key: rsa.RSAPrivateKey) -> None:
    with pytest.raises(ValueError, match="private key"):
        make_connector("snowflake", CFG, None)
    conn = make_connector("snowflake", CFG, _pem(key))
    assert conn.dialect == "snowflake" and sqlglot_dialect("snowflake") == "snowflake"
    run_guarded(conn, "select 1 as one", row_cap=10, timeout_s=7.5)
    (kw,) = fake.connects
    assert kw["session_parameters"] == {"STATEMENT_TIMEOUT_IN_SECONDS": 8, "QUERY_TAG": "satchel"}
    assert kw["authenticator"] == "SNOWFLAKE_JWT" and "password" not in kw
    assert (kw["account"], kw["user"], kw["role"], kw["warehouse"], kw["database"]) == (
        "myorg-myaccount", "SATCHEL_READER", "SATCHEL_READ", "SATCHEL_WH", "JAFFLE")
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    assert kw["private_key"] == der
    assert all(c.closed for c in fake.conns)


# ------------------------------------------------------------- gateway: guardrail, cap, timeout


def test_guardrail_row_cap_and_timeout(fake: FakeSnowflake, key: rsa.RSAPrivateKey) -> None:
    conn = make_connector("snowflake", CFG, _pem(key))
    for bad in ("delete from analytics.orders", "select $1 from @raw_stage",
                "select system$cancel_all_queries(1)", "use role accountadmin"):
        with pytest.raises(GuardrailError):
            run_guarded(conn, bad, row_cap=10, timeout_s=5)
    assert fake.connects == []  # rejected before anything reached Snowflake

    res = run_guarded(conn, "select order_id from ANALYTICS.ORDERS order by order_id",
                      row_cap=25, timeout_s=5)
    assert res.table.num_rows == 25 and res.truncated and fake.fetch_sizes == [26]
    assert res.tables == ["ANALYTICS.ORDERS"]

    fake.raise_on_execute = ProgrammingError(
        msg="Statement reached its statement or warehouse timeout of 5 second(s) and was "
        "canceled.", errno=630, sqlstate="57014")
    with pytest.raises(SqlTimeout, match="5s timeout"):
        run_guarded(conn, "select 1", row_cap=1, timeout_s=5)
    fake.raise_on_execute = ProgrammingError(msg="Object does not exist", errno=2003)
    with pytest.raises(ProgrammingError):
        run_guarded(conn, "select * from nope", row_cap=1, timeout_s=5)


# ------------------------------------------------------------------------------------ crawl


def test_introspect_maps_information_schema(fake: FakeSnowflake, key: rsa.RSAPrivateKey) -> None:
    conn = make_connector("snowflake", CFG | {"schemas": "analytics, marts"}, _pem(key))
    sql, params = conn.introspect_sql()  # type: ignore[attr-defined]
    assert "JAFFLE.INFORMATION_SCHEMA.COLUMNS" in sql and params == ["analytics", "marts"]
    specs = conn.introspect()
    by = {s.fqn: s for s in specs}
    assert specs[0].kind == "database" and specs[0].fqn == "JAFFLE"
    assert by["analytics"].kind == "schema" and by["analytics"].parent_fqn == "JAFFLE"
    orders = by["analytics.orders"]
    assert orders.kind == "table" and orders.comment == "One row per customer order (AUD cents)."
    amount = by["analytics.orders.amount"]
    assert amount.kind == "column" and amount.parent_fqn == "analytics.orders" and amount.data_type
    assert fake.connects[-1]["session_parameters"]["STATEMENT_TIMEOUT_IN_SECONDS"] == 120
    # Rows straight from Snowflake (upper-case names, empty comments).
    rows = [("ANALYTICS", "MARTS", "ORDERS", "ID", "NUMBER", "", "Orders"),
            ("ANALYTICS", "MARTS", "ORDERS", "AMOUNT", "NUMBER", "Cents", "Orders")]
    got = [(s.kind, s.fqn, s.parent_fqn, s.data_type, s.comment)
           for s in sf.column_specs("ANALYTICS", rows)]
    assert got == [
        ("database", "ANALYTICS", None, None, None),
        ("schema", "MARTS", "ANALYTICS", None, None),
        ("table", "MARTS.ORDERS", "MARTS", None, "Orders"),
        ("column", "MARTS.ORDERS.ID", "MARTS.ORDERS", "NUMBER", None),
        ("column", "MARTS.ORDERS.AMOUNT", "MARTS.ORDERS", "NUMBER", "Cents"),
    ]
    assert sf.ident("analytics") == "analytics" and sf.ident('my "db"') == '"my ""db"""'


# ----------------------------------------------------------------------------- Settings API


def test_settings_api_seals_decrypted_key(
    env: Env, fake: FakeSnowflake, key: rsa.RSAPrivateKey,  # noqa: F811
) -> None:
    kinds = {k["kind"]: k for k in env.client.get("/v1/connection-kinds", headers=env.h()).json()}
    assert kinds["snowflake"]["secret_multiline"] and kinds["snowflake"]["available"]
    encrypted = _pem(key, PASS)
    bad = env.post("/v1/connections/test", {"kind": "snowflake", "config": CFG,
                                            "secret": encrypted, "passphrase": "nope"})
    assert not bad.json()["ok"] and "wrong passphrase" in bad.json()["error"]
    ok = env.post("/v1/connections/test", {"kind": "snowflake", "config": CFG,
                                           "secret": encrypted, "passphrase": PASS})
    assert ok.json()["ok"], ok.text
    r = env.post("/v1/connections", {"name": "sf", "kind": "snowflake", "config": CFG,
                                     "secret": encrypted, "passphrase": PASS})
    assert r.status_code == 201 and r.json()["secret"] == "stored", r.text
    assert "passphrase" not in r.json() and PASS not in r.text
    row = env.conn("sf")
    sealed = Keyring.from_settings(st()).decrypt(row.secret_ref or "",
                                                connection_aad(env.ws_id, row.id))
    assert sealed == _pem(key)  # unencrypted PKCS#8, sealed; the passphrase is not kept
    # A passphrase alone (no new key) is refused.
    p = env.client.patch(f"/v1/connections/{row.id}", headers=env.h(), json={"passphrase": PASS})
    assert p.status_code == 422 and "needs the private key" in p.text
    assert env.post("/v1/connections/test", {"connection_id": str(row.id)}).json()["ok"]
    crawl = env.post(f"/v1/connections/{row.id}/crawl?wait=true", {}, role="curator")
    assert crawl.status_code == 200 and crawl.json()["created"] > 5, crawl.text
    with admin_session(engine=env.engine) as s:
        fqns = set(s.scalars(select(Asset.fqn).where(Asset.connection_id == row.id)))
        assert "analytics.orders.amount" in fqns
        events = list(s.scalars(select(AuditEvent).where(AuditEvent.workspace_id == env.ws_id)))
        dumped = json.dumps([e.target for e in events])
        assert PASS not in dumped and "PRIVATE KEY" not in dumped
    # A failure that echoes key material is scrubbed.
    body = _pem(key).splitlines()[3]
    fake.raise_on_connect = RuntimeError(f"bad key near {body}")
    t = env.post("/v1/connections/test", {"connection_id": str(row.id)}).json()
    assert not t["ok"] and body not in t["error"] and "***" in t["error"]


# ------------------------------------------------------------- run_sql / query_metrics via MCP


@pytest.fixture(scope="module")
def mcp_env(app_engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> McpEnv:
    slug = unique_slug("sf")
    r = import_context(copy_example(tmp_path_factory.mktemp("sfctx")), slug,
                       embedder=HashEmbedder(), engine=app_engine)
    add_connection(slug, "warehouse", "snowflake", CFG, secret_ref="env:SATCHEL_TEST_SF_KEY",
                   engine=app_engine)
    email = f"{slug}@example.com"
    raw, _ = create_token(slug, email, "t", engine=app_engine)
    p = verify_token(raw, engine=app_engine)
    assert p is not None
    return McpEnv(app_engine, slug, r.workspace_id, email, p)


@pytest.fixture
def mcp_fake(
    fake: FakeSnowflake, key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Iterator[FakeSnowflake]:
    monkeypatch.setenv("SATCHEL_TEST_SF_KEY", _pem(key))
    monkeypatch.setenv("SATCHEL_RESULT_DIR", str(tmp_path))
    yield fake


def test_run_sql_and_query_metrics(
    mcp_env: McpEnv, mcp_fake: FakeSnowflake, warehouse: Path,
) -> None:
    crawl_connection(mcp_env.slug, "warehouse", engine=mcp_env.engine)

    async def fn(c: ClientSession) -> tuple[Any, Any, Any]:
        await c.initialize()
        s = await call(c, "start_analysis", question="revenue")
        sid = json.loads(text_of(s))["session_id"]
        q = await call(c, "run_sql", session_id=sid, question="orders",
                       sql="select count(*) as n from ANALYTICS.ORDERS")
        m = await call(c, "query_metrics", session_id=sid, question="revenue",
                       metrics=["revenue"])
        bad = await call(c, "run_sql", session_id=sid, question="x",
                         sql="drop table ANALYTICS.ORDERS")
        return q, m, bad

    q, m, bad = with_client(mcp_env, fn)
    assert not q.isError and not m.isError, (text_of(q), text_of(m))
    assert bad.isError and "SQL rejected" in text_of(bad)
    want = run_duck(warehouse, "select sum(amount)/100.0 from analytics.orders where status "
                    "not in ('returned','return_pending') and customer_id <> 0").column(0)[0]
    rid = text_of(m).split("result_id: ")[1].split()[0]
    with admin_session(engine=mcp_env.engine) as s:
        row = s.get(QueryResult, uuid.UUID(rid))
        assert row is not None
        assert read_page(row, 0, 10)[1][0][0] == pytest.approx(want.as_py())
    sent = [sql for sql, _ in mcp_fake.statements]
    assert "select count(*) as n from ANALYTICS.ORDERS" in sent
    metric_sql = next(x for x in sent if "SUM(" in x.upper() and "INFORMATION_SCHEMA" not in x)
    assert "DATE_TRUNC" in metric_sql or "SUM(CAST" in metric_sql  # compiled for Snowflake
    assert not any(x.lower().startswith("drop") for x in sent)
    assert all(kw["session_parameters"]["QUERY_TAG"] == "satchel" for kw in mcp_fake.connects)
