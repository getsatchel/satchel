"""Built-in dbt source adapter: reads ``manifest.json`` (plus optional ``semantic_manifest.json``
and ``catalog.json``). No dbt runtime. Written exactly like a third-party adapter: it imports only
the standard library, ``satchel_adapter_sdk`` and the packages listed in ``deps.txt``."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from satchel_adapter_sdk import (
    AdapterInfo,
    FileInput,
    SourceFiles,
    SourceSnapshot,
    SourceWarning,
)

from satchel.sources.adapters.dbt.assets import build_assets
from satchel.sources.adapters.dbt.manifest import Dbt
from satchel.sources.adapters.dbt.semantic import build_semantic

__all__ = ["DbtAdapter", "DbtConfig", "adapter"]


class DbtConfig(BaseModel):
    """Paths to the dbt artifacts (written by `dbt parse` / `dbt build` / `dbt docs generate`)."""

    model_config = ConfigDict(extra="forbid")

    manifest: str | None = Field(
        default=None, description="Path to target/manifest.json (required)"
    )
    semantic_manifest: str | None = Field(
        default=None,
        description="Path to target/semantic_manifest.json (metrics); falls back to manifest.json",
    )
    catalog: str | None = Field(
        default=None, description="Path to target/catalog.json (exact column types)"
    )


class DbtAdapter:
    info = AdapterInfo(
        kind="dbt",
        display_name="dbt",
        version="1.0.0",
        maturity="beta",
        capabilities=frozenset({"assets", "semantic", "queries", "lineage"}),
        config_model=DbtConfig,
        file_inputs=(
            FileInput(name="manifest", description="target/manifest.json", required=True),
            FileInput(name="semantic_manifest", description="target/semantic_manifest.json"),
            FileInput(name="catalog", description="target/catalog.json"),
        ),
        description="dbt models, tests, exposures, MetricFlow metrics and saved queries",
        homepage="https://github.com/satchel-dev/satchel",
    )

    def load(self, config: DbtConfig, files: SourceFiles) -> SourceSnapshot:
        manifest = _json(files, "manifest")
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json must be a JSON object")
        early: list[SourceWarning] = []
        semantic = _json(files, "semantic_manifest", early)
        catalog = _json(files, "catalog", early)
        dbt = Dbt(manifest, semantic if isinstance(semantic, dict) else None,
                  catalog if isinstance(catalog, dict) else None)
        dbt.warnings.extend(early)
        assets = build_assets(dbt)
        models, queries = build_semantic(dbt)
        return SourceSnapshot(
            kind="dbt",
            fingerprint=files.fingerprint(["manifest", "semantic_manifest", "catalog"]),
            project=dbt.project_name,
            assets=assets,
            semantic_models=models,
            queries=queries,
            warnings=sorted(dbt.warnings, key=_warn_key),
        )


def _warn_key(w: SourceWarning) -> tuple[str, str, str]:
    return (w.key or "", w.code, w.message)


def _json(
    files: SourceFiles, name: str, warnings: list[SourceWarning] | None = None
) -> Any:
    """Parse an input. ``warnings=None`` means required: failures raise. Optional inputs that are
    absent are skipped; ones that do not parse are skipped with a warning."""
    if not files.has(name):
        if warnings is None:
            raise FileNotFoundError(f"dbt {name}.json was not provided")
        return None
    try:
        return json.loads(files.read(name))
    except json.JSONDecodeError as e:
        if warnings is None:
            raise ValueError(f"{name} is not valid JSON: {e}") from None
        warnings.append(SourceWarning(
            code="unreadable_input", message=f"{name} is not valid JSON ({e}); ignored"))
        return None


adapter = DbtAdapter()
