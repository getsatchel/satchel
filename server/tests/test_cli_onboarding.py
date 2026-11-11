"""CLI onboarding: config file, init/connect/disconnect/doctor/status and the schema guard.

Everything runs against a tmp HOME/XDG and the testcontainers Postgres; Docker itself is never
started (``init --database-url``) or is mocked (doctor).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from satchel.cli import clients, config, doctor, infra, ops
from satchel.cli.main import app
from satchel.settings import Settings
from satchel.store.db import admin_session, get_engine
from satchel.store.models import ApiToken, Workspace
from satchel.store.schema import alembic_config, guard_message, head_revision
from tests.helpers import unique_slug

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_satchel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [k for k in os.environ if k.startswith("SATCHEL_") and k != "SATCHEL_CONFIG"]:
        monkeypatch.delenv(k)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_DATA_HOME", str(h / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(h / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / "xdg"))
    monkeypatch.delenv("SATCHEL_CONFIG", raising=False)
    return h


def _url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)


# ---- config file ----------------------------------------------------------------------------


def test_config_path_resolution(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.config_path() == home / "xdg" / "satchel" / "config.toml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config.config_path() == home / ".config" / "satchel" / "config.toml"
    monkeypatch.setenv("SATCHEL_CONFIG", str(home / "x.toml"))
    assert config.config_path() == home / "x.toml"


def test_settings_precedence_env_over_file_over_default(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.save_profile({
        "database_url": "postgresql+psycopg://file@h/db",
        "workspace": "from-file",
        "embedding_provider": "hash",
        "chat": {"api_key_env": "MY_LLM_KEY"},
    })
    config.save_profile({"workspace": "other"}, name="work")
    s = Settings()
    assert s.database_url == "postgresql+psycopg://file@h/db"  # file beats default
    assert s.embedding_provider == "hash"
    assert s.result_dir == "~/.cache/satchel/results"  # default where the file is silent
    monkeypatch.setenv("SATCHEL_DATABASE_URL", "postgresql+psycopg://env@h/db")
    assert Settings().database_url == "postgresql+psycopg://env@h/db"  # env beats file
    assert Settings(database_url="postgresql+psycopg://init@h/db").database_url.endswith("init@h/db")
    # profiles
    assert Settings().workspace == "from-file"
    monkeypatch.setenv("SATCHEL_PROFILE", "work")
    assert Settings().workspace == "other"
    assert Settings().embedding_provider == "fastembed"  # the other profile does not leak
    monkeypatch.delenv("SATCHEL_PROFILE")


def test_chat_key_is_read_from_the_named_env_var_and_never_stored(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.save_profile({"chat": {"api_key_env": "MY_LLM_KEY"}})
    assert Settings().chat_api_key is None
    monkeypatch.setenv("MY_LLM_KEY", "sk-secret-123")
    assert Settings().chat_api_key == "sk-secret-123"
    assert "sk-secret-123" not in config.config_path().read_text()
    monkeypatch.setenv("SATCHEL_CHAT_API_KEY", "explicit")
    assert Settings().chat_api_key == "explicit"  # env still wins


def test_config_file_is_0600_and_roundtrips(home: Path) -> None:
    config.save_profile({"token": 'satchel_pat_a"b\\c', "workspace": "w"})
    config.save_profile({"workspace": None, "chat": {"api_key_env": "K"}})  # None deletes
    p = config.config_path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    prof = config.get_profile()
    assert prof["token"] == 'satchel_pat_a"b\\c'
    assert "workspace" not in prof
    assert prof["chat"] == {"api_key_env": "K"}
    config.save_profile({"workspace": "x"}, name="other")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert config.get_profile()["token"]  # the first profile survived


def test_broken_config_is_an_error_not_silence(home: Path) -> None:
    p = config.config_path()
    p.parent.mkdir(parents=True)
    p.write_text("this is = = not toml")
    with pytest.raises(config.ConfigError):
        config.read_config()


# ---- connect / disconnect -------------------------------------------------------------------


@pytest.fixture
def fake_exe(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    exe = home / "bin" / "satchel"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(clients, "resolve_satchel_executable", lambda: str(exe))
    monkeypatch.setattr(clients, "claude_cli", lambda: None)
    return exe


def test_connect_desktop_merges_backs_up_and_keeps_other_servers(
    home: Path, fake_exe: Path
) -> None:
    path = clients.config_path("claude-desktop")
    path.parent.mkdir(parents=True)
    other = {"command": "npx", "args": ["other"]}
    path.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": other}}))

    long = "satchel_pat_" + "x" * 30
    r = runner.invoke(app, ["connect", "claude-desktop", "--token", long])
    assert r.exit_code == 0, r.output
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and data["mcpServers"]["other"] == other
    entry = data["mcpServers"]["satchel"]
    assert entry["command"] == str(fake_exe)
    assert entry["args"] == ["mcp", "stdio"]
    assert entry["env"] == {"SATCHEL_TOKEN": long}  # URL etc. come from config.toml
    backups = list(path.parent.glob("claude_desktop_config.json.bak-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text())["mcpServers"] == {"other": other}
    assert long not in r.output  # the token is masked in the printed summary

    # running again replaces only the satchel entry, with another backup
    r = runner.invoke(app, ["connect", "claude-desktop", "--token", "satchel_pat_y"])
    assert r.exit_code == 0, r.output
    data = json.loads(path.read_text())
    assert data["mcpServers"]["satchel"]["env"]["SATCHEL_TOKEN"] == "satchel_pat_y"
    assert data["mcpServers"]["other"] == other
    assert len(list(path.parent.glob("claude_desktop_config.json.bak-*"))) == 2


def test_connect_refuses_a_dev_checkout_path(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = home / "satchel" / "server"
    (proj / ".venv" / "bin").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("[project]\nname='satchel'\n")
    dev = proj / ".venv" / "bin" / "satchel"
    dev.write_text("")
    monkeypatch.setattr(clients, "resolve_satchel_executable", lambda: str(dev))
    assert clients.is_dev_executable(dev)

    path = clients.config_path("claude-desktop")
    r = runner.invoke(app, ["connect", "claude-desktop", "--token", "t"])
    assert r.exit_code != 0 and "dev checkout" in r.output
    assert not path.exists()
    r = runner.invoke(app, ["connect", "claude-desktop", "--token", "t", "--dev"])
    assert r.exit_code == 0, r.output
    assert json.loads(path.read_text())["mcpServers"]["satchel"]["command"] == str(dev)


def test_connect_print_changes_nothing(home: Path, fake_exe: Path) -> None:
    r = runner.invoke(app, ["connect", "cursor", "--print"])
    assert r.exit_code == 0, r.output
    assert str(fake_exe) in r.output and "mcpServers" in r.output
    assert not clients.config_path("cursor").exists()


def test_connect_http_variants(home: Path, fake_exe: Path) -> None:
    r = runner.invoke(app, ["connect", "cursor", "--http", "http://h:8080/mcp", "--token", "tk"])
    assert r.exit_code == 0, r.output
    cur = json.loads(clients.config_path("cursor").read_text())["mcpServers"]["satchel"]
    assert cur == {"url": "http://h:8080/mcp", "headers": {"Authorization": "Bearer tk"}}
    r = runner.invoke(app, ["connect", "claude-desktop", "--http", "http://h/mcp", "--token", "tk"])
    assert r.exit_code == 0, r.output
    d = json.loads(clients.config_path("claude-desktop").read_text())["mcpServers"]["satchel"]
    assert d["command"] == "npx" and "mcp-remote" in d["args"]
    assert d["env"]["SATCHEL_AUTH"] == "Bearer tk"


def test_connect_claude_code_falls_back_to_dot_claude_json(home: Path, fake_exe: Path) -> None:
    p = clients.config_path("claude-code")
    p.write_text(json.dumps({"projects": {"/x": {}}, "mcpServers": {}}))
    r = runner.invoke(app, ["connect", "claude-code", "--token", "t"])
    assert r.exit_code == 0, r.output
    d = json.loads(p.read_text())
    assert d["projects"] == {"/x": {}} and d["mcpServers"]["satchel"]["type"] == "stdio"


def test_connect_refuses_to_overwrite_invalid_json(home: Path, fake_exe: Path) -> None:
    p = clients.config_path("cursor")
    p.parent.mkdir(parents=True)
    p.write_text("{ not json")
    r = runner.invoke(app, ["connect", "cursor", "--token", "t"])
    assert r.exit_code != 0 and "not valid JSON" in r.output
    assert p.read_text() == "{ not json"


def test_claude_add_args_order() -> None:
    e = clients.stdio_entry("/a/satchel", "tok", {"SATCHEL_PROFILE": "p"})
    args = clients.claude_add_args(e, "satchel", "user", http_url=None)
    assert args[:5] == ["mcp", "add", "--scope", "user", "satchel"]
    assert args[-4:] == ["--", "/a/satchel", "mcp", "stdio"] and "SATCHEL_TOKEN=tok" in args


@pytest.fixture
def configured(
    home: Path, app_engine: Engine, migrated: str, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """A config file pointing at the test database with a workspace, admin and token."""
    slug = unique_slug("cli")
    email = f"{slug}@example.com"
    ops.ensure_workspace_admin(app_engine, slug, email)
    tok = ops.mint_token(app_engine, slug, email, "satchel-cli")
    config.save_profile({
        "database_url": _url(app_engine), "migration_database_url": migrated,
        "workspace": slug, "token": tok, "email": email, "embedding_provider": "hash",
        "result_dir": str(home / "results"),
    })
    get_engine.cache_clear()
    return {"slug": slug, "email": email, "token": tok}


def test_connect_mints_a_named_token_and_disconnect_revokes_it(
    configured: dict[str, Any], fake_exe: Path, app_engine: Engine
) -> None:
    slug = configured["slug"]
    r = runner.invoke(app, ["connect", "claude-desktop"])
    assert r.exit_code == 0, r.output
    path = clients.config_path("claude-desktop")
    tok = json.loads(path.read_text())["mcpServers"]["satchel"]["env"]["SATCHEL_TOKEN"]
    assert ops.token_info(app_engine, tok).valid
    # reconnecting revokes the previous token of the same name
    r = runner.invoke(app, ["connect", "claude-desktop"])
    tok2 = json.loads(path.read_text())["mcpServers"]["satchel"]["env"]["SATCHEL_TOKEN"]
    assert tok2 != tok and not ops.token_info(app_engine, tok).valid
    assert ops.token_info(app_engine, tok2).valid
    # disconnect removes the entry and revokes
    r = runner.invoke(app, ["disconnect", "claude-desktop"])
    assert r.exit_code == 0, r.output
    assert "satchel" not in json.loads(path.read_text())["mcpServers"]
    ti = ops.token_info(app_engine, tok2)
    assert not ti.valid and ti.reason == "revoked"
    with admin_session(engine=app_engine) as s:
        live = s.scalar(
            select(func.count()).select_from(ApiToken)
            .join(Workspace, Workspace.id == ApiToken.workspace_id)
            .where(Workspace.slug == slug, ApiToken.name == "claude-desktop",
                   ApiToken.revoked_at.is_(None))
        )
    assert live == 0


# ---- schema guard ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def old_db_url(migrated: str) -> str:
    """A database migrated only to 0005 (behind this build's head)."""
    from alembic import command

    admin = create_engine(migrated, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text("create database oldlam"))
    admin.dispose()
    url = make_url(migrated).set(database="oldlam").render_as_string(hide_password=False)
    command.upgrade(alembic_config(url), "0005")
    return url


def test_guard_message_names_both_revisions(old_db_url: str, migrated: str) -> None:
    eng = create_engine(old_db_url)
    assert guard_message(eng) == (
        f"database is at 0005 but this satchel needs {head_revision()} — run: satchel upgrade"
    )
    eng.dispose()
    ok = create_engine(migrated)
    assert guard_message(ok) is None
    ok.dispose()


def test_guard_on_an_empty_database(migrated: str) -> None:
    admin = create_engine(migrated, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text("create database emptylam"))
    admin.dispose()
    eng = create_engine(make_url(migrated).set(database="emptylam"))
    assert "no satchel schema" in (guard_message(eng) or "")
    eng.dispose()


def test_serve_api_exits_with_the_message(
    old_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SATCHEL_DATABASE_URL", old_db_url)
    get_engine.cache_clear()
    try:
        for cmd in (["serve", "api"], ["serve", "mcp"]):
            r = runner.invoke(app, cmd)
            assert r.exit_code == 1
            assert f"database is at 0005 but this satchel needs {head_revision()} — run: satchel upgrade" in r.output
    finally:
        get_engine.cache_clear()


@pytest.mark.slow
def test_stdio_answers_every_call_with_the_message(old_db_url: str, tmp_path: Path) -> None:
    exe = Path(sys.executable).parent / "satchel"
    if not exe.exists():
        pytest.skip("satchel console script not installed")
    params = StdioServerParameters(
        command=str(exe), args=["mcp", "stdio"], cwd="/",
        env={"SATCHEL_TOKEN": "satchel_pat_anything", "SATCHEL_DATABASE_URL": old_db_url,
             "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )

    async def go() -> list[Any]:
        async with stdio_client(params) as (rd, wr), ClientSession(rd, wr) as c:
            await c.initialize()
            tools = await c.list_tools()
            return [await c.call_tool(t.name, {}) for t in tools.tools] + [
                await c.call_tool("start_analysis", {"question": "q"})
            ]

    results = anyio.run(go)
    assert len(results) == 2
    for r in results:
        assert r.isError
        assert f"database is at 0005 but this satchel needs {head_revision()} — run: satchel upgrade" in str(
            r.content[0].text
        )


def test_migrate_brings_old_database_to_head(old_db_url: str) -> None:
    from satchel.cli.onboarding import migrate

    migrate(old_db_url)
    eng = create_engine(old_db_url)
    assert guard_message(eng) is None
    eng.dispose()


# ---- doctor / status ------------------------------------------------------------------------


def _by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


def test_doctor_all_green_with_docker_mocked(
    configured: dict[str, Any], fake_exe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(infra, "docker_available", lambda: (True, "docker 27.0"))
    checks = _by_name(doctor.run_checks(Settings(), check_clients=False))
    bad = {k: v for k, v in checks.items() if v.status == doctor.FAIL}
    assert not bad, bad
    assert checks["Docker"].status == doctor.OK
    assert "not a superuser" in checks["App role"].detail
    assert "valid for" in checks["Token"].detail
    assert "hash-" in checks["Embedder"].detail


def test_doctor_flags_a_superuser_role_and_a_revoked_token(
    configured: dict[str, Any], migrated: str, app_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(infra, "docker_available", lambda: (False, "docker is not installed"))
    from satchel.mcp.auth import revoke_token

    revoke_token(raw=configured["token"], engine=app_engine)
    checks = _by_name(doctor.run_checks(Settings(database_url=migrated), check_clients=False))
    assert checks["Docker"].status == doctor.WARN  # DB reachable: Docker is optional
    assert checks["App role"].status == doctor.FAIL and "row-level security" in checks["App role"].detail
    assert checks["Token"].status == doctor.FAIL and "revoked" in checks["Token"].detail
    assert checks["Token"].fix

    r = runner.invoke(app, ["doctor", "--no-clients"], env={"SATCHEL_DATABASE_URL": migrated})
    assert r.exit_code == 1
    assert "✗ App role" in r.output and "fix:" in r.output


def test_doctor_reports_an_unreachable_database_with_a_fix(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(infra, "docker_available", lambda: (False, "docker is not installed"))
    s = Settings(database_url="postgresql+psycopg://x:y@127.0.0.1:1/satchel",
                 result_dir=str(home / "r"))
    checks = _by_name(doctor.run_checks(s, check_clients=False))
    assert checks["Database"].status == doctor.FAIL and checks["Database"].fix == "satchel up"
    assert checks["Docker"].status == doctor.FAIL
    assert checks["Schema"].status == doctor.SKIP


def test_doctor_client_config_checks(
    configured: dict[str, Any], fake_exe: Path, home: Path, app_engine: Engine
) -> None:
    s = Settings()
    desk = clients.config_path("claude-desktop")
    desk.parent.mkdir(parents=True)
    proj = home / "dev" / "server"
    (proj / ".venv" / "bin").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("")
    (proj / ".venv" / "bin" / "satchel").write_text("")
    desk.write_text(json.dumps({"mcpServers": {
        "satchel": {"command": str(fake_exe), "args": ["mcp", "stdio"],
                 "env": {"SATCHEL_TOKEN": configured["token"]}},
        "satchel-dev": {"command": "uv", "args": ["run", "--project", str(proj), "satchel", "mcp",
                                               "stdio"], "env": {}},
        "satchel-gone": {"command": str(home / "nope" / "satchel"), "args": ["mcp", "stdio"]},
        "satchel-badtok": {"command": str(fake_exe), "args": ["mcp", "stdio"],
                        "env": {"SATCHEL_TOKEN": "satchel_pat_nope"}},
    }}))
    checks = _by_name(doctor.client_checks(app_engine, s))
    assert checks["Client claude-desktop (satchel)"].status == doctor.OK
    assert checks["Client claude-desktop (satchel-dev)"].status == doctor.WARN
    assert "dev checkout" in checks["Client claude-desktop (satchel-dev)"].detail
    assert checks["Client claude-desktop (satchel-gone)"].status == doctor.FAIL
    bad = checks["Client claude-desktop (satchel-badtok)"]
    assert bad.status == doctor.FAIL and "unknown token" in bad.detail
    assert bad.fix == "satchel connect claude-desktop"


def test_status_json(configured: dict[str, Any]) -> None:
    r = runner.invoke(app, ["status", "--json"])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["database_reachable"] and d["schema"]["state"] == "ok"
    assert d["token"]["valid"] and d["workspace"]["slug"] == configured["slug"]
    assert d["server"]["pid"] is None
    assert "password" not in d["database"].lower().replace("***", "")


# ---- init -----------------------------------------------------------------------------------


def test_init_is_idempotent_and_loads_the_example(
    home: Path, app_engine: Engine, migrated: str
) -> None:
    slug = unique_slug("init")
    args = [
        "init", "--yes", "--database-url", _url(app_engine), "--migration-database-url", migrated,
        "-w", slug, "--email", "admin@example.com", "--embedding", "hash", "--example", "jaffle",
    ]
    r = runner.invoke(app, args)
    assert r.exit_code == 0, r.output
    prof = config.get_profile()
    assert prof["workspace"] == slug and prof["embedding_provider"] == "hash"
    assert prof["database_url"] == _url(app_engine)
    assert stat.S_IMODE(config.config_path().stat().st_mode) == 0o600
    first_token = prof["token"]
    assert ops.token_info(app_engine, first_token).valid
    s1 = ops.workspace_summary(app_engine, slug)
    assert s1.items > 0 and s1.releases == 1 and s1.connections == (("warehouse", "duckdb"),)
    assert s1.embedding_models and s1.embedding_models[0].startswith("hash-")

    r = runner.invoke(app, args)  # again: nothing new
    assert r.exit_code == 0, r.output
    assert "existing token is still valid" in r.output
    assert "already exist" in r.output
    assert config.get_profile()["token"] == first_token
    s2 = ops.workspace_summary(app_engine, slug)
    assert (s2.items, s2.releases, s2.connections) == (s1.items, s1.releases, s1.connections)
    with admin_session(engine=app_engine) as s:
        assert s.scalar(
            select(func.count()).select_from(Workspace).where(Workspace.slug == slug)
        ) == 1


def test_init_mints_a_new_token_when_the_old_one_is_revoked(
    home: Path, app_engine: Engine, migrated: str
) -> None:
    from satchel.mcp.auth import revoke_token

    slug = unique_slug("rot")
    args = ["init", "--yes", "--database-url", _url(app_engine), "--migration-database-url",
            migrated, "-w", slug, "--email", "a@example.com", "--embedding", "hash"]
    assert runner.invoke(app, args).exit_code == 0
    old = config.get_profile()["token"]
    revoke_token(raw=old, engine=app_engine)
    assert runner.invoke(app, args).exit_code == 0
    new = config.get_profile()["token"]
    assert new != old and ops.token_info(app_engine, new).valid


def test_init_reuses_a_healthy_satchel_postgres_without_touching_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(infra, "probe_postgres", lambda port: "satchel")
    monkeypatch.setattr(infra, "compose", lambda *a, **k: calls.append(a) or (0, ""))
    action, detail = infra.ensure_postgres(5433)
    assert action == "reused" and "5433" in detail and not calls

    monkeypatch.setattr(infra, "probe_postgres", lambda port: "other")
    action, detail = infra.ensure_postgres(5433)
    assert action == "error" and "--pg-port" in detail

    monkeypatch.setattr(infra, "probe_postgres", lambda port: "down")
    monkeypatch.setattr(infra, "docker_available", lambda: (False, "docker is not installed"))
    action, detail = infra.ensure_postgres(5433)
    assert action == "error" and "--database-url" in detail and not calls


def test_down_never_purges_without_confirmation(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(infra, "docker_available", lambda: (True, "docker"))
    monkeypatch.setattr(infra, "compose_postgres_running", lambda: True)
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(infra, "compose", lambda *a, **k: seen.append(a) or (0, ""))
    r = runner.invoke(app, ["down", "--purge"], input="n\n")
    assert r.exit_code == 0 and seen == [("stop", "postgres")], (r.output, seen)
    seen.clear()
    r = runner.invoke(app, ["down", "--all"])
    assert seen == [("stop", "postgres")]  # --all alone never uses `down -v`
    seen.clear()
    r = runner.invoke(app, ["down", "--purge"], input="y\n")
    assert seen == [("down", "-v")]


def test_install_spec_and_kind(tmp_path: Path) -> None:
    from satchel.cli.onboarding import INSTALL_SPEC, install_spec

    assert install_spec(None) == INSTALL_SPEC
    repo = tmp_path / "repo"
    (repo / "server").mkdir(parents=True)
    (repo / "server" / "pyproject.toml").write_text("")
    assert install_spec(str(repo)) == str(repo / "server")
    assert install_spec("https://github.com/getsatchel/satchel") == (
        "satchel @ git+https://github.com/getsatchel/satchel#subdirectory=server"
    )


@pytest.mark.slow
def test_up_detach_and_down(
    configured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    r = runner.invoke(app, ["up", "--detach", "--yes", "--port", str(port)])
    try:
        assert r.exit_code == 0, r.output
        info = infra.read_server()
        assert info is not None and infra.healthy(info.url)
        again = runner.invoke(app, ["up", "--detach", "--yes", "--port", str(port)])
        assert "already running" in again.output
        st = json.loads(runner.invoke(app, ["status", "--json"]).output)
        assert st["server"]["healthy"] or st["server"]["pid"] == info.pid
    finally:
        runner.invoke(app, ["down"])
    assert infra.read_server() is None
    assert os.path.exists(infra.logfile())
