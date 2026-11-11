"""M8 storage: the result store (local, GCS and S3 fakes, real boto3 against moto), expired-result
purge, audit export."""

from __future__ import annotations

import builtins
import json
import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from satchel import audit
from satchel.api.app import create_app
from satchel.cli.main import app as cli
from satchel.data import objstore, s3
from satchel.data.connections import add_connection
from satchel.data.result_store import (
    ROW_GROUP_ROWS,
    LocalResultStore,
    ObjectResultStore,
    configured_store,
)
from satchel.data.results import purge_expired, read_page, store_result
from satchel.llm.embeddings import HashEmbedder
from satchel.mcp.auth import create_token, revoke_token
from satchel.settings import Settings
from satchel.sources.service import add_source, remove_source
from satchel.store.db import admin_session, workspace_session
from satchel.store.models import AppUser, AuditEvent, QueryResult, Workspace
from tests.helpers import MemoryBucket, unique_slug, use_test_engines
from tests.test_data_plane import (  # noqa: F401  (fixtures)
    Env,
    call,
    env,
    result_dir,
    rid_of,
    start,
    text_of,
    warehouse,
    with_client,
)

BUCKET = "satchel-test-bucket"


@pytest.fixture
def bucket() -> Iterator[MemoryBucket]:
    b = MemoryBucket()
    for scheme in objstore.SCHEMES:
        objstore.register_bucket(scheme, BUCKET, b)
    yield b
    for scheme in objstore.SCHEMES:
        objstore.register_bucket(scheme, BUCKET, None)


def table(n: int) -> pa.Table:
    return pa.table({"n": list(range(n)), "s": [f"row-{i}" for i in range(n)]})


# ------------------------------------------------------------------------------- result store


def test_local_store_writes_file_uri_and_reads_legacy_relative_paths(tmp_path: Path) -> None:
    st = LocalResultStore(tmp_path)
    ws, rid = uuid.uuid4(), uuid.uuid4()
    ref = st.write(ws, rid, table(30))
    assert ref == (tmp_path / f"{ws}/{rid}.parquet").resolve().as_uri()
    assert st.exists(ref) and st.read_page(ref, 28, 10)[1] == [(28, "row-28"), (29, "row-29")]
    legacy = f"{ws}/{rid}.parquet"  # rows written before refs became URIs
    assert st.exists(legacy) and st.read_page(legacy, 0, 2)[0] == ["n", "s"]
    assert st.delete(ref) and not st.exists(ref) and not st.delete(ref)
    with pytest.raises(FileNotFoundError):
        st.read_page(ref, 0, 1)


@pytest.mark.parametrize("scheme", objstore.SCHEMES)
def test_object_store_pages_across_row_groups_with_range_reads(
    bucket: MemoryBucket, scheme: str
) -> None:
    st = ObjectResultStore(scheme, BUCKET, "results/", bucket=bucket)
    n = ROW_GROUP_ROWS * 40
    rnd = random.Random(7)  # incompressible, so the object is big
    big = pa.table({"n": list(range(n)), "s": [f"{rnd.getrandbits(192):048x}" for _ in range(n)]})
    ws, rid = uuid.uuid4(), uuid.uuid4()
    ref = st.write(ws, rid, big)
    key = f"results/{ws}/{rid}.parquet"
    assert ref == f"{scheme}://{BUCKET}/{key}" and st.exists(ref)
    size = len(bucket.objects[key])
    assert size > 500_000

    bucket.range_reads.clear()
    cols, rows = st.read_page(ref, 20_500, 3)
    assert cols == ["n", "s"] and [r[0] for r in rows] == [20_500, 20_501, 20_502]
    fetched = sum(e - s for _n, s, e in bucket.range_reads)
    assert fetched < size / 4, (fetched, size)  # footer + one row group, not the object

    # a page straddling a row-group boundary
    rows = st.read_page(ref, ROW_GROUP_ROWS - 2, 5)[1]
    assert [r[0] for r in rows] == list(range(ROW_GROUP_ROWS - 2, ROW_GROUP_ROWS + 3))
    assert st.read_page(ref, n - 1, 50)[1][0][0] == n - 1
    assert st.read_page(ref, n + 10, 5) == (["n", "s"], [])

    assert st.delete(ref) and not st.exists(ref) and not st.delete(ref)
    with pytest.raises(FileNotFoundError):
        st.read_page(ref, 0, 1)


def test_gcs_missing_extra_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *a: Any, **k: Any) -> Any:
        if name.startswith("google.cloud"):
            raise ImportError("No module named 'google.cloud'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    objstore.reset_clients()
    with pytest.raises(RuntimeError, match=r"satchel-server\[gcp\]"):
        objstore.open_bucket("gs", "nope")


def test_s3_missing_extra_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *a: Any, **k: Any) -> Any:
        if name.startswith(("boto3", "botocore")):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    objstore.reset_clients()
    with pytest.raises(RuntimeError, match=r"satchel-server\[aws\]"):
        objstore.open_bucket("s3", "nope")


def test_object_uris_parse_and_reject_other_schemes() -> None:
    assert objstore.split_uri("s3://b/a/b/") == ("s3", "b", "a/b")
    assert objstore.split_uri("gs://b") == ("gs", "b", "")
    assert objstore.is_object_uri("s3://b/x") and not objstore.is_object_uri("/var/x")
    for bad in ("s3://", "az://b/x", "/tmp/x"):
        with pytest.raises(ValueError):
            objstore.split_uri(bad)


def test_s3_bucket_against_moto_ranges_heads_and_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real boto3 code path (inclusive HTTP ranges, 404 heads, idempotent delete)."""
    moto = pytest.importorskip("moto")
    for k in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")
    with moto.mock_aws():
        objstore.reset_clients()
        client = s3.make_client()
        client.create_bucket(Bucket="satchel-moto",
                             CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
        b = objstore.open_bucket("s3", "satchel-moto")
        assert b.size("missing") is None and b.delete("missing") is False
        b.put("a/b.bin", bytes(range(256)) * 4, "application/octet-stream")
        assert b.size("a/b.bin") == 1024
        assert b.read_range("a/b.bin", 10, 14) == bytes([10, 11, 12, 13])
        assert b.read_range("a/b.bin", 5, 5) == b""

        st = ObjectResultStore("s3", "satchel-moto", "r")
        ws, rid = uuid.uuid4(), uuid.uuid4()
        ref = st.write(ws, rid, table(2500))
        assert ref == f"s3://satchel-moto/r/{ws}/{rid}.parquet"
        assert st.read_page(ref, 1999, 3)[1] == [(1999, "row-1999"), (2000, "row-2000"),
                                                 (2001, "row-2001")]
        assert st.delete(ref) and not st.exists(ref) and not st.delete(ref)
        with pytest.raises(FileNotFoundError):
            st.read_page(ref, 0, 1)
    objstore.reset_clients()


def test_s3_custom_endpoint_uses_path_style(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("boto3")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://localhost:4566")
    c = s3.make_client()
    assert c.meta.endpoint_url == "http://localhost:4566"
    assert c.meta.config.s3["addressing_style"] == "path"
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3")
    assert s3.make_client().meta.config.s3["addressing_style"] == "auto"


def test_result_dir_scheme_selects_the_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for scheme in objstore.SCHEMES:
        monkeypatch.setenv("SATCHEL_RESULT_DIR", f"{scheme}://{BUCKET}/some/prefix")
        st = configured_store()
        assert isinstance(st, ObjectResultStore)
        assert (st.scheme, st.prefix) == (scheme, "some/prefix")
    monkeypatch.setenv("SATCHEL_RESULT_DIR", str(tmp_path))
    assert isinstance(configured_store(), LocalResultStore)


def test_run_sql_then_get_result_across_two_replicas(
    env: Env, bucket: MemoryBucket, result_dir: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setenv("SATCHEL_RESULT_DIR", f"gs://{BUCKET}/prefix")
    q = "list orders"
    before = {p for p in result_dir.rglob("*") if p.is_file()}

    async def replica_a(c: ClientSession) -> tuple[str, str]:
        await c.initialize()
        sid = await start(c, q)
        r = await call(c, "run_sql", session_id=sid, question=q,
                       sql="select order_id, amount from analytics.orders order by order_id")
        return sid, rid_of(text_of(r))

    sid, rid = with_client(env, replica_a)
    with admin_session(engine=env.engine) as s:
        row = s.get(QueryResult, uuid.UUID(rid))
        assert row is not None and row.path.startswith(f"gs://{BUCKET}/prefix/")
        key = row.path.removeprefix(f"gs://{BUCKET}/")
        assert key in bucket.objects and row.row_count == 700
    assert {p for p in result_dir.rglob("*") if p.is_file()} == before  # nothing on local disk

    # "Replica B": a different server instance, same bucket, no shared memory or disk.
    bucket.range_reads.clear()

    async def replica_b(c: ClientSession) -> str:
        await c.initialize()
        p = await call(c, "get_result", session_id=sid, result_id=rid, offset=100, limit=3)
        return text_of(p)

    page = with_client(env, replica_b)
    assert "rows 101-103 of 700" in page and "| 101 |" in page
    assert bucket.range_reads  # served by ranged reads

    # and the module-level helper agrees, with two independent store instances
    with admin_session(engine=env.engine) as s:
        row = s.get(QueryResult, uuid.UUID(rid))
        assert row is not None
        a = read_page(row, 690, 50, store=ObjectResultStore("gs", BUCKET, "prefix", bucket=bucket))
        b = read_page(row, 690, 50, store=ObjectResultStore("gs", BUCKET, "prefix", bucket=bucket))
        assert a == b and len(a[1]) == 10


# ------------------------------------------------------------------------------------- purge


def _add_result(engine: Engine, ws_id: uuid.UUID, store: Any, *, expires: datetime) -> QueryResult:
    with admin_session(engine=engine) as s:
        row = store_result(s, workspace_id=ws_id, session_id=None, tool_call_id=None,
                           connection_id=None, sql="select 1", table=table(5), truncated=False,
                           store=store)
        row.expires_at = expires
        s.flush()
        s.expunge(row)
        return row


def _ws(engine: Engine) -> uuid.UUID:
    with admin_session(engine=engine) as s:
        w = Workspace(slug=unique_slug("purge"), name="p")
        s.add(w)
        s.flush()
        return w.id


def _exists(engine: Engine, rid: uuid.UUID) -> bool:
    with admin_session(engine=engine) as s:
        return s.get(QueryResult, rid) is not None


@pytest.mark.parametrize("backend", ["local", "gs", "s3"])
def test_purge_expired_deletes_rows_and_objects(
    app_engine: Engine, tmp_path: Path, bucket: MemoryBucket, backend: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if backend == "local":
        monkeypatch.setenv("SATCHEL_RESULT_DIR", str(tmp_path))
        store: Any = LocalResultStore(tmp_path)
    else:
        monkeypatch.setenv("SATCHEL_RESULT_DIR", f"{backend}://{BUCKET}/p")
        store = ObjectResultStore(backend, BUCKET, "p", bucket=bucket)
    ws = _ws(app_engine)
    now = datetime.now(UTC)
    old1 = _add_result(app_engine, ws, store, expires=now - timedelta(days=2))
    old2 = _add_result(app_engine, ws, store, expires=now - timedelta(seconds=5))
    fresh = _add_result(app_engine, ws, store, expires=now + timedelta(days=1))
    gone = _add_result(app_engine, ws, store, expires=now - timedelta(days=1))
    store.delete(gone.path)  # object already missing: the row must still go
    mine = {old1.id, old2.id, gone.id}

    dry = purge_expired(engine=app_engine, dry_run=True)
    assert dry.rows >= 3 and all(_exists(app_engine, i) for i in mine)
    assert store.exists(old1.path)

    rep = purge_expired(engine=app_engine)
    assert rep.rows >= 3 and rep.failed == 0
    assert not any(_exists(app_engine, i) for i in mine) and _exists(app_engine, fresh.id)
    assert not store.exists(old1.path) and not store.exists(old2.path)
    assert store.exists(fresh.path)
    assert purge_expired(engine=app_engine).rows == 0  # idempotent


def test_purge_keeps_the_row_when_the_object_cannot_be_deleted(
    app_engine: Engine, bucket: MemoryBucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SATCHEL_RESULT_DIR", f"gs://{BUCKET}/p")
    ws = _ws(app_engine)
    row = _add_result(app_engine, ws, ObjectResultStore("gs", BUCKET, "p", bucket=bucket),
                      expires=datetime.now(UTC) - timedelta(days=1))

    def boom(name: str) -> bool:
        raise OSError("storage unavailable")

    monkeypatch.setattr(bucket, "delete", boom)
    rep = purge_expired(engine=app_engine)
    assert rep.failed == 1 and _exists(app_engine, row.id)
    monkeypatch.undo()
    objstore.register_bucket("gs", BUCKET, bucket)
    monkeypatch.setenv("SATCHEL_RESULT_DIR", f"gs://{BUCKET}/p")
    assert purge_expired(engine=app_engine).failed == 0
    assert not _exists(app_engine, row.id)


def test_results_purge_cli_and_worker_pass(
    app_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_test_engines(monkeypatch, app_engine)
    monkeypatch.setenv("SATCHEL_RESULT_DIR", str(tmp_path))
    ws = _ws(app_engine)
    st = LocalResultStore(tmp_path)
    past = datetime.now(UTC) - timedelta(days=3)
    a = _add_result(app_engine, ws, st, expires=past)
    res = CliRunner().invoke(cli, ["results", "purge", "--dry-run"])
    assert res.exit_code == 0 and "would purge" in res.output and _exists(app_engine, a.id)
    res = CliRunner().invoke(cli, ["results", "purge"])
    assert res.exit_code == 0 and "purged" in res.output and not _exists(app_engine, a.id)
    assert not st.exists(a.path)

    # the worker loop purges on every pass
    from satchel.worker import main as worker

    b = _add_result(app_engine, ws, st, expires=past)
    monkeypatch.setattr(worker, "run_once", lambda **_k: [])
    monkeypatch.setattr(worker, "sync_sources", lambda *_a: None)  # other tests' sources
    monkeypatch.setattr(worker, "run_evals", lambda *_a: None)  # other tests' suites
    worker.run_forever(once=True)
    assert not _exists(app_engine, b.id) and not st.exists(b.path)


# -------------------------------------------------------------------------------------- audit


@pytest.fixture
def ws(app_engine: Engine) -> tuple[str, uuid.UUID]:
    slug = unique_slug("aud")
    with admin_session(engine=app_engine) as s:
        w = Workspace(slug=slug, name=slug)
        s.add(w)
        s.flush()
        return slug, w.id


def _events(engine: Engine, ws_id: uuid.UUID) -> list[AuditEvent]:
    with admin_session(engine=engine) as s:
        rows = list(s.scalars(select(AuditEvent).where(AuditEvent.workspace_id == ws_id)
                              .order_by(AuditEvent.id)))
        s.expunge_all()
        return rows


def test_security_relevant_actions_write_audit_events(
    app_engine: Engine, ws: tuple[str, uuid.UUID], tmp_path: Path
) -> None:
    from satchel.cli.ops import ensure_workspace_admin

    slug, wid = ws
    email = f"boss@{slug}.test"
    ensure_workspace_admin(app_engine, slug, email)  # existing workspace, new admin user
    with admin_session(engine=app_engine) as s:  # demote, then re-promote: a role change
        u = s.scalars(select(AppUser).where(AppUser.email == email)).one()
        u.role = "analyst"
    ensure_workspace_admin(app_engine, slug, email)
    raw, tid = create_token(slug, f"new@{slug}.test", "laptop", role="curator", engine=app_engine)
    create_token(slug, f"new@{slug}.test", "second", engine=app_engine)
    assert revoke_token(token_id=tid, engine=app_engine) == 1
    assert revoke_token(raw=raw, engine=app_engine) == 0  # already revoked: no new event
    add_connection(slug, "wh", "duckdb", {"path": str(tmp_path / "x.duckdb")},
                   secret_ref="env:WH_PASS", engine=app_engine)
    add_connection(slug, "wh", "duckdb", {"path": str(tmp_path / "y.duckdb")}, engine=app_engine)
    add_source(slug, "dbt", "jaffle", {"manifest": str(tmp_path / "m.json")}, engine=app_engine)
    assert remove_source(slug, "jaffle", engine=app_engine) == 0

    ev = _events(app_engine, wid)
    assert [e.action for e in ev] == [
        "user.create", "user.role_change", "user.create", "token.create", "token.create",
        "token.revoke", "connection.add", "connection.update", "source.add", "source.remove",
    ]
    by = {e.action: e for e in ev}
    assert by["user.role_change"].target["from"] == "analyst"
    assert by["user.role_change"].target["to"] == "admin"
    assert by["token.revoke"].target["name"] == "laptop"
    assert by["token.create"].target["role"] == "curator"
    assert by["connection.add"].target["secret_ref"] == "env:WH_PASS"
    assert raw not in json.dumps([e.target for e in ev])  # never the credential


def test_workspace_create_is_audited(app_engine: Engine) -> None:
    from satchel.cli.ops import ensure_workspace_admin

    slug = unique_slug("mk")
    wid, _uid, _ = ensure_workspace_admin(app_engine, slug, f"a@{slug}.test")
    assert [e.action for e in _events(app_engine, wid)] == ["workspace.create", "user.create"]


@pytest.fixture
def api(app_engine: Engine) -> TestClient:
    return TestClient(create_app(app_engine, HashEmbedder(), settings=Settings(dev_auth=True)))


def _seed_events(engine: Engine, ws_id: uuid.UUID, n: int, start: datetime) -> uuid.UUID:
    actor = uuid.uuid4()
    with admin_session(engine=engine) as s:
        s.add(AppUser(id=actor, workspace_id=ws_id, role="admin", email=f"{actor}@x.test"))
        s.flush()
        for i in range(n):
            s.add(AuditEvent(workspace_id=ws_id, actor_user_id=actor, action=f"test.e{i}",
                             target={"i": i}, created_at=start + timedelta(minutes=i)))
    return actor


def test_audit_api_keyset_pagination_filters_and_rls(
    api: TestClient, app_engine: Engine, ws: tuple[str, uuid.UUID]
) -> None:
    slug, wid = ws
    other_slug = unique_slug("other")
    with admin_session(engine=app_engine) as s:
        other = Workspace(slug=other_slug, name="o")
        s.add(other)
        s.flush()
        other_id = other.id
    t0 = datetime(2030, 5, 1, 23, 50, tzinfo=UTC)
    actor = _seed_events(app_engine, wid, 7, t0)
    _seed_events(app_engine, other_id, 3, t0)
    h = {"X-Satchel-Workspace": slug, "X-Satchel-User": str(actor)}

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"since": "2030-05-01T00:00:00Z", "limit": 3}
        if cursor:
            params["cursor"] = cursor
        r = api.get("/v1/audit", params=params, headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        pages += 1
        seen += [e["action"] for e in body["events"]]
        assert all(e["workspace"] == slug for e in body["events"])  # RLS + scope
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3 and seen == [f"test.e{i}" for i in range(7)]  # ordered, no gaps/dupes
    first = api.get("/v1/audit", params={"since": "2030-05-01T00:00:00Z", "limit": 1},
                    headers=h).json()["events"][0]
    assert first["actor_email"] == f"{actor}@x.test" and first["target"] == {"i": 0}

    r = api.get("/v1/audit", params={"since": "2030-05-01T23:52:00Z",
                                     "until": "2030-05-01T23:55:00+00:00"}, headers=h)
    assert [e["action"] for e in r.json()["events"]] == ["test.e2", "test.e3", "test.e4"]
    assert api.get("/v1/audit", params={"cursor": "garbage"}, headers=h).status_code == 422
    assert api.get("/v1/audit", params={"since": "yesterday"}, headers=h).status_code == 422


# ----------------------------------------------------------------------------------- export


def _jsonl(data: bytes | str) -> list[dict[str, Any]]:
    text = data.decode() if isinstance(data, bytes) else data
    return [json.loads(x) for x in text.splitlines()]


def test_export_to_file_and_cli(
    app_engine: Engine, ws: tuple[str, uuid.UUID], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug, wid = ws
    _seed_events(app_engine, wid, 5, datetime(2031, 1, 1, 10, 0, tzinfo=UTC))
    out = tmp_path / "audit.jsonl"
    rep = audit.export_jsonl(since=datetime(2031, 1, 1, 10, 1, tzinfo=UTC),
                             until=datetime(2031, 1, 1, 10, 4, tzinfo=UTC), workspace=slug,
                             to=str(out), engine=app_engine)
    rows = _jsonl(out.read_text())
    assert rep.events == 3 and [r["action"] for r in rows] == ["test.e1", "test.e2", "test.e3"]
    assert rows[0]["workspace"] == slug and rows[0]["created_at"].startswith("2031-01-01T10:01")

    use_test_engines(monkeypatch, app_engine)
    out2 = tmp_path / "cli.jsonl"
    res = CliRunner().invoke(cli, ["audit", "export", "--since", "2031-01-01", "--workspace",
                                   slug, "--to", str(out2)])
    assert res.exit_code == 0, res.output
    assert len(_jsonl(out2.read_text())) == 5
    bad = CliRunner().invoke(cli, ["audit", "export", "--since", "nonsense"])
    assert bad.exit_code == 1
    unknown = CliRunner().invoke(cli, ["audit", "export", "--since", "2031-01-01", "-w", "nope-x"])
    assert unknown.exit_code == 1


@pytest.mark.parametrize("scheme", objstore.SCHEMES)
def test_export_to_object_store_one_object_per_day_is_idempotent(
    app_engine: Engine, ws: tuple[str, uuid.UUID], bucket: MemoryBucket,
    monkeypatch: pytest.MonkeyPatch, scheme: str,
) -> None:
    slug, wid = ws
    # 3 events late on Jan 1 (UTC), 3 on Jan 2: two days
    _seed_events(app_engine, wid, 3, datetime(2032, 1, 1, 23, 58, tzinfo=UTC))
    _seed_events(app_engine, wid, 3, datetime(2032, 1, 2, 5, 0, tzinfo=UTC))
    k1 = f"exports/audit/{slug}/2032/01/01.jsonl"
    k2 = f"exports/audit/{slug}/2032/01/02.jsonl"

    rep = audit.export_jsonl(since=datetime(2032, 1, 1, tzinfo=UTC), workspace=slug,
                             to=f"{scheme}://{BUCKET}/exports", engine=app_engine)
    assert rep.events >= 6 and set(rep.files or []) == {f"{scheme}://{BUCKET}/{k1}",
                                                          f"{scheme}://{BUCKET}/{k2}"}
    assert set(bucket.objects) == {k1, k2}
    day1, day2 = _jsonl(bucket.objects[k1]), _jsonl(bucket.objects[k2])
    assert len(day1) == 2 and len(day2) == 4  # 23:58, 23:59 | 00:00 (e2), 05:00 x3
    snapshot = dict(bucket.objects)

    # Re-run mid-day: widened to whole days, files rewritten identically (never truncated).
    audit.export_jsonl(since=datetime(2032, 1, 2, 5, 30, tzinfo=UTC), workspace=slug,
                       to=f"{scheme}://{BUCKET}/exports", engine=app_engine)
    assert bucket.objects == snapshot
    # New event on Jan 2 shows up in that day's file only.
    with admin_session(engine=app_engine) as s:
        s.add(AuditEvent(workspace_id=wid, action="late", target={},
                         created_at=datetime(2032, 1, 2, 20, 0, tzinfo=UTC)))
    audit.export_jsonl(since=datetime(2032, 1, 1, tzinfo=UTC), workspace=slug,
                       to=f"{scheme}://{BUCKET}/exports", engine=app_engine)
    assert bucket.objects[k1] == snapshot[k1]
    assert _jsonl(bucket.objects[k2])[-1]["action"] == "late"

    # The CLI goes through open_bucket(), which the fixture pointed at the fake.
    use_test_engines(monkeypatch, app_engine)
    res = CliRunner().invoke(cli, ["audit", "export", "--since", "2032-01-01", "-w", slug,
                                   "--to", f"{scheme}://{BUCKET}/viaCli"])
    assert res.exit_code == 0, res.output
    assert f"viaCli/audit/{slug}/2032/01/02.jsonl" in " ".join(bucket.objects)


def test_workspace_session_still_sees_only_its_events(
    app_engine: Engine, ws: tuple[str, uuid.UUID]
) -> None:
    slug, wid = ws
    _seed_events(app_engine, wid, 2, datetime(2033, 1, 1, tzinfo=UTC))
    with workspace_session(uuid.uuid4(), None, engine=app_engine) as s:
        assert audit.fetch_page(s)[0] == []
