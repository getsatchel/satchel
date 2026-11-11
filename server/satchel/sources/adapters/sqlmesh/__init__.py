"""Built-in SQLMesh source adapter: reads a SQLMesh project folder (``MODEL (...)`` blocks in
``.sql`` files, ``@model`` Python models, ``config.yaml`` defaults) statically. Written exactly like
a third-party adapter: it imports only the standard library, ``satchel_adapter_sdk`` and the
packages in ``deps.txt``.

It does not import ``sqlmesh``: loading a SQLMesh ``Context`` would execute the project's Python and
pin ``sqlglot~=30.8`` while satchel needs ``>=30.25``, so the project is read with sqlglot, a small
bracket-aware ``MODEL`` scanner and ``ast`` (see ``parse.py``)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from satchel_adapter_sdk import (
    AdapterInfo,
    FileInput,
    SourceFiles,
    SourceSnapshot,
    SourceWarning,
)

from satchel.sources.adapters.sqlmesh.project import Project

__all__ = ["SqlmeshAdapter", "SqlmeshConfig", "adapter"]


class SqlmeshConfig(BaseModel):
    """Where the SQLMesh project lives."""

    model_config = ConfigDict(extra="forbid")

    project: str | None = Field(
        default=None,
        description="Path to the SQLMesh project folder (the one with config.yaml and models/)",
    )
    dialect: str | None = Field(
        default=None,
        description="SQL dialect of the models when neither the model nor config.yaml sets one",
    )


class SqlmeshAdapter:
    info = AdapterInfo(
        kind="sqlmesh",
        display_name="SQLMesh",
        version="1.0.0",
        maturity="beta",
        capabilities=frozenset({"assets", "lineage"}),
        config_model=SqlmeshConfig,
        file_inputs=(
            FileInput(
                name="project", description="SQLMesh project folder", required=True,
                multiple=True, extensions=(".sql", ".py", ".yaml", ".yml"),
            ),
        ),
        description="SQLMesh models: descriptions, column descriptions and types, grain, kind, "
        "owner, tags, audits and lineage",
        homepage="https://github.com/satchel-dev/satchel",
    )

    def load(self, config: SqlmeshConfig, files: SourceFiles) -> SourceSnapshot:
        project = Project(config.dialect)
        project.read(files.group("project"))
        assets = project.assets()
        warnings = [SourceWarning(code=c, message=m, key=k) for c, m, k in project.warnings]
        if not project.models:
            warnings.append(SourceWarning(
                code="no_models",
                message="no SQLMesh models found (expected MODEL (...) blocks in .sql files "
                        "or @model Python models under the project folder)"))
        return SourceSnapshot(
            kind="sqlmesh",
            fingerprint=files.fingerprint(),
            project=None,
            assets=assets,
            warnings=sorted(warnings, key=lambda w: (w.key or "", w.code, w.message)),
        )


adapter = SqlmeshAdapter()
