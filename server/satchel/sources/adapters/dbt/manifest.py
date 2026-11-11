"""Tolerant readers for dbt's ``manifest.json``, ``semantic_manifest.json`` and ``catalog.json``.

No dbt runtime is needed. Models ignore unknown fields and default everything, so manifest schema
versions v10 to v12 (dbt 1.6 to 1.12) all load; a node that does not validate is reported as a
warning and skipped instead of failing the import. Schemas: https://schemas.getdbt.com
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from satchel_adapter_sdk import SourceWarning

SUPPORTED_SCHEMA_VERSIONS = range(10, 13)


class _T(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Column(_T):
    name: str
    description: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    data_type: str | None = None
    tags: list[str] = Field(default_factory=list)


class NodeConfig(_T):
    enabled: bool = True
    materialized: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    where: str | None = None  # tests
    contract: dict[str, Any] = Field(default_factory=dict)
    access: str | None = None
    group: str | None = None


class TestMetadata(_T):
    name: str
    namespace: str | None = None
    kwargs_: dict[str, Any] = Field(default_factory=dict, alias="kwargs")


class DependsOn(_T):
    nodes: list[str] = Field(default_factory=list)


class Node(_T):
    """A model, seed, snapshot, source table or test."""

    unique_id: str
    resource_type: str
    name: str
    package_name: str | None = None
    original_file_path: str | None = None
    database: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    alias: str | None = None
    identifier: str | None = None  # sources
    source_name: str | None = None  # sources
    source_description: str | None = None  # sources
    relation_name: str | None = None
    description: str | None = None
    columns: dict[str, Column] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    config: NodeConfig = Field(default_factory=NodeConfig)
    group: str | None = None
    access: str | None = None
    contract: dict[str, Any] = Field(default_factory=dict)
    primary_key: list[str] = Field(default_factory=list)
    version: str | int | float | None = None
    depends_on: DependsOn = Field(default_factory=DependsOn)
    # tests
    test_metadata: TestMetadata | None = None
    column_name: str | None = None
    attached_node: str | None = None

    @property
    def table(self) -> str | None:
        return self.identifier or self.alias or self.name

    def all_meta(self) -> dict[str, Any]:
        """``meta`` merged with ``config.meta`` (config wins; both exist across versions)."""
        return {**self.meta, **self.config.meta}

    def effective_access(self) -> str | None:
        return self.access or self.config.access

    def contract_enforced(self) -> bool:
        return bool((self.contract or self.config.contract or {}).get("enforced"))


class Owner(_T):
    name: str | None = None
    email: str | None = None

    def label(self) -> str | None:
        return self.email or self.name


class Group(_T):
    name: str
    owner: Owner = Field(default_factory=Owner)


class Exposure(_T):
    unique_id: str
    name: str
    label: str | None = None
    type: str | None = None
    url: str | None = None
    description: str | None = None
    maturity: str | None = None
    owner: Owner = Field(default_factory=Owner)
    depends_on: DependsOn = Field(default_factory=DependsOn)
    config: NodeConfig = Field(default_factory=NodeConfig)


class Dbt:
    """The parts of the dbt artifacts the adapter reads, parsed once."""

    def __init__(
        self,
        manifest: dict[str, Any],
        semantic_manifest: dict[str, Any] | None,
        catalog: dict[str, Any] | None,
    ) -> None:
        self.warnings: list[SourceWarning] = []
        self.raw_manifest = manifest
        meta = manifest.get("metadata") or {}
        self.adapter_type: str | None = meta.get("adapter_type")
        self.dbt_version: str | None = meta.get("dbt_version")
        self.schema_version = _schema_version(str(meta.get("dbt_schema_version") or ""))
        if self.schema_version is None:
            self.warn("manifest_version", "manifest has no dbt_schema_version; assuming v12")
        elif self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            self.warn(
                "manifest_version",
                f"manifest schema v{self.schema_version} is outside the tested range "
                f"v{SUPPORTED_SCHEMA_VERSIONS.start} to v{SUPPORTED_SCHEMA_VERSIONS.stop - 1}; "
                "importing what can be read",
            )
        self.nodes: dict[str, Node] = {}
        self.sources: dict[str, Node] = {}
        self.tests: list[Node] = []
        for uid, raw in sorted((manifest.get("nodes") or {}).items()):
            node = self._node(uid, raw)
            if node is None:
                continue
            if node.resource_type == "test":
                self.tests.append(node)
            elif node.resource_type in ("model", "seed", "snapshot"):
                self.nodes[uid] = node
        for uid, raw in sorted((manifest.get("sources") or {}).items()):
            node = self._node(uid, raw)
            if node is not None:
                self.sources[uid] = node
        self.exposures: list[Exposure] = []
        for uid, raw in sorted((manifest.get("exposures") or {}).items()):
            try:
                self.exposures.append(Exposure.model_validate({"unique_id": uid, **raw}))
            except ValidationError as e:
                self.warn("invalid_node", _first_error(e), uid)
        self.groups: dict[str, Group] = {}
        for uid, raw in (manifest.get("groups") or {}).items():
            try:
                self.groups[uid] = Group.model_validate(raw)
            except ValidationError as e:
                self.warn("invalid_node", _first_error(e), uid)
        self.semantic = semantic_manifest
        self.catalog = catalog
        self.project_name = str(
            meta.get("project_name")
            or next((n.package_name for n in self.nodes.values() if n.package_name), None)
            or "dbt"
        )

    def warn(self, code: str, message: str, key: str | None = None) -> None:
        self.warnings.append(SourceWarning(code=code, message=message, key=key))

    def _node(self, uid: str, raw: Any) -> Node | None:
        if not isinstance(raw, dict):
            self.warn("invalid_node", "not a JSON object", uid)
            return None
        try:
            return Node.model_validate({"unique_id": uid, **raw})
        except ValidationError as e:
            self.warn("invalid_node", _first_error(e), uid)
            return None

    def catalog_columns(self, unique_id: str) -> dict[str, str]:
        """``column -> data type`` (type lower case) from catalog.json, in table order."""
        if not self.catalog:
            return {}
        for section in ("nodes", "sources"):
            entry = (self.catalog.get(section) or {}).get(unique_id)
            if isinstance(entry, dict):
                cols = entry.get("columns") or {}
                ordered = sorted(
                    (c for c in cols.values() if isinstance(c, dict) and c.get("name")),
                    key=lambda c: (c.get("index") or 0, str(c.get("name"))),
                )
                return {str(c["name"]): str(c.get("type") or "").lower() for c in ordered}
        return {}


def _schema_version(url: str) -> int | None:
    m = re.search(r"/v(\d+)\.json", url)
    return int(m.group(1)) if m else None


def _first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    return f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
