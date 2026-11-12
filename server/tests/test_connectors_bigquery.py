"""BigQuery connector: config and key validation, the job config (cost guard, timeout, labels,
default dataset), guardrail and row cap, crawl mapping, the Settings API, and run_sql /
query_metrics end to end.

The warehouse is ``tests.warehouse_fakes.FakeBigQuery`` (BigQuery SQL transpiled onto the
Jaffle DuckDB). The goccy bigquery-emulator was not used: it ignores maximum_bytes_billed and
job timeouts and covers little of INFORMATION_SCHEMA, the parts worth testing here."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlglot
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.api_core.exceptions import Forbidden
from mcp.client.session import ClientSession
from sqlalchemy import Engine, select

from satchel.core.crypto import Keyring
from satchel.data import connection_admin as ca
from satchel.data.connections import add_connection, sqlglot_dialect
from satchel.data.connectors import SqlTimeout
from satchel.data.connectors import bigquery_conn as bq
from satchel.data.crawl import crawl_connection
from satchel.data.gateway import GuardrailError, run_guarded
from satchel.data.results import read_page
from satchel.llm.embeddings import HashEmbedder
from satchel.mcp.auth import create_token, verify_token
from satchel.secrets import connection_aad
from satchel.store.db import admin_session
from satchel.store.models import Asset, QueryResult
from satchel.sync import import_context
from tests.helpers import build_jaffle_db, copy_example, unique_slug
from tests.test_mcp import Env as McpEnv
from tests.test_mcp import call, text_of, with_client
from tests.test_settings_admin import Env, env, st  # noqa: F401 - the Settings API fixture
from tests.warehouse_fakes import FakeBigQuery, run_duck

CFG = {"project": "acme-analytics", "location": "EU", "datasets": "analytics"}


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_jaffle_db(tmp_path_factory.mktemp("bq") / "jaffle.duckdb")


@pytest.fixture(scope="module")
def sa_key() -> str:
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    return json.dumps({
        "type": "service_account", "project_id": "acme-analytics", "private_key_id": "abc",
        "private_key": pem, "client_email": "satchel@acme-analytics.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }, indent=2)


@pytest.fixture
def fake(warehouse: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBigQuery:
    f = FakeBigQuery(warehouse)
    made: list[tuple[dict[str, Any], str | None]] = []

    def make_client(config: dict[str, Any], secret: str | None) -> FakeBigQuery:
        made.append((config, secret))
        return f

    monkeypatch.setattr(bq, "make_client", make_client)
    f.made = made  # type: ignore[attr-defined]
    return f


def connector(fake: FakeBigQuery, **cfg: Any) -> bq.BigQueryConnector:
    return bq.BigQueryConnector(CFG | cfg, client=fake)


# --------------------------------------------------------------------- config and key


def test_clean_config_and_secret(sa_key: str) -> None:
    out = ca.clean_config("bigquery", {"project": "acme-analytics"})
    assert out == {"project": "acme-analytics", "maximum_gib_billed": 10}  # cost guard default
    assert ca.clean_config("bigquery", CFG | {"maximum_gib_billed": "250"})[
        "maximum_gib_billed"] == 250
    for cfg, msg in [
        ({"location": "EU"}, "Project is required"),
        ({"project": "Not A Project"}, "not a Google Cloud project ID"),
        (CFG | {"datasets": "analytics, other-project.marts"}, "dataset 'other-project.marts'"),
        (CFG | {"maximum_gib_billed": 0}, "out of range"),
        (CFG | {"credentials": "x"}, "unknown field"),
    ]:
        with pytest.raises(ca.ConnectionAdminError, match=msg):
            ca.clean_config("bigquery", cfg)
    assert ca.clean_secret("bigquery", sa_key + "\n") == sa_key
    assert ca.clean_secret("bigquery", None) is None  # Application Default Credentials
    for secret, msg in [
        ("{not json", "not valid JSON"),
        (json.dumps({"type": "authorized_user"}), "service-account JSON key"),
        (json.dumps({"type": "service_account", "client_email": "x"}), "no private_key"),
    ]:
        with pytest.raises(ca.ConnectionAdminError, match=msg):
            ca.clean_secret("bigquery", secret)
    with pytest.raises(ca.ConnectionAdminError, match="no passphrase"):
        ca.clean_secret("bigquery", sa_key, "pw")


def test_real_client_uses_adc_or_the_key(sa_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from google.cloud import bigquery

    seen: dict[str, Any] = {}

    class Client:
        def __init__(self, **kw: Any) -> None:
            seen.update(kw)

    monkeypatch.setattr(bigquery, "Client", Client)
    bq.make_client(CFG, None)
    assert seen["credentials"] is None and seen["project"] == "acme-analytics"  # ADC
    assert seen["location"] == "EU"
    bq.make_client(CFG, sa_key)
    creds = seen["credentials"]
    assert creds.service_account_email == "satchel@acme-analytics.iam.gserviceaccount.com"
    assert tuple(creds.scopes) == bq.SCOPES


def test_job_config_cost_guard_timeout_and_labels(fake: FakeBigQuery) -> None:
    c = connector(fake)
    assert c.dialect == "bigquery" and sqlglot_dialect("bigquery") == "bigquery"
    run_guarded(c, "select 1 as one", row_cap=10, timeout_s=12.5)
    (job,) = fake.jobs
    cfg = job.job_config
    assert cfg.maximum_bytes_billed == 10 * 1024**3 and cfg.use_legacy_sql is False
    assert int(cfg.job_timeout_ms) == 12500 and cfg.labels == {"app": "satchel"}
    assert cfg.default_dataset.dataset_id == "analytics"
    assert cfg.default_dataset.project == "acme-analytics"
    assert fake.query_kwargs == [{"location": "EU", "timeout": 12.5}]
    assert fake.result_calls == [{"timeout": 12.5, "max_results": 11}]  # row cap + 1
    two = connector(fake, datasets="a, b", maximum_gib_billed=1, data_project="other-proj")
    cfg2 = two.job_config(30)
    assert cfg2.maximum_bytes_billed == 1024**3 and cfg2.default_dataset is None


# ------------------------------------------------------------- gateway: guardrail, cap, timeout


def test_guardrail_row_cap_timeout_and_cost_guard(fake: FakeBigQuery) -> None:
    c = connector(fake)
    for bad in ("delete from analytics.orders where true", "drop table analytics.orders",
                "export data options(uri='gs://x/*.csv') as select 1",
                "select * from external_query('conn', 'select 1')",
                "declare x int64; select x"):
        with pytest.raises(GuardrailError):
            run_guarded(c, bad, row_cap=10, timeout_s=5)
    assert fake.jobs == []  # nothing reached BigQuery

    res = run_guarded(c, "select order_id from `acme-analytics.analytics.orders` "
                      "order by order_id", row_cap=25, timeout_s=5)
    assert res.table.num_rows == 25 and res.truncated and res.tables == ["analytics.orders"]

    fake.bytes_billed = 11 * 1024**3
    with pytest.raises(ValueError, match="more than 10 GiB, the connection's cost guard"):
        run_guarded(c, "select * from analytics.orders", row_cap=10, timeout_s=5)
    fake.bytes_billed = 0

    fake.slow = True
    with pytest.raises(SqlTimeout, match="5s timeout"):
        run_guarded(c, "select 1", row_cap=1, timeout_s=5)
    assert fake.jobs[-1].cancelled
    fake.slow = False
    fake.raise_on_result = Forbidden("Access Denied: Table acme:analytics.secret")
    with pytest.raises(Forbidden):
        run_guarded(c, "select * from analytics.secret", row_cap=1, timeout_s=5)


# ------------------------------------------------------------------------------------ crawl


def test_introspect_maps_information_schema(fake: FakeBigQuery) -> None:
    c = connector(fake, datasets="")
    assert c.list_datasets() == ["analytics"]  # every dataset when none are configured
    sql = c.introspect_sql("analytics")
    assert "`acme-analytics.analytics.INFORMATION_SCHEMA.COLUMNS`" in sql
    assert "COLUMN_FIELD_PATHS" in sql and "option_name = 'description'" in sql
    sqlglot.parse_one(sql, read="bigquery")
    specs = c.introspect()
    by = {s.fqn: s for s in specs}
    assert specs[0].kind == "database" and specs[0].fqn == "acme-analytics"
    assert by["analytics"].kind == "schema" and by["analytics"].parent_fqn == "acme-analytics"
    assert by["analytics.orders"].comment == "One row per customer order (AUD cents)."
    amount = by["analytics.orders.amount"]
    assert amount.kind == "column" and amount.parent_fqn == "analytics.orders"
    assert all(j.job_config.labels == {"app": "satchel"} for j in fake.jobs)
    rows = [{"table_name": "t", "column_name": "a", "data_type": "STRUCT<x INT64>",
             "column_comment": "", "table_comment": '"Say \\"hi\\"\\nbye"'}]
    got = [(s.kind, s.fqn, s.data_type, s.comment) for s in bq.dataset_specs("ds", rows)]
    assert got == [("table", "ds.t", None, 'Say "hi"\nbye'),
                   ("column", "ds.t.a", "STRUCT<x INT64>", None)]


# ----------------------------------------------------------------------------- Settings API


def test_settings_api(env: Env, fake: FakeBigQuery, sa_key: str) -> None:  # noqa: F811
    kinds = {k["kind"]: k for k in env.client.get("/v1/connection-kinds", headers=env.h()).json()}
    k = kinds["bigquery"]
    assert k["available"] and k["secret_multiline"] and k["passphrase_label"] is None
    gib = next(f for f in k["fields"] if f["name"] == "maximum_gib_billed")
    assert gib["type"] == "number" and gib["default"] == 10 and gib["max"] == 1_000_000
    # ADC: no secret at all.
    r = env.post("/v1/connections", {"name": "bq", "kind": "bigquery", "config": CFG})
    assert r.status_code == 201 and r.json()["secret"] is None, r.text
    cid = r.json()["id"]
    assert env.post("/v1/connections/test", {"connection_id": cid}).json()["ok"]
    crawl = env.post(f"/v1/connections/{cid}/crawl?wait=true", {}, role="curator")
    assert crawl.status_code == 200 and crawl.json()["created"] > 5, crawl.text
    with admin_session(engine=env.engine) as s:
        fqns = set(s.scalars(select(Asset.fqn).where(Asset.connection_id == uuid.UUID(cid))))
    assert {"acme-analytics", "analytics.orders", "analytics.orders.amount"} <= fqns
    # A service-account key is sealed and handed to the client.
    p = env.client.patch(f"/v1/connections/{cid}", headers=env.h(), json={"secret": sa_key})
    assert p.status_code == 200 and p.json()["secret"] == "stored" and "private_key" not in p.text
    row = env.conn("bq")
    assert Keyring.from_settings(st()).decrypt(
        row.secret_ref or "", connection_aad(env.ws_id, row.id)) == sa_key
    assert env.post("/v1/connections/test", {"connection_id": cid}).json()["ok"]
    assert fake.made[-1][1] == sa_key  # type: ignore[attr-defined]
    bad = env.client.patch(f"/v1/connections/{cid}", headers=env.h(), json={"secret": "{x"})
    assert bad.status_code == 422 and "not valid JSON" in bad.text
    # Errors that echo key material are scrubbed.
    line = json.loads(sa_key)["private_key"].splitlines()[2]
    fake.raise_on_result = RuntimeError(f"invalid_grant near {line}")
    t = env.post("/v1/connections/test", {"connection_id": cid}).json()
    assert not t["ok"] and line not in t["error"] and "***" in t["error"]


# ------------------------------------------------------------- run_sql / query_metrics via MCP


@pytest.fixture(scope="module")
def mcp_env(app_engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> McpEnv:
    slug = unique_slug("bq")
    r = import_context(copy_example(tmp_path_factory.mktemp("bqctx")), slug,
                       embedder=HashEmbedder(), engine=app_engine)
    add_connection(slug, "warehouse", "bigquery", CFG, engine=app_engine)
    email = f"{slug}@example.com"
    raw, _ = create_token(slug, email, "t", engine=app_engine)
    p = verify_token(raw, engine=app_engine)
    assert p is not None
    return McpEnv(app_engine, slug, r.workspace_id, email, p)


def test_run_sql_and_query_metrics(
    mcp_env: McpEnv, fake: FakeBigQuery, warehouse: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SATCHEL_RESULT_DIR", str(tmp_path))
    crawl_connection(mcp_env.slug, "warehouse", engine=mcp_env.engine)

    async def fn(c: ClientSession) -> tuple[Any, Any, Any]:
        await c.initialize()
        s = await call(c, "start_analysis", question="revenue")
        sid = json.loads(text_of(s))["session_id"]
        q = await call(c, "run_sql", session_id=sid, question="orders",
                       sql="select count(*) as n from orders")  # default dataset
        m = await call(c, "query_metrics", session_id=sid, question="revenue",
                       metrics=["revenue"], dimensions=["order_date:month"])
        fake.bytes_billed = 50 * 1024**3
        big = await call(c, "run_sql", session_id=sid, question="all",
                         sql="select * from analytics.orders")
        return q, m, big

    q, m, big = with_client(mcp_env, fn)
    assert not q.isError and not m.isError, (text_of(q), text_of(m))
    assert big.isError and "cost guard" in text_of(big)
    want = run_duck(warehouse, "select sum(amount)/100.0 from analytics.orders where status "
                    "not in ('returned','return_pending') and customer_id <> 0").column(0)[0]
    rid = text_of(m).split("result_id: ")[1].split()[0]
    with admin_session(engine=mcp_env.engine) as s:
        row = s.get(QueryResult, uuid.UUID(rid))
        assert row is not None
        total = sum(r[1] for r in read_page(row, 0, 1000)[1])
    assert total == pytest.approx(want.as_py())
    metric_sql = next(j.sql for j in fake.jobs if "TIMESTAMP_TRUNC" in j.sql)
    assert "INT64" in metric_sql  # compiled for BigQuery, not Postgres
    assert all(j.job_config.labels == {"app": "satchel"} for j in fake.jobs)
