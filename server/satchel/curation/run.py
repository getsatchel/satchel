"""The curation pipeline for one workspace (design §7): embed signals -> hygiene -> golden
candidates -> gap detection -> suggestion drafting.

Runs in-process: from the CLI, or as a ``curation.run`` job (``satchel.curation.jobs``) that the
worker's pass and ``POST /v1/curate/run`` queue. Each phase has its own transaction, so a failing
LLM never rolls back clustering; a Postgres advisory lock stops two runs from clustering the same
workspace at once.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, select, text

from satchel.curation.config import CurationConfig, load_config
from satchel.curation.gaps import GapReport, detect_gaps
from satchel.curation.golden import detect_golden_candidates
from satchel.curation.hygiene import HygieneReport, run_hygiene
from satchel.curation.merge import merge_duplicates
from satchel.curation.signal_text import embed_pending_signals
from satchel.curation.suggest import DraftResult, draft_suggestion, undrafted
from satchel.llm.chat import ChatProvider
from satchel.llm.connections import chat_for_workspace
from satchel.llm.embeddings import EmbeddingProvider, make_embedder
from satchel.settings import Settings, get_settings
from satchel.store.db import admin_session, get_engine, workspace_session
from satchel.store.models import Workspace

log = logging.getLogger("satchel.curation")


@dataclass
class CurationReport:
    workspace: str
    llm: str = "none"
    embedded: int = 0
    hygiene: HygieneReport = field(default_factory=HygieneReport)
    golden_candidates: int = 0
    gaps: GapReport = field(default_factory=GapReport)
    drafts: list[DraftResult] = field(default_factory=list)
    undrafted_left: int = 0
    merged: int = 0  # duplicate suggestions folded into an identical older one
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """The counts ``POST /v1/curate/run`` returns (``CurateRunOut``) and the audit logs."""
        drafted = [d for d in self.drafts if d.status == "drafted"]
        return {
            "workspace": self.workspace, "llm": self.llm, "embedded": self.embedded,
            "hygiene": dict(self.hygiene.emitted), "hygiene_notes": list(self.hygiene.notes),
            "golden_candidates": self.golden_candidates,
            "signals_considered": self.gaps.signals_considered,
            "clusters_found": self.gaps.clusters_found,
            "below_threshold": self.gaps.below_threshold,
            "suggestions_created": len(self.gaps.created),
            "suggestions_extended": len(self.gaps.attached),
            "drafted": len(drafted), "changes_proposed": sum(d.changes for d in drafted),
            "invalid_dropped": sum(d.dropped for d in drafted),
            "draft_failed": sum(1 for d in self.drafts if d.status == "failed"),
            "undrafted_left": self.undrafted_left, "suggestions_merged": self.merged,
            "errors": list(self.errors),
        }

    def lines(self) -> list[str]:
        out = [
            f"workspace {self.workspace}: llm={self.llm}",
            f"  embedded {self.embedded} signal(s)",
            f"  hygiene: {self.hygiene.line()}",
            f"  golden-query candidates: {self.golden_candidates}",
            f"  gaps: {self.gaps.line()}",
        ]
        drafted = [d for d in self.drafts if d.status == "drafted"]
        out.append(
            f"  drafting: {len(drafted)} drafted ({sum(d.changes for d in drafted)} change(s), "
            f"{sum(d.dropped for d in drafted)} invalid dropped), "
            f"{sum(1 for d in self.drafts if d.status == 'failed')} failed, "
            f"{self.undrafted_left} left undrafted"
        )
        if self.merged:
            out.append(f"  merged {self.merged} duplicate suggestion(s) into identical ones")
        out += [f"  ERROR {e}" for e in self.errors]
        return out


@contextmanager
def run_lock(engine: Engine, workspace_id: uuid.UUID) -> Iterator[bool]:
    """Session-level advisory lock per workspace; yields False when another run holds it."""
    with engine.connect() as conn:
        got = bool(conn.execute(
            text("select pg_try_advisory_lock(hashtextextended(:k, 0))"),
            {"k": f"satchel-curate:{workspace_id}"},
        ).scalar())
        conn.commit()
        try:
            yield got
        finally:
            if got:
                conn.execute(
                    text("select pg_advisory_unlock(hashtextextended(:k, 0))"),
                    {"k": f"satchel-curate:{workspace_id}"},
                )
                conn.commit()


def resolve_workspace(
    workspace: str | uuid.UUID, engine: Engine
) -> tuple[uuid.UUID, str, CurationConfig]:
    with admin_session(engine=engine) as s:
        q = select(Workspace)
        if isinstance(workspace, uuid.UUID):
            q = q.where(Workspace.id == workspace)
        else:
            q = q.where(Workspace.slug == workspace)
        ws = s.scalars(q).first()
        if ws is None:
            raise LookupError(f"unknown workspace: {workspace}")
        return ws.id, ws.slug, load_config(ws.settings)


def run_curation(
    workspace: str | uuid.UUID, *, engine: Engine | None = None,
    embedder: EmbeddingProvider | None = None, chat: ChatProvider | None = None,
    use_llm: bool = True, settings: Settings | None = None, now: datetime | None = None,
) -> CurationReport | None:
    """Run the pipeline. ``chat`` overrides the configured provider; ``use_llm=False`` skips
    every model call (clustering and hygiene still run). Returns ``None`` when another run for
    the workspace is in progress."""
    eng = engine or get_engine()
    st = settings or get_settings()
    emb = embedder or make_embedder(st)
    ws_id, slug, cfg = resolve_workspace(workspace, eng)
    provider: ChatProvider | None = None
    if use_llm:
        provider = chat or chat_for_workspace(ws_id, st, eng)
        if provider is None:
            log.warning(
                "curation[%s]: no LLM configured (add a default connection in Settings → "
                "Models, or set SATCHEL_CHAT_PROVIDER): suggestion drafting is skipped; "
                "clustering and hygiene still run", slug)
    report = CurationReport(workspace=slug, llm=provider.model_id if provider else "none")

    with run_lock(eng, ws_id) as got:
        if not got:
            log.warning("curation[%s]: another run is in progress; skipping", slug)
            return None

        def phase(name: str, fn: Callable[[], None]) -> None:
            try:
                fn()
            except Exception as e:
                log.exception("curation[%s]: %s failed", slug, name)
                report.errors.append(f"{name}: {type(e).__name__}: {e}")

        def embed() -> None:
            with workspace_session(ws_id, engine=eng) as s:
                report.embedded = embed_pending_signals(s, ws_id, emb)

        def hygiene() -> None:
            with admin_session(engine=eng) as s:
                report.hygiene = run_hygiene(s, ws_id, cfg, emb, provider, now=now)

        def golden() -> None:
            with workspace_session(ws_id, engine=eng) as s:
                report.golden_candidates = detect_golden_candidates(s, ws_id, cfg, now=now)

        def gaps() -> None:
            # Hygiene and golden signals are new rows: embed them too (cheap) before clustering.
            with workspace_session(ws_id, engine=eng) as s:
                report.embedded += embed_pending_signals(s, ws_id, emb)
            with workspace_session(ws_id, engine=eng) as s:
                report.gaps = detect_gaps(s, ws_id, cfg, now=now)

        def draft() -> None:
            with workspace_session(ws_id, engine=eng) as s:
                ids = undrafted(s, ws_id, cfg.max_drafts, golden_only=provider is None)
                total = len(undrafted(s, ws_id, 10_000))
            report.undrafted_left = total - len(ids)
            for sid in ids:
                with workspace_session(ws_id, engine=eng) as s:
                    r = draft_suggestion(s, sid, provider, emb, cfg, now=now)
                report.drafts.append(r)
                if r.status == "failed":
                    log.warning("curation[%s]: draft failed: %s", slug, r.note)
            if provider is None:
                report.undrafted_left = total - sum(1 for d in report.drafts
                                                    if d.status == "drafted")

        phase("embed", embed)
        phase("hygiene", hygiene)
        phase("golden", golden)
        phase("gaps", gaps)
        def merge() -> None:
            with workspace_session(ws_id, engine=eng) as s:
                report.merged = len(merge_duplicates(s, ws_id))

        phase("draft", draft)
        phase("merge", merge)
    for line in report.lines():
        log.info(line)
    return report
