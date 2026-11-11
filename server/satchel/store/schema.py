"""Schema revision checks: is the database at the revision this satchel build needs?

Used by the startup guard of ``satchel mcp stdio`` / ``serve api`` / ``serve mcp`` (a clear message
instead of ``UndefinedColumn`` on every call) and by ``satchel status`` / ``doctor`` / ``up``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError


def alembic_config(url: str | None = None) -> Config:
    store = Path(__file__).resolve().parent
    cfg = Config(str(store / "alembic.ini"))
    cfg.set_main_option("script_location", str(store / "migrations"))
    if url:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None
    return head


def known_revisions() -> set[str]:
    return {r.revision for r in ScriptDirectory.from_config(alembic_config()).walk_revisions()}


@dataclass(frozen=True)
class SchemaStatus:
    state: str  # ok | behind | ahead | empty | unreadable
    current: str | None
    head: str

    @property
    def ok(self) -> bool:
        # An unreadable alembic_version (e.g. no SELECT grant) is not proof of a mismatch.
        return self.state in ("ok", "unreadable")

    @property
    def message(self) -> str:
        if self.state == "behind":
            return (
                f"database is at {self.current} but this satchel needs {self.head}"
                " — run: satchel upgrade"
            )
        if self.state == "ahead":
            return (
                f"database is at {self.current}, which this satchel ({self.head}) does not know"
                " — run: satchel upgrade (it installs the newer satchel)"
            )
        if self.state == "empty":
            return "database has no satchel schema yet — run: satchel init (or: satchel upgrade)"
        return f"database is at {self.head}"


def schema_status(engine: Engine) -> SchemaStatus:
    """Compare the database's alembic revision with this build's head.

    Raises ``OperationalError`` when the database cannot be reached."""
    head = head_revision()
    try:
        with engine.connect() as c:
            current = c.execute(text("select version_num from alembic_version")).scalar()
    except ProgrammingError as e:
        if "does not exist" in str(e) or "UndefinedTable" in type(e.orig).__name__:
            return SchemaStatus("empty", None, head)
        return SchemaStatus("unreadable", None, head)
    except OperationalError:
        raise
    except DBAPIError:
        return SchemaStatus("unreadable", None, head)
    if current is None:
        return SchemaStatus("empty", None, head)
    if current == head:
        return SchemaStatus("ok", current, head)
    return SchemaStatus("behind" if current in known_revisions() else "ahead", current, head)


def guard_message(engine: Engine) -> str | None:
    """None when the database is usable by this build, else the one-line reason."""
    try:
        status = schema_status(engine)
    except OperationalError as e:
        reason = str(e.orig).strip().splitlines()[0] if e.orig else "connection failed"
        return f"cannot reach the database ({reason}) — run: satchel doctor"
    return None if status.ok else status.message
