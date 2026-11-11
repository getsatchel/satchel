"""``satchel audit export`` and ``satchel results purge`` (deploy/storage; docs/deploy/storage-and-audit.md)."""

from __future__ import annotations

from typing import Annotated

import typer


def register_storage_commands(app: typer.Typer) -> None:
    audit_app = typer.Typer(help="Audit trail", no_args_is_help=True)
    results_app = typer.Typer(help="Cached query results", no_args_is_help=True)
    app.add_typer(audit_app, name="audit")
    app.add_typer(results_app, name="results")

    @audit_app.command("export")
    def audit_export(
        since: Annotated[str, typer.Option("--since", help="ISO-8601 start (inclusive)")],
        until: Annotated[str | None, typer.Option("--until", help="ISO-8601 end (exclusive)")] = None,
        workspace: Annotated[
            str | None, typer.Option("--workspace", "-w", help="Slug; default: every workspace")
        ] = None,
        to: Annotated[
            str | None,
            typer.Option("--to", help="A file, or gs:// / s3://bucket/prefix (one object per day); "
                                      "default stdout"),
        ] = None,
    ) -> None:
        """Export audit events as JSON Lines.

        To GCS or S3 the range is widened to whole UTC days and each day's object is overwritten
        (audit/<workspace>/<yyyy>/<mm>/<dd>.jsonl), so re-running is safe."""
        from satchel import audit

        try:
            start = audit.parse_when(since)
            end = audit.parse_when(until) if until else None
            rep = audit.export_jsonl(since=start, until=end, workspace=workspace, to=to)
        except (ValueError, LookupError, RuntimeError) as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        for f in rep.files or []:
            typer.echo(f"wrote {f}", err=True)
        typer.echo(f"exported {rep.events} event(s)", err=True)

    @results_app.command("purge")
    def results_purge(
        dry_run: Annotated[bool, typer.Option("--dry-run", help="Count only; delete nothing")] = False,
    ) -> None:
        """Delete expired cached results (rows and Parquet objects, local, GCS or S3)."""
        from satchel.data.results import purge_expired

        rep = purge_expired(dry_run=dry_run)
        typer.echo(rep.line(dry_run))
        if rep.failed:
            raise typer.Exit(1)
