"""``satchel worker``: run curation for every workspace on an interval (design §7.1 nightly job),
sync sources whose schedule is due (``satchel.sources.automation``) and run eval suites whose
schedule is due (``satchel.evals.schedule``).

Everything heavy is a job (``satchel.jobs``), so it can run beside the API or as a Cloud Run
Job / Kubernetes CronJob (``--once``). Every ``curate_interval_min`` the pass queues a
``curation.run`` job per workspace and purges expired results, old sessions and old jobs. A
thread queues the sources whose scheduled sync is due every ``source_tick_min`` minutes, and
another the eval suites whose schedule is due every ``eval_tick_min`` minutes. Job threads
(``worker_job_threads``) run every queued job: those, and the syncs, crawls, eval runs and
curation runs the web servers queued. ``--once`` runs the pass and one check of each, then
every due job.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from types import FrameType

from sqlalchemy import Engine, select

from satchel.curation.run import CurationReport, run_curation
from satchel.jobs.runner import JobRunner, Runtime
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


def queue_curations(*, workspace: str | None = None, use_llm: bool = True,
                    engine: Engine | None = None) -> list[int]:
    """Queue a ``curation.run`` job for one workspace (by slug) or each of them; the job
    threads run them. A workspace whose pass is still queued or running is not queued again."""
    from satchel.curation.jobs import queue_curation

    eng = engine or get_engine()
    with admin_session(engine=eng) as s:
        q = select(Workspace.id, Workspace.slug).order_by(Workspace.slug)
        rows = [(wid, slug) for wid, slug in s.execute(q) if workspace in (None, slug)]
    if workspace is not None and not rows:
        raise LookupError(f"unknown workspace: {workspace}")
    ids: list[int] = []
    for wid, slug in rows:
        try:
            ids.append(queue_curation(eng, wid, use_llm=use_llm)[0])
        except Exception:
            log.exception("queueing curation of %s failed", slug)
    return ids


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


def run_evals(workspace: str | None = None) -> None:
    """Run the scheduled eval suites that are due (of one workspace, or all). Never fails."""
    from satchel.evals.schedule import run_due_evals

    try:
        ws_id = None
        if workspace is not None:
            with admin_session() as s:
                ws_id = s.scalars(select(Workspace.id).where(Workspace.slug == workspace)).first()
            if ws_id is None:
                return
        run_due_evals(workspace_id=ws_id)
    except Exception:
        log.exception("scheduled eval runs failed")


def purge_jobs() -> None:
    """Delete finished jobs older than two weeks. Never fails the pass."""
    from satchel.jobs.queue import purge

    try:
        with admin_session() as s:
            n = purge(s)
    except Exception:
        log.exception("job purge failed")
        return
    if n:
        log.info("purged %d finished job(s)", n)


def default_jobs(settings: Settings) -> JobRunner:
    """The worker's consumers: every job kind."""
    return JobRunner(Runtime(get_engine(), settings), threads=settings.worker_job_threads,
                     poll_s=settings.job_poll_s, name="worker")


def _tick_loop(stop: threading.Event, every_s: float, fn: Callable[[], object],
               what: str = "source") -> None:
    while not stop.is_set():
        try:
            fn()
        except Exception:
            log.exception("%s tick failed", what)
        stop.wait(every_s)


def run_forever(
    *, interval_min: int | None = None, workspace: str | None = None, once: bool = False,
    stop: threading.Event | None = None, pass_fn: Callable[[], object] | None = None,
    use_llm: bool = True, sources_fn: Callable[[], object] | None = None,
    tick_min: int | None = None, evals_fn: Callable[[], object] | None = None,
    eval_tick_min: int | None = None, jobs: JobRunner | None = None,
) -> None:
    """Loop until ``stop`` is set (SIGINT/SIGTERM set it). ``once`` runs a single pass.

    Scheduled source syncs (``sources_fn``, default :func:`sync_sources`) run on a separate
    thread every ``tick_min`` minutes (``SATCHEL_SOURCE_TICK_MIN``; 0 = off). A caller that
    passes its own ``pass_fn`` (tests) gets no source ticks unless it passes ``sources_fn``.
    Scheduled evals (``evals_fn``, default :func:`run_evals`) work the same way with
    ``eval_tick_min`` (``SATCHEL_EVAL_TICK_MIN``). ``jobs`` (default: :func:`default_jobs`
    unless ``pass_fn`` is given) runs queued jobs on its threads, or all due jobs after a
    ``once`` pass."""
    st = get_settings()
    interval = (interval_min if interval_min is not None else st.curate_interval_min) * 60
    tick = (tick_min if tick_min is not None else st.source_tick_min) * 60
    stop = stop or threading.Event()
    tick_fn = sources_fn or (None if pass_fn else (lambda: sync_sources(workspace)))
    if not tick:
        tick_fn = None
    eval_tick = (eval_tick_min if eval_tick_min is not None else st.eval_tick_min) * 60
    eval_fn = evals_fn or (None if pass_fn else (lambda: run_evals(workspace)))
    if not eval_tick:
        eval_fn = None
    runner = jobs or (None if pass_fn else default_jobs(st))

    def then_nudge(fn: Callable[[], object] | None) -> Callable[[], object] | None:
        """The tick queues jobs; have an idle job thread pick them up now."""
        if fn is None or runner is None:
            return fn
        queue_fn, r = fn, runner

        def ticked() -> None:
            queue_fn()
            r.nudge()
        return ticked

    tick_fn, eval_fn = then_nudge(tick_fn), then_nudge(eval_fn)

    def _halt(_sig: int, _frame: FrameType | None) -> None:
        stop.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _halt)
        signal.signal(signal.SIGINT, _halt)
    def default_pass() -> None:
        try:
            queue_curations(workspace=workspace, use_llm=use_llm)
            if runner is not None:
                runner.nudge()
        finally:
            purge_results()
            purge_sessions()
            purge_jobs()

    do = pass_fn or default_pass
    tickers: list[threading.Thread] = []
    if not once and runner is not None:
        runner.start()
    if not once:
        for fn, every, name in ((tick_fn, tick, "source"), (eval_fn, eval_tick, "eval")):
            if fn is not None:
                t = threading.Thread(target=_tick_loop, args=(stop, every, fn, name),
                                     name=f"satchel-{name}s", daemon=True)
                t.start()
                tickers.append(t)
    try:
        while not stop.is_set():
            try:
                do()
            except Exception:
                log.exception("curation pass failed")
            if once:
                for fn in (tick_fn, eval_fn):
                    if fn is not None:
                        fn()
                if runner is not None:
                    runner.drain()
                return
            log.info("next curation pass in %d minute(s)", interval // 60)
            stop.wait(interval)
    finally:
        stop.set()
        for t in tickers:
            t.join(timeout=5)
        if runner is not None:
            runner.stop()
