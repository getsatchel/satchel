"""`satchel eval run | compare | gate | suite ...` (design §10). Registered on the ``eval`` group
by ``register_eval_commands``; kept out of ``main.py`` so that file stays small."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from satchel.api.eval_schemas import RunOut
from satchel.mcp.auth import Principal

WorkspaceOpt = Annotated[str, typer.Option("--workspace", "-w", help="Workspace slug")]


def _ws_id(slug: str) -> uuid.UUID:
    from satchel.store.db import admin_session
    from satchel.store.models import Workspace

    with admin_session() as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == slug)).first()
        if ws is None:
            typer.echo(f"unknown workspace: {slug}", err=True)
            raise typer.Exit(1)
        return ws.id


def _eval_principal(wid: uuid.UUID, user_email: str | None) -> Principal:
    from satchel.mcp.auth import Principal
    from satchel.store.db import admin_session
    from satchel.store.models import AppUser

    with admin_session() as s:
        q = select(AppUser).where(AppUser.workspace_id == wid)
        if user_email:
            q = q.where(AppUser.email == user_email)
        else:
            q = q.where(AppUser.role.in_(["admin", "curator", "analyst"])).order_by(
                AppUser.created_at
            )
        u = s.scalars(q.limit(1)).first()
        if u is None:
            typer.echo("no user to run evals as; pass --user EMAIL "
                       "(create one with `satchel token create`)", err=True)
            raise typer.Exit(1)
        return Principal(wid, u.id, u.role)


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _print_run(run: RunOut) -> None:
    typer.echo(
        f"run {run.id} suite={run.suite_key}@v{run.suite_version} "
        f"release=#{run.release_seq} ({run.release_kind}) status={run.status} "
        f"model={run.model or 'none (no LLM)'}"
    )
    typer.echo(f"cases: total={run.cases_total} pass={run.cases_pass} fail={run.cases_fail} "
               f"error={run.cases_error} skipped={run.cases_skipped}")
    for m in run.measures:
        typer.echo(f"  {m.measure:<16} {_pct(m.pass_rate):>5}  (pass {m.passed} fail {m.failed} "
                   f"error {m.errored} skipped {m.skipped} warn {m.warnings})")
    if run.tokens:
        typer.echo(f"tokens: in={run.tokens.get('input', 0)} out={run.tokens.get('output', 0)} "
                   f"seconds={run.seconds}")


def _resolve_release(s: Session, wid: uuid.UUID, release: str | None) -> uuid.UUID:
    from satchel.curation.releases import latest_published

    if release:
        return uuid.UUID(release)
    head = latest_published(s, wid)
    if head is None:
        typer.echo("workspace has no published release", err=True)
        raise typer.Exit(1)
    return head.id



def register_eval_commands(eval_app: typer.Typer) -> None:
    suite_app = typer.Typer(help="Eval suites", no_args_is_help=True)
    eval_app.add_typer(suite_app, name="suite")

    @suite_app.command("import")
    def eval_suite_import(
        file: Path,
        workspace: WorkspaceOpt,
        git: Annotated[bool, typer.Option("--git", help="Mark as managed by git")] = False,
    ) -> None:
        """Validate and store an eval suite YAML (a new version if it changed)."""
        from satchel.evals.store import save_suite
        from satchel.evals.suite import SuiteError, load_suite_file
        from satchel.store.db import workspace_session

        wid = _ws_id(workspace)
        try:
            suite = load_suite_file(file)
        except SuiteError as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(1) from e
        with workspace_session(wid) as s:
            row, created = save_suite(s, wid, suite, managed_by="git" if git else "ui")
            state = "new version" if created else "unchanged"
            typer.echo(f"{row.key} v{row.version_seq} ({state}): {len(suite.cases)} cases, "
                       f"measures {', '.join(suite.measures())}")


    @suite_app.command("list")
    def eval_suite_list(workspace: WorkspaceOpt) -> None:
        """List the workspace's eval suites."""
        from satchel.evals.store import latest_suites, suite_of
        from satchel.store.db import workspace_session

        wid = _ws_id(workspace)
        with workspace_session(wid) as s:
            for row in latest_suites(s, wid):
                suite = suite_of(row)
                typer.echo(f"{row.key:<24} v{row.version_seq:<3} {row.managed_by:<4} "
                           f"{len(suite.cases)} cases  {'LLM' if suite.needs_agent else 'no LLM'}")


    @eval_app.command("run")
    def eval_run(
        suite: Annotated[str, typer.Argument(help="A suite YAML file or a stored suite key")],
        workspace: WorkspaceOpt,
        preview: Annotated[str | None, typer.Option(help="Preview release id")] = None,
        release: Annotated[str | None, typer.Option(help="Release id (default: latest published)")]
        = None,
        model: Annotated[str | None, typer.Option(help="Override the model, e.g. claude-haiku-4-5")]
        = None,
        user: Annotated[str | None, typer.Option(help="Run as this user (email)")] = None,
        max_steps: int = 12,
    ) -> None:
        """Run a suite against a release (a preview with --preview) and print the result."""
        from satchel.api.eval_schemas import RunOut  # noqa: F401
        from satchel.api.evals import run_out
        from satchel.evals.providers import resolve_provider
        from satchel.evals.runner import run_suite
        from satchel.evals.store import latest_suite, save_suite, suite_of
        from satchel.evals.suite import SuiteError, load_suite_file
        from satchel.llm.embeddings import make_embedder
        from satchel.store.db import get_engine, workspace_session
        from satchel.store.models import EvalRun

        wid = _ws_id(workspace)
        principal = _eval_principal(wid, user)
        with workspace_session(wid, principal.user_id) as s:
            if Path(suite).is_file():
                try:
                    parsed = load_suite_file(suite)
                except SuiteError as e:
                    typer.echo(f"error: {e}", err=True)
                    raise typer.Exit(1) from e
                row, _ = save_suite(s, wid, parsed, created_by=principal.user_id)
            else:
                found = latest_suite(s, wid, suite)
                if found is None:
                    typer.echo(f"unknown suite {suite!r}", err=True)
                    raise typer.Exit(1)
                row = found
            rid = _resolve_release(s, wid, preview or release)
            s.expunge(row)
        provider = resolve_provider(suite_of(row).model, model, workspace_id=wid)
        if suite_of(row).needs_agent and provider is None:
            typer.echo("note: no LLM configured (add one in Settings → Models, or set "
                       "SATCHEL_CHAT_PROVIDER / ANTHROPIC_API_KEY); "
                       "agent-driven cases will be skipped", err=True)
        run_id = run_suite(get_engine(), make_embedder(), principal, row, rid,
                           provider=provider, max_steps=max_steps)
        with workspace_session(wid, principal.user_id) as s:
            run = s.get(EvalRun, run_id)
            assert run is not None
            _print_run(run_out(s, run))


    @eval_app.command("compare")
    def eval_compare(a: str, b: str, workspace: WorkspaceOpt) -> None:
        """Compare two runs (A = baseline, B = candidate): pass rate per measure and case moves."""
        from satchel.evals.runner import compare_summaries
        from satchel.store.db import workspace_session
        from satchel.store.models import EvalRun

        wid = _ws_id(workspace)
        with workspace_session(wid) as s:
            ra, rb = s.get(EvalRun, uuid.UUID(a)), s.get(EvalRun, uuid.UUID(b))
            if ra is None or rb is None:
                typer.echo("unknown run id", err=True)
                raise typer.Exit(1)
            cmp = compare_summaries(ra.summary, rb.summary)
        for m in cmp["measures"]:
            d = "" if m["delta"] is None else f"  ({m['delta'] * 100:+.0f} pts)"
            typer.echo(f"  {m['measure']:<16} {_pct(m['a']):>5} -> {_pct(m['b']):>5}{d}")
        for label, key in (("newly failing", "newly_failing"), ("newly passing", "newly_passing")):
            for c in cmp[key]:
                typer.echo(f"  {label}: {c['case']} ({c['a']} -> {c['b']})")


    @eval_app.command("gate")
    def eval_gate(
        preview: Annotated[str, typer.Argument(help="Preview release id")],
        workspace: WorkspaceOpt,
        model: Annotated[str | None, typer.Option(help="Override the model")] = None,
        user: Annotated[str | None, typer.Option(help="Run as this user (email)")] = None,
    ) -> None:
        """Run the required suites on a preview and its parent; exit 1 unless the gate passes."""
        from satchel.evals.gate import evaluate_gate, start_gate_runs
        from satchel.evals.providers import resolve_provider
        from satchel.evals.runner import execute_run
        from satchel.evals.store import suite_of
        from satchel.llm.embeddings import make_embedder
        from satchel.store.db import get_engine, workspace_session
        from satchel.store.models import EvalRun, EvalSuite, Release

        wid = _ws_id(workspace)
        principal = _eval_principal(wid, user)
        eng, emb = get_engine(), make_embedder()
        with workspace_session(wid, principal.user_id) as s:
            rel = s.get(Release, uuid.UUID(preview))
            if rel is None or rel.kind != "preview":
                typer.echo("unknown preview", err=True)
                raise typer.Exit(1)
            ids = start_gate_runs(eng, s, principal, rel,
                                  lambda row: resolve_provider(suite_of(row).model, model,
                                                              workspace_id=wid))
        for rid in ids:
            with workspace_session(wid, principal.user_id) as s:
                run = s.get(EvalRun, rid)
                row = s.get(EvalSuite, run.suite_id) if run else None
                assert row is not None
                prov = resolve_provider(suite_of(row).model, model, workspace_id=wid)
            execute_run(eng, emb, principal, rid, provider=prov)
        with workspace_session(wid, principal.user_id) as s:
            rel = s.get(Release, uuid.UUID(preview))
            assert rel is not None
            g = evaluate_gate(s, wid, rel)
        typer.echo(f"gate: {g.status} (threshold {g.threshold} pts)")
        for sg in g.suites:
            typer.echo(f" suite {sg.suite_key}@v{sg.suite_version}: {sg.status} {sg.message}")
            for m in (sg.comparison or {}).get("measures", []):
                typer.echo(f"   {m['measure']:<16} {_pct(m['a']):>5} -> {_pct(m['b']):>5}")
            for rg in sg.regressions:
                typer.echo(f"   REGRESSION {rg['measure']}: {rg['reason']}")
            for c in (sg.comparison or {}).get("newly_failing", []):
                typer.echo(f"   newly failing: {c['case']}")
        if g.status in ("fail", "pending", "running"):
            raise typer.Exit(1)


