import os
import uuid
from importlib.metadata import version as _version
from pathlib import Path
from typing import Annotated

import typer
from alembic import command
from sqlalchemy import select

from satchel.cli.evals_cmds import register_eval_commands
from satchel.cli.secrets_cmds import register_secrets_commands
from satchel.cli.source_cmds import register_source_commands
from satchel.cli.storage_cmds import register_storage_commands
from satchel.install_hints import DIST
from satchel.store.schema import alembic_config

app = typer.Typer(help="Satchel: context server for data agents", no_args_is_help=True)
db_app = typer.Typer(help="Database commands", no_args_is_help=True)
serve_app = typer.Typer(help="Run servers", no_args_is_help=True)
sync_app = typer.Typer(help="Git / folder sync", no_args_is_help=True)
eval_app = typer.Typer(help="Evaluations", no_args_is_help=True)
token_app = typer.Typer(help="Personal access tokens", no_args_is_help=True)
mcp_app = typer.Typer(help="MCP server", no_args_is_help=True)
workspace_app = typer.Typer(help="Workspace settings", no_args_is_help=True)
conn_app = typer.Typer(help="Data connections (data plane)", no_args_is_help=True)
curate_app = typer.Typer(help="Curation: gap detection, hygiene, suggestions", no_args_is_help=True)
app.add_typer(curate_app, name="curate")
app.add_typer(token_app, name="token")
app.add_typer(mcp_app, name="mcp")
app.add_typer(workspace_app, name="workspace")
app.add_typer(conn_app, name="connection")
app.add_typer(db_app, name="db")
app.add_typer(serve_app, name="serve")
app.add_typer(sync_app, name="sync")
app.add_typer(eval_app, name="eval")
source_app = typer.Typer(help="Sources: dbt and other upstream tools", no_args_is_help=True)
app.add_typer(source_app, name="source")
register_source_commands(source_app)
register_storage_commands(app)  # audit export, results purge (satchel/cli/storage_cmds.py)
register_secrets_commands(app)  # generate-key, rotate (satchel/cli/secrets_cmds.py)
register_eval_commands(eval_app)  # run / compare / gate / suite (satchel/cli/evals_cmds.py)

WorkspaceOpt = Annotated[
    str,
    typer.Option(
        "--workspace", "-w", envvar="SATCHEL_WORKSPACE",
        help="Workspace slug (default: `workspace` in the config file)",
    ),
]


@app.callback()
def _root(
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Config file profile (SATCHEL_PROFILE; default 'local')"),
    ] = None,
) -> None:
    from satchel.cli.config import ConfigError, get_profile

    if profile:
        os.environ["SATCHEL_PROFILE"] = profile
    if not os.environ.get("SATCHEL_WORKSPACE"):
        try:
            ws = get_profile().get("workspace")
        except ConfigError:
            ws = None  # commands that need settings report the broken file themselves
        if isinstance(ws, str) and ws:
            os.environ["SATCHEL_WORKSPACE"] = ws


def _require_schema() -> None:
    """Startup guard: refuse to serve a database this build cannot use."""
    from satchel.store.db import AdminDatabaseUrlMissing, admin_database_url, get_engine
    from satchel.store.schema import guard_message

    try:
        admin_database_url()
    except AdminDatabaseUrlMissing as e:
        typer.echo(f"satchel: {e}", err=True)
        raise typer.Exit(1) from e
    msg = guard_message(get_engine())
    if msg:
        typer.echo(f"satchel: {msg}", err=True)
        raise typer.Exit(1)


@app.command("version")
def version() -> None:
    """Print the satchel version."""
    typer.echo(_version(DIST))


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Run alembic migrations up to REVISION (default: head)."""
    from satchel.cli.onboarding import _ensure_worker_from
    from satchel.settings import get_settings

    command.upgrade(alembic_config(), revision)
    # A local pre-0010 database gets satchel_worker as NOLOGIN; give it its dev login.
    _ensure_worker_from(get_settings())


@db_app.command("bootstrap")
def db_bootstrap(
    owner: Annotated[
        str | None,
        typer.Option(help="Role that runs migrations and owns the tables (default: you)"),
    ] = None,
) -> None:
    """Create the vector extension, the satchel_app and satchel_worker roles, grants and default
    privileges. Idempotent; re-run to rotate passwords.

    Connects with SATCHEL_BOOTSTRAP_DATABASE_URL (a superuser, or Cloud SQL's postgres user).
    Passwords come from SATCHEL_APP_DB_PASSWORD / SATCHEL_WORKER_DB_PASSWORD; an unset one keeps the
    existing role's password."""
    from pydantic import SecretStr

    from satchel.settings import get_settings
    from satchel.store.roles import APP_ROLE, WORKER_ROLE, BootstrapError, bootstrap

    s = get_settings()
    if not s.bootstrap_database_url:
        typer.secho("error: set SATCHEL_BOOTSTRAP_DATABASE_URL (an admin URL)", fg="red", err=True)
        raise typer.Exit(1)

    def pw(v: SecretStr | None) -> str | None:
        return v.get_secret_value() if v is not None else None

    try:
        res = bootstrap(
            s.bootstrap_database_url,
            {APP_ROLE: pw(s.app_db_password), WORKER_ROLE: pw(s.worker_db_password)},
            owner=owner,
        )
    except BootstrapError as e:
        typer.secho(f"error: {e}", fg="red", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"database {res.database}, owner {res.owner}")
    for c in res.changes:
        typer.echo(f"  {c}")
    if not res.changes:
        typer.echo("  nothing to change")


@app.command("openapi")
def openapi(
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Write to a file")] = None,
) -> None:
    """Print the REST API's OpenAPI document (the web UI generates its TS types from it)."""
    import json

    from satchel.api.app import create_app
    from satchel.llm.embeddings import HashEmbedder

    # Schema only: no database access or embedding model is needed.
    doc = create_app(embedder=HashEmbedder(), mcp=False).openapi()
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if out is not None:
        out.write_text(text)
    else:
        typer.echo(text, nl=False)


@serve_app.command("api")
def serve_api(
    host: str = "127.0.0.1",
    port: Annotated[int, typer.Option(envvar="PORT", help="Listen port (env: PORT)")] = 8080,
    reload: bool = False,
) -> None:
    """Run the REST API + /mcp + OAuth/OIDC (bearer or session cookie; SATCHEL_DEV_AUTH=1 for
    unverified dev headers)."""
    import uvicorn

    from satchel.runtime import enforce_production, uvicorn_log_config
    from satchel.settings import get_settings

    st = get_settings()
    enforce_production(st)
    _require_schema()
    uvicorn.run(
        "satchel.api.app:create_app", factory=True, host=host, port=port, reload=reload,
        log_config=uvicorn_log_config(st.log_format),
    )


@serve_app.command("mcp")
def serve_mcp(host: str = "127.0.0.1", port: int = 8081) -> None:
    """Run only /mcp (PAT or OAuth) plus the OAuth routes. `serve api` also mounts them."""
    import uvicorn

    from satchel.runtime import enforce_production, uvicorn_log_config
    from satchel.settings import get_settings

    st = get_settings()
    enforce_production(st)
    _require_schema()
    uvicorn.run(
        "satchel.mcp.http:create_standalone_app", factory=True, host=host, port=port,
        log_config=uvicorn_log_config(st.log_format),
    )


@mcp_app.command("stdio")
def mcp_stdio() -> None:
    """Run the MCP server over stdio. Needs SATCHEL_TOKEN and SATCHEL_DATABASE_URL."""
    from satchel.mcp.stdio import run_stdio

    run_stdio()


@token_app.command("create")
def token_create(
    workspace: WorkspaceOpt,
    user: Annotated[str, typer.Option("--user", help="User email (created if missing)")],
    name: Annotated[str, typer.Option("--name", help="Label for this token")],
    role: Annotated[str, typer.Option(help="Role if the user is created")] = "analyst",
    expires_in_days: Annotated[int | None, typer.Option(help="Expiry; default never")] = None,
) -> None:
    """Create a personal access token. The token is printed once."""
    from satchel.mcp.auth import create_token

    try:
        raw, tid = create_token(
            workspace, user, name, role=role, expires_in_days=expires_in_days
        )
    except LookupError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"token id: {tid}", err=True)
    typer.echo(raw)


@token_app.command("revoke")
def token_revoke(
    token: Annotated[str | None, typer.Option("--token", help="The raw token")] = None,
    token_id: Annotated[str | None, typer.Option("--id", help="Token id")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace", "-w")] = None,
    name: Annotated[str | None, typer.Option("--name")] = None,
) -> None:
    """Revoke a token by value, id, or --workspace + --name."""
    from satchel.mcp.auth import revoke_token

    try:
        n = revoke_token(
            raw=token,
            token_id=uuid.UUID(token_id) if token_id else None,
            workspace_slug=workspace,
            name=name,
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"revoked {n} token(s)")


@workspace_app.command("create")
def workspace_create(
    slug: Annotated[str, typer.Argument(help="Workspace slug")],
    admin_email: Annotated[
        str, typer.Option("--admin-email", help="OIDC login with this email maps to admin")
    ],
    name: Annotated[str | None, typer.Option("--name", help="Display name (default: slug)")] = None,
) -> None:
    """Create a workspace and its first admin. Idempotent: re-running changes nothing."""
    from satchel.cli.ops import ensure_workspace_admin
    from satchel.store.db import get_engine

    _, _, notes = ensure_workspace_admin(get_engine(), slug, admin_email, name=name)
    for n in notes:
        typer.echo(n)
    if not notes:
        typer.echo(f"workspace '{slug}' and admin {admin_email} already exist")


@workspace_app.command("auth")
def workspace_auth(
    workspace: WorkspaceOpt,
    provision: Annotated[
        str | None,
        typer.Option(help="Unknown IdP users: 'explorer' (create) or 'reject' (default)"),
    ] = None,
    allow_domain: Annotated[
        list[str] | None,
        typer.Option("--allow-domain", help="Only provision emails in these domains (repeat)"),
    ] = None,
) -> None:
    """Show or set how OIDC logins are mapped to users in WORKSPACE."""
    from satchel.store.db import admin_session
    from satchel.store.models import Workspace

    if provision not in (None, "explorer", "reject"):
        typer.echo("--provision must be 'explorer' or 'reject'", err=True)
        raise typer.Exit(1)
    with admin_session() as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == workspace)).first()
        if ws is None:
            typer.echo(f"unknown workspace: {workspace}", err=True)
            raise typer.Exit(1)
        cfg = dict((ws.settings or {}).get("auth", {}))
        if provision is not None:
            cfg["provision"] = provision
        if allow_domain is not None:
            cfg["allowed_email_domains"] = [d.lower().lstrip("@") for d in allow_domain]
        if provision is not None or allow_domain is not None:
            ws.settings = {**(ws.settings or {}), "auth": cfg}
            from satchel import audit

            audit.record(s, ws.id, None, "workspace.auth.update", {"auth": cfg, "via": "cli"})
        typer.echo(f"auth: {cfg or {'provision': 'reject'}}")


@sync_app.command("import")
def sync_import(path: Path, workspace: WorkspaceOpt) -> None:
    """Import a context repo folder into WORKSPACE (created if missing). Idempotent."""
    from satchel.sync import ImportValidationError, import_context

    try:
        r = import_context(path, workspace)
    except ImportValidationError as e:
        for err in e.errors:
            typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1) from e
    c = r.counts()
    typer.echo(
        f"created={c['created']} updated={c['updated']} unchanged={c['unchanged']} "
        f"deprecated={c['deprecated']}"
    )
    groups = (("created", r.created), ("updated", r.updated), ("deprecated", r.deprecated))
    for label, keys in groups:
        for k in keys:
            typer.echo(f"  {label}: {k}")
    rel = f"release #{r.release_seq}" + (" (new)" if r.release_created else " (unchanged)")
    typer.echo(f"{rel}; chunks embedded: {r.chunks_embedded}")
    if r.eval_suites:
        typer.echo(f"eval suites: {', '.join(r.eval_suites)}")
    for sr in r.sources:
        sc = sr.counts()
        typer.echo(f"source {sr.source}: created={sc['created']} updated={sc['updated']} "
                   f"unchanged={sc['unchanged']} deprecated={sc['deprecated']} "
                   f"({sr.release_kind or 'no'} release #{sr.release_seq})")
    for err in r.source_errors:
        typer.echo(f"error: {err}", err=True)
    skipped = [f for f in r.skipped_files if not f.startswith("evals/")]
    if skipped:
        typer.echo(f"skipped (not a context file): {', '.join(skipped)}")
    if r.source_errors:
        raise typer.Exit(1)


@sync_app.command("reembed")
def sync_reembed(workspace: WorkspaceOpt) -> None:
    """Re-embed chunks whose model differs from SATCHEL_EMBEDDING_PROVIDER's."""
    from satchel.llm.embeddings import make_embedder
    from satchel.store.db import admin_session
    from satchel.store.models import Workspace
    from satchel.sync.embedding_sync import reembed_stale

    emb = make_embedder()
    with admin_session() as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == workspace)).first()
        if ws is None:
            typer.echo(f"unknown workspace: {workspace}", err=True)
            raise typer.Exit(1)
        n = reembed_stale(s, ws.id, emb)
    typer.echo(f"re-embedded {n} chunks with {emb.model_id}")


def _ws_id(slug: str) -> uuid.UUID:
    from satchel.store.db import admin_session
    from satchel.store.models import Workspace

    with admin_session() as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == slug)).first()
        if ws is None:
            typer.echo(f"unknown workspace: {slug}", err=True)
            raise typer.Exit(1)
        return ws.id


@app.command("search")
def search(
    query: str,
    workspace: WorkspaceOpt,
    k: int = 10,
    types: Annotated[str | None, typer.Option(help="Comma-separated item types")] = None,
) -> None:
    """Hybrid search over published context."""
    from satchel.llm.embeddings import make_embedder
    from satchel.retrieval import search_context
    from satchel.store.db import workspace_session

    wid = _ws_id(workspace)
    with workspace_session(wid) as s:
        hits = search_context(
            s, query, types=types.split(",") if types else None, k=k, embedder=make_embedder()
        )
    typer.echo(f"{'#':>2}  {'score':>7}  {'type':<14} {'item':<52} sources")
    for i, h in enumerate(hits, start=1):
        src = ",".join(f"{n}:{r}" for n, r in h.sources.items())
        typer.echo(f"{i:>2}  {h.score:>7.4f}  {h.type:<14} {h.tag:<52} {src}")


@eval_app.command("retrieval")
def eval_retrieval(
    file: Path,
    workspace: WorkspaceOpt,
    k: int = 5,
    verbose: bool = False,
) -> None:
    """Report recall@K and MRR for labelled questions in FILE."""
    from satchel.evals.retrieval import load_cases, run_retrieval_eval
    from satchel.llm.embeddings import make_embedder
    from satchel.store.db import workspace_session

    wid = _ws_id(workspace)
    emb = make_embedder()
    with workspace_session(wid) as s:
        res = run_retrieval_eval(s, load_cases(file), emb, k=k)
    for c in res.cases:
        if verbose or c.recall < 1.0:
            mark = "ok  " if c.recall == 1.0 else "MISS"
            typer.echo(f"{mark} {c.question}\n       want {c.expected}\n       got  {c.got}")
    typer.echo(
        f"embedder={emb.model_id} cases={len(res.cases)} "
        f"recall@{k}={res.recall_at_k:.3f} MRR={res.mrr:.3f}"
    )


@conn_app.command("add")
def connection_add(
    workspace: WorkspaceOpt,
    name: Annotated[str, typer.Option("--name", help="Connection name")],
    kind: Annotated[str, typer.Option("--kind", help="duckdb | postgres | bigquery | snowflake")],
    path: Annotated[str | None, typer.Option("--path", help="DuckDB file")] = None,
    parquet: Annotated[
        list[str] | None, typer.Option("--parquet", help="DuckDB view as name=path_or_url")
    ] = None,
    host: Annotated[str | None, typer.Option(help="Postgres host")] = None,
    port: Annotated[int | None, typer.Option(help="Postgres port")] = None,
    database: Annotated[str | None, typer.Option(help="Postgres database")] = None,
    user: Annotated[str | None, typer.Option(help="Postgres user")] = None,
    role: Annotated[str | None, typer.Option(help="Postgres role to SET ROLE to")] = None,
    secret_ref: Annotated[
        str | None, typer.Option("--secret-ref", help="env:NAME holding the password/token")
    ] = None,
) -> None:
    """Add or update a connection. Secrets are referenced (env:NAME), never stored."""
    from pathlib import Path

    from satchel.data.connections import add_connection

    config: dict[str, object] = {}
    if path:
        config["path"] = str(Path(path).expanduser().resolve())
    if parquet:
        config["parquet"] = dict(p.split("=", 1) for p in parquet)
    for k, v in (("host", host), ("port", port), ("database", database), ("user", user),
                 ("role", role)):
        if v is not None:
            config[k] = v
    try:
        conn = add_connection(workspace, name, kind, config, secret_ref=secret_ref)
    except (ValueError, LookupError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"connection {conn.name!r} ({conn.kind}) saved: {conn.id}")


@conn_app.command("list")
def connection_list(workspace: WorkspaceOpt) -> None:
    """List a workspace's connections."""
    from satchel.store.db import workspace_session
    from satchel.store.models import Connection

    with workspace_session(_ws_id(workspace)) as s:
        for c in s.scalars(select(Connection).order_by(Connection.name)):
            typer.echo(f"{c.name}\t{c.kind}\t{c.config}\tsecret_ref={c.secret_ref}")


@conn_app.command("crawl")
def connection_crawl(name: str, workspace: WorkspaceOpt) -> None:
    """Introspect NAME: upsert assets, mark vanished ones dropped, import comments as drafts."""
    from satchel.data.connections import ConnectionError_
    from satchel.data.crawl import crawl_connection

    try:
        r = crawl_connection(workspace, name)
    except (LookupError, ConnectionError_, NotImplementedError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(
        f"crawled {r.connection}: seen={r.seen} created={r.created} updated={r.updated} "
        f"dropped={r.dropped} revived={r.revived} draft_docs={r.draft_docs}"
    )


@curate_app.command("run")
def curate_run(
    workspace: WorkspaceOpt,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Skip drafting and LLM checks")] = False,
) -> None:
    """Embed signals, run hygiene, detect gaps and draft suggestions for WORKSPACE."""
    import logging

    from satchel.curation.run import run_curation

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        report = run_curation(workspace, use_llm=not no_llm)
    except LookupError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    if report is None:
        typer.echo("another curation run for this workspace is in progress", err=True)
        raise typer.Exit(1)
    for line in report.lines():
        typer.echo(line)
    if report.errors:
        raise typer.Exit(1)


@curate_app.command("show")
def curate_show(
    workspace: WorkspaceOpt,
    status: Annotated[str, typer.Option(help="open | completed | dismissed | all")] = "open",
    bodies: Annotated[bool, typer.Option("--bodies", help="Print proposed bodies")] = False,
) -> None:
    """List suggestions and their proposed changes."""
    from sqlalchemy import select as sa_select

    from satchel.store.db import workspace_session
    from satchel.store.models import ContextItem, Suggestion, SuggestionChange

    with workspace_session(_ws_id(workspace)) as s:
        q = sa_select(Suggestion).order_by(
            Suggestion.evidence_count.desc(), Suggestion.created_at.desc())
        if status != "all":
            q = q.where(Suggestion.status == status)
        rows = list(s.scalars(q))
        if not rows:
            typer.echo(f"no {status} suggestions")
        for sug in rows:
            typer.echo(
                f"[{sug.status}/{sug.source}] {sug.id}  evidence={sug.evidence_count} "
                f"users={sug.distinct_users} sessions={sug.distinct_sessions}\n  {sug.title}"
            )
            if sug.summary:
                typer.echo(f"  {sug.summary}")
            if sug.draft_note:
                typer.echo(f"  note: {sug.draft_note}")
            for c in s.scalars(sa_select(SuggestionChange)
                               .where(SuggestionChange.suggestion_id == sug.id)
                               .order_by(SuggestionChange.ord)):
                target = c.proposed_key
                if c.target_item_id:
                    item = s.get(ContextItem, c.target_item_id)
                    target = item.key if item else str(c.target_item_id)
                typer.echo(f"  - {c.op} {c.item_type or ''} {target} [{c.status}]")
                if c.rationale:
                    typer.echo(f"      why: {c.rationale}")
                if bodies and c.proposed_body:
                    typer.echo("      " + c.proposed_body.replace("\n", "\n      "))


@app.command("worker")
def worker(
    workspace: Annotated[str | None, typer.Option("--workspace", "-w", help="Only this")] = None,
    interval_min: Annotated[
        int | None, typer.Option(help="Minutes between passes (SATCHEL_CURATE_INTERVAL_MIN)")
    ] = None,
    once: Annotated[bool, typer.Option("--once", help="One pass, then exit (CronJob)")] = False,
    no_llm: Annotated[bool, typer.Option("--no-llm")] = False,
) -> None:
    """Run curation for every workspace on an interval (nightly by default)."""
    from satchel.runtime import configure_logging, enforce_production
    from satchel.settings import get_settings
    from satchel.worker.main import run_forever

    st = get_settings()
    enforce_production(st)
    configure_logging(st.log_format)
    run_forever(interval_min=interval_min, workspace=workspace, once=once, use_llm=not no_llm)


@workspace_app.command("curation")
def workspace_curation(
    workspace: WorkspaceOpt,
    min_users: Annotated[int | None, typer.Option(help="Distinct users a gap needs")] = None,
    min_sessions: Annotated[int | None, typer.Option(help="Distinct sessions")] = None,
    min_signals: Annotated[int | None, typer.Option(help="Signals in a cluster")] = None,
    similarity: Annotated[float | None, typer.Option(help="Cluster cosine threshold")] = None,
    instructions: Annotated[
        str | None, typer.Option(help="Free-text guidance given to the drafting model")
    ] = None,
) -> None:
    """Show or set the workspace's curation settings (thresholds, instructions)."""
    from satchel.curation.config import CurationConfig
    from satchel.store.db import admin_session
    from satchel.store.models import Workspace

    updates = {k: v for k, v in {
        "min_users": min_users, "min_sessions": min_sessions, "min_signals": min_signals,
        "similarity": similarity, "instructions": instructions}.items() if v is not None}
    with admin_session() as s:
        ws = s.scalars(select(Workspace).where(Workspace.slug == workspace)).first()
        if ws is None:
            typer.echo(f"unknown workspace: {workspace}", err=True)
            raise typer.Exit(1)
        cur = dict((ws.settings or {}).get("curation", {}))
        if updates:
            cur.update(updates)
            try:
                CurationConfig.model_validate(cur)  # reject out-of-range values
            except ValueError as e:
                typer.echo(f"invalid curation settings: {e}", err=True)
                raise typer.Exit(1) from e
            ws.settings = {**(ws.settings or {}), "curation": cur}
        effective = CurationConfig.model_validate(cur)
        typer.echo(effective.model_dump_json(indent=2))


from satchel.cli import onboarding  # noqa: E402

onboarding.register(app)
