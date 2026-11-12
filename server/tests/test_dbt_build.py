"""dbt sources built from a git repository with ``dbt deps`` + ``dbt parse`` on the server
(satchel.sources.dbt_build): config rules, the sandboxed process (environment, limits, timeout,
output caps, preflight checks) with a fake dbt, and, when a real dbt is installed
(``SATCHEL_DBT_BIN`` or /opt/dbt/bin/dbt), the whole flow on the Jaffle Shop dbt project."""

from __future__ import annotations

import json
import os
import resource
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from satchel.settings import Settings
from satchel.sources import dbt_build, git
from satchel.sources.config import SourceConfigError, validate_config
from satchel.sources.dbt_build import BuildOptions, DbtBuildError
from satchel.sources.registry import get_entry
from tests.test_git_sources import sh
from tests.test_settings_admin import DBT, Env, env  # noqa: F401 - fixture

PROJECT = Path(__file__).resolve().parents[2] / "examples" / "jaffle-shop" / "dbt"


def _real_dbt() -> str | None:
    cand = os.environ.get("SATCHEL_DBT_BIN") or dbt_build.DEFAULT_BIN
    return str(dbt_build.find_dbt(cand)) if dbt_build.find_dbt(cand) else None


REAL_DBT = _real_dbt()
needs_dbt = pytest.mark.skipif(REAL_DBT is None, reason="no dbt (set SATCHEL_DBT_BIN)")

# A stand-in for dbt: records what it was run with, then acts out the mode in <script>.mode.
FAKE = """#!{python}
import json, os, resource, sys, time
from pathlib import Path
here = Path(__file__)
mode = here.with_suffix(".mode").read_text().strip() if here.with_suffix(".mode").exists() else "ok"
args = sys.argv[1:]
if args[0] == "--version":
    print({{"fusion": "dbt-fusion 2.0.0-preview.212",
           "old": "Core:\\n  - installed: 1.7.4\\n\\nPlugins:\\n  - duckdb: 1.7.1",
           "noduck": "Core:\\n  - installed: 1.12.6\\n\\nPlugins:\\n  - postgres: 1.10.0",
           "garbage": "hello"}}.get(mode, "Core:\\n  - installed: 1.12.6\\n  - latest: 1.12.6\\n\\n"
                                     "Plugins:\\n  - duckdb: 1.11.0 - Up to date!"))
    sys.exit(0)
with here.with_suffix(".calls").open("a") as f:
    f.write(args[0] + "\\n")
if args[0] == "deps":
    sys.exit(0)
if mode == "sleep":
    time.sleep(60)
if mode == "fail":
    print("Running with dbt=9.9.9")
    print("Compilation Error in model orders (" + os.getcwd() + "/models/orders.sql)")
    sys.exit(2)
if mode == "noisy":
    sys.stdout.write("x" * 5_000_000 + "\\nlast line\\n")
    sys.exit(1)
target = Path(args[args.index("--target-path") + 1])
manifest = json.loads(Path({manifest!r}).read_text())
manifest["metadata"]["dbt_version"] = "9.9.9"
manifest["_probe"] = {{
    "env": dict(os.environ), "argv": args, "cwd": os.getcwd(),
    "fsize": resource.getrlimit(resource.RLIMIT_FSIZE)[0],
    "core": resource.getrlimit(resource.RLIMIT_CORE)[0],
    "profiles": (Path(os.environ["DBT_PROFILES_DIR"]) / "profiles.yml").read_text(),
}}
(target / "manifest.json").write_text(json.dumps(manifest))
if mode != "nosemantic":
    (target / "semantic_manifest.json").write_text(Path({semantic!r}).read_text())
"""


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    exe = tmp_path / "bin" / "dbt"
    exe.parent.mkdir()
    exe.write_text(FAKE.format(python=sys.executable, manifest=str(DBT / "manifest.json"),
                               semantic=str(DBT / "semantic_manifest.json")))
    exe.chmod(0o755)
    return exe


def mode(exe: Path, m: str) -> None:
    exe.with_suffix(".mode").write_text(m)


def calls(exe: Path) -> list[str]:
    p = exe.with_suffix(".calls")
    return p.read_text().split() if p.exists() else []


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout with the Jaffle Shop dbt project in ``dbt/``."""
    root = tmp_path / "repo"
    shutil.copytree(PROJECT, root / "dbt", ignore=shutil.ignore_patterns("target", "logs",
                                                                         "*.duckdb"))
    return root


OPTS = BuildOptions(project_dir="dbt", target_schema="analytics")


def probe(built: dbt_build.Built) -> dict[str, Any]:
    return dict(json.loads(built.files["manifest"])["_probe"])


# ----------------------------------------------------------------------------- config rules


def check(kind: str, cfg: dict[str, Any]) -> None:
    validate_config(get_entry(kind), cfg)


GIT = {"git_url": "https://github.com/acme/analytics.git"}


def test_project_dir_config_is_valid() -> None:
    check("dbt", {**GIT, "dbt_project_dir": "dbt", "dbt_target_schema": "analytics",
                  "dbt_skip_deps": True, "dbt_vars": {"start_date": "2024-01-01", "n": 3}})
    check("dbt", {**GIT, "manifest": "target/manifest.json"})  # the committed-manifest mode


@pytest.mark.parametrize(("kind", "cfg", "msg"), [
    ("dbt", {**GIT, "dbt_project_dir": "dbt", "dbt_target_schema": "a",
             "manifest": "target/manifest.json"}, "not both"),
    ("dbt", {**GIT, "dbt_project_dir": "dbt", "dbt_target_schema": "a",
             "semantic_manifest": "target/semantic_manifest.json"}, "not both"),
    ("dbt", {**GIT, "dbt_project_dir": "dbt"}, "dbt_target_schema is required"),
    ("dbt", {"dbt_project_dir": "dbt", "dbt_target_schema": "a"}, "needs git_url"),
    ("dbt", {**GIT, "dbt_skip_deps": True}, "needs dbt_project_dir"),
    ("dbt", {**GIT, "dbt_project_dir": "../x", "dbt_target_schema": "a"}, "inside"),
    ("dbt", {**GIT, "dbt_project_dir": "dbt", "dbt_target_schema": "a b"}, "dbt_target_schema"),
    ("dbt", {**GIT, "dbt_project_dir": "dbt", "dbt_target_schema": "a",
             "dbt_vars": {"api_token": "x"}}, "looks like a secret"),
    ("dbt", {**GIT, "dbt_project_dir": "dbt", "dbt_target_schema": "a",
             "dbt_vars": {"bad name": 1}}, "not a valid variable name"),
    ("cube", {**GIT, "dbt_project_dir": "dbt", "models": "model"}, "only dbt sources"),
])
def test_config_rules(kind: str, cfg: dict[str, Any], msg: str) -> None:
    with pytest.raises(SourceConfigError, match=msg):
        check(kind, cfg)


def create(env: Env, config: dict[str, Any], name: str = "d") -> Any:  # noqa: F811
    return env.post("/v1/sources", {"kind": "dbt", "name": name, "config": config})


def test_rest_rules(env: Env) -> None:  # noqa: F811
    neither = create(env, {**GIT})
    assert neither.status_code == 422 and "set either the manifest path" in neither.json()["detail"]
    both = create(env, {**GIT, "manifest": "t/m.json", "dbt_project_dir": "dbt",
                        "dbt_target_schema": "analytics"})
    assert both.status_code == 422 and "not both" in both.json()["detail"]
    bucket = create(env, {"dbt_project_dir": "dbt", "dbt_target_schema": "analytics"})
    assert bucket.status_code == 422 and "needs git_url" in bucket.json()["detail"]
    ok = create(env, {**GIT, "dbt_project_dir": "./dbt/", "dbt_target_schema": "analytics",
                      "dbt_skip_deps": True, "dbt_vars": {"region": "eu"}})
    assert ok.status_code == 201, ok.text
    cfg = ok.json()["config"]
    assert cfg["dbt_project_dir"] == "dbt" and cfg["dbt_skip_deps"] is True
    assert cfg["dbt_vars"] == {"region": "eu"} and "manifest" not in cfg
    kinds = {k["kind"]: k for k in env.client.get("/v1/source-kinds", headers=env.h()).json()}
    assert set(kinds["dbt"]["dbt_parse"]) == {"available", "message"}
    assert kinds["cube"]["dbt_parse"] is None


# ----------------------------------------------------------------------------- the process


def test_environment_is_built_from_scratch(monkeypatch: pytest.MonkeyPatch,
                                           tmp_path: Path) -> None:
    for k, v in {"SATCHEL_ENCRYPTION_KEY": "k", "SATCHEL_DATABASE_URL": "postgresql://x",
                 "DATABASE_URL": "postgresql://x", "AWS_SECRET_ACCESS_KEY": "a",
                 "AWS_SESSION_TOKEN": "t", "GOOGLE_APPLICATION_CREDENTIALS": "/k.json",
                 "AZURE_CLIENT_SECRET": "s", "PGPASSWORD": "p", "DBT_ENV_SECRET_X": "s",
                 "PYTHONPATH": "/evil", "LD_PRELOAD": "/evil.so",
                 "HTTPS_PROXY": "http://proxy:3128"}.items():
        monkeypatch.setenv(k, v)
    senv = dbt_build.sandbox_env(tmp_path, Path("/opt/dbt/bin/dbt"))
    assert senv["HOME"] == str(tmp_path / "home") and senv["TMPDIR"] == str(tmp_path / "tmp")
    assert senv["DBT_SEND_ANONYMOUS_USAGE_STATS"] == "false" and senv["DO_NOT_TRACK"] == "1"
    assert senv["PATH"].split(os.pathsep)[0] == "/opt/dbt/bin"
    assert senv["HTTPS_PROXY"] == "http://proxy:3128"  # dbt deps behind a proxy
    leaked = {k for k in senv if k.startswith(("SATCHEL_", "AWS_", "GOOGLE_", "AZURE_", "PG",
                                              "PYTHONPATH", "LD_"))
              or k in ("DATABASE_URL", "DBT_ENV_SECRET_X")}
    assert not leaked


def test_build_with_a_fake_dbt(monkeypatch: pytest.MonkeyPatch, fake: Path,
                               checkout: Path) -> None:
    monkeypatch.setenv("SATCHEL_ENCRYPTION_KEY", "server-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    built = dbt_build.build(checkout, BuildOptions(project_dir="dbt", target_schema="analytics",
                                                   vars={"region": "eu"}), dbt_bin=str(fake))
    assert built.dbt_version == "9.9.9" and set(built.files) == {"manifest", "semantic_manifest"}
    p = probe(built)
    assert "server-secret" not in json.dumps(p) and "aws-secret" not in json.dumps(p)
    assert not any(k.startswith(("SATCHEL_", "AWS_")) for k in p["env"])
    assert Path(p["cwd"]).resolve() == (checkout / "dbt").resolve()
    args = p["argv"]
    assert args[0] == "parse" and args[args.index("--profile") + 1] == "satchel"
    assert args[args.index("--target") + 1] == "prod" and "--no-partial-parse" in args
    assert json.loads(args[args.index("--vars") + 1]) == {"region": "eu"}
    profile = json.loads(p["profiles"])["satchel"]
    assert profile["outputs"]["prod"] == {"type": "duckdb", "path": ":memory:",
                                          "schema": "analytics", "threads": 1}
    if sys.platform.startswith("linux") or p["fsize"] != resource.RLIM_INFINITY:
        assert p["fsize"] == dbt_build.LIMIT_FSIZE
    assert p["core"] == 0
    assert calls(fake) == ["parse"]  # no packages.yml: no dbt deps


def test_deps_runs_only_with_packages_and_can_be_skipped(fake: Path, checkout: Path) -> None:
    (checkout / "dbt" / "packages.yml").write_text(
        "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n")
    dbt_build.build(checkout, OPTS, dbt_bin=str(fake))
    assert calls(fake) == ["deps", "parse"]
    fake.with_suffix(".calls").unlink()
    dbt_build.build(checkout, BuildOptions(project_dir="dbt", target_schema="a", skip_deps=True),
                    dbt_bin=str(fake))
    assert calls(fake) == ["parse"]


def test_semantic_manifest_is_optional(fake: Path, checkout: Path) -> None:
    mode(fake, "nosemantic")
    assert set(dbt_build.build(checkout, OPTS, dbt_bin=str(fake)).files) == {"manifest"}


def test_failure_shows_the_log_tail_without_local_paths(fake: Path, checkout: Path) -> None:
    mode(fake, "fail")
    with pytest.raises(DbtBuildError) as e:
        dbt_build.build(checkout, OPTS, dbt_bin=str(fake))
    msg = str(e.value)
    assert msg.startswith("dbt parse failed (exit 2)") and "Compilation Error" in msg
    assert "<project>/models/orders.sql" in msg and str(checkout) not in msg


def test_output_is_capped(fake: Path, checkout: Path) -> None:
    mode(fake, "noisy")
    with pytest.raises(DbtBuildError) as e:
        dbt_build.build(checkout, OPTS, dbt_bin=str(fake))
    assert len(str(e.value)) < 1700 and "last line" in str(e.value)


def test_timeout_kills_dbt(fake: Path, checkout: Path) -> None:
    mode(fake, "sleep")
    with pytest.raises(DbtBuildError, match="timed out after 1 s"):
        dbt_build.build(checkout, OPTS, dbt_bin=str(fake), timeout=1)


def test_manifest_size_is_capped(monkeypatch: pytest.MonkeyPatch, fake: Path,
                                 checkout: Path) -> None:
    monkeypatch.setattr(dbt_build, "MAX_MANIFEST_BYTES", 1000)
    with pytest.raises(DbtBuildError, match="larger than"):
        dbt_build.build(checkout, OPTS, dbt_bin=str(fake))


@pytest.mark.parametrize(("m", "msg"), [
    ("fusion", "is dbt Fusion 2.0.0-preview.212; Satchel builds manifests with dbt Core"),
    ("old", "is dbt Core 1.7.4; Satchel needs dbt Core 1.8 or later"),
    ("noduck", "without the dbt-duckdb plugin"),
    ("garbage", "did not report a dbt Core version"),
])
def test_only_dbt_core_with_duckdb_is_used(fake: Path, checkout: Path, m: str, msg: str) -> None:
    mode(fake, m)
    st = dbt_build.status(str(fake))
    assert st.available is False and msg in st.message and st.version is None
    with pytest.raises(DbtBuildError, match=msg.split(";")[0].replace(".", r"\.")):
        dbt_build.build(checkout, OPTS, dbt_bin=str(fake))
    assert calls(fake) == []  # refused before deps or parse


def test_version_is_reported_and_probed_once(fake: Path, checkout: Path) -> None:
    st = dbt_build.status(str(fake))
    assert st.available and st.version == "1.12.6"
    assert "dbt Core 1.12.6, dbt-duckdb 1.11.0" in st.message
    mode(fake, "fusion")  # same binary (path and mtime): the cached answer stands
    assert dbt_build.status(str(fake)).available
    # A replaced binary (new mtime) is probed again.
    os.utime(fake, ns=(fake.stat().st_atime_ns, fake.stat().st_mtime_ns + 1_000_000_000))
    assert dbt_build.status(str(fake)).available is False


def test_missing_dbt_and_project(tmp_path: Path, fake: Path, checkout: Path) -> None:
    with pytest.raises(DbtBuildError, match="dbt is not installed on this server"):
        dbt_build.build(checkout, OPTS, dbt_bin=str(tmp_path / "nope"))
    assert dbt_build.status(str(tmp_path / "nope")).available is False
    assert dbt_build.status(str(fake)).available is True
    with pytest.raises(DbtBuildError, match="no dbt_project.yml in dbt/models"):
        dbt_build.build(checkout, BuildOptions(project_dir="dbt/models", target_schema="a"),
                        dbt_bin=str(fake))


@pytest.mark.parametrize(("file", "text", "msg"), [
    ("dbt_project.yml", "name: x\nmodel-paths: ['/etc']\n", "relative path inside"),
    ("dbt_project.yml", "name: x\nmacro-paths: ['../../../..']\n", "outside the repository"),
    ("dbt_project.yml", "name: x\ndocs-paths: [\"{{ env_var('D') }}\"]\n", "Jinja"),
    ("packages.yml", "packages:\n  - local: /var/run/secrets\n", "relative path inside"),
    ("packages.yml", "packages:\n  - local: ../../outside\n", "outside the repository"),
    ("packages.yml", "packages:\n  - git: http://github.com/o/r.git\n", "https://"),
    ("packages.yml", "packages:\n  - git: git@github.com:o/r.git\n", "https://"),
    ("packages.yml", "packages:\n  - tarball: https://169.254.169.254/x.tgz\n", "link-local"),
    ("packages.yml", "packages:\n  - git: \"https://{{ env_var('T') }}@github.com/o/r\"\n",
     "Jinja"),
    ("dependencies.yml", "packages:\n  - private: acme/pkg\n", "private packages"),
])
def test_preflight_refuses_paths_and_urls_outside(fake: Path, checkout: Path, file: str,
                                                   text: str, msg: str) -> None:
    (checkout / "dbt" / file).write_text(text)
    with pytest.raises(DbtBuildError, match=msg):
        dbt_build.build(checkout, OPTS, dbt_bin=str(fake))
    assert calls(fake) == []  # dbt never ran


def test_preflight_accepts_paths_inside(checkout: Path) -> None:
    (checkout / "shared").mkdir()
    (checkout / "dbt" / "packages.yml").write_text(
        "packages:\n  - local: ../shared\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n"
        "  - git: https://github.com/dbt-labs/dbt-codegen.git\n    revision: 0.13.1\n")
    assert dbt_build.preflight(checkout, checkout / "dbt") is True
    (checkout / "dbt" / "packages.yml").unlink()
    assert dbt_build.preflight(checkout, checkout / "dbt") is False


# ----------------------------------------------------------------------------- load_snapshot


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    """A git repository holding the Jaffle Shop dbt project (no target/), plus a model that uses
    ``env_var`` with a default and ``var``."""
    monkeypatch.setattr(git, "_ALLOW_FILE_FOR_TESTS", True)
    src = tmp_path / "src"
    shutil.copytree(PROJECT, src / "analytics" / "dbt",
                    ignore=shutil.ignore_patterns("target", "logs", "*.duckdb", "profiles.yml"))
    (src / "analytics" / "dbt" / "macros").mkdir()
    (src / "analytics" / "dbt" / "macros" / "region.sql").write_text(
        "{% macro region() %}{{ return(env_var('SATCHEL_TEST_REGION', 'fallback_region')) }}"
        "{% endmacro %}\n")
    (src / "analytics" / "dbt" / "models" / "regional_orders.sql").write_text(
        "{{ config(tags=[region(), var('order_tag', 'no_var')]) }}\n"
        "select * from {{ ref('orders') }}\n")
    (src / "README.md").write_text("hello\n")
    sh(src, "init", "-q", "-b", "main")
    sh(src, "add", "-A")
    sh(src, "commit", "-q", "-m", "init")
    yield f"file://{src}", sh(src, "rev-parse", "HEAD")


def test_sync_with_a_fake_dbt(env: Env, repo: tuple[str, str], fake: Path,  # noqa: F811
                              monkeypatch: pytest.MonkeyPatch) -> None:
    from satchel.sources.service import load_snapshot

    url, head = repo
    r = create(env, {"git_url": url, "dbt_project_dir": "analytics/dbt",
                     "dbt_target_schema": "analytics"})
    assert r.status_code == 201, r.text
    loaded = load_snapshot(env.src("d"), settings=Settings(dbt_bin=str(fake)))
    assert loaded.git_commit == head and loaded.dbt_version == "9.9.9"
    assert loaded.snapshot.summary()["metrics"] == 3
    # an uploaded manifest wins: no build
    load_snapshot(env.src("d"), uploads={"manifest": (DBT / "manifest.json").read_bytes()},
                  settings=Settings(dbt_bin=str(fake)))
    assert calls(fake) == ["parse"]


# ----------------------------------------------------------------------------- real dbt


@needs_dbt
def test_real_dbt_parse_end_to_end(env: Env, repo: tuple[str, str],  # noqa: F811
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SATCHEL_TEST_REGION", "leaked_from_server")
    monkeypatch.setattr(dbt_build, "DEFAULT_BIN", str(REAL_DBT))  # app settings: dbt_bin unset
    url, head = repo
    r = create(env, {"git_url": url, "dbt_project_dir": "analytics/dbt",
                     "dbt_target_schema": "analytics", "dbt_vars": {"order_tag": "from_vars"}})
    assert r.status_code == 201, r.text
    sync = env.client.post(f"/v1/sources/{r.json()['id']}/sync?wait=true", headers=env.h())
    assert sync.status_code == 200, sync.text
    body = sync.json()
    assert body["git_commit"] == head and body["dbt_version"]
    assert body["counts"]["created"] > 0
    assert env.src("d").last_report["dbt_version"] == body["dbt_version"]


@needs_dbt
def test_real_dbt_manifest_contents(checkout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SATCHEL_TEST_REGION", "leaked_from_server")
    (checkout / "dbt" / "macros").mkdir()
    (checkout / "dbt" / "macros" / "region.sql").write_text(
        "{% macro region() %}{{ return(env_var('SATCHEL_TEST_REGION', 'fallback_region')) }}"
        "{% endmacro %}\n")
    (checkout / "dbt" / "models" / "regional_orders.sql").write_text(
        "{{ config(tags=[region(), var('order_tag', 'no_var')]) }}\nselect 1 as x\n")
    built = dbt_build.build(checkout, BuildOptions(project_dir="dbt", target_schema="analytics",
                                                   vars={"order_tag": "from_vars"}),
                            dbt_bin=REAL_DBT)
    m = json.loads(built.files["manifest"])
    node = m["nodes"]["model.jaffle_shop.regional_orders"]
    assert sorted(node["tags"]) == ["fallback_region", "from_vars"]  # server env not visible
    assert node["schema"] == "analytics"
    assert m["nodes"]["model.jaffle_shop.orders"]["description"]
    assert json.loads(built.files["semantic_manifest"])["metrics"]
    assert built.dbt_version == m["metadata"]["dbt_version"]


@needs_dbt
def test_real_dbt_errors_are_clear(checkout: Path) -> None:
    models = checkout / "dbt" / "models"
    (models / "needs_env.sql").write_text("select '{{ env_var('SATCHEL_UNSET_VAR') }}' as x\n")
    with pytest.raises(DbtBuildError) as e:
        dbt_build.build(checkout, OPTS, dbt_bin=REAL_DBT)
    assert "dbt parse failed" in str(e.value) and "SATCHEL_UNSET_VAR" in str(e.value)
    (models / "needs_env.sql").write_text("select {{ ref( }}\n")
    with pytest.raises(DbtBuildError, match="dbt parse failed"):
        dbt_build.build(checkout, OPTS, dbt_bin=REAL_DBT)


def test_cli_flags() -> None:
    from satchel.cli.source_cmds import parse_flags

    cfg = parse_flags(get_entry("dbt"), [
        "--git-url", "https://github.com/acme/a.git", "--dbt-project-dir", "dbt",
        "--dbt-target-schema", "analytics", "--dbt-skip-deps", "--dbt-vars", '{"n": 1}'])
    assert cfg == {"git_url": "https://github.com/acme/a.git", "dbt_project_dir": "dbt",
                   "dbt_target_schema": "analytics", "dbt_skip_deps": True, "dbt_vars": {"n": 1}}
    check("dbt", cfg)
