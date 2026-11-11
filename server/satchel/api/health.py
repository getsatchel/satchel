"""``/readyz``: database reachable and schema at head (liveness is ``/healthz``, no DB)."""

from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from satchel.store.schema import schema_status

AT_HEAD_TTL_S = 30.0
DB_TIMEOUT_S = 3.0


class Readiness:
    """The at-head result is cached ~30 s (a revision cannot go backwards under a running
    build); reachability is re-checked on every call with a short timeout."""

    def __init__(self, engine: Engine, *, ttl_s: float = AT_HEAD_TTL_S,
                 timeout_s: float = DB_TIMEOUT_S) -> None:
        self.engine = engine
        self.ttl_s = ttl_s
        self.timeout_s = timeout_s
        self._ok_until = 0.0

    def _check(self) -> str | None:
        try:
            with self.engine.connect() as c:
                c.execute(text("set local statement_timeout = 2000"))
                c.execute(text("select 1"))
            if time.monotonic() >= self._ok_until:
                status = schema_status(self.engine)
                if not status.ok:
                    return status.message
                self._ok_until = time.monotonic() + self.ttl_s
        except SQLAlchemyError:
            self._ok_until = 0.0
            return "database unreachable"
        return None

    async def reason(self) -> str | None:
        """None when ready, else why not."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._check), self.timeout_s)
        except TimeoutError:
            return "database check timed out"


def mount_readyz(app: FastAPI, engine: Engine) -> None:
    ready = Readiness(engine)

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        reason = await ready.reason()
        if reason is None:
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "unavailable", "reason": reason}, status_code=503)
