"""Process-level runtime concerns shared by ``serve`` and ``worker``: the production guard and
structured logging (``SATCHEL_LOG_FORMAT=json``, Cloud Logging-compatible)."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime
from typing import Any

from satchel.settings import Settings

# ---------------------------------------------------------------------------------------------
# production guard
# ---------------------------------------------------------------------------------------------


def production_problems(st: Settings) -> list[str]:
    """Why ``SATCHEL_ENV=production`` must refuse to start (empty = fine or not production)."""
    if st.env != "production":
        return []
    problems: list[str] = []
    if st.dev_auth:
        problems.append(
            "SATCHEL_DEV_AUTH is set: unverified header auth must not run in production"
            " (unset SATCHEL_DEV_AUTH)"
        )
    missing = [
        name
        for name, val in (
            ("SATCHEL_OIDC_ISSUER", st.oidc_issuer),
            ("SATCHEL_OIDC_CLIENT_ID", st.oidc_client_id),
            ("SATCHEL_OIDC_CLIENT_SECRET", st.oidc_client_secret),
        )
        if not val
    ]
    if missing:
        problems.append(f"OIDC is not configured (missing {', '.join(missing)})")
    if not st.admin_database_url:
        problems.append(
            "SATCHEL_ADMIN_DATABASE_URL is not set: server-internal jobs need the satchel_worker"
            " role"
            " (see docs/deploy/database.md)"
        )
    if not st.public_url.lower().startswith("https://") and not st.allow_insecure_public_url:
        problems.append(
            f"SATCHEL_PUBLIC_URL must be an https:// URL in production (got {st.public_url!r});"
            " set SATCHEL_ALLOW_INSECURE_PUBLIC_URL=1 only if TLS is terminated elsewhere"
        )
    return problems


def enforce_production(st: Settings) -> None:
    """Raise ``SystemExit(1)`` with every problem listed. Serve and worker call this; CLI admin
    commands (``db upgrade``, ``workspace create``) do not."""
    problems = production_problems(st)
    if problems:
        import sys

        print("satchel: refusing to start with SATCHEL_ENV=production:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        raise SystemExit(1)


# ---------------------------------------------------------------------------------------------
# structured logging
# ---------------------------------------------------------------------------------------------

_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar("satchel_trace", default=None)

_CLOUD_TRACE = re.compile(r"^([0-9a-fA-F]{32})(?:/(\d+))?(?:;o=\d)?$")
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")

_SEVERITY = {
    "DEBUG": "DEBUG", "INFO": "INFO", "WARNING": "WARNING", "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


def parse_trace(headers: dict[str, str]) -> str | None:
    """Trace id from ``X-Cloud-Trace-Context`` (TRACE/SPAN;o=1) or W3C ``traceparent``."""
    h = headers.get("x-cloud-trace-context")
    if h and (m := _CLOUD_TRACE.match(h.strip())):
        return m.group(1).lower()
    h = headers.get("traceparent")
    if h and (m := _TRACEPARENT.match(h.strip())):
        return m.group(1)
    return None


def _trace_field(trace_id: str) -> str:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("SATCHEL_GCP_PROJECT")
    return f"projects/{project}/traces/{trace_id}" if project else trace_id


class JsonFormatter(logging.Formatter):
    """One JSON object per line with Cloud Logging's special fields."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        doc: dict[str, Any] = {
            "severity": _SEVERITY.get(record.levelname, "DEFAULT"),
            "message": msg,
            "logger": record.name,
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
        }
        trace = _trace.get()
        if trace:
            doc["logging.googleapis.com/trace"] = _trace_field(trace)
        args = record.args
        if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) == 5:
            client, method, path, http_version, status = args
            doc["httpRequest"] = {
                "requestMethod": str(method), "requestUrl": str(path),
                "status": int(str(status)), "protocol": f"HTTP/{http_version}",
                "remoteIp": str(client).rsplit(":", 1)[0],
            }
        if record.exc_info:
            doc["message"] = f"{msg}\n{self.formatException(record.exc_info)}".rstrip()
        return json.dumps(doc, default=str)


def uvicorn_log_config(log_format: str) -> dict[str, Any] | None:
    """A ``log_config`` for ``uvicorn.run``: JSON for error and access logs, else None."""
    if log_format != "json":
        return None
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": "satchel.runtime.JsonFormatter"}},
        "handlers": {
            "default": {"class": "logging.StreamHandler", "formatter": "json",
                        "stream": "ext://sys.stderr"},
            "access": {"class": "logging.StreamHandler", "formatter": "json",
                       "stream": "ext://sys.stdout"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        },
        "root": {"handlers": ["default"], "level": "INFO"},
    }


def configure_logging(log_format: str) -> None:
    """Root logging for the worker (and anything outside uvicorn)."""
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.INFO)


Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class TraceMiddleware:
    """Pure ASGI middleware: stash the request's trace id so log lines can carry it."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        token = _trace.set(parse_trace(headers))
        try:
            await self.app(scope, receive, send)
        finally:
            _trace.reset(token)
