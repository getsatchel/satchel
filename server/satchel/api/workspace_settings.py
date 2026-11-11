"""Settings → Limits and Settings → Deployment.

* ``GET /v1/settings/limits`` (curator): each limit's effective value, the workspace override
  and the deployment default. ``PUT /v1/settings/limits`` (admin): ``{"values": {name: v}}``,
  where ``v`` is a number, ``null`` (no limit, where allowed) or ``"default"`` (remove the
  override). Audited as ``workspace.limits``.
* ``GET /v1/settings/deployment`` (admin): the server's effective configuration, read-only.
  Secrets show only whether they are set; database URLs hide their password. Changing any of it
  means changing the deployment's environment (``SATCHEL_*``) and restarting.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from satchel import limits as lim
from satchel.api.auth import Auth, Caller
from satchel.settings import Settings
from satchel.store.db import workspace_session
from satchel.store.models import Workspace
from satchel.store.schema import schema_status

# Loose on purpose: satchel.limits validates and gives the message the UI shows.
LimitValue = int | float | str | None


class LimitOut(BaseModel):
    name: str
    label: str
    unit: str
    help: str
    min: int
    max: int
    nullable: bool
    null_label: str | None
    value: int | None = Field(description="In effect for this workspace.")
    workspace: int | None = Field(description="The override (null: no limit, if overridden).")
    overridden: bool
    deployment: int | None = Field(description="The deployment default (SATCHEL_*).")
    env: str


class LimitsPut(BaseModel):
    values: dict[str, LimitValue]


class DeploymentRow(BaseModel):
    label: str
    env: str | None = Field(description="The environment variable that sets it.")
    value: str | None
    source: Literal["env", "default", "derived"]
    secret: bool = False


class DeploymentGroup(BaseModel):
    title: str
    rows: list[DeploymentRow]


class DeploymentOut(BaseModel):
    version: str
    groups: list[DeploymentGroup]


def limits_out(ws_settings: dict[str, Any] | None, st: Settings) -> list[LimitOut]:
    o = lim.overrides(ws_settings)
    eff = lim.resolve(ws_settings, st)
    out = []
    for spec in lim.SPECS:
        v = getattr(eff, spec.name)
        dep = lim.deployment_value(st, spec.name)
        out.append(LimitOut(
            name=spec.name, label=spec.label, unit=spec.unit, help=spec.help, min=spec.min,
            max=spec.max, nullable=spec.nullable, null_label=spec.null_label,
            value=None if v is None else int(v), workspace=o.get(spec.name),
            overridden=spec.name in o, deployment=None if dep is None else int(dep),
            env=f"SATCHEL_{spec.name.upper()}",
        ))
    return out


def _url(u: str | None) -> str | None:
    if not u:
        return None
    try:
        return make_url(u).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - never echo a URL we cannot parse (it may hold a secret)
        return "(unparseable)"


def _str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _duration(seconds: float) -> str:
    n = int(seconds)
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if n >= size and n % size == 0:
            k = n // size
            return f"{k} {unit}{'s' if k != 1 else ''}"
    return f"{n} seconds"


def deployment_out(st: Settings, schema: str | None) -> DeploymentOut:
    given = st.model_fields_set

    def row(label: str, field: str, *, kind: str = "plain", unit: str = "",
            none_label: str | None = None) -> DeploymentRow:
        raw = getattr(st, field)
        src: Literal["env", "default"] = "env" if field in given else "default"
        envname = f"SATCHEL_{field.upper()}"
        if kind == "secret":
            secret_set = raw is not None and (
                raw.get_secret_value() if hasattr(raw, "get_secret_value") else raw) != ""
            return DeploymentRow(label=label, env=envname, value="set" if secret_set else None,
                                 source=src, secret=True)
        if kind == "url":
            return DeploymentRow(label=label, env=envname, value=_url(raw), source=src)
        if unit == "duration":
            v = None if raw is None else _duration(raw)
        else:
            v = _str(raw)
            if v is not None and unit:
                v = f"{v} {unit}"
        if v is None and none_label:
            v = none_label
        return DeploymentRow(label=label, env=envname, value=v, source=src)

    def derived(label: str, value: str | None) -> DeploymentRow:
        return DeploymentRow(label=label, env=None, value=value, source="derived")

    prev = st.encryption_key_previous.get_secret_value() if st.encryption_key_previous else ""
    n_prev = len([k for k in prev.split(",") if k.strip()])
    emb_model = {"fastembed": "fastembed_model", "openai_compat": "embedding_model"}.get(
        st.embedding_provider)

    groups = [
        DeploymentGroup(title="Server", rows=[
            row("Environment", "env"),
            row("Public URL", "public_url"),
            row("Log format", "log_format"),
            row("Insecure public URL allowed", "allow_insecure_public_url"),
            row("Header auth (dev only)", "dev_auth"),
        ]),
        DeploymentGroup(title="Database", rows=[
            row("App connection", "database_url", kind="url"),
            row("Worker connection", "admin_database_url", kind="url"),
            row("Migration connection", "migration_database_url", kind="url"),
            derived("Schema revision", schema),
        ]),
        DeploymentGroup(title="Sign-in", rows=[
            row("OIDC issuer", "oidc_issuer"),
            row("OIDC client ID", "oidc_client_id"),
            row("OIDC client secret", "oidc_client_secret", kind="secret"),
            row("OIDC scopes", "oidc_scopes"),
            row("Require verified email", "oidc_require_verified_email"),
            row("UI session lifetime", "session_ttl_s", unit="duration"),
            row("Access token lifetime", "oauth_access_ttl_s", unit="duration"),
            row("Refresh token lifetime", "oauth_refresh_ttl_s", unit="duration"),
            row("Client ID metadata documents", "oauth_cimd_enabled"),
        ]),
        DeploymentGroup(title="Encryption", rows=[
            row("Encryption key", "encryption_key", kind="secret"),
            derived("Previous keys", str(n_prev) if n_prev else None),
        ]),
        DeploymentGroup(title="Embeddings", rows=[
            row("Provider", "embedding_provider"),
            *([row("Model", emb_model)] if emb_model else []),
            row("Dimensions", "embedding_dim"),
            *([row("Base URL", "embedding_base_url"),
               row("API key", "embedding_api_key", kind="secret")]
              if st.embedding_provider == "openai_compat" else []),
        ]),
        DeploymentGroup(title="Curation model", rows=[
            row("Provider", "chat_provider"),
            row("Model", "chat_model"),
            row("Base URL", "chat_base_url"),
            row("API key", "chat_api_key", kind="secret"),
            row("Curation interval", "curate_interval_min", unit="min"),
        ]),
        DeploymentGroup(title="Data", rows=[
            row("Result storage", "result_dir"),
            row("SQL row cap (default)", "sql_row_cap", unit="rows"),
            row("SQL timeout (default)", "sql_timeout_s", unit="duration"),
            row("Result cache (default)", "result_ttl_days", unit="days"),
            row("Session retention (default)", "session_retention_days", unit="days",
                none_label="keep forever"),
        ]),
    ]
    try:
        ver = pkg_version("satchel")
    except PackageNotFoundError:
        ver = "unknown"
    return DeploymentOut(version=ver, groups=groups)


def make_router(auth: Auth) -> APIRouter:
    r = APIRouter(tags=["settings"])
    Cur = Annotated[Caller, Depends(auth.require("curator"))]
    Adm = Annotated[Caller, Depends(auth.require("admin"))]
    eng, st = auth.ctx.engine, auth.ctx.settings

    @r.get("/v1/settings/limits", response_model=list[LimitOut])
    def get_limits(who: Cur) -> list[LimitOut]:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            ws = s.get(Workspace, who.workspace_id)
            return limits_out(ws.settings if ws else None, st)

    @r.put("/v1/settings/limits", response_model=list[LimitOut])
    def put_limits(who: Adm, req: LimitsPut) -> list[LimitOut]:
        with workspace_session(who.workspace_id, who.user_id, engine=eng) as s:
            try:
                lim.set_limits(s, who.workspace_id, who.user_id, dict(req.values))
            except lim.LimitsError as e:
                raise HTTPException(422, str(e)) from None
            ws = s.get(Workspace, who.workspace_id)
            return limits_out(ws.settings if ws else None, st)

    @r.get("/v1/settings/deployment", response_model=DeploymentOut)
    def get_deployment(who: Adm) -> DeploymentOut:
        try:
            status = schema_status(eng)
            schema = (status.current if status.ok
                      else f"{status.current or 'none'} (build expects {status.head})")
        except SQLAlchemyError:
            schema = None
        return deployment_out(st, schema)

    return r
