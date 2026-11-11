"""Local infrastructure for ``satchel init`` / ``up`` / ``down``: Docker, Postgres, the server process.

Everything here is a thin, mockable wrapper (tests patch ``docker_available`` / ``run``).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from satchel.cli.config import active_profile, data_dir, state_dir

COMPOSE_PROJECT = "satchel"
OWNER = ("satchel", "satchel")
APP_ROLE = ("satchel_app", "satchel_app")
WORKER_ROLE = ("satchel_worker", "satchel_worker")  # server-internal jobs (admin_session)
DB_NAME = "satchel"
DEFAULT_PG_PORT = 5433
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def run(cmd: list[str], *, timeout: float = 180, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run a command, returning (exit code, combined output). Never raises on failure."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(cmd[:3])}: timed out after {timeout:.0f}s"
    return p.returncode, (p.stdout + p.stderr).strip()


def docker_available() -> tuple[bool, str]:
    """(ok, detail): the docker CLI exists and the daemon answers."""
    if shutil.which("docker") is None:
        return False, "docker is not installed"
    code, out = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
    if code != 0:
        return False, "docker is installed but the daemon is not running"
    return True, f"docker {out.strip()}"


def pg_urls(port: int) -> tuple[str, str]:
    """(runtime URL as satchel_app, migration URL as the owner) for a local satchel Postgres."""
    def u(user: str, pw: str) -> str:
        return URL.create(
            "postgresql+psycopg", user, pw, "localhost", port, DB_NAME
        ).render_as_string(hide_password=False)

    return u(*APP_ROLE), u(*OWNER)


def worker_url(port: int) -> str:
    """URL of the local dev ``satchel_worker`` role (``admin_database_url``)."""
    return URL.create(
        "postgresql+psycopg", *WORKER_ROLE, "localhost", port, DB_NAME
    ).render_as_string(hide_password=False)


def url_port(url: str) -> int | None:
    return make_url(url).port


def is_local_url(url: str) -> bool:
    return (make_url(url).host or "localhost") in LOCAL_HOSTS


def probe_postgres(port: int) -> str:
    """Classify what listens on localhost:PORT.

    ``satchel`` = a healthy satchel Postgres (owner login works and the satchel_app role exists);
    ``other`` = something answers but it is not ours; ``down`` = nothing there."""
    owner_url = pg_urls(port)[1]
    eng = create_engine(owner_url, connect_args={"connect_timeout": 3})
    try:
        with eng.connect() as c:
            has = c.execute(
                text("select 1 from pg_roles where rolname = :r"), {"r": APP_ROLE[0]}
            ).scalar()
        return "satchel" if has else "other"
    except Exception as e:
        msg = str(e).lower()
        if "authentication" in msg or "does not exist" in msg or "password" in msg:
            return "other"
        if "refused" in msg or "could not connect" in msg or "timeout" in msg:
            return "down"
        return "other"
    finally:
        eng.dispose()


def _compose_dir() -> Path:
    """Copy the packaged compose file + initdb SQL to a stable, Docker-shareable directory."""
    dest = data_dir() / "compose"
    (dest / "initdb").mkdir(parents=True, exist_ok=True)
    src = resources.files("satchel.resources")
    (dest / "compose.yml").write_text((src / "compose.yml").read_text())
    for f in (src / "initdb").iterdir():
        if f.name.endswith(".sql"):
            (dest / "initdb" / f.name).write_text(f.read_text())
    return dest


def compose(*args: str, port: int = DEFAULT_PG_PORT, timeout: float = 300) -> tuple[int, str]:
    d = _compose_dir()
    return run(
        ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", str(d / "compose.yml"), *args],
        timeout=timeout,
        env={"SATCHEL_PG_PORT": str(port)},
    )


def compose_postgres_running() -> bool:
    """Is the satchel-owned compose project's Postgres up?"""
    code, out = run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
         "--filter", "label=com.docker.compose.service=postgres", "--format", "{{.Names}}"],
        timeout=20,
    )
    return code == 0 and bool(out.strip())


def wait_for_postgres(url: str, timeout: float = 90) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        eng = create_engine(url, connect_args={"connect_timeout": 3})
        try:
            with eng.connect() as c:
                c.execute(text("select 1"))
            return True
        except Exception:
            time.sleep(1.5)
        finally:
            eng.dispose()
    return False


def ensure_postgres(port: int) -> tuple[str, str]:
    """Make a satchel Postgres available on PORT. Returns (action, detail):
    ``reused`` (a healthy satchel Postgres was already there; left untouched),
    ``started`` (compose project ``satchel`` brought it up) or ``error``."""
    kind = probe_postgres(port)
    if kind == "satchel":
        return "reused", f"found a healthy satchel Postgres on localhost:{port}"
    if kind == "other":
        return "error", (
            f"localhost:{port} is in use by something that is not a satchel Postgres. "
            f"Pick another port: satchel init --pg-port {port + 1}"
        )
    ok, detail = docker_available()
    if not ok:
        return "error", f"{detail}. Start Docker Desktop, or pass --database-url for your own Postgres."
    code, out = compose("up", "-d", "--wait", "postgres", port=port)
    if code != 0:
        return "error", f"docker compose failed:\n{out}"
    owner = pg_urls(port)[1]
    if not wait_for_postgres(owner):
        return "error", f"Postgres did not become ready on localhost:{port}"
    return "started", f"started Postgres (docker compose project '{COMPOSE_PROJECT}') on localhost:{port}"


# ---- the API server process (pidfile + log under ~/.local/state/satchel) ------------------------


@dataclass
class ServerInfo:
    pid: int
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"http://{'localhost' if self.host in ('0.0.0.0', '127.0.0.1') else self.host}:{self.port}"


def pidfile() -> Path:
    return state_dir() / f"server-{active_profile()}.pid"


def logfile() -> Path:
    return state_dir() / f"server-{active_profile()}.log"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_server() -> ServerInfo | None:
    """The detached server recorded in the pidfile, if it is still alive (stale file removed)."""
    p = pidfile()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        info = ServerInfo(int(d["pid"]), str(d["host"]), int(d["port"]))
    except (ValueError, KeyError, OSError):
        p.unlink(missing_ok=True)
        return None
    if not pid_alive(info.pid):
        p.unlink(missing_ok=True)
        return None
    return info


def healthy(url: str, timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=timeout) as r:  # noqa: S310
            return bool(r.status == 200)
    except Exception:
        return False


def start_detached(host: str, port: int) -> ServerInfo:
    """Spawn ``satchel serve api`` in its own session; pidfile + log under the state dir."""
    state_dir().mkdir(parents=True, exist_ok=True)
    log = open(logfile(), "ab")  # noqa: SIM115 (inherited by the child)
    code = "from satchel.cli.main import app; app()"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, "serve", "api", "--host", host, "--port", str(port)],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    info = ServerInfo(proc.pid, host, port)
    pidfile().write_text(json.dumps({"pid": proc.pid, "host": host, "port": port}))
    for _ in range(60):
        if healthy(info.url):
            return info
        if proc.poll() is not None:
            pidfile().unlink(missing_ok=True)
            raise RuntimeError(f"server exited with {proc.returncode}; see {logfile()}")
        time.sleep(0.5)
    raise RuntimeError(f"server did not become healthy on {info.url}; see {logfile()}")


def stop_server(timeout: float = 10) -> ServerInfo | None:
    info = read_server()
    if info is None:
        return None
    os.kill(info.pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and pid_alive(info.pid):
        time.sleep(0.2)
    if pid_alive(info.pid):
        os.kill(info.pid, signal.SIGKILL)
    pidfile().unlink(missing_ok=True)
    return info
