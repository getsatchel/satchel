"""Audit trail: recording events, keyset-paginated reads, and JSONL export (design §9).

``audit_event`` is written by the REST routers (``satchel.api.common.audit``) and by the service
functions behind security-relevant actions that the CLI, OAuth and OIDC flows also reach
(tokens, users, sources, connections, consent, login). Those call :func:`record` directly.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select, tuple_
from sqlalchemy.orm import Session

from satchel.data.objstore import BucketLike, is_object_uri, join_uri, open_bucket, split_uri
from satchel.store.db import admin_session
from satchel.store.models import AppUser, AuditEvent, Workspace


def record(
    s: Session,
    workspace_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    target: dict[str, Any],
) -> None:
    """Add an audit event in the caller's transaction (same commit as the action itself)."""
    s.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=action,
            target=json.loads(json.dumps(target, default=str)),
        )
    )
    s.flush()


# ---- reading ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditRow:
    id: int
    workspace_id: uuid.UUID
    workspace: str
    actor_user_id: uuid.UUID | None
    actor_email: str | None
    action: str
    target: dict[str, Any]
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": str(self.workspace_id),
            "workspace": self.workspace,
            "actor_user_id": str(self.actor_user_id) if self.actor_user_id else None,
            "actor_email": self.actor_email,
            "action": self.action,
            "target": self.target,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
        }

    def json_line(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)


Cursor = tuple[datetime, int]


def encode_cursor(c: Cursor) -> str:
    raw = f"{c[0].astimezone(UTC).isoformat()}|{c[1]}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(token: str) -> Cursor:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        ts, _, i = raw.partition("|")
        return datetime.fromisoformat(ts), int(i)
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError("invalid cursor") from e


def parse_when(value: str) -> datetime:
    """An ISO-8601 date or timestamp; naive values are UTC."""
    try:
        d = datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"not an ISO-8601 date or time: {value!r}") from e
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def fetch_page(
    s: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    after: Cursor | None = None,
    limit: int = 500,
    workspace_id: uuid.UUID | None = None,
) -> tuple[list[AuditRow], Cursor | None]:
    """Events ordered by (created_at, id), ``since <= created_at < until``, after ``after``.

    Returns the rows and a cursor for the next page (None when this was the last). The
    session's RLS context decides which workspaces are visible."""
    stmt = (
        select(AuditEvent, Workspace.slug, AppUser.email)
        .join(Workspace, Workspace.id == AuditEvent.workspace_id)
        .outerjoin(AppUser, AppUser.id == AuditEvent.actor_user_id)
        .order_by(AuditEvent.created_at, AuditEvent.id)
        .limit(limit + 1)
    )
    if workspace_id is not None:
        stmt = stmt.where(AuditEvent.workspace_id == workspace_id)
    if since is not None:
        stmt = stmt.where(AuditEvent.created_at >= since)
    if until is not None:
        stmt = stmt.where(AuditEvent.created_at < until)
    if after is not None:
        stmt = stmt.where(tuple_(AuditEvent.created_at, AuditEvent.id) > tuple_(*after))
    found = list(s.execute(stmt))
    more = len(found) > limit
    rows = [
        AuditRow(e.id, e.workspace_id, slug, e.actor_user_id, email, e.action, e.target,
                 e.created_at)
        for e, slug, email in found[:limit]
    ]
    nxt = (rows[-1].created_at, rows[-1].id) if more and rows else None
    return rows, nxt


# ---- export ----------------------------------------------------------------------------------


def day_key(workspace: str, day: date) -> str:
    return f"audit/{workspace}/{day:%Y}/{day:%m}/{day:%d}.jsonl"


@dataclass
class ExportReport:
    events: int = 0
    files: list[str] | None = None

    def __post_init__(self) -> None:
        if self.files is None:
            self.files = []


def _floor_day(d: datetime) -> datetime:
    d = d.astimezone(UTC)
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _iter_workspace(
    engine: Engine, ws_id: uuid.UUID, since: datetime | None, until: datetime | None,
    batch: int = 1000,
) -> Iterator[AuditRow]:
    after: Cursor | None = None
    while True:
        with admin_session(engine=engine) as s:
            rows, after = fetch_page(
                s, since=since, until=until, after=after, limit=batch, workspace_id=ws_id
            )
        yield from rows
        if after is None:
            return


def _export_workspace_objects(
    bucket: BucketLike, scheme: str, bucket_name: str, prefix: str, slug: str,
    rows: Iterator[AuditRow], report: ExportReport,
) -> None:
    """Write one object per UTC day from rows ordered by created_at."""
    day: date | None = None
    lines: list[str] = []

    def flush(d: date | None, buf: list[str]) -> None:
        if d is None or not buf:
            return
        key = "/".join(p for p in (prefix, day_key(slug, d)) if p)
        bucket.put(key, ("\n".join(buf) + "\n").encode(), "application/x-ndjson")
        assert report.files is not None
        report.files.append(join_uri(scheme, bucket_name, key))

    for r in rows:
        d = r.created_at.astimezone(UTC).date()
        if d != day:
            flush(day, lines)
            day, lines = d, []
        lines.append(r.json_line())
        report.events += 1
    flush(day, lines)


def export_jsonl(
    *,
    since: datetime,
    until: datetime | None = None,
    workspace: str | None = None,
    to: str | None = None,
    engine: Engine | None = None,
    bucket: BucketLike | None = None,
) -> ExportReport:
    """Export audit events as JSON Lines.

    ``to`` is a file path (one file, exactly the requested range), ``-``/None (stdout), or
    ``gs://bucket/prefix`` / ``s3://bucket/prefix`` (one object per workspace and UTC day,
    ``<prefix>/audit/<workspace>/<yyyy>/<mm>/<dd>.jsonl``). The object-store export is
    idempotent: the range is widened to whole UTC days and each day's object is rewritten in
    full, so running it again (or a longer range) never leaves a partial day."""
    import sys

    to_objects = to is not None and is_object_uri(to)
    if to_objects:
        since = _floor_day(since)
        if until is not None and until != _floor_day(until):
            until = _floor_day(until) + timedelta(days=1)
    with admin_session(engine=engine) as s:
        stmt = select(Workspace.id, Workspace.slug).order_by(Workspace.slug)
        if workspace is not None:
            stmt = stmt.where(Workspace.slug == workspace)
        targets = [(wid, slug) for wid, slug in s.execute(stmt)]
    if workspace is not None and not targets:
        raise LookupError(f"unknown workspace: {workspace}")

    report = ExportReport()
    assert report.files is not None
    if to_objects:
        assert to is not None
        scheme, bname, prefix = split_uri(to)
        b = bucket or open_bucket(scheme, bname)
        for wid, slug in targets:
            _export_workspace_objects(
                b, scheme, bname, prefix, slug,
                _iter_workspace(engine or _default_engine(), wid, since, until), report,
            )
        return report

    out = sys.stdout if to in (None, "-") else open(Path(str(to)).expanduser(), "w")  # noqa: SIM115
    try:
        for wid, _slug in targets:
            for r in _iter_workspace(engine or _default_engine(), wid, since, until):
                out.write(r.json_line() + "\n")
                report.events += 1
    finally:
        if out is not sys.stdout:
            out.close()
    if to not in (None, "-"):
        report.files.append(str(to))
    return report


def _default_engine() -> Engine:
    from satchel.store.db import get_engine

    return get_engine()
