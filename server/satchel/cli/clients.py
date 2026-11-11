"""MCP client configs: Claude Desktop, Claude Code and Cursor.

Paths, entry builders, safe merge-with-backup writes, and discovery of satchel entries (used by
``satchel connect`` / ``disconnect`` and ``satchel doctor``). Nothing here talks to the database.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CLIENTS = ("claude-desktop", "claude-code", "cursor")
SERVER_NAME = "satchel"
# Keys whose values are tokens (hidden when printing).
TOKEN_KEYS = ("SATCHEL_TOKEN", "SATCHEL_AUTH", "Authorization")


class ClientConfigError(Exception):
    pass


def config_path(client: str) -> Path:
    home = Path.home()
    if client == "claude-desktop":
        if sys.platform == "darwin":
            return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        if sys.platform.startswith("win"):
            appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
            return appdata / "Claude" / "claude_desktop_config.json"
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        return base / "Claude" / "claude_desktop_config.json"
    if client == "claude-code":
        return home / ".claude.json"
    if client == "cursor":
        return home / ".cursor" / "mcp.json"
    raise ClientConfigError(f"unknown client {client!r} (choose from {', '.join(CLIENTS)})")


# ---- the satchel executable ---------------------------------------------------------------------


def is_dev_executable(path: str | Path) -> bool:
    """True for a project virtualenv's script (``<checkout>/server/.venv/bin/satchel``), which is
    what ``uv run --project`` uses: it changes with every uncommitted edit and every branch."""
    p = Path(path)
    parts = p.parts
    for i, part in enumerate(parts):
        if part == ".venv" or part == "venv":
            return (Path(*parts[:i]) / "pyproject.toml").exists()
    return False


def resolve_satchel_executable() -> str | None:
    """Absolute path of the ``satchel`` script that is running (kept as a symlink so uv tool
    upgrades keep working), else the first ``satchel`` on PATH."""
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and Path(argv0).name in ("satchel", "satchel.exe"):
        p = Path(os.path.abspath(argv0))
        if p.exists():
            return str(p)
    return shutil.which("satchel")


# ---- entries ---------------------------------------------------------------------------------


def stdio_entry(exe: str, token: str, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    env = {"SATCHEL_TOKEN": token, **(extra_env or {})}
    return {"command": exe, "args": ["mcp", "stdio"], "env": env}


def http_entry(client: str, url: str, token: str) -> dict[str, Any]:
    bearer = f"Bearer {token}"
    if client == "cursor":
        return {"url": url, "headers": {"Authorization": bearer}}
    if client == "claude-code":
        return {"type": "http", "url": url, "headers": {"Authorization": bearer}}
    # Claude Desktop only launches commands: bridge with mcp-remote. The header goes through
    # env so there is no space inside an argument (a Windows quoting bug in some clients).
    return {
        "command": "npx",
        "args": ["-y", "mcp-remote", url, "--header", "Authorization:${SATCHEL_AUTH}"],
        "env": {"SATCHEL_AUTH": bearer},
    }


def mask(entry: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``entry`` with token values hidden (for printing)."""
    def m(v: Any, key: str = "") -> Any:
        if isinstance(v, dict):
            return {k: m(x, k) for k, x in v.items()}
        if isinstance(v, str) and key in TOKEN_KEYS:
            head, _, tail = v.rpartition(" ")
            return f"{head + ' ' if head else ''}{tail[:12]}…"
        return v

    out = m(entry)
    assert isinstance(out, dict)
    return out


def entry_token(entry: dict[str, Any]) -> str | None:
    """The satchel token inside a client entry (stdio env or an Authorization header), if any."""
    env = entry.get("env") or {}
    raw = (
        env.get("SATCHEL_TOKEN") or env.get("SATCHEL_AUTH")
        or (entry.get("headers") or {}).get("Authorization")
    )
    if not isinstance(raw, str):
        return None
    return raw.removeprefix("Bearer ").strip() or None


# ---- reading / writing ----------------------------------------------------------------------


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError as e:
        raise ClientConfigError(
            f"{path} is not valid JSON ({e}); fix it by hand first, satchel will not overwrite it"
        ) from e
    if not isinstance(data, dict):
        raise ClientConfigError(f"{path} does not hold a JSON object")
    return data


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    n = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{n}")
        n += 1
    shutil.copy2(path, dest)
    return dest


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_name(path.name + ".satchel-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


@dataclass
class WriteResult:
    path: Path
    backup: Path | None
    previous: dict[str, Any] | None  # the entry that was replaced, if any


def set_entry(path: Path, name: str, entry: dict[str, Any]) -> WriteResult:
    """Merge ``mcpServers[name] = entry`` into the client config, leaving every other key and
    server alone; the original file is copied to ``<file>.bak-<timestamp>`` first."""
    data = read_json(path)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ClientConfigError(f"{path}: 'mcpServers' is not an object")
    previous = servers.get(name)
    bak = backup(path)
    servers[name] = entry
    _write_json(path, data)
    return WriteResult(path, bak, previous if isinstance(previous, dict) else None)


def remove_entry(path: Path, name: str) -> WriteResult | None:
    data = read_json(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return None
    previous = servers.pop(name)
    bak = backup(path)
    _write_json(path, data)
    return WriteResult(path, bak, previous if isinstance(previous, dict) else None)


def get_entry(path: Path, name: str = SERVER_NAME) -> dict[str, Any] | None:
    servers = read_json(path).get("mcpServers")
    if isinstance(servers, dict) and isinstance(servers.get(name), dict):
        return dict(servers[name])
    return None


def find_satchel_entries(client: str) -> list[tuple[str, dict[str, Any]]]:
    """Every server in the client's config that looks like satchel (by name or command)."""
    try:
        servers = read_json(config_path(client)).get("mcpServers")
    except ClientConfigError:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    if not isinstance(servers, dict):
        return out
    for name, e in servers.items():
        if not isinstance(e, dict):
            continue
        blob = " ".join([str(e.get("command", "")), *map(str, e.get("args", []) or [])])
        if "satchel" in name.lower() or "satchel" in blob.lower():
            out.append((name, e))
    return out


# ---- claude code CLI ------------------------------------------------------------------------


def claude_cli() -> str | None:
    return shutil.which("claude")


def claude_add_args(
    entry: dict[str, Any], name: str, scope: str, *, http_url: str | None
) -> list[str]:
    """``claude mcp add`` argv (after the executable) for a stdio or http entry."""
    if http_url:
        auth = (entry.get("headers") or {}).get("Authorization", "")
        return ["mcp", "add", "--scope", scope, "--transport", "http", name, http_url,
                "--header", f"Authorization: {auth}"]
    # Name first, then -e (variadic in the claude CLI), then `--` to end the options.
    args = ["mcp", "add", "--scope", scope, name]
    for k, v in (entry.get("env") or {}).items():
        args += ["-e", f"{k}={v}"]
    return [*args, "--", entry["command"], *entry["args"]]
