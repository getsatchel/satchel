"""`satchel source ...` (design §8.3): add, list, sync, remove sources and validate an adapter
without Postgres. Registered on the ``source`` group by ``register_source_commands``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from sqlalchemy import select

from satchel.sources.registry import AdapterEntry, discover, get_entry

WorkspaceOpt = Annotated[
    str,
    typer.Option("--workspace", "-w", envvar="SATCHEL_WORKSPACE", help="Workspace slug"),
]
EXTRA = {"allow_extra_args": True, "ignore_unknown_options": True}


def _flag_fields(entry: AdapterEntry) -> dict[str, dict[str, Any]]:
    props: dict[str, dict[str, Any]] = entry.info.config_schema().get("properties", {})
    return props


def _coerce(schema: dict[str, Any], raw: str | None) -> Any:
    kinds = {t.get("type") for t in schema.get("anyOf", [])} | {schema.get("type")}
    if "boolean" in kinds:
        return True if raw is None else raw.lower() in ("1", "true", "yes")
    if raw is None:
        raise typer.BadParameter("flag needs a value")
    if "object" in kinds:  # --dbt-vars '{"start_date": "2024-01-01"}'
        try:
            return yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise typer.BadParameter(f"expected a JSON or YAML map: {e}") from None
    if "integer" in kinds:
        return int(raw)
    if "number" in kinds:
        return float(raw)
    return raw


def parse_flags(entry: AdapterEntry, args: list[str]) -> dict[str, Any]:
    """Turn ``--manifest PATH --publish preview`` into a config dict using the adapter's JSON
    schema (so a new adapter gets CLI flags for free). File inputs become absolute paths."""
    from satchel.sources.config import HostSettings

    props = {**HostSettings.model_json_schema().get("properties", {}), **_flag_fields(entry)}
    props.pop("git_token", None)  # stored encrypted: set it on the Sources page
    files = {f.name for f in entry.info.file_inputs}
    # With a git repository, file inputs are paths inside it, not local paths.
    from_git = any(a == "--git-url" or a.startswith("--git-url=") for a in args)
    out: dict[str, Any] = {}
    i = 0
    while i < len(args):
        tok = args[i]
        if not tok.startswith("--"):
            raise typer.BadParameter(f"unexpected argument {tok!r}")
        name, eq, val = tok[2:].partition("=")
        field = name.replace("-", "_")
        if field not in props:
            known = ", ".join("--" + p.replace("_", "-") for p in sorted(props))
            raise typer.BadParameter(f"unknown option --{name}; this source accepts: {known}")
        schema = props[field]
        is_bool = schema.get("type") == "boolean"
        if not eq and not is_bool:
            i += 1
            if i >= len(args):
                raise typer.BadParameter(f"--{name} needs a value")
            val = args[i]
        value = _coerce(schema, val if (eq or not is_bool) else None)
        if field in files and isinstance(value, str) and not from_git:
            value = str(Path(value).expanduser().resolve())
        if schema.get("type") == "array":
            out.setdefault(field, []).append(value)
        else:
            out[field] = value
        i += 1
    return out


def _load_config_file(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise typer.BadParameter(f"{path}: expected a mapping")
    return data


def _echo_warnings(warnings: list[Any]) -> None:
    for w in warnings:
        d = w if isinstance(w, dict) else w.model_dump()
        where = f" [{d['key']}]" if d.get("key") else ""
        typer.echo(f"  warning {d['code']}{where}: {d['message']}")


def register_source_commands(app: typer.Typer) -> None:
    @app.command("kinds")
    def kinds() -> None:
        """List installed source adapters (built-in and external)."""
        _print_kinds(discover())

    @app.command("list")
    def list_(workspace: WorkspaceOpt) -> None:
        """List installed adapters and this workspace's configured sources."""
        entries = discover()
        _print_kinds(entries)
        from satchel.cli.main import _ws_id  # noqa: PLC0415
        from satchel.sources.automation import interval_of, next_sync_at
        from satchel.sources.config import WEBHOOK_KEY
        from satchel.store.db import workspace_session
        from satchel.store.models import ContextItem, ContextSource

        with workspace_session(_ws_id(workspace)) as s:
            rows = list(s.scalars(select(ContextSource).order_by(ContextSource.name)))
            typer.echo("")
            if not rows:
                typer.echo("no sources configured (add one: satchel source add dbt NAME --manifest PATH)")
            for src in rows:
                n = len(list(s.scalars(select(ContextItem.id).where(
                    ContextItem.source_id == src.id, ContextItem.state != "deprecated"))))
                e = entries.get(src.kind)
                tag = f"{e.info.version} {e.info.maturity}" if e and e.adapter else "not installed"
                when = src.last_synced_at.isoformat(timespec="seconds") if src.last_synced_at \
                    else "never synced"
                typer.echo(f"{src.name:<16} {src.kind:<8} {tag:<22} {src.last_status or '-':<6} "
                           f"{n:>4} items  publish={src.config.get('publish', 'auto')}  {when}")
                if src.last_error:
                    typer.echo(f"  error: {src.last_error}")
                hours = interval_of(src)
                auto = [f"every {hours}h, next {nxt.isoformat(timespec='minutes')}"
                        if hours and (nxt := next_sync_at(src)) else "",
                        "push webhook" if src.config.get(WEBHOOK_KEY) else ""]
                if any(auto):
                    typer.echo("  automatic: " + ", ".join(x for x in auto if x))

    @app.command("add", context_settings=EXTRA)
    def add(
        ctx: typer.Context,
        kind: Annotated[str, typer.Argument(help="Adapter kind, e.g. dbt (see: satchel source kinds)")],
        name: Annotated[str, typer.Argument(help="Name for this source")],
        workspace: WorkspaceOpt,
        config_file: Annotated[
            Path | None, typer.Option("--config", help="YAML/JSON config file (flags override)")
        ] = None,
    ) -> None:
        """Add or update a source. Flags come from the adapter, e.g. for dbt:
        --manifest PATH [--semantic-manifest PATH] [--catalog PATH] [--connection NAME]
        [--publish auto|preview] [--priority N]. See `satchel source info KIND`."""
        from satchel.sources.config import SourceConfigError
        from satchel.sources.service import add_source

        try:
            entry = get_entry(kind)
            cfg = _load_config_file(config_file) if config_file else {}
            cfg.update(parse_flags(entry, list(ctx.args)))
            src = add_source(workspace, kind, name, cfg)
        except (LookupError, ValueError, SourceConfigError) as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        typer.echo(f"source {src.name!r} ({src.kind}) saved. Sync it: satchel source sync {src.name}")

    @app.command("info")
    def info(kind: str) -> None:
        """Show an adapter's options (generated from its config model)."""
        try:
            entry = get_entry(kind, require_available=False)
        except LookupError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        i = entry.info
        typer.echo(f"{i.display_name} ({i.kind}) {i.version} [{i.maturity}] "
                   f"{'built-in' if entry.builtin else 'external: ' + entry.package}")
        typer.echo(f"capabilities: {', '.join(sorted(i.capabilities))}")
        if entry.missing:
            typer.echo(f"UNAVAILABLE: needs {', '.join(entry.missing)}; install with: "
                       f"{entry.install_hint}")
        if i.description:
            typer.echo(i.description)
        typer.echo("options (also: --publish auto|preview, --priority N, --connection NAME, "
                   "--git-url URL [--git-ref REF] to read the files from a public repository):")
        for field, schema in _flag_fields(entry).items():
            folders = {f.name for f in i.file_inputs if f.multiple}
            req = " (file or folder)" if field in folders else (
                " (file)" if field in {f.name for f in i.file_inputs} else "")
            typer.echo(f"  --{field.replace('_', '-')}{req}  {schema.get('description', '')}")

    @app.command("sync")
    def sync(
        workspace: WorkspaceOpt,
        name: Annotated[str | None, typer.Argument(help="Source name")] = None,
        all_: Annotated[bool, typer.Option("--all", help="Sync every source")] = False,
    ) -> None:
        """Sync a source (or --all) and print what changed."""
        from satchel.cli.main import _ws_id  # noqa: PLC0415
        from satchel.sources.apply import SourceSyncError
        from satchel.sources.service import sync_source
        from satchel.store.db import workspace_session
        from satchel.store.models import ContextSource

        if bool(name) == all_:
            typer.echo("give a source name or --all", err=True)
            raise typer.Exit(1)
        with workspace_session(_ws_id(workspace)) as s:
            names = [name] if name else list(s.scalars(
                select(ContextSource.name).order_by(ContextSource.name)))
        failed = 0
        for n in names:
            try:
                r = sync_source(workspace, str(n))
            except (LookupError, SourceSyncError) as e:
                typer.echo(f"{n}: error: {e}", err=True)
                failed += 1
                continue
            c = r.counts()
            typer.echo(f"{r.source} ({r.kind}): created={c['created']} updated={c['updated']} "
                       f"unchanged={c['unchanged']} deprecated={c['deprecated']} held={c['held']}")
            for label, keys in (("created", r.created), ("updated", r.updated),
                                ("deprecated", r.deprecated), ("held", r.held)):
                for k in keys:
                    typer.echo(f"  {label}: {k}")
            if r.release_kind:
                state = "new" if r.release_created else "unchanged"
                typer.echo(f"{r.release_kind} release #{r.release_seq} ({state}); "
                           f"chunks embedded: {r.chunks_embedded}")
            if r.gate_message:
                typer.echo(f"  not published: {r.gate_message}")
            if r.signals:
                typer.echo("  curation signals: " + ", ".join(f"{k}={v}" for k, v in r.signals.items()))
            for o in r.overlaps:
                if o["type"] == "asset_doc":
                    also = ", ".join(f"{x['source']}" for x in o["also_described_by"])
                    typer.echo(f"  overlap: {o['asset']} also described by {also}; primary is "
                               f"{o['primary']['source']}")
            _echo_warnings([w for w in r.warnings if w["code"] != "also_described_by"])
        if not names:
            typer.echo("no sources configured")
        if failed:
            raise typer.Exit(1)

    @app.command("webhook")
    def webhook(
        name: Annotated[str, typer.Argument(help="Source name")],
        workspace: WorkspaceOpt,
        rotate: Annotated[bool, typer.Option(
            "--rotate", help="Replace the existing secret (the old one stops working)")] = False,
        remove: Annotated[bool, typer.Option(
            "--remove", help="Remove the webhook; deliveries are refused from then on")] = False,
    ) -> None:
        """Set up a push webhook for a source and print its URL and secret (shown once).

        With a webhook already set up it prints the URL; --rotate makes a new secret.
        --remove turns the webhook off.
        GitHub: Settings > Webhooks, application/json, the secret, push events.
        GitLab: Settings > Webhooks, the secret as Secret token, push events."""
        from satchel.cli.main import _ws_id  # noqa: PLC0415
        from satchel.core.crypto import EncryptionUnavailable, Keyring, configured
        from satchel.settings import get_settings
        from satchel.sources import admin as source_admin
        from satchel.sources.automation import webhook_url
        from satchel.store.db import admin_session
        from satchel.store.models import ContextSource

        if rotate and remove:
            raise typer.BadParameter("give --rotate or --remove, not both")
        st = get_settings()
        ws = _ws_id(workspace)
        with admin_session() as s:
            src = s.scalars(select(ContextSource).where(
                ContextSource.workspace_id == ws, ContextSource.name == name)).first()
            if src is None:
                typer.echo(f"no source named {name!r} in {workspace}", err=True)
                raise typer.Exit(1)
            url = webhook_url(src, st.public_url)
            if remove:
                done = source_admin.remove_webhook(s, src, None)
                typer.echo(f"webhook for {name!r} removed" if done
                           else f"{name!r} has no webhook")
                return
            if url and not rotate:
                typer.echo(f"webhook for {name!r} is set up: {url}")
                typer.echo("the secret is not shown again; --rotate makes a new one")
                return
            keyring = Keyring.from_settings(st) if configured(st) else None
            try:
                secret, rotated = source_admin.set_webhook(s, src, None, keyring)
            except (source_admin.SourceAdminError, EncryptionUnavailable) as e:
                typer.echo(str(e), err=True)
                raise typer.Exit(1) from None
            url = webhook_url(src, st.public_url) or ""
        typer.echo(f"webhook for {name!r} {'rotated (the old secret no longer works)' if rotated else 'set up'}")
        typer.echo(f"  url:    {url}")
        typer.echo(f"  secret: {secret}")
        typer.echo("the secret is shown once; add both to the repository's webhook settings")
        if not st.public_url.startswith("https://"):
            typer.echo(f"note: SATCHEL_PUBLIC_URL is {st.public_url}; the git host must be able "
                       "to reach it", err=True)

    @app.command("remove")
    def remove(name: str, workspace: WorkspaceOpt) -> None:
        """Remove a source. Its items are deprecated (they leave the next release)."""
        from satchel.sources.service import remove_source

        try:
            n = remove_source(workspace, name)
        except LookupError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        typer.echo(f"removed source {name!r}; {n} item(s) deprecated and detached")

    @app.command("validate", context_settings=EXTRA)
    def validate(
        ctx: typer.Context,
        kind: Annotated[str, typer.Argument(help="Adapter kind")],
        config_file: Annotated[
            Path | None, typer.Option("--config", help="YAML/JSON config file (flags override)")
        ] = None,
        as_json: Annotated[bool, typer.Option("--json", help="Print the full snapshot")] = False,
        out: Annotated[Path | None, typer.Option("--out", help="Write the snapshot JSON")] = None,
    ) -> None:
        """Load an adapter and print its snapshot. No database, no server: the contributor
        inner loop. Flags are the adapter's options, e.g. --manifest target/manifest.json."""
        from satchel.sources.config import SourceConfigError, read_files, validate_config

        try:
            entry = get_entry(kind)
            cfg_raw = _load_config_file(config_file) if config_file else {}
            cfg_raw.update(parse_flags(entry, list(ctx.args)))
            _, cfg = validate_config(entry, cfg_raw)
            files = read_files(entry, cfg)
            assert entry.adapter is not None
            snap = entry.adapter.load(cfg, files)
        except (LookupError, SourceConfigError, FileNotFoundError, ValueError) as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
        if out:
            out.write_text(snap.canonical_json())
        if as_json:
            typer.echo(snap.canonical_json(), nl=False)
            return
        typer.echo(f"{entry.info.display_name} adapter {entry.info.version}, IR {snap.ir_version}, "
                   f"fingerprint {snap.fingerprint[:12]}")
        for k, v in snap.summary().items():
            typer.echo(f"  {k:<16} {v}")
        for a in snap.assets[:12]:
            typer.echo(f"  asset {a.fqn:<40} {len(a.columns)} columns  {a.key}")
        if len(snap.assets) > 12:
            typer.echo(f"  ... {len(snap.assets) - 12} more assets (use --json for everything)")
        for m in snap.semantic_models:
            typer.echo(f"  semantic model {m.name}: metrics {', '.join(m.queryable_metrics)}")
            for n in m.notes:
                typer.echo(f"    note: {n}")
        _echo_warnings(snap.warnings)


def _print_kinds(entries: dict[str, AdapterEntry]) -> None:
    typer.echo("installed adapters:")
    for e in entries.values():
        origin = "built-in" if e.builtin else f"external ({e.package} {e.package_version or ''})"
        if e.adapter is None:
            typer.echo(f"  {e.kind:<10} FAILED TO LOAD  {origin}: {e.error}")
        else:
            i = e.info
            typer.echo(f"  {e.kind:<10} {i.version:<8} {i.maturity:<13} {origin}")
            if e.missing:
                typer.echo(f"             unavailable: needs {', '.join(e.missing)}; "
                           f"install with: {e.install_hint}")
