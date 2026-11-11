"""Context repo uploads for CI (``/v1/sync/preview``, ``/v1/sync/import``): pull-request previews
leave drafts and what is served alone, merging reuses their versions, the publish gate runs on
them, and unsafe bundles are refused."""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select

from satchel.store.db import admin_session
from satchel.store.models import ContextItem, ContextVersion, Release
from satchel.sync.bundle import BundleError, unpacked
from tests.helpers import EXAMPLE_CONTEXT, build_jaffle_db
from tests.test_evals import Env, make_env

GLOSSARY = "glossary/business-terms.yaml"
NEW_GUIDE = "guides/loyalty-programme.md"
GONE = "queries/refund-rate.yaml"
MARKER = "Zanzibar loyalty points"


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_jaffle_db(tmp_path_factory.mktemp("wh") / "jaffle.duckdb")


@pytest.fixture(autouse=True)
def _result_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SATCHEL_RESULT_DIR", str(tmp_path / "results"))


@pytest.fixture
def env(app_engine: Engine, tmp_path: Path, warehouse: Path) -> Env:
    return make_env(app_engine, tmp_path, warehouse, chat=None, prefix="upl")


def pr_folder(tmp_path: Path, *, gate: bool = False) -> Path:
    """The example repo with one changed, one new and one deleted file."""
    dest = tmp_path / "pr"
    shutil.copytree(tmp_path / "context", dest)
    g = dest / GLOSSARY
    g.write_text(g.read_text().replace("Revenue", "Revenue (gross)", 1))
    (dest / NEW_GUIDE).write_text(
        "---\nname: Loyalty programme\ndescription: How loyalty points work.\n---\n\n"
        f"# Loyalty\n\nMembers earn {MARKER} per order.\n")
    (dest / GONE).unlink()
    return dest


def tgz(folder: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(folder, arcname=".")
    return buf.getvalue()


def upload(env: Env, path: str, data: bytes, role: str = "curator", **fields: str) -> Any:
    return env.client.post(path, files={"bundle": ("context.tar.gz", data)},
                           data={"ref": "refs/pull/7/merge@abc1234", **fields},
                           headers=env.h(role))


def versions(env: Env, key: str) -> int:
    with admin_session(engine=env.engine) as s:
        return s.scalar(select(func.count()).select_from(ContextVersion).join(
            ContextItem, ContextItem.id == ContextVersion.item_id).where(
            ContextItem.workspace_id == env.ws_id, ContextItem.key == key)) or 0


def test_preview_changes_nothing_that_is_served_or_drafted(env: Env, tmp_path: Path) -> None:
    head = env.head()
    r = upload(env, "/v1/sync/preview", tgz(pr_folder(tmp_path)))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] == [NEW_GUIDE] and out["removed"] == [GONE]
    assert any(k.startswith(GLOSSARY) for k in out["updated"]), out
    assert out["preview_id"] and out["eval_suites_changed"] == []

    # what is served and drafted is unchanged
    assert env.head() == head
    assert env.client.get("/v1/changes", headers=env.h("curator")).json()["changes"] == []
    lib = env.client.get("/v1/items", params={"limit": 500}, headers=env.h("explorer")).json()
    assert NEW_GUIDE not in {i["key"] for i in lib["items"]}

    # the preview serves the new guide and drops the deleted query
    hits = env.client.post(f"/v1/previews/{out['preview_id']}/search", json={"query": MARKER},
                           headers=env.h("curator")).json()
    assert hits and hits[0]["key"] == NEW_GUIDE
    pv = env.client.get(f"/v1/previews/{out['preview_id']}", headers=env.h("curator")).json()
    assert pv["ref"] == "refs/pull/7/merge@abc1234" and GONE in pv["removed_keys"]

    # published by merging, not from the UI
    pub = env.client.post(f"/v1/previews/{out['preview_id']}/publish", headers=env.h("admin"))
    assert pub.status_code == 409 and "pull request merges" in pub.json()["detail"]

    # the same folder again reuses the versions
    before = versions(env, NEW_GUIDE)
    again = upload(env, "/v1/sync/preview", tgz(tmp_path / "pr")).json()
    assert again["preview_id"] != out["preview_id"] and versions(env, NEW_GUIDE) == before


def test_unchanged_folder_makes_no_preview(env: Env, tmp_path: Path) -> None:
    out = upload(env, "/v1/sync/preview", tgz(tmp_path / "context")).json()
    assert out["preview_id"] is None and out["unchanged"] > 0
    assert out["created"] == out["updated"] == out["removed"] == []


def test_merge_import_reuses_the_preview_versions(env: Env, tmp_path: Path) -> None:
    folder = pr_folder(tmp_path)
    pv = upload(env, "/v1/sync/preview", tgz(folder)).json()
    n_guide, n_gloss = versions(env, NEW_GUIDE), versions(env, pv["updated"][0])

    assert upload(env, "/v1/sync/import", tgz(folder), role="curator").status_code == 403
    r = upload(env, "/v1/sync/import", tgz(folder), role="admin")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["release_created"] and NEW_GUIDE in out["created"] and GONE in out["deprecated"]
    assert pv["updated"][0] in out["updated"]
    assert (versions(env, NEW_GUIDE), versions(env, pv["updated"][0])) == (n_guide, n_gloss)

    with admin_session(engine=env.engine) as s:
        item = s.scalars(select(ContextItem).where(ContextItem.workspace_id == env.ws_id,
                                                   ContextItem.key == NEW_GUIDE)).one()
        assert item.state == "published" and item.current_version_id == item.published_version_id
        preview = s.get(Release, pv["preview_id"])
        assert preview is not None and preview.kind == "preview"
    lib = env.client.get("/v1/items", params={"limit": 500}, headers=env.h("explorer")).json()
    assert NEW_GUIDE in {i["key"] for i in lib["items"]}
    hits = env.client.post("/v1/search", json={"query": MARKER}, headers=env.h("explorer")).json()
    assert hits[0]["key"] == NEW_GUIDE


def test_a_draft_after_a_preview_gets_the_next_version_number(env: Env, tmp_path: Path) -> None:
    pv = upload(env, "/v1/sync/preview", tgz(pr_folder(tmp_path))).json()
    key = pv["updated"][0]
    n = versions(env, key)
    # an import of the main branch (not the PR's content) still numbers after the preview
    main = tmp_path / "main"
    shutil.copytree(tmp_path / "context", main)
    g = main / GLOSSARY
    g.write_text(g.read_text().replace("Revenue", "Revenue (net of nothing)", 1))
    assert upload(env, "/v1/sync/import", tgz(main), role="admin").status_code == 200
    assert versions(env, key) == n + 1


def test_gate_runs_on_a_pull_request_preview(
    app_engine: Engine, tmp_path: Path, warehouse: Path
) -> None:
    e = make_env(app_engine, tmp_path, warehouse, gate=True, chat=None, prefix="uplg")
    pv = upload(e, "/v1/sync/preview", tgz(pr_folder(tmp_path))).json()
    assert pv["gate_required"] == ["retrieval"]
    g = e.client.post(f"/v1/previews/{pv['preview_id']}/evals", json={"wait": True},
                      headers=e.h("analyst"))
    assert g.status_code == 200, g.text
    assert g.json()["status"] in ("pass", "fail")  # the runs completed
    assert [x["status"] for x in g.json()["suites"]] == ["completed"]


def test_changed_eval_suites_are_reported(env: Env, tmp_path: Path) -> None:
    folder = pr_folder(tmp_path)
    suite = folder / "evals/retrieval.yaml"
    suite.write_text(suite.read_text().replace("top_n: 5", "top_n: 3", 1))
    out = upload(env, "/v1/sync/preview", tgz(folder)).json()
    assert out["eval_suites_changed"] == ["retrieval"]


def _tar(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def _file(name: str, data: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def test_unsafe_bundles_are_refused(env: Env) -> None:
    link = tarfile.TarInfo("evil")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    for data, msg in ((_tar(_file("../escape.md")), "unsafe path"),
                      (_tar(_file("/abs.md")), "unsafe path"),
                      (_tar((link, None)), "only files and folders"),
                      (b"not a tarball", "not a .tar.gz")):
        with pytest.raises(BundleError, match=msg), unpacked(data):
            pass
        r = upload(env, "/v1/sync/preview", data)
        assert r.status_code == 422 and msg in r.json()["detail"], r.text
    assert env.client.post("/v1/sync/preview", json={}, headers=env.h("curator")
                           ).status_code == 415
    assert upload(env, "/v1/sync/preview", _tar(_file("x.md")), role="analyst").status_code == 403


def test_a_lone_top_level_folder_is_the_repo(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(EXAMPLE_CONTEXT, arcname="context")
    with unpacked(buf.getvalue()) as root:
        assert (root / "satchel.config.yaml").is_file()
