"""``satchel doctor``: check the install end to end and print the exact fix for each failure."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError

from satchel.cli import clients, infra
from satchel.cli.config import config_path, get_profile
from satchel.cli.ops import token_info, workspace_summary
from satchel.settings import Settings
from satchel.store.db import admin_session
from satchel.store.models import Connection, Workspace
from satchel.store.schema import schema_status

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
MARKS = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "-"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def _db_error(e: Exception) -> str:
    orig = getattr(e, "orig", None) or e
    return str(orig).strip().splitlines()[0] if str(orig).strip() else type(e).__name__


def run_checks(settings: Settings, *, check_clients: bool = True) -> list[Check]:
    out: list[Check] = []
    local = infra.is_local_url(settings.database_url)
    engine: Engine | None = _app_engine(settings)
    assert engine is not None
    reachable = False
    db_err = ""
    try:
        with engine.connect() as c:
            c.execute(text("select 1"))
        reachable = True
    except SQLAlchemyError as e:
        db_err = _db_error(e)

    # 1. Docker
    docker_ok, docker_detail = infra.docker_available()
    if docker_ok:
        out.append(Check("Docker", OK, docker_detail))
    elif reachable or not local:
        out.append(Check("Docker", WARN, f"{docker_detail} (not needed: the database is reachable)"))
    else:
        out.append(Check("Docker", FAIL, docker_detail,
                         "start Docker Desktop, then: satchel up"))

    # 2. Database connectivity
    where = f"{engine.url.host}:{engine.url.port}/{engine.url.database}"
    if not reachable:
        fix = "satchel up" if local else "check database_url in " + str(config_path())
        out.append(Check("Database", FAIL, f"cannot connect to {where}: {db_err}", fix))
        for name in ("App role", "Worker role", "Schema", "Token", "Embedder", "Result dir",
                     "Connections"):
            out.append(Check(name, SKIP, "needs the database"))
        _result_dir(settings, out)
        if check_clients:
            out.extend(client_checks(None, settings))
        engine.dispose()
        return _reorder(out)
    out.append(Check("Database", OK, f"connected to {where}"))

    # 3. Non-superuser app role (RLS)
    row = engine.connect().execute(
        text("select current_user, r.rolsuper, r.rolbypassrls from pg_roles r"
             " where r.rolname = current_user")
    ).one()
    if row[1] or row[2]:
        out.append(Check(
            "App role", FAIL,
            f"'{row[0]}' is a {'superuser' if row[1] else 'BYPASSRLS role'}: row-level security "
            "is NOT enforced",
            "connect as satchel_app: set database_url to .../satchel_app@... in "
            f"{config_path()} (satchel init creates the role)",
        ))
    else:
        out.append(Check("App role", OK, f"'{row[0]}' is not a superuser (RLS applies)"))

    # 3b. Worker role (admin_session)
    out.append(worker_check(settings, local=local))

    # 4. Schema at head
    st = schema_status(engine)
    if st.ok:
        out.append(Check("Schema", OK, f"at head ({st.head})"))
    else:
        out.append(Check("Schema", FAIL, st.message.split(" — ")[0], "satchel upgrade"))

    # 5. Token
    info = None
    if not settings.token:
        out.append(Check("Token", FAIL, "no token configured",
                         "satchel init   (or: satchel token create -w WORKSPACE --user EMAIL --name cli)"))
    elif st.state in ("empty", "behind", "ahead"):
        out.append(Check("Token", SKIP, "needs the schema at head"))
    else:
        info = token_info(engine, settings.token)
        if info.valid:
            exp = f", expires {info.expires_at:%Y-%m-%d}" if info.expires_at else ", no expiry"
            out.append(Check("Token", OK, f"valid for {info.email} in '{info.workspace}'{exp}"))
        else:
            out.append(Check("Token", FAIL, info.reason,
                             "satchel init --yes   (mints a new token and updates the config)"))

    slug = info.workspace if info and info.valid else settings.workspace
    ready = st.ok and slug is not None
    summary = workspace_summary(engine, slug) if ready and slug else None

    # 6. Embedder
    out.append(_embedder(settings, slug, summary.embedding_models if summary else ()))

    # 7. Result dir
    _result_dir(settings, out)

    # 7b. Encryption key (secrets stored in the database)
    out.append(_encryption(settings, engine if st.ok else None))

    # 8. Connections
    if summary is None or not summary.exists:
        out.append(Check("Connections", SKIP, "no workspace to check"))
    elif not summary.connections:
        out.append(Check("Connections", OK, "none configured (context-only workspace)"))
    else:
        out.extend(_connections(engine, slug or ""))

    if check_clients:
        out.extend(client_checks(engine, settings))
    engine.dispose()
    return _reorder(out)


def _app_engine(settings: Settings) -> Engine:
    from satchel.store.db import AdminDatabaseUrlMissing, create_app_engine

    kw: dict[str, Any] = {"connect_args": {"connect_timeout": 5}}
    try:
        return create_app_engine(settings.database_url, settings=settings, **kw)
    except AdminDatabaseUrlMissing:
        return create_engine(settings.database_url, pool_pre_ping=True, **kw)


def worker_check(settings: Settings, *, local: bool) -> Check:
    """Can ``admin_session`` connect as satchel_worker? Prints the exact fix when not."""
    from satchel.store.db import AdminDatabaseUrlMissing, admin_database_url
    from satchel.store.roles import WORKER_ROLE, worker_status

    name = "Worker role"
    try:
        url = admin_database_url(settings)
    except AdminDatabaseUrlMissing as e:
        return Check(name, FAIL, str(e), "set SATCHEL_ADMIN_DATABASE_URL to the satchel_worker URL")
    eng = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        with eng.connect() as c:
            user = c.execute(text("select current_user")).scalar()
        return Check(name, OK, f"server-internal jobs connect as '{user}'")
    except SQLAlchemyError as e:
        err = _db_error(e)
    finally:
        eng.dispose()
    try:
        st = worker_status(settings.database_url)
    except SQLAlchemyError:
        st = None
    remote_fix = ("satchel db bootstrap   (with SATCHEL_BOOTSTRAP_DATABASE_URL and "
                  "SATCHEL_WORKER_DB_PASSWORD; see docs/deploy/database.md)")
    if st is not None and not st.exists:
        return Check(name, FAIL, f"role {WORKER_ROLE} does not exist ({err})",
                     "satchel upgrade   (migration 0010 creates it)" if local else remote_fix)
    if st is not None and not st.can_login:
        local_fix = (
            "satchel db upgrade   (sets LOGIN and the dev password when the migration URL is a "
            "local superuser), or as a superuser run: "
            "ALTER ROLE satchel_worker LOGIN PASSWORD 'satchel_worker';"
        )
        return Check(name, FAIL, f"role {WORKER_ROLE} cannot log in ({err})",
                     local_fix if local else remote_fix)
    return Check(name, FAIL, f"cannot connect as {WORKER_ROLE}: {err}",
                 f"set admin_database_url in {config_path()} (or SATCHEL_ADMIN_DATABASE_URL) to the "
                 f"{WORKER_ROLE} URL with its password; to rotate it: {remote_fix}")


def _reorder(checks: list[Check]) -> list[Check]:
    return checks


def _embedder(settings: Settings, slug: str | None, stored: tuple[str, ...]) -> Check:
    from satchel.llm.embeddings import make_embedder

    try:
        emb = make_embedder(settings)
        emb.embed(["satchel doctor"])
    except Exception as e:
        return Check("Embedder", FAIL, f"{settings.embedding_provider} failed to load: {e}",
                     "satchel init --embedding hash   (offline), or check the model download / API key")
    if stored and emb.model_id not in stored:
        return Check("Embedder", FAIL,
                     f"loaded {emb.model_id} but chunks were embedded with {', '.join(stored)}",
                     f"satchel sync reembed -w {slug}   (or set embedding_provider to match)")
    note = "matches stored chunks" if stored else "no chunks stored yet"
    return Check("Embedder", OK, f"{emb.model_id} loaded; {note}")


def _encryption(settings: Settings, engine: Engine | None) -> Check:
    from satchel.core.crypto import EncryptionUnavailable, Keyring

    name = "Encryption key"
    try:
        kr = Keyring.from_settings(settings)
    except EncryptionUnavailable as e:
        unset = settings.encryption_key is None
        return Check(name, WARN if unset else FAIL,
                     "SATCHEL_ENCRYPTION_KEY not set: LLM connections with an API key cannot be "
                     "stored" if unset else str(e).split(";")[0],
                     "SATCHEL_ENCRYPTION_KEY=$(satchel secrets generate-key)")
    if engine is None:
        return Check(name, OK, f"key {kr.current_id} configured")
    from satchel.secrets import check_secrets

    try:
        _, stale, bad = check_secrets(kr, engine)
    except Exception as e:  # noqa: BLE001 - worker role missing etc. is reported elsewhere
        return Check(name, OK, f"key {kr.current_id} configured (stored secrets not checked: "
                     f"{type(e).__name__})")
    if bad:
        return Check(name, FAIL, f"{bad} stored secret(s) cannot be decrypted with the "
                     "configured keys", "restore the old key in SATCHEL_ENCRYPTION_KEY_PREVIOUS")
    if stale:
        return Check(name, WARN, f"{stale} secret(s) still on a previous key",
                     "satchel secrets rotate")
    return Check(name, OK, f"key {kr.current_id} configured")


def _result_dir(settings: Settings, out: list[Check]) -> None:
    from satchel.data.objstore import describe, is_object_uri, split_uri

    if is_object_uri(settings.result_dir):
        kind = describe(split_uri(settings.result_dir)[0])
        out.append(Check("Result dir", OK, f"{settings.result_dir} ({kind}; not probed)"))
        return
    d = Path(settings.result_dir).expanduser()
    try:
        d.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=d):
            pass
        out.append(Check("Result dir", OK, f"{d} is writable"))
    except OSError as e:
        out.append(Check("Result dir", FAIL, f"{d}: {e.strerror or e}",
                         f"mkdir -p {d} && chmod u+w {d}   (or set result_dir in {config_path()})"))


def _connections(engine: Engine, slug: str) -> list[Check]:
    from satchel.data.connections import ConnectionError_, open_connector

    checks: list[Check] = []
    with admin_session(engine=engine) as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == slug)).one()
        conns = list(
            s.scalars(
                select(Connection).where(Connection.workspace_id == ws.id).order_by(Connection.name)
            )
        )
        for c in conns:
            name = f"Connection {c.name}"
            try:
                conn = open_connector(c)
                try:
                    conn.execute("select 1", limit=1, timeout_s=10)
                finally:
                    conn.close()
                checks.append(Check(name, OK, f"{c.kind} reachable"))
            except (ConnectionError_, Exception) as e:
                checks.append(Check(name, FAIL, f"{c.kind}: {str(e).splitlines()[0]}",
                                    f"satchel connection list -w {slug}   (then: satchel connection add ...)"))
    return checks


def client_checks(engine: Engine | None, settings: Settings) -> list[Check]:
    """Claude Desktop / Code / Cursor entries that point at satchel."""
    out: list[Check] = []
    found = False
    for client in clients.CLIENTS:
        try:
            path = clients.config_path(client)
        except clients.ClientConfigError:
            continue
        for name, entry in clients.find_satchel_entries(client):
            found = True
            out.append(_client_entry(client, path, name, entry, engine, settings))
    if not found:
        out.append(Check("Clients", SKIP, "no Claude Desktop/Code/Cursor entry mentions satchel",
                         "satchel connect claude-desktop"))
    return out


def _client_entry(
    client: str,
    path: Path,
    name: str,
    entry: dict[str, Any],
    engine: Engine | None,
    settings: Settings,
) -> Check:
    label = f"Client {client} ({name})"
    fix = f"satchel connect {client}"
    if "url" in entry:  # http variant
        return Check(label, OK, f"remote MCP at {entry['url']}")
    cmd = str(entry.get("command", ""))
    if cmd == "npx":
        npx = shutil.which("npx") is not None
        return Check(label, OK if npx else FAIL,
                     "mcp-remote bridge" if npx else "npx not found (install Node.js)",
                     "" if npx else "brew install node")
    resolved = shutil.which(cmd) if not Path(cmd).is_absolute() else (cmd if Path(cmd).exists() else None)
    if resolved is None:
        return Check(label, FAIL, f"command not found: {cmd}", fix)
    args = [str(a) for a in entry.get("args", [])]
    if cmd.endswith("/uv") or cmd == "uv" or "--project" in args or clients.is_dev_executable(cmd):
        return Check(label, WARN,
                     "runs a dev checkout (uv run --project / .venv): uncommitted code or an "
                     "un-migrated schema breaks every call",
                     f"uv tool install ... then: satchel connect {client}")
    env = entry.get("env") or {}
    problems: list[str] = []
    cfg = env.get("SATCHEL_CONFIG")
    if cfg and not Path(cfg).expanduser().exists():
        problems.append(f"SATCHEL_CONFIG {cfg} does not exist")
    if not env.get("SATCHEL_DATABASE_URL") and not cfg and not get_profile().get("database_url"):
        problems.append(f"no SATCHEL_DATABASE_URL and no database_url in {config_path()}")
    tok = clients.entry_token(entry)
    if tok is None:
        problems.append("no SATCHEL_TOKEN in the entry")
    elif engine is not None:
        ti = token_info(engine, tok)
        if not ti.valid:
            problems.append(f"its token is {ti.reason}")
    if problems:
        return Check(label, FAIL, "; ".join(problems), fix)
    return Check(label, OK, f"{resolved}; env resolves; token valid")


def render(checks: list[Check]) -> list[str]:
    lines: list[str] = []
    for c in checks:
        lines.append(f"{MARKS[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status in (FAIL, WARN, SKIP):
            lines.append(f"    fix: {c.fix}")
    return lines


def failed(checks: list[Check]) -> bool:
    return any(c.status == FAIL for c in checks)


