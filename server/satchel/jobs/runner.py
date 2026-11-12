"""Job consumers (``satchel.jobs.queue``): a registry of job kinds and :class:`JobRunner`, the
threads that claim and run them.

The worker runs every kind; the web server runs a few consumer threads for the kinds a person
starts from the UI or the API (eval runs), so they run even when the worker is a CronJob or off,
and a restart no longer loses them: an unfinished job is claimed again once its lease runs out.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine

from satchel.jobs import queue
from satchel.jobs.queue import Claimed
from satchel.llm.chat import ChatProvider
from satchel.llm.embeddings import EmbeddingProvider, make_embedder
from satchel.settings import Settings
from satchel.store.db import admin_session

log = logging.getLogger("satchel.jobs")


@dataclass
class Runtime:
    """What a job handler gets: the engine, settings, embedder and (tests) a chat override."""

    engine: Engine
    settings: Settings
    _embedder: EmbeddingProvider | None = None
    chat: ChatProvider | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def embedder(self) -> EmbeddingProvider:
        with self._lock:
            if self._embedder is None:
                self._embedder = make_embedder(self.settings)
            return self._embedder


class RetryLater(Exception):  # noqa: N818 - a signal, not an error
    """Raised by a handler that cannot run yet (a source another sync holds): the attempt
    ends quietly and the job is retried after the backoff."""


class Permanent(Exception):  # noqa: N818
    """Raised by a handler for a failure a retry cannot fix (a bad configuration): the job
    fails at once with this message."""


Handler = Callable[[Runtime, Claimed], dict[str, Any] | None]
GiveUp = Callable[[Runtime, Claimed, str], None]


@dataclass(frozen=True)
class Kind:
    name: str
    handle: Handler
    give_up: GiveUp | None = None


KINDS: dict[str, Kind] = {}
# The kinds each web server process runs too: what a person or a webhook starts there.
WEB_KINDS = {"eval.run", "source.sync", "connection.crawl", "curation.run"}


def register(name: str, handle: Handler, *, give_up: GiveUp | None = None) -> Kind:
    """Register a job kind. ``give_up`` runs when a job of the kind fails for good (its last
    attempt raised, or its process stopped). The attempt limit is set per job by ``enqueue``."""
    k = Kind(name, handle, give_up)
    KINDS[name] = k
    return k


def load_kinds() -> dict[str, Kind]:
    """Import the modules that register the built-in kinds."""
    import satchel.curation.jobs  # noqa: F401
    import satchel.data.jobs  # noqa: F401
    import satchel.evals.jobs  # noqa: F401
    import satchel.sources.jobs  # noqa: F401

    return KINDS


class JobRunner:
    """Claims and runs jobs of ``kinds`` (default: every registered kind) on ``threads``
    daemon threads. :meth:`start` is idempotent; :meth:`wake` starts it and nudges the threads
    (an enqueue in this process); other processes' jobs are found by polling every ``poll_s``."""

    def __init__(self, rt: Runtime, *, kinds: set[str] | None = None, threads: int = 2,
                 poll_s: float = 5.0, name: str = "worker",
                 lease_s: int = queue.LEASE_S) -> None:
        load_kinds()
        self.rt, self.threads, self.poll_s, self.lease_s = rt, threads, poll_s, lease_s
        self.kinds = sorted(kinds if kinds is not None else KINDS)
        self.ident = f"{name}:{socket.gethostname()}:{os.getpid()}"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._start_lock = threading.Lock()

    # ---- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        with self._start_lock:
            if self._threads or self.threads <= 0:
                return
            self._stop.clear()
            for i in range(self.threads):
                t = threading.Thread(target=self._loop, name=f"satchel-jobs-{i}", daemon=True)
                t.start()
                self._threads.append(t)

    def wake(self) -> None:
        """Start the threads if needed and have an idle one look for jobs now."""
        self.start()
        self.nudge()

    def nudge(self) -> None:
        """Have an idle thread (if any runs) look for jobs now."""
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout)
        self._threads = []

    # ---- work --------------------------------------------------------------------------------

    def run_one(self) -> bool:
        """Claim and run one job. False when none was due."""
        who = f"{self.ident}:{threading.current_thread().name}"
        self._reap()
        with admin_session(engine=self.rt.engine) as s:
            job = queue.claim(s, who, self.kinds, lease_s=self.lease_s)
        if job is None:
            return False
        self._run(job, who)
        return True

    def drain(self, max_jobs: int = 1000) -> int:
        """Run due jobs in this thread until none is left (``satchel worker --once``, tests)."""
        n = 0
        while n < max_jobs and self.run_one():
            n += 1
        return n

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.run_one():
                    continue
            except Exception:
                log.exception("job loop failed")
            self._wake.wait(self.poll_s)
            self._wake.clear()

    def _reap(self) -> None:
        with admin_session(engine=self.rt.engine) as s:
            lost = queue.reap(s, self.kinds)
        for job in lost:
            log.warning("job %s (%s) lost its process %d time(s); giving up",
                        job.id, job.kind, job.attempts)
            self._give_up(job, "the process running it stopped")

    def _give_up(self, job: Claimed, error: str) -> None:
        kind = KINDS.get(job.kind)
        if kind is None or kind.give_up is None:
            return
        try:
            kind.give_up(self.rt, job, error)
        except Exception:
            log.exception("give-up hook of job %s (%s) failed", job.id, job.kind)

    def _run(self, job: Claimed, who: str) -> None:
        kind = KINDS.get(job.kind)
        done = threading.Event()

        def beat() -> None:
            while not done.wait(self.lease_s / 4):
                try:
                    with admin_session(engine=self.rt.engine) as s:
                        if not queue.heartbeat(s, job.id, who, lease_s=self.lease_s):
                            return
                except Exception:
                    log.exception("heartbeat of job %s failed", job.id)

        hb = threading.Thread(target=beat, name=f"satchel-job-{job.id}-hb", daemon=True)
        hb.start()
        try:
            if kind is None:
                raise LookupError(f"no handler for job kind {job.kind!r}")
            log.info("job %s (%s) attempt %d/%d", job.id, job.kind, job.attempts,
                     job.max_attempts)
            result = kind.handle(self.rt, job)
        except Exception as e:  # noqa: BLE001 - a failing job never stops the consumer
            if isinstance(e, RetryLater):
                log.info("job %s (%s) will retry: %s", job.id, job.kind, e)
                msg = str(e) or "retry later"
            elif isinstance(e, Permanent):
                log.warning("job %s (%s) failed: %s", job.id, job.kind, e)
                msg = str(e)
            else:
                log.exception("job %s (%s) failed", job.id, job.kind)
                msg = f"{type(e).__name__}: {e}"
            done.set()
            with admin_session(engine=self.rt.engine) as s:
                final = queue.fail(s, job, who, msg, final=isinstance(e, Permanent))
            if final:
                self._give_up(job, msg)
            return
        finally:
            done.set()
            hb.join(1)
        with admin_session(engine=self.rt.engine) as s:
            queue.finish(s, job.id, who, result)
