"""Sources read from a git repository: URL/ref/path validation, the locked-down sparse checkout,
REST create/sync/test, and secrets. The repository is a local one served as ``file://`` (a
test-only switch; production accepts https only)."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from satchel.secrets import is_sealed
from satchel.sources import git
from tests.test_settings_admin import DBT, Env, env  # noqa: F401 - fixture

SQLMESH = Path(__file__).parent / "fixtures" / "sqlmesh" / "project"
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"]}


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    """A repository with a SQLMesh project, a committed dbt target/ and a symlink that points
    at a server file. Yields (file:// URL, head commit)."""
    monkeypatch.setattr(git, "_ALLOW_FILE_FOR_TESTS", True)
    src = tmp_path / "src"
    shutil.copytree(SQLMESH, src / "analytics" / "sqlmesh")
    (src / "dbt" / "target").mkdir(parents=True)
    shutil.copy(DBT / "manifest.json", src / "dbt" / "target" / "manifest.json")
    (src / "README.md").write_text("hello\n")
    secret = tmp_path / "server-secret.txt"
    secret.write_text("TOP SECRET\n")
    os.symlink(secret, src / "analytics" / "sqlmesh" / "models" / "leak.sql")
    sh(src, "init", "-q", "-b", "main")
    sh(src, "-c", "core.symlinks=true", "add", "-A")
    sh(src, "commit", "-q", "-m", "init")
    sh(src, "tag", "v1")
    yield f"file://{src}", sh(src, "rev-parse", "HEAD")


# ----------------------------------------------------------------------------- validation


@pytest.mark.parametrize("url", [
    "http://github.com/o/r.git", "ssh://git@github.com/o/r.git", "git@github.com:o/r.git",
    "https://user:pw@github.com/o/r.git", "https://169.254.169.254/x.git",
    "https://metadata.google.internal/x.git", "https://github.com/o/r.git?x=1",
    "file:///etc", "ext::sh -c id",
])
def test_bad_urls_are_refused(url: str) -> None:
    with pytest.raises(git.GitSourceError):
        git.validate_url(url)


@pytest.mark.parametrize("ref", ["-x", "--upload-pack=touch /tmp/x", "a..b", "a b", "x.lock",
                                 "a//b", "/main"])
def test_bad_refs_are_refused(ref: str) -> None:
    with pytest.raises(git.GitSourceError):
        git.validate_ref(ref)


def test_paths_stay_inside_the_repository() -> None:
    assert git.validate_path("") == "." and git.validate_path("./") == "."
    assert git.validate_path("./a//b/") == "a/b"
    for p in ("/etc/passwd", "../x", "a/../../x", "C:/x", ".git/config", "a/.git"):
        with pytest.raises(git.GitSourceError):
            git.validate_path(p)


def test_loopback_hosts_are_refused() -> None:
    with pytest.raises(git.GitSourceError, match="loopback"):
        git._pin("https://127.0.0.1/r.git")


# ----------------------------------------------------------------------------- checkout


def test_sparse_checkout_reads_only_what_is_asked(repo: tuple[str, str]) -> None:
    url, head = repo
    with git.checkout(git.GitSpec(url=url, ref="main"), ["analytics/sqlmesh"]) as co:
        root = co.root
        assert co.commit == head
        assert (root / "analytics" / "sqlmesh" / "config.yaml").is_file()
        assert not (root / "README.md").exists() and not (root / "dbt").exists()
        leak = root / "analytics" / "sqlmesh" / "models" / "leak.sql"
        assert not leak.is_symlink() and "TOP SECRET" not in leak.read_text()
    assert not root.exists()  # removed on exit
    with git.checkout(git.GitSpec(url=url, ref="v1"), ["."]) as co:
        assert (co.root / "README.md").is_file() and co.commit == head


def test_resolve_and_errors(repo: tuple[str, str]) -> None:
    url, head = repo
    assert git.resolve(git.GitSpec(url=url)) == head
    assert git.resolve(git.GitSpec(url=url, ref="v1")) == head
    with pytest.raises(git.GitSourceError, match="no branch or tag"):
        git.resolve(git.GitSpec(url=url, ref="nope"))
    with (pytest.raises(git.GitSourceError, match="fetch failed"),
          git.checkout(git.GitSpec(url=url, ref="nope"), ["."])):
        pass


# ----------------------------------------------------------------------------- REST


def create(env: Env, kind: str, config: dict[str, Any], name: str = "g") -> Any:  # noqa: F811
    return env.post("/v1/sources", {"kind": kind, "name": name, "config": config})


def test_sqlmesh_source_from_git_syncs(env: Env, repo: tuple[str, str]) -> None:  # noqa: F811
    url, head = repo
    r = create(env, "sqlmesh", {"git_url": url, "git_ref": "main",
                                "project": "analytics/sqlmesh", "git_token": "tok-123456"})
    assert r.status_code == 201, r.text
    assert r.json()["config"]["git_token"] == "***"
    assert is_sealed(env.src("g").config["git_token"])
    sync = env.client.post(f"/v1/sources/{r.json()['id']}/sync", headers=env.h("curator"))
    assert sync.status_code == 200, sync.text
    assert sync.json()["git_commit"] == head and sync.json()["counts"]["created"] > 0
    assert env.src("g").last_report["git_commit"] == head


def test_dbt_manifest_committed_to_git(env: Env, repo: tuple[str, str]) -> None:  # noqa: F811
    url, _ = repo
    r = create(env, "dbt", {"git_url": url, "manifest": "dbt/target/manifest.json"})
    assert r.status_code == 201, r.text
    sync = env.client.post(f"/v1/sources/{r.json()['id']}/sync", headers=env.h())
    assert sync.status_code == 200, sync.text

    # A missing path is a clear sync error naming the repository path.
    env.client.put(f"/v1/sources/{r.json()['id']}", headers=env.h(), json={
        "config": {"git_url": url, "manifest": "dbt/target/nope.json"}})
    bad = env.client.post(f"/v1/sources/{r.json()['id']}/sync", headers=env.h())
    assert bad.status_code == 422, bad.text
    assert "dbt/target/nope.json in the repository" in bad.json()["detail"]


@pytest.mark.parametrize(("config", "msg"), [
    ({"git_url": "REPO", "manifest": "s3://b/m.json"}, "path inside it"),
    ({"git_url": "REPO", "manifest": "/etc/passwd"}, "not an absolute path"),
    ({"git_url": "REPO", "manifest": "../m.json"}, "inside the repository"),
    ({"git_url": "REPO", "git_ref": "--upload-pack=x"}, "git_ref"),
    ({"git_url": "http://example.com/r.git"}, "https://"),
    ({"git_ref": "main"}, "needs git_url"),
    ({"manifest": "target/manifest.json"}, "s3:// or gs://"),
])
def test_rest_rules_for_git(env: Env, repo: tuple[str, str],  # noqa: F811
                            config: dict[str, Any], msg: str) -> None:
    cfg = {k: (repo[0] if v == "REPO" else v) for k, v in config.items()}
    r = create(env, "dbt", cfg)
    assert r.status_code == 422 and msg in r.json()["detail"], r.text


def test_git_test_endpoint(env: Env, repo: tuple[str, str]) -> None:  # noqa: F811
    url, head = repo
    ok = env.post("/v1/sources/git/test", {"url": url, "ref": "main"})
    assert ok.status_code == 200 and ok.json() == {"ok": True, "commit": head, "error": None}
    bad = env.post("/v1/sources/git/test", {"url": url, "ref": "nope"})
    assert bad.json()["ok"] is False and "nope" in bad.json()["error"]
    assert env.post("/v1/sources/git/test", {"url": url}, role="curator").status_code == 403
    # Saved source: the stored token is used when none is sent.
    sid = create(env, "sqlmesh", {"git_url": url, "project": "analytics/sqlmesh",
                                  "git_token": "tok-123456"}).json()["id"]
    again = env.post("/v1/sources/git/test", {"url": url, "source_id": sid})
    assert again.json()["ok"] is True


def test_token_goes_in_a_header_and_is_scrubbed(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    spec = git.GitSpec(url="https://git.example.com/o/r.git", token="sekret-token-42")
    genv = git._env(spec, tmp_path, "git.example.com:443:203.0.113.5")
    values = [genv[k] for k in genv if k.startswith("GIT_CONFIG_VALUE_")]
    assert any(v.startswith("Authorization: Basic ") for v in values)
    assert "git.example.com:443:203.0.113.5" in values
    assert all("sekret-token-42" not in v for v in values)  # base64, not the raw token

    def fake_run(*a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(a, 128, "", "fatal: bad sekret-token-42 401")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(git.GitSourceError) as e:
        git._run(["fetch"], genv, tmp_path, spec, 5)
    assert "sekret-token-42" not in str(e.value) and "authentication failed" in str(e.value)


def test_removing_the_repository_drops_its_token(env: Env, repo: tuple[str, str]) -> None:  # noqa: F811
    url, _ = repo
    sid = create(env, "dbt", {"git_url": url, "git_token": "tok-123456",
                              "manifest": "dbt/target/manifest.json"}).json()["id"]
    assert "git_token" in env.src("g").config
    r = env.client.put(f"/v1/sources/{sid}", headers=env.h(), json={
        "config": {"manifest": "s3://artifacts/m.json", "git_token": "***"}})
    assert r.status_code == 200, r.text
    assert "git_token" not in env.src("g").config and "git_url" not in env.src("g").config


def test_cli_keeps_repository_paths_and_refuses_tokens() -> None:
    from satchel.cli.source_cmds import parse_flags
    from satchel.sources.registry import get_entry

    entry = get_entry("sqlmesh")
    cfg = parse_flags(entry, ["--git-url", "https://github.com/acme/a.git", "--project", "mesh"])
    assert cfg["project"] == "mesh"  # not resolved to a local absolute path
    assert parse_flags(entry, ["--project", "mesh"])["project"].startswith("/")
    with pytest.raises(Exception, match="unknown option --git-token"):
        parse_flags(entry, ["--git-token", "x"])
