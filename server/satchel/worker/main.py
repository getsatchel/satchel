"""``satchel worker``: run curation for every workspace on an interval (design §7.1 nightly job),
and sync sources whose schedule is due (``satchel.sources.automation``).

Deliberately small: no queue, one process, a per-workspace Postgres advisory lock so it can
run beside the API or as a Cloud Run Job / Kubernetes CronJob (``--once``). Scheduled source
syncs run on their own thread every ``source_tick_min`` minutes (a per-source advisory lock),
so a long curation pass never delays them; ``--once`` runs one check after the pass.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from types import FrameType

from sqlalchemy import Engine, select

from satchel.curation.run import CurationReport, run_curation
from satchel.llm.chat import ChatProvider
from satchel.llm.embeddings import EmbeddingProvider, make_embedder
from satchel.settings import Settings, get_settings
from satchel.store.db import admin_session, get_engine
from satchel.store.models import Workspace

log = logging.getLogger("satchel.worker")


def run_once(
    *, workspace: str | None = None, engine: Engine | None = None,
    embedder: EmbeddingProvider | None = None, chat: ChatProvider | None = None,
    settings: Settings | None = None, use_llm: bool = True,
) -> list[CurationReport]:
    """One curation pass over one workspace (by slug) or all of them."""
    eng = engine or get_engine()
    st = settings or get_settings()
    emb = embedder or make_embedder(st)
    with admin_session(engine=eng) as s:
        slugs = list(s.scalars(select(Workspace.slug).order_by(Workspace.slug)))
    if workspace is not None:
        slugs = [x for x in slugs if x == workspace]
        if not slugs:
            raise LookupError(f"unknown workspace: {workspace}")
    reports: list[CurationReport] = []
    for slug in slugs:
        try:
            r = run_curation(slug, engine=eng, embedder=emb, chat=chat, settings=st,
                             use_llm=use_llm)
        except Exception:
            log.exception("curation of %s failed", slug)
            continue
        if r is not None:
            reports.append(r)
    return reports


def purge_results() -> None:
    """Drop expired cached results (rows and objects). Never fails the pass."""
    from satchel.data.results import purge_expired

    try:
        report = purge_expired()
    except Exception:
        log.exception("result purge failed")
        return
    if report.rows or report.failed:
        log.info(report.line())


def purge_sessions() -> None:
    """Apply each workspace's session retention. Never fails the pass."""
    from satchel.limits import purge_old_sessions

    try:
        report = purge_old_sessions(get_settings())
    except Exception:
        log.exception("session retention failed")
        return
    if report.sessions:
        log.info(report.line())


def sync_sources(workspace: str | None = None) -> None:
    """Sync the scheduled sources that are due (of one workspace, or all). Never fails."""
    from satchel.sources.automation import sync_due_sources

    try:
        ws_id = None
        if workspace is not None:
            with admin_session() as s:
                ws_id = s.scalars(select(Workspace.id).where(Workspace.slug == workspace)).first()
            if ws_id is None:
                return
        sync_due_sources(workspace_id=ws_id)
    except Exception:
        log.exception("scheduled source syncs failed")


def _tick_loop(stop: threading.Event, every_s: float, fn: Callable[[], object]) -> None:
    while not stop.is_set():
        try:
            fn()
        except Exception:
            log.exception("source tick failed")
        stop.wait(every_s)


def run_forever(
    *, interval_min: int | None = None, workspace: str | None = None, once: bool = False,
    stop: threading.Event | None = None, pass_fn: Callable[[], object] | None = None,
    use_llm: bool = True, sources_fn: Callable[[], object] | None = None,
    tick_min: int | None = None,
) -> None:
    """Loop until ``stop`` is set (SIGINT/SIGTERM set it). ``once`` runs a single pass.

    Scheduled source syncs (``sources_fn``, default :func:`sync_sources`) run on a separate
    thread every ``tick_min`` minutes (``SATCHEL_SOURCE_TICK_MIN``; 0 = off). A caller that
    passes its own ``pass_fn`` (tests) gets no source ticks unless it passes ``sources_fn``."""
    st = get_settings()
    interval = (interval_min if interval_min is not None else st.curate_interval_min) * 60
    tick = (tick_min if tick_min is not None else st.source_tick_min) * 60
    stop = stop or threading.Event()
    tick_fn = sources_fn or (None if pass_fn else (lambda: sync_sources(workspace)))
    if not tick:
        tick_fn = None

    def _halt(_sig: int, _frame: FrameType | None) -> None:
        stop.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _halt)
        signal.signal(signal.SIGINT, _halt)
    def default_pass() -> None:
        try:
            run_once(workspace=workspace, use_llm=use_llm)
        finally:
            purge_results()
            purge_sessions()

    do = pass_fn or default_pass
    ticker: threading.Thread | None = None
    if tick_fn is not None and not once:
        ticker = threading.Thread(target=_tick_loop, args=(stop, tick, tick_fn),
                                  name="satchel-sources", daemon=True)
        ticker.start()
    try:
        while not stop.is_set():
            try:
                do()
            except Exception:
                log.exception("curation pass failed")
            if once:
                if tick_fn is not None:
                    tick_fn()
                return
            log.info("next curation pass in %d minute(s)", interval // 60)
            stop.wait(interval)
    finally:
        stop.set()
        if ticker is not None:
            ticker.join(timeout=5)
