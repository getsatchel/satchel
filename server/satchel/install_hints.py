"""How to install satchel and its extras, for messages shown to users.

Satchel is published on PyPI as ``satchel-server``. The PyPI name ``satchel`` belongs to an
unrelated project, so a hint must never say ``pip install satchel``. The command and the import
package are still called ``satchel``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

DIST = "satchel-server"
REPO_URL = "git+https://github.com/getsatchel/satchel"


def installed_version() -> str | None:
    """The installed satchel-server version, or ``None`` when it is not an installed package."""
    try:
        return version(DIST)
    except PackageNotFoundError:
        return None


def _name(extras: tuple[str, ...]) -> str:
    return DIST + (f"[{','.join(extras)}]" if extras else "")


def pypi_spec(*extras: str, pin: bool = True) -> str:
    """A PyPI requirement, e.g. ``satchel-server[aws]==0.1.1`` (pinned to the running version)."""
    v = installed_version() if pin else None
    return _name(extras) + (f"=={v}" if v else "")


def git_spec(*extras: str, ref: str | None = None) -> str:
    """A requirement from the git repository, e.g.
    ``satchel-server[aws] @ git+https://github.com/getsatchel/satchel@v0.1.1#subdirectory=server``.
    ``ref`` defaults to the running version's tag; pass ``""`` for the default branch. A git
    install does not include the built UI; the PyPI package does."""
    if ref is None:
        v = installed_version()
        ref = f"@v{v}" if v else ""
    return f"{_name(extras)} @ {REPO_URL}{ref}#subdirectory=server"


def extra_hint(*extras: str) -> str:
    """The command that adds optional extras to a satchel install."""
    spec = pypi_spec(*extras)
    return f'uv tool install --force "{spec}" (or pip install "{spec}")'
