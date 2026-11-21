"""What is running: the server's version and the result widget's URI.

``/healthz`` and ``/readyz`` return these, so an operator can check an upgrade took with one
request (``curl https://<domain>/healthz``) instead of looking inside the container, and
``satchel status`` compares the server's version with the CLI's.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version

DIST = "satchel-server"


@lru_cache(maxsize=1)
def server_version() -> str:
    try:
        return version(DIST)
    except PackageNotFoundError:  # a source tree that was never installed
        return "unknown"


def build_info() -> dict[str, str]:
    """``{"version": "0.8.0", "widget": "ui://satchel/result/<hash>"}``. The widget URI changes
    with every build, so it also tells two builds of the same version apart."""
    from satchel.mcp.apps import result_uri

    return {"version": server_version(), "widget": result_uri()}
