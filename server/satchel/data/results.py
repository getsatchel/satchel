"""Result cache (design §11): Arrow -> Parquet in a ``ResultStore`` (local directory or GCS,
see :mod:`satchel.data.result_store`), indexed by the ``query_result`` table. ``get_result``
pages through the store; ``purge_expired`` deletes results past their expiry
(``result_ttl_days``, which a workspace can override: see :mod:`satchel.limits`)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as OrmSession

from satchel.data.result_store import (
    LocalResultStore,
    ResultStore,
    configured_store,
    store_for_ref,
)
from satchel.settings import get_settings
from satchel.store.db import admin_session
from satchel.store.models import QueryResult

log = logging.getLogger("satchel.results")


def result_root() -> Path:
    return Path(get_settings().result_dir).expanduser()


def store_result(
    s: OrmSession,
    *,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID | None,
    tool_call_id: int | None,
    connection_id: uuid.UUID | None,
    sql: str,
    table: pa.Table,
    truncated: bool,
    root: Path | None = None,
    store: ResultStore | None = None,
) -> QueryResult:
    from satchel.limits import for_workspace

    ttl_days = for_workspace(s, workspace_id, get_settings()).result_ttl_days
    rid = uuid.uuid4()
    st = store or (LocalResultStore(root) if root is not None else configured_store())
    ref = st.write(workspace_id, rid, table)
    row = QueryResult(
        id=rid,
        workspace_id=workspace_id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        connection_id=connection_id,
        sql=sql,
        row_count=table.num_rows,
        truncated=truncated,
        columns=[{"name": f.name, "type": str(f.type)} for f in table.schema],
        path=ref,
        expires_at=datetime.now(UTC) + timedelta(days=ttl_days),
    )
    s.add(row)
    s.flush()
    return row


def read_page(
    row: QueryResult,
    offset: int,
    limit: int,
    *,
    root: Path | None = None,
    store: ResultStore | None = None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    st = store or (LocalResultStore(root) if root is not None else store_for_ref(row.path))
    return st.read_page(row.path, offset, limit)


@dataclass
class PurgeReport:
    rows: int = 0  # expired query_result rows found (deleted unless dry_run)
    objects_deleted: int = 0
    objects_missing: int = 0  # already gone
    failed: int = 0  # object delete errored: the row is kept and retried next pass

    def line(self, dry_run: bool = False) -> str:
        verb = "would purge" if dry_run else "purged"
        return (f"{verb} {self.rows} expired result(s): {self.objects_deleted} object(s) deleted, "
                f"{self.objects_missing} already gone, {self.failed} failed")


def purge_expired(
    *, engine: Engine | None = None, dry_run: bool = False, now: datetime | None = None,
    batch: int = 500,
) -> PurgeReport:
    """Delete expired ``query_result`` rows and their objects (local or GCS).

    The object goes first; if that fails the row stays so the next pass retries, instead of
    orphaning the object. Server-internal job: runs as the worker role across workspaces."""
    cutoff = now or datetime.now(UTC)
    report = PurgeReport()
    skip: set[uuid.UUID] = set()
    while True:
        with admin_session(engine=engine) as s:
            stmt = (select(QueryResult).where(QueryResult.expires_at <= cutoff)
                    .order_by(QueryResult.expires_at).limit(batch))
            if skip:
                stmt = stmt.where(QueryResult.id.not_in(skip))
            rows = list(s.scalars(stmt))
            if not rows:
                return report
            report.rows += len(rows)
            if dry_run:
                # Dry run does not delete, so paging by id keeps the loop finite.
                skip.update(r.id for r in rows)
                for r in rows:
                    _count_object(r.path, report)
                continue
            for r in rows:
                try:
                    if store_for_ref(r.path).delete(r.path):
                        report.objects_deleted += 1
                    else:
                        report.objects_missing += 1
                except FileNotFoundError:
                    report.objects_missing += 1
                except Exception:
                    log.exception("could not delete result object %s", r.path)
                    report.failed += 1
                    skip.add(r.id)
                    report.rows -= 1
                    continue
                s.delete(r)


def _count_object(ref: str, report: PurgeReport) -> None:
    try:
        if store_for_ref(ref).exists(ref):
            report.objects_deleted += 1
        else:
            report.objects_missing += 1
    except FileNotFoundError:
        report.objects_missing += 1
    except Exception:
        log.exception("could not check result object %s", ref)
        report.failed += 1
        report.rows -= 1


@dataclass
class Preview:
    markdown: str
    shown: int


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return format(round(v, 6), "f").rstrip("0").rstrip(".") if abs(v) < 1e15 else str(v)
    return str(v).replace("|", "\\|").replace("\n", " ")[:120]


def markdown_table(cols: list[str], rows: list[tuple[Any, ...]]) -> str:
    head = "| " + " | ".join(cols) + " |\n| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join("| " + " | ".join(_cell(v) for v in r) + " |" for r in rows)
    return head + ("\n" + body if body else "")


def json_cell(v: Any) -> Any:
    """A JSON-safe cell for the result widget: numbers stay numbers, the rest become text."""
    if v is None or isinstance(v, bool | int | str):
        return v
    if isinstance(v, float):
        return v if v == v and abs(v) != float("inf") else None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def column_kind(arrow_type: str) -> str:
    """'time' | 'number' | 'text' from an Arrow type name; drives the widget's auto chart."""
    t = arrow_type.lower()
    if t.startswith(("timestamp", "date")):
        return "time"
    if t.startswith(("int", "uint", "float", "double", "decimal", "halffloat")):
        return "number"
    return "text"
