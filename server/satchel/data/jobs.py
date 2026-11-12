"""The ``connection.crawl`` job: crawl one connection into the catalog
(``satchel.data.crawl.crawl_connection``), queued by **Crawl** in Settings → Connections
(``POST /v1/connections/{id}/crawl``). At most one crawl of a connection is queued or running.
A connection error (bad credentials, unreachable warehouse) fails the job at once."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from sqlalchemy import Engine

from satchel.data.connections import ConnectionError_
from satchel.data.crawl import crawl_connection
from satchel.jobs import queue
from satchel.jobs.queue import Claimed
from satchel.jobs.runner import Permanent, Runtime, register
from satchel.store.db import admin_session
from satchel.store.models import Connection, Workspace

KIND = "connection.crawl"


def queue_crawl(engine: Engine, workspace_id: uuid.UUID, connection_id: uuid.UUID
                ) -> tuple[int, bool]:
    """Queue a crawl. Returns ``(job id, queued)``; False when one was already unfinished."""
    key = str(connection_id)
    with admin_session(engine=engine) as s:
        jid = queue.enqueue(s, workspace_id, KIND, {"connection_id": key}, dedupe_key=key,
                            max_attempts=2)
        if jid is not None:
            return jid, True
        found = queue.unfinished(s, KIND, key)
    if found is None:  # it finished in between
        return queue_crawl(engine, workspace_id, connection_id)
    return found, False


def _handle(rt: Runtime, job: Claimed) -> dict[str, Any]:
    with admin_session(engine=rt.engine) as s:
        conn = s.get(Connection, uuid.UUID(job.payload["connection_id"]))
        ws = s.get(Workspace, job.workspace_id)
        if conn is None or ws is None:
            raise Permanent("the connection was deleted")
        slug, name = ws.slug, conn.name
    try:
        rep = crawl_connection(slug, name, engine=rt.engine, settings=rt.settings)
    except ConnectionError_ as e:
        raise Permanent(str(e)) from None
    return asdict(rep)


register(KIND, _handle)
