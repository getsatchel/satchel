"""Getting-started commands: init, up, down, status, doctor, connect, disconnect, upgrade.

Registered onto the main Typer app by ``register`` (called from ``satchel.cli.main``). The aim is
that a new user needs `uv tool install`, `satchel init`, `satchel connect claude-desktop` and nothing
else: no env vars, no raw URLs, no hand-edited JSON, no remembering to migrate.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from importlib import resources
from importlib.metadata import version as _version
from pathlib import Path
from typing import Annotated, Any

import typer
from alembic import command
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from satchel.cli import clients, doctor, infra, ops
from satchel.cli.config import (
    ConfigError,
    active_profile,
    config_path,
    data_dir,
    get_profile,
    save_profile,
)
from satchel.install_hints import DIST
from satchel.settings import Settings, get_settings
from satchel.store.schema import alembic_config, schema_status

EMBEDDINGS = ("fastembed", "hash", "openai_compat")
DEFAULT_PUBLIC_URL = "http://localhost:8080"
INSTALL_SPEC = DIST  # the PyPI package; it includes the built UI


def say(msg: str = "") -> None:
    typer.echo(msg)


def step(msg: str) -> None:
    typer.secho(f"==> {msg}", bold=True)


def fail(msg: str, code: int = 1) -> typer.Exit:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def jaffle_dir() -> Path | None:
    """The Jaffle Shop example: packaged with the wheel, else the repo checkout."""
    packaged = Path(str(resources.files("satchel.resources"))) / "jaffle-shop"
    if (packaged / "context").is_dir():
        return packaged
    repo = Path(__file__).resolve().parents[3] / "examples" / "jaffle-shop"
    return repo if (repo / "context").is_dir() else None


def _fresh_settings() -> Settings:
    """Settings re-read from env + config file (and the cached engine dropped)."""
    from satchel.store.db import clear_engine_cache

    clear_engine_cache()
    return get_settings()


def _git_email() -> str | None:
    code, out = infra.run(["git", "config", "user.email"], timeout=5)
    return out.strip() or None if code == 0 else None


def _engine(url: str, admin_url: str | None = None) -> Engine:
    """App-role engine; ``admin_session`` on it runs as satchel_worker (``admin_url`` or the
    configured / derived admin URL)."""
    from satchel.store.db import create_app_engine

    return create_app_engine(url, admin_url=admin_url)


def ensure_worker_role(migration_url: str, admin_url: str, *, env: str = "development") -> None:
    """Local installs: give satchel_worker LOGIN + its dev password when migrations created it
    NOLOGIN (an existing pre-0010 database) and the migration user is a superuser."""
    from satchel.store.roles import ensure_local_worker

    if env != "development" or not infra.is_local_url(migration_url):
        return
    try:
        changes = ensure_local_worker(migration_url, admin_url)
    except SQLAlchemyError as e:
        say(f"could not check the satchel_worker role: {str(e).splitlines()[0]}  (run: satchel doctor)")
        return
    for c in changes:
        say(f"satchel_worker: {c}")


def migrate(url: str) -> None:
    """Run migrations to head as the schema owner."""
    command.upgrade(alembic_config(url), "head")


def _port_of(url: str | None) -> int | None:
    if not url or not infra.is_local_url(url):
        return None
    return infra.url_port(url)


# ---------------------------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------------------------


def init(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="No prompts: use defaults")] = False,
    pg_port: Annotated[
        int | None, typer.Option(help="Local Postgres port (default: the profile's, else 5433)")
    ] = None,
    database_url: Annotated[
        str | None,
        typer.Option(help="Use your own Postgres (runtime role URL) instead of starting Docker"),
    ] = None,
    migration_database_url: Annotated[
        str | None, typer.Option(help="Owner URL for migrations (with --database-url)")
    ] = None,
    admin_database_url: Annotated[
        str | None,
        typer.Option(help="satchel_worker URL for server-internal jobs (with --database-url)"),
    ] = None,
    workspace: Annotated[
        str | None, typer.Option("--workspace", "-w", help="Workspace slug")
    ] = None,
    email: Annotated[str | None, typer.Option(help="Admin email (default: git user.email)")] = None,
    embedding: Annotated[
        str | None, typer.Option(help="fastembed (default, local model) | hash (offline) | openai_compat")
    ] = None,
    example: Annotated[
        str | None, typer.Option(help="Load a bundled example: 'jaffle'")
    ] = None,
    context: Annotated[
        Path | None, typer.Option(help="Import this context repo folder into the workspace")
    ] = None,
    public_url: Annotated[str | None, typer.Option(help="External URL of the server")] = None,
    token_name: Annotated[str, typer.Option(help="Label of the CLI's own token")] = "satchel-cli",
    chat_key_env: Annotated[
        str | None,
        typer.Option(help="NAME of the env var holding your LLM key (e.g. ANTHROPIC_API_KEY)"),
    ] = None,
) -> None:
    """Set up satchel on this machine: Postgres, migrations, workspace, admin, token, config.

    Safe to re-run: existing Postgres, workspace, user, valid token and data are reused."""

    def ask(text: str, default: str) -> str:
        return default if yes else str(typer.prompt(text, default=default))

    prof = get_profile()
    profile = active_profile()
    if example not in (None, "jaffle"):
        raise fail(f"unknown example {example!r} (available: jaffle)")
    if embedding is not None and embedding not in EMBEDDINGS:
        raise fail(f"--embedding must be one of {', '.join(EMBEDDINGS)}")

    say(f"satchel init (profile '{profile}', config {config_path()})")
    slug = workspace or prof.get("workspace") or ask("Workspace slug", "main")
    admin_email = email or prof.get("email") or ask(
        "Admin email", _git_email() or "admin@localhost"
    )
    emb = embedding or prof.get("embedding_provider") or ask(
        "Embeddings: fastembed (local model, ~640 MB first use) | hash (offline, instant)",
        "fastembed",
    )
    if emb not in EMBEDDINGS:
        raise fail(f"embeddings must be one of {', '.join(EMBEDDINGS)}")

    # -- Postgres ---------------------------------------------------------------------------
    step("Postgres")
    if database_url:
        app_url = database_url
        mig_url = migration_database_url or prof.get("migration_database_url") or database_url
        from satchel.store.db import derived_admin_url

        adm_url = (
            admin_database_url or prof.get("admin_database_url") or derived_admin_url(app_url)
        )
        say(f"using your database ({make_display(app_url)}); Docker not needed")
    else:
        port = pg_port or _port_of(prof.get("database_url")) or infra.DEFAULT_PG_PORT
        action, detail = infra.ensure_postgres(port)
        if action == "error":
            raise fail(detail)
        say(detail + (" (left untouched)" if action == "reused" else ""))
        app_url, mig_url = infra.pg_urls(port)
        adm_url = infra.worker_url(port)

    # -- schema -----------------------------------------------------------------------------
    step("Database schema")
    try:
        migrate(mig_url)
    except SQLAlchemyError as e:
        raise fail(f"migration failed: {str(e).splitlines()[0]}") from e
    say("schema is at head")
    ensure_worker_role(mig_url, adm_url)

    eng = _engine(app_url, adm_url)
    try:
        step("Workspace, admin user and token")
        _, _, notes = ops.ensure_workspace_admin(eng, slug, admin_email)
        for n in notes:
            say(n)
        if not notes:
            say(f"workspace '{slug}' and admin {admin_email} already exist")
        token = prof.get("token")
        info = ops.token_info(eng, token) if token else None
        if info and info.valid and info.workspace == slug:
            say("existing token is still valid; kept")
        else:
            token = ops.mint_token(eng, slug, admin_email, token_name)
            say(f"minted token '{token_name}'")

        values: dict[str, Any] = {
            "database_url": app_url,
            "admin_database_url": adm_url,
            "migration_database_url": mig_url,
            "workspace": slug,
            "token": token,
            "embedding_provider": emb,
            "public_url": public_url or prof.get("public_url") or DEFAULT_PUBLIC_URL,
            "email": admin_email,
        }
        if chat_key_env:
            values["chat"] = {"api_key_env": chat_key_env}
        # Encrypts LLM API keys added in Settings → Models. Generated once and never replaced:
        # a new key would make every stored secret unreadable.
        if not prof.get("encryption_key") and not os.environ.get("SATCHEL_ENCRYPTION_KEY"):
            from satchel.core.crypto import generate_key

            values["encryption_key"] = generate_key()
            say("generated an encryption key for secrets stored in the database")
        path = save_profile(values)
        say(f"wrote {path} (mode 0600)")
        _fresh_settings()

        # -- content ------------------------------------------------------------------------
        if example or context:
            _load_content(eng, slug, example, context)
    finally:
        eng.dispose()

    say()
    say("Satchel is set up. Next:")
    say("  satchel up                       start the server (UI + REST + MCP) at "
        + (public_url or prof.get("public_url") or DEFAULT_PUBLIC_URL))
    say("  satchel connect claude-desktop   connect Claude Desktop (also: claude-code, cursor)")
    say("  satchel doctor                   check everything")


def _ensure_worker_from(s: Settings) -> None:
    from satchel.store.db import admin_database_url

    if s.env != "development":
        return
    ensure_worker_role(
        s.migration_database_url or s.database_url,
        admin_database_url(s),
        env=s.env,
    )


def make_display(url: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(url).render_as_string(hide_password=True)


def _load_content(eng: Engine, slug: str, example: str | None, ctx: Path | None) -> None:
    from satchel.llm.embeddings import make_embedder
    from satchel.sync import ImportValidationError, import_context

    embedder = make_embedder()

    def do_import(path: Path) -> None:
        try:
            r = import_context(path, slug, embedder=embedder, engine=eng)
        except (ImportValidationError, FileNotFoundError) as e:
            errs = getattr(e, "errors", [str(e)])
            raise fail("import failed: " + "; ".join(map(str, errs))) from e
        c = r.counts()
        say(f"created={c['created']} updated={c['updated']} unchanged={c['unchanged']} "
            f"deprecated={c['deprecated']}; release #{r.release_seq}; "
            f"chunks embedded: {r.chunks_embedded}")

    if example:
        d = jaffle_dir()
        if d is None:
            raise fail("the Jaffle Shop example is not available in this install")
        step("Jaffle Shop example")
        wh = data_dir() / "examples" / "jaffle" / "jaffle.duckdb"
        if not wh.exists():
            import importlib.util

            spec = importlib.util.spec_from_file_location("jaffle_seed", d / "seed.py")
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            wh.parent.mkdir(parents=True, exist_ok=True)
            mod.build_database(wh)
            say(f"seeded warehouse {wh}")
        else:
            say(f"warehouse {wh} already exists; kept")
        do_import(d / "context")
        from satchel.data.connections import add_connection
        from satchel.data.crawl import crawl_connection

        add_connection(slug, "warehouse", "duckdb", {"path": str(wh)}, engine=eng)
        r = crawl_connection(slug, "warehouse", engine=eng)
        say(f"connection 'warehouse' crawled: seen={r.seen} created={r.created} "
            f"updated={r.updated}")
    if ctx:
        step(f"Context repo {ctx}")
        do_import(ctx)


# ---------------------------------------------------------------------------------------------
# up / down / status
# ---------------------------------------------------------------------------------------------


def _ensure_ready(s: Settings, yes: bool) -> None:
    """Start local Postgres if it is down and bring the schema to head (with confirmation)."""
    eng = _engine(s.database_url)
    try:
        try:
            st = schema_status(eng)
        except SQLAlchemyError:
            port = _port_of(s.database_url)
            if port is None:
                raise fail(
                    f"cannot reach {make_display(s.database_url)}. Check database_url in "
                    f"{config_path()} or run: satchel doctor"
                ) from None
            step("Starting Postgres")
            action, detail = infra.ensure_postgres(port)
            if action == "error":
                raise fail(detail) from None
            say(detail)
            st = schema_status(eng)
        if st.state in ("empty", "behind"):
            say(st.message.split(" — ")[0] + ".")
            if not (yes or typer.confirm("Upgrade the database now?", default=True)):
                raise fail("the server needs the upgraded schema: " + st.message)
            migrate(s.migration_database_url or s.database_url)
            say("database upgraded")
        elif st.state == "ahead":
            raise fail(st.message)
        _ensure_worker_from(s)
    finally:
        eng.dispose()


def up(
    detach: Annotated[bool, typer.Option("--detach", "-d", help="Run in the background")] = False,
    host: Annotated[str, typer.Option(help="Bind address")] = "127.0.0.1",
    port: Annotated[int | None, typer.Option(help="Port (default: from public_url, else 8080)")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Upgrade the database without asking")] = False,
) -> None:
    """Start Postgres if needed, check migrations, and run the server (UI + REST + MCP)."""
    from sqlalchemy.engine import make_url

    s = get_settings()
    _ensure_ready(s, yes)
    port = port or make_url(s.public_url).port or 8080
    running = infra.read_server()
    if running and infra.healthy(running.url):
        say(f"already running (pid {running.pid}) at {running.url}")
        return
    url = f"http://localhost:{port}"
    if infra.healthy(url):
        raise fail(f"something is already serving {url}; stop it or choose --port")
    if not os.environ.get("SATCHEL_PUBLIC_URL") and not get_profile().get("public_url"):
        os.environ["SATCHEL_PUBLIC_URL"] = url
    if detach:
        try:
            info = infra.start_detached(host, port)
        except RuntimeError as e:
            raise fail(str(e)) from e
        say(f"Satchel is running in the background (pid {info.pid}) at {info.url}")
        say(f"  MCP {info.url}/mcp   API docs {info.url}/docs")
        say(f"  log {infra.logfile()}   stop with: satchel down")
        return
    say(f"satchel at {url}  (MCP {url}/mcp, API docs {url}/docs)  Ctrl-C to stop")
    from satchel.cli.main import serve_api

    serve_api(host=host, port=port, reload=False)


def down(
    all_: Annotated[bool, typer.Option("--all", help="Also stop Postgres")] = False,
    purge: Annotated[
        bool, typer.Option("--purge", help="Also DELETE satchel's Postgres data volume (asks first)")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the --purge confirmation")] = False,
) -> None:
    """Stop the background server; Postgres only with --all. Data is kept unless --purge."""
    info = infra.stop_server()
    say(f"stopped server (pid {info.pid})" if info else "no background server is running")
    if not (all_ or purge):
        return
    docker_ok, detail = infra.docker_available()
    if not docker_ok:
        raise fail(detail)
    if infra.compose_postgres_running():
        if purge:
            say("Postgres data volume 'satchel_satchel-pgdata' would be permanently deleted.")
            if not (yes or typer.confirm("Delete all satchel data in it?", default=False)):
                say("not purging; stopping Postgres only")
                purge = False
        code, out = infra.compose("down", "-v") if purge else infra.compose("stop", "postgres")
        if code != 0:
            raise fail(out)
        say("Postgres data DELETED" if purge else "Postgres stopped (data kept)")
    else:
        say("no satchel-managed Postgres is running (an existing Postgres you started yourself "
            "is never touched)")


def status(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """Show version, config, database, schema, server, token, workspace and connections."""
    from sqlalchemy.engine import make_url

    s = get_settings()
    rows: dict[str, Any] = {
        "version": _version(DIST),
        "config": str(config_path()) + ("" if config_path().exists() else " (missing: run satchel init)"),
        "profile": active_profile(),
        "database": make_display(s.database_url),
    }
    eng = _engine(s.database_url)
    reachable = False
    try:
        st = schema_status(eng)
        reachable = True
        rows["database_reachable"] = True
        rows["schema"] = {"current": st.current, "head": st.head, "state": st.state}
    except SQLAlchemyError as e:
        rows["database_reachable"] = False
        rows["database_error"] = str(getattr(e, "orig", e)).strip().splitlines()[0]
    srv = infra.read_server()
    url = srv.url if srv else f"http://localhost:{make_url(s.public_url).port or 8080}"
    rows["server"] = {
        "pid": srv.pid if srv else None,
        "url": url,
        "healthy": infra.healthy(url),
    }
    if reachable and rows["schema"]["state"] == "ok":
        if s.token:
            ti = ops.token_info(eng, s.token)
            rows["token"] = {"valid": ti.valid, "reason": ti.reason, "workspace": ti.workspace,
                             "email": ti.email, "user_id": str(ti.user_id),
                             "expires_at": ti.expires_at.isoformat() if ti.expires_at else None}
        if s.workspace:
            w = ops.workspace_summary(eng, s.workspace)
            rows["workspace"] = {"slug": s.workspace, "exists": w.exists, "items": w.items,
                                 "releases": w.releases, "latest_release": w.latest_release,
                                 "connections": [f"{n} ({k})" for n, k in w.connections],
                                 "embedding_models": list(w.embedding_models)}
    eng.dispose()
    rows["embedding_provider"] = s.embedding_provider
    if as_json:
        say(json.dumps(rows, indent=2))
        return
    say(f"satchel {rows['version']}")
    say(f"config      {rows['config']}   profile: {rows['profile']}")
    if reachable:
        sc = rows["schema"]
        sdesc = {"ok": "at head", "behind": "BEHIND: run satchel upgrade",
                 "ahead": "AHEAD of this satchel: run satchel upgrade", "empty": "empty: run satchel init",
                 "unreadable": "unreadable"}[sc["state"]]
        say(f"database    {rows['database']}  reachable")
        say(f"schema      {sc['current']} (head {sc['head']})  {sdesc}")
    else:
        say(f"database    {rows['database']}  UNREACHABLE ({rows['database_error']})  -> satchel up")
    sv = rows["server"]
    say(f"server      {'running' if sv['healthy'] else 'not running'} at {sv['url']}"
        + (f" (pid {sv['pid']})" if sv["pid"] else ""))
    if "token" in rows:
        t = rows["token"]
        say("token       " + (f"valid for {t['email']} in '{t['workspace']}'"
                              + (f", expires {t['expires_at'][:10]}" if t["expires_at"] else "")
                              if t["valid"] else f"INVALID ({t['reason']})  -> satchel init --yes"))
    elif reachable and not s.token:
        say("token       none  -> satchel init")
    if "workspace" in rows:
        w = rows["workspace"]
        if w["exists"]:
            rel = f"#{w['latest_release']}" if w["latest_release"] else "none"
            say(f"workspace   {w['slug']}: {w['items']} items, {w['releases']} releases "
                f"(latest {rel})")
            say("connections " + (", ".join(w["connections"]) or "none"))
        else:
            say(f"workspace   {w['slug']} does not exist  -> satchel init")
    say(f"embeddings  {rows['embedding_provider']}")


# ---------------------------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------------------------


def doctor_cmd(
    no_clients: Annotated[bool, typer.Option("--no-clients", help="Skip client config checks")] = False,
) -> None:
    """Check Docker, database, RLS role, schema, token, embedder, result dir, connections and
    client configs; print the exact fix for each failure. Exits 1 if anything failed."""
    try:
        s = get_settings()
    except ConfigError as e:
        raise fail(str(e)) from e
    say(f"satchel {_version(DIST)}  config {config_path()}  profile {active_profile()}")
    checks = doctor.run_checks(s, check_clients=not no_clients)
    for line in doctor.render(checks):
        say(line)
    bad = [c for c in checks if c.status == doctor.FAIL]
    say()
    say("all checks passed" if not bad else f"{len(bad)} check(s) failed")
    if bad:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------------------------

ClientArg = Annotated[str, typer.Argument(help="claude-desktop | claude-code | cursor")]


def _check_client(client: str) -> None:
    if client not in clients.CLIENTS:
        raise fail(f"unknown client {client!r}; choose from {', '.join(clients.CLIENTS)}")


def connect(
    client: ClientArg,
    token: Annotated[str | None, typer.Option(help="Reuse this token instead of minting one")] = None,
    name: Annotated[str | None, typer.Option(help="Token label (default: the client name)")] = None,
    server_name: Annotated[str, typer.Option(help="Key in the client's mcpServers")] = "satchel",
    print_only: Annotated[
        bool, typer.Option("--print", help="Show the config; change nothing, mint nothing")
    ] = False,
    http: Annotated[
        str | None, typer.Option("--http", help="Connect to a running server URL (…/mcp)")
    ] = None,
    dev: Annotated[
        bool, typer.Option("--dev", help="Allow a uv-run/.venv dev checkout path")
    ] = False,
    scope: Annotated[str, typer.Option(help="claude-code scope: user | local | project")] = "user",
    user: Annotated[str | None, typer.Option(help="Email to mint the token for")] = None,
) -> None:
    """Add satchel to a client's MCP config (backs the file up, merges, mints a named token)."""
    _check_client(client)
    s = get_settings()
    exe = ""
    if not http:
        found = clients.resolve_satchel_executable()
        if found is None:
            raise fail("cannot find the `satchel` executable. Install it: uv tool install "
                       f'"{INSTALL_SPEC}"')
        exe = found
        if clients.is_dev_executable(exe) and not dev:
            raise fail(
                f"{exe} is a dev checkout (.venv): Claude would run uncommitted code against a "
                "database that may not be migrated. Install a stable copy with "
                f'`uv tool install "{INSTALL_SPEC}"` (or --from <path>) and run its `satchel connect`, '
                "or pass --dev if you really mean it."
            )

    if token:
        raw = token
    elif print_only:
        raw = "<token minted by `satchel connect`>"
    else:
        slug = s.workspace
        email = user or get_profile().get("email")
        if not slug or not email:
            raise fail("no workspace/email in the config: run `satchel init`, or pass --token")
        eng = _engine(s.database_url)
        try:
            msg = _guard_or_none(eng)
            if msg:
                raise fail(msg)
            raw = ops.mint_token(eng, slug, email, name or client)
        except SQLAlchemyError as e:
            raise fail(f"cannot reach the database: {str(e).splitlines()[0]}  (run: satchel up)") from e
        finally:
            eng.dispose()
        say(f"minted token '{name or client}' (any earlier one with that name was revoked)")

    extra_env: dict[str, str] = {}
    if active_profile() != "local":
        extra_env["SATCHEL_PROFILE"] = active_profile()
    if cfg_file := os.environ.get("SATCHEL_CONFIG"):
        extra_env["SATCHEL_CONFIG"] = str(Path(cfg_file).expanduser().resolve())
    entry = (
        clients.http_entry(client, http, raw) if http
        else clients.stdio_entry(exe, raw, extra_env)
    )
    if client == "claude-code" and not http:
        entry = {"type": "stdio", **entry}

    path = clients.config_path(client)
    shown = clients.mask(entry) if not print_only else entry
    use_cli = client == "claude-code" and clients.claude_cli() is not None
    if print_only:
        if use_cli:
            argv = clients.claude_add_args(entry, server_name, scope, http_url=http)
            say("claude " + " ".join(_q(a) for a in argv))
        say(f"# {path}")
        say(json.dumps({"mcpServers": {server_name: shown}}, indent=2))
        return

    try:
        if use_cli:
            cli = clients.claude_cli() or "claude"
            infra.run([cli, "mcp", "remove", server_name, "--scope", scope], timeout=30)
            code, out = infra.run(
                [cli, *clients.claude_add_args(entry, server_name, scope, http_url=http)],
                timeout=60,
            )
            if code != 0:
                raise fail(f"`claude mcp add` failed: {out}")
            say(f"added '{server_name}' via `claude mcp add --scope {scope}`")
        else:
            res = clients.set_entry(path, server_name, entry)
            say(f"{'updated' if res.previous else 'added'} '{server_name}' in {res.path}")
            if res.backup:
                say(f"backup: {res.backup}")
    except clients.ClientConfigError as e:
        raise fail(str(e)) from e
    say(json.dumps({server_name: shown}, indent=2))
    say(f"Restart {'Claude Desktop' if client == 'claude-desktop' else client} to pick it up. "
        "Check with: satchel doctor")


def _q(a: str) -> str:
    import shlex

    return shlex.quote(a)


def _guard_or_none(eng: Engine) -> str | None:
    from satchel.store.schema import guard_message

    return guard_message(eng)


def disconnect(
    client: ClientArg,
    server_name: Annotated[str, typer.Option(help="Key in the client's mcpServers")] = "satchel",
    scope: Annotated[str, typer.Option(help="claude-code scope")] = "user",
    keep_token: Annotated[bool, typer.Option(help="Do not revoke the token")] = False,
) -> None:
    """Remove satchel from a client's MCP config and revoke the token it used."""
    from satchel.mcp.auth import revoke_token

    _check_client(client)
    path = clients.config_path(client)
    try:
        entry = clients.get_entry(path, server_name)
        removed = False
        if client == "claude-code" and clients.claude_cli():
            code, _ = infra.run(
                [clients.claude_cli() or "claude", "mcp", "remove", server_name, "--scope", scope],
                timeout=30,
            )
            removed = code == 0
        if not removed:
            res = clients.remove_entry(path, server_name)
            removed = res is not None
            if res and res.backup:
                say(f"backup: {res.backup}")
    except clients.ClientConfigError as e:
        raise fail(str(e)) from e
    say(f"removed '{server_name}' from {client}" if removed
        else f"no '{server_name}' entry found for {client}")
    if keep_token:
        return
    s = get_settings()
    eng = _engine(s.database_url)
    try:
        tok = clients.entry_token(entry) if entry else None
        n = 0
        if tok:
            n = revoke_token(raw=tok, engine=eng)
        elif s.workspace:
            n = revoke_token(workspace_slug=s.workspace, name=client, engine=eng)
        say(f"revoked {n} token(s)")
    except SQLAlchemyError as e:
        say(f"could not revoke the token (database unreachable: {str(e).splitlines()[0]})")
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------------------------


def install_kind() -> str:
    """How this satchel was installed: ``uv-tool`` | ``dev`` | ``pip``."""
    prefix = Path(sys.prefix).resolve()
    tool_dir = os.environ.get("UV_TOOL_DIR")
    if (tool_dir and str(prefix).startswith(str(Path(tool_dir).resolve()))) or (
        f"{os.sep}uv{os.sep}tools{os.sep}" in str(prefix)
    ):
        return "uv-tool"
    if clients.is_dev_executable(Path(sys.executable)) or (prefix / ".." / "pyproject.toml").exists():
        return "dev"
    return "pip"


def install_spec(src: str | None) -> str:
    """The `uv tool install` / pip target for ``--from`` (path or git URL), else the default."""
    if not src:
        return INSTALL_SPEC
    p = Path(src).expanduser()
    if p.exists():
        p = p.resolve()
        return str(p / "server") if (p / "server" / "pyproject.toml").exists() else str(p)
    url = src if src.startswith("git+") else f"git+{src}"
    if "#" not in url:
        url += "#subdirectory=server"
    return f"{DIST} @ {url}"


def upgrade(
    from_: Annotated[
        str | None, typer.Option("--from", help="Install from a PATH or git URL")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip prompts")] = False,
) -> None:
    """Reinstall satchel (uv tool) and migrate the database to the new version's schema."""
    kind = install_kind()
    spec = install_spec(from_)
    s = get_settings()
    mig = s.migration_database_url or s.database_url
    if kind == "uv-tool":
        step("Reinstalling satchel")
        if from_:
            argv = ["uv", "tool", "install", "--force", "--reinstall", spec]
        else:
            argv = ["uv", "tool", "upgrade", DIST]
        code, out = infra.run(argv, timeout=900)
        say(out)
        if code != 0:
            raise fail(f"{' '.join(argv)} failed")
        exe = clients.resolve_satchel_executable() or shutil.which("satchel")
        step("Migrating the database with the new version")
        # The new code carries the new migrations: run it, not this (old) process.
        code, out = infra.run([exe or "satchel", "db", "upgrade"], timeout=300)
        say(out or "schema is at head")
        if code != 0:
            raise fail("migration failed")
        return
    if kind == "dev":
        say("This is a dev checkout. Update the code yourself, then migrate:")
        say("  git pull && (cd server && uv sync) && satchel db upgrade")
        if yes or typer.confirm("Run the migration now with the current code?", default=True):
            migrate(mig)
            say("schema is at head")
            _ensure_worker_from(s)
        return
    say("satchel was installed with pip. Run:")
    # Never `pip install -U satchel`: that PyPI name is an unrelated project.
    say(f'  pip install -U "{spec}"')
    say("  satchel db upgrade")


def register(app: typer.Typer) -> None:
    app.command("init")(init)
    app.command("up")(up)
    app.command("down")(down)
    app.command("status")(status)
    app.command("doctor")(doctor_cmd)
    app.command("connect")(connect)
    app.command("disconnect")(disconnect)
    app.command("upgrade")(upgrade)


