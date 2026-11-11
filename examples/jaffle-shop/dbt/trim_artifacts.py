"""Trim dbt artifacts to what satchel reads, for committed test fixtures.

    python trim_artifacts.py target/ ../../../server/tests/fixtures/dbt/

Drops macros, docs, compiled SQL, timestamps and other bulk so the fixtures stay small and
diff-stable. Regenerate the inputs with: dbt build && dbt docs generate (see docs/integrations/dbt.md).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

DROP_TOP = ("macros", "docs", "selectors", "parent_map", "child_map", "group_map", "unit_tests",
            "functions")
DROP_NODE = ("raw_code", "compiled_code", "compiled_path", "build_path", "created_at", "checksum",
             "unrendered_config", "extra_ctes", "extra_ctes_injected", "compiled", "doc_blocks",
             "patch_path", "language", "docs", "deferred", "refs", "sources", "metrics",
             "functions", "fqn", "path")


def trim_node(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    out = {k: v for k, v in node.items() if k not in DROP_NODE}
    deps = out.get("depends_on")
    if isinstance(deps, dict):
        deps.pop("macros", None)
    return out


def trim_manifest(m: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in m.items() if k not in DROP_TOP}
    meta = out.get("metadata") or {}
    out["metadata"] = {k: meta[k] for k in ("dbt_schema_version", "dbt_version", "project_name",
                                            "adapter_type") if k in meta}
    for section in ("nodes", "sources", "exposures", "semantic_models", "metrics",
                    "saved_queries"):
        out[section] = {k: trim_node(v) for k, v in sorted((out.get(section) or {}).items())}
    return out


def trim_catalog(c: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"metadata": {k: v for k, v in (c.get("metadata") or {}).items()
                                        if k == "dbt_schema_version"}}
    for section in ("nodes", "sources"):
        out[section] = {
            uid: {"unique_id": uid, "columns": e.get("columns", {})}
            for uid, e in sorted((c.get(section) or {}).items())
        }
    out["errors"] = None
    return out


def main(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    jobs = {"manifest.json": trim_manifest, "catalog.json": trim_catalog,
            "semantic_manifest.json": lambda d: d}
    for name, fn in jobs.items():
        data = json.loads((src / name).read_text())
        (dst / name).write_text(json.dumps(fn(data), indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
