"""Built-in Cube source adapter: reads Cube data-model files (YAML ``cubes:`` and ``views:``),
optionally enriched by the ``/cubejs-api/v1/meta`` response. Written exactly like a third-party
adapter: it imports only the standard library, ``satchel_adapter_sdk`` and the packages in
``deps.txt``.

Cubes become asset docs (the table, dimensions as columns, joins as join hints, segments as
caveats, views as "used by"), and their measures become one Wren MDL semantic model so that
``query_metrics`` works over them. JavaScript models are not read (warned); pass ``/meta`` for
those."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from satchel_adapter_sdk import (
    AdapterInfo,
    FileInput,
    SourceFiles,
    SourceSnapshot,
    SourceTerm,
    SourceWarning,
    fingerprint_bytes,
)

from satchel.sources.adapters.cube.assets import Base, base_of, build_assets
from satchel.sources.adapters.cube.meta import (
    Fetch,
    apply_meta,
    http_fetch,
    meta_endpoint,
    resolve_token,
)
from satchel.sources.adapters.cube.model import Model
from satchel.sources.adapters.cube.semantic import build_semantic

__all__ = ["CubeAdapter", "CubeConfig", "adapter"]


class CubeConfig(BaseModel):
    """Where the Cube data model lives, and optionally a running Cube to read ``/meta`` from."""

    model_config = ConfigDict(extra="forbid")

    models: str | None = Field(
        default=None,
        description="Path to the Cube data-model folder (model/ or schema/) or one YAML file",
    )
    meta: str | None = Field(
        default=None, description="Path to a saved /cubejs-api/v1/meta response (JSON)"
    )
    meta_url: str | None = Field(
        default=None,
        description="Base URL of a running Cube; its /cubejs-api/v1/meta is read on every sync "
        "(this is the adapter's only network access)",
    )
    meta_token_env: str | None = Field(
        default=None, description="Name of the environment variable that holds the Cube API token"
    )
    meta_token: str | None = Field(
        default=None,
        description="Cube API token for meta_url (stored in the source config; prefer "
        "meta_token_env)",
    )
    project: str = Field(default="cube", description="Name of the semantic model in satchel")
    dialect: str | None = Field(
        default=None,
        description="Warehouse dialect of the cubes (duckdb, postgres, snowflake, ...)",
    )
    default_schema: str | None = Field(
        default=None, description="Schema to assume for a sql_table written without one"
    )


class CubeAdapter:
    info = AdapterInfo(
        kind="cube",
        display_name="Cube",
        version="1.0.0",
        maturity="beta",
        capabilities=frozenset({"assets", "semantic", "glossary", "network"}),
        config_model=CubeConfig,
        file_inputs=(
            FileInput(
                name="models", description="Cube data-model folder or YAML file", multiple=True,
                extensions=(".yml", ".yaml", ".js"),
            ),
            FileInput(name="meta", description="saved /cubejs-api/v1/meta response"),
        ),
        description="Cube cubes, dimensions, measures, joins and segments from YAML data models",
        homepage="https://github.com/satchel-dev/satchel",
    )

    def __init__(self, fetch: Fetch = http_fetch) -> None:
        self._fetch = fetch

    def load(self, config: CubeConfig, files: SourceFiles) -> SourceSnapshot:
        model = Model()
        sources = files.group("models")
        for path in sorted(sources):
            model.add_file(path, sources[path])
        meta_bytes: bytes | None = files.read("meta") if files.has("meta") else None
        if config.meta_url:
            token = resolve_token(config.meta_token, config.meta_token_env)
            meta_bytes = self._fetch(config.meta_url, token)
        if not sources and meta_bytes is None:
            raise ValueError("give the Cube data model (models) or its /meta (meta or meta_url)")
        model.resolve_extends()
        terms: list[SourceTerm] = apply_meta(model, meta_bytes) if meta_bytes is not None else []

        bases: dict[str, Base] = {}
        for name in sorted(model.cubes):
            base = base_of(model.cubes[name], config.default_schema, model)
            if base is not None:
                bases[name] = base
        assets = build_assets(model, config.default_schema, bases)
        semantic = build_semantic(model, bases, config.project, config.dialect)
        parts = [files.fingerprint().encode()]
        if config.meta_url and meta_bytes is not None:
            parts.append(meta_endpoint(config.meta_url).encode() + b"\0" + meta_bytes)
        return SourceSnapshot(
            kind="cube",
            fingerprint=fingerprint_bytes(*parts),
            project=config.project,
            assets=sorted(assets, key=lambda a: a.key),
            semantic_models=semantic,
            terms=sorted(terms, key=lambda t: t.key),
            warnings=sorted(model.warnings, key=_warn_key),
        )


def _warn_key(w: SourceWarning) -> tuple[str, str, str]:
    return (w.key or "", w.code, w.message)


adapter = CubeAdapter()
