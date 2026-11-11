"""A minimal satchel source adapter. Copy this package and replace the parsing in `load`.

The adapter is pure: it reads only the bytes the host gives it (`files`), never the database,
the filesystem or the network, and returns the same snapshot for the same input.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from satchel_adapter_sdk import (
    AdapterInfo,
    FileInput,
    SourceAsset,
    SourceColumn,
    SourceFiles,
    SourceSnapshot,
    normalise_fqn,
)

__all__ = ["ExampleAdapter", "ExampleConfig", "adapter"]


class ExampleConfig(BaseModel):
    """Becomes the CLI flags of `satchel source add example NAME --export PATH` and the UI form."""

    model_config = ConfigDict(extra="forbid")

    export: str | None = Field(default=None, description="Path to the tool's JSON export")


class ExampleAdapter:
    info = AdapterInfo(
        kind="example",  # lower case; must equal the entry-point name
        display_name="Example tool",
        version="0.1.0",
        maturity="experimental",
        capabilities=frozenset({"assets"}),
        config_model=ExampleConfig,
        file_inputs=(FileInput(name="export", description="the tool's JSON export", required=True),),
        description="Tables and columns from an example JSON export",
    )

    def load(self, config: ExampleConfig, files: SourceFiles) -> SourceSnapshot:
        data = json.loads(files.read("export"))
        assets = [
            SourceAsset(
                key=f"table.{t['schema']}.{t['name']}",
                fqn=normalise_fqn(f"{t['schema']}.{t['name']}"),
                description=t.get("description"),
                owner=t.get("owner"),
                columns=[
                    SourceColumn(name=c["name"], description=c.get("description"),
                                 data_type=c.get("type"))
                    for c in t.get("columns", [])
                ],
            )
            for t in sorted(data.get("tables", []), key=lambda t: (t["schema"], t["name"]))
        ]
        return SourceSnapshot(
            kind=self.info.kind,
            fingerprint=files.fingerprint(["export"]),
            assets=assets,
        )


adapter = ExampleAdapter()
