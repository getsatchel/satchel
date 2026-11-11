"""How to install satchel and its extras, for messages shown to users.

The PyPI name ``satchel`` belongs to an unrelated project, so a hint must never say
``pip install satchel``. Every hint installs from the git repository, pinned to the tag of the
running version.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

REPO_URL = "git+https://github.com/getsatchel/satchel"


def release_ref() -> str:
    """``@v<version>`` of the installed satchel, or ``""`` when it is not an installed package."""
    try:
        return f"@v{version('satchel')}"
    except PackageNotFoundError:
        return ""


def git_spec(*extras: str, ref: str | None = None) -> str:
    """A pip / uv requirement for satchel from git, e.g.
    ``satchel[aws] @ git+https://github.com/getsatchel/satchel@v0.1.0#subdirectory=server``.
    ``ref`` defaults to the running version's tag; pass ``""`` for the default branch."""
    name = "satchel" + (f"[{','.join(extras)}]" if extras else "")
    return f"{name} @ {REPO_URL}{release_ref() if ref is None else ref}#subdirectory=server"


def extra_hint(*extras: str) -> str:
    """The command that adds optional extras to a satchel install."""
    spec = git_spec(*extras)
    return f'uv tool install --force "{spec}" (or pip install "{spec}")'
