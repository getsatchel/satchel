"""Database engines and RLS-aware session helpers.

RLS model (see migrations 0001 and 0010):
- Every workspace-scoped table has FORCE ROW LEVEL SECURITY, so even the table owner is
  filtered. (Superusers and BYPASSRLS roles are never filtered; the app must connect as a
  plain role.)
- Workspace policies compare ``workspace_id`` to ``current_setting('satchel.workspace_id', true)``;
  they apply to every role. Request traffic connects as ``satchel_app`` (``SATCHEL_DATABASE_URL``)
  and uses ``workspace_session``.
- Server-internal jobs (workers, sync, OAuth bookkeeping) use ``admin_session()``, which runs on
  a separate engine connected as ``satchel_worker`` (``SATCHEL_ADMIN_DATABASE_URL``). Each table
  has a ``<table>_worker`` policy ``TO satchel_worker USING (true)``, so the boundary is the
  database role: ``satchel_app`` cannot widen its own access by setting anything in its session.

Engines come in pairs: ``admin_session(engine=E)`` runs on the worker engine paired with the
app engine ``E`` (``create_app_engine`` / ``pair_admin_engine``; ``get_engine()`` is paired with
``get_admin_engine()``). An engine with no pair is used as-is, so passing a plain ``satchel_app``
engine fails closed (RLS hides the rows) rather than widening access.
"""

from __future__ import annotations

import logging
import threading
import uuid
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session as OrmSession

from satchel.settings import Settings, get_settings

log = logging.getLogger(__name__)

WORKER_ROLE = "satchel_worker"
# Dev-only password of the local satchel_worker role (initdb SQL / `satchel init` /
# `satchel db upgrade`).
DEV_WORKER_PASSWORD = "satchel_worker"

_warned_fallback = False
AdminTarget = Engine | str | Callable[[], Engine]
_pairs: weakref.WeakKeyDictionary[Engine, AdminTarget] = weakref.WeakKeyDictionary()
_pairs_lock = threading.Lock()


class AdminDatabaseUrlMissing(RuntimeError):
    """SATCHEL_ADMIN_DATABASE_URL is required (SATCHEL_ENV=production) but unset."""


def derived_admin_url(database_url: str) -> str:
    """``database_url`` with the user and password of the local dev ``satchel_worker`` role."""
    url = make_url(database_url).set(username=WORKER_ROLE, password=DEV_WORKER_PASSWORD)
    return url.render_as_string(hide_password=False)


def admin_database_url(settings: Settings | None = None, database_url: str | None = None) -> str:
    """The URL ``admin_session`` connects with.

    ``SATCHEL_ADMIN_DATABASE_URL`` when set. Otherwise, in development only, the app URL
    (``database_url`` or ``settings.database_url``) re-pointed at the dev ``satchel_worker`` role,
    with a one-time warning. In production a missing setting raises."""
    global _warned_fallback
    s = settings or get_settings()
    if s.admin_database_url:
        return s.admin_database_url
    if s.env == "production":
        raise AdminDatabaseUrlMissing(
            "SATCHEL_ADMIN_DATABASE_URL is required when SATCHEL_ENV=production "
            "(a satchel_worker URL; see docs/deploy/database.md)"
        )
    url = derived_admin_url(database_url or s.database_url)
    if not _warned_fallback:
        _warned_fallback = True
        log.warning(
            "SATCHEL_ADMIN_DATABASE_URL is not set; server-internal jobs connect as %s with the "
            "dev password on the SATCHEL_DATABASE_URL host (development only). Set "
            "admin_database_url to silence this.",
            WORKER_ROLE,
        )
    return url


def _create(url: str, **kw: Any) -> Engine:
    kw.setdefault("pool_pre_ping", True)
    return create_engine(url, **kw)


def pair_admin_engine(app_engine: Engine, admin: AdminTarget) -> Engine:
    """Make ``admin_session(engine=app_engine)`` run on ``admin``: an engine, a URL (connected
    lazily on first use) or a zero-argument factory. Returns ``app_engine``."""
    with _pairs_lock:
        _pairs[app_engine] = admin
    return app_engine


def create_app_engine(
    url: str, *, admin_url: str | None = None, settings: Settings | None = None, **kw: Any
) -> Engine:
    """An app-role engine paired with its worker engine (``admin_url``, else resolved by
    ``admin_database_url`` for this app URL). The worker engine connects lazily."""
    eng = _create(url, **kw)
    target = admin_url or admin_database_url(settings, url)
    return pair_admin_engine(eng, target)


def admin_engine_for(engine: Engine | None = None) -> Engine:
    """The engine ``admin_session(engine=...)`` runs on (see the module docstring)."""
    if engine is None:
        return get_admin_engine()
    with _pairs_lock:
        target = _pairs.get(engine)
        if isinstance(target, str):
            target = _create(target)
            _pairs[engine] = target
    if target is None:
        return engine
    return target if isinstance(target, Engine) else target()


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return pair_admin_engine(_create(get_settings().database_url), lambda: get_admin_engine())


@lru_cache(maxsize=1)
def get_admin_engine() -> Engine:
    return _create(admin_database_url())


def clear_engine_cache() -> None:
    """Forget the cached engines (settings changed)."""
    get_engine.cache_clear()
    get_admin_engine.cache_clear()


@contextmanager
def workspace_session(
    workspace_id: uuid.UUID, user_id: uuid.UUID | None = None, *, engine: Engine | None = None
) -> Iterator[OrmSession]:
    """Transaction with RLS context set via SET LOCAL (set_config(..., true))."""
    with OrmSession(engine or get_engine()) as session, session.begin():
        session.execute(
            text("select set_config('satchel.workspace_id', :w, true)"), {"w": str(workspace_id)}
        )
        session.execute(
            text("select set_config('satchel.user_id', :u, true)"),
            {"u": str(user_id) if user_id else ""},
        )
        yield session


@contextmanager
def admin_session(*, engine: Engine | None = None) -> Iterator[OrmSession]:
    """Transaction as ``satchel_worker`` (sees every workspace). Server-internal use only."""
    with OrmSession(admin_engine_for(engine)) as session, session.begin():
        yield session
