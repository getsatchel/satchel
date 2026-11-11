"""``sources:`` in a context repository's ``satchel.config.yaml`` (design §8.3).

    sources:
      - kind: dbt
        name: jaffle
        manifest: ../dbt/target/manifest.json
        semantic_manifest: ../dbt/target/semantic_manifest.json
        catalog: ../dbt/target/catalog.json
        connection: warehouse
        publish: auto        # or: preview
        priority: 10

Relative paths are resolved against the repository folder. The older ``import.dbt_manifest`` key
is kept as an alias for a source named ``dbt``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from satchel.llm.embeddings import EmbeddingProvider
from satchel.sources.apply import SourceSyncError, SyncReport
from satchel.sources.registry import get_entry
from satchel.sources.service import add_source, sync_source

LEGACY_DBT = {
    "dbt_manifest": "manifest",
    "dbt_semantic_manifest": "semantic_manifest",
    "dbt_catalog": "catalog",
}


def declared_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    raw = config.get("sources")
    if isinstance(raw, list):
        out += [dict(e) for e in raw if isinstance(e, dict)]
    legacy = config.get("import")
    if (
        isinstance(legacy, dict) and legacy.get("dbt_manifest")
        and not any(e.get("name") == "dbt" for e in out)
    ):
        entry: dict[str, Any] = {"kind": "dbt", "name": "dbt"}
        entry.update({new: legacy[old] for old, new in LEGACY_DBT.items() if legacy.get(old)})
        out.append(entry)
    return out


def validate_declared(config: dict[str, Any]) -> list[str]:
    """Structural problems in the ``sources:`` list (no filesystem access)."""
    errors: list[str] = []
    names: set[str] = set()
    for i, e in enumerate(declared_sources(config)):
        if not e.get("kind") or not e.get("name"):
            errors.append(f"satchel.config.yaml sources[{i}]: kind and name are required")
        elif e["name"] in names:
            errors.append(f"satchel.config.yaml sources: duplicate name {e['name']!r}")
        else:
            names.add(str(e["name"]))
    return errors


def sync_repo_sources(
    config: dict[str, Any], root: Path, workspace_slug: str, *, embedder: EmbeddingProvider,
    engine: Engine | None = None,
) -> tuple[list[SyncReport], list[str]]:
    """Create/update and sync every declared source. Returns ``(reports, errors)``; one failing
    source does not stop the others."""
    reports: list[SyncReport] = []
    errors: list[str] = []
    for e in declared_sources(config):
        kind, name = str(e.pop("kind", "")), str(e.pop("name", ""))
        try:
            entry = get_entry(kind)
            for fi in entry.info.file_inputs:  # repo-relative paths -> absolute
                v = e.get(fi.name)
                if e.get("git_url"):
                    break  # paths inside the source's own git repository
                if isinstance(v, str) and v and not Path(v).expanduser().is_absolute():
                    e[fi.name] = str((root / v).resolve())
            add_source(workspace_slug, kind, name, e, engine=engine)
            reports.append(sync_source(workspace_slug, name, embedder=embedder, engine=engine))
        except (LookupError, ValueError, SourceSyncError) as ex:
            errors.append(f"source {name or kind}: {ex}")
    return reports, errors
