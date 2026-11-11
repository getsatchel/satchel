"""The satchel config file: ``~/.config/satchel/config.toml`` (profiles of settings).

Resolution order for the path: ``SATCHEL_CONFIG`` (a file), else ``$XDG_CONFIG_HOME/satchel/config.toml``,
else ``~/.config/satchel/config.toml``. The active profile is ``SATCHEL_PROFILE`` (set by
``satchel --profile``), default ``local``.

    [profile.local]
    database_url = "postgresql+psycopg://satchel_app:satchel_app@localhost:5433/satchel"
    migration_database_url = "postgresql+psycopg://satchel:satchel@localhost:5433/satchel"
    workspace = "main"
    token = "satchel_pat_..."        # the CLI's own PAT (0600 file); LLM keys are never stored
    embedding_provider = "fastembed"
    public_url = "http://localhost:8080"
    result_dir = "~/.cache/satchel/results"
    email = "you@example.com"     # not a setting: who `satchel connect` mints tokens for

    [profile.local.chat]
    api_key_env = "ANTHROPIC_API_KEY"   # the NAME of the env var holding the LLM key

Any other ``Settings`` field may also appear in a profile. Settings precedence is
environment > this file's profile > defaults (see ``satchel.settings.Settings``).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_PROFILE = "local"
# Keys that live in a profile but are not Settings fields.
EXTRA_KEYS = {"email"}


class ConfigError(Exception):
    pass


def config_path() -> Path:
    explicit = os.environ.get("SATCHEL_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "satchel" / "config.toml"


def state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "satchel"


def data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "satchel"


def active_profile() -> str:
    return os.environ.get("SATCHEL_PROFILE") or DEFAULT_PROFILE


def read_config(path: Path | None = None) -> dict[str, Any]:
    """The whole file as a dict ({} when missing)."""
    p = path or config_path()
    if not p.exists():
        return {}
    try:
        return tomllib.loads(p.read_text())
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise ConfigError(f"cannot read {p}: {e}") from e


def get_profile(name: str | None = None, path: Path | None = None) -> dict[str, Any]:
    profiles = read_config(path).get("profile", {})
    prof = profiles.get(name or active_profile(), {})
    return dict(prof) if isinstance(prof, dict) else {}


def profile_settings(fields: set[str]) -> dict[str, Any]:
    """Settings values from the active profile (the pydantic-settings config source).

    A missing file is not an error; an unreadable one is."""
    prof = get_profile()
    out: dict[str, Any] = {k: v for k, v in prof.items() if k in fields}
    chat = prof.get("chat")
    if isinstance(chat, dict):
        env_name = chat.get("api_key_env")
        if isinstance(env_name, str) and os.environ.get(env_name):
            out["chat_api_key"] = os.environ[env_name]
        for key in ("provider", "model", "base_url"):
            if key in chat and f"chat_{key}" in fields:
                out[f"chat_{key}"] = chat[key]
    return out


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def _toml_key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else _toml_value(k)


def dumps(data: dict[str, Any]) -> str:
    """Minimal TOML writer for nested tables of scalars (all this file ever holds)."""
    lines: list[str] = []

    def table(path: list[str], d: dict[str, Any]) -> None:
        scalars = {k: v for k, v in d.items() if not isinstance(v, dict)}
        subs = {k: v for k, v in d.items() if isinstance(v, dict)}
        if path and (scalars or not subs):
            lines.append("")
            lines.append("[" + ".".join(_toml_key(p) for p in path) + "]")
        for k, v in scalars.items():
            if v is not None:
                lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
        for k, v in subs.items():
            table([*path, k], v)

    table([], data)
    return "\n".join(lines).lstrip("\n") + "\n"


def write_config(data: dict[str, Any], path: Path | None = None) -> Path:
    """Atomically write ``data`` with 0600 permissions (it may hold a PAT)."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# Written by `satchel init`. Contains a personal access token: keep it private.\n")
        f.write(dumps(data))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p


def save_profile(
    values: dict[str, Any], name: str | None = None, path: Path | None = None
) -> Path:
    """Merge ``values`` into profile ``name`` (None values delete the key); keep the rest."""
    data = read_config(path)
    profiles = data.setdefault("profile", {})
    prof = dict(profiles.get(name or active_profile(), {}))
    for k, v in values.items():
        if v is None:
            prof.pop(k, None)
        elif isinstance(v, dict) and isinstance(prof.get(k), dict):
            prof[k] = {**prof[k], **v}
        else:
            prof[k] = v
    profiles[name or active_profile()] = prof
    return write_config(data, path)
