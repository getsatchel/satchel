"""A toy adapter used to exercise the SDK and its conformance kit."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from satchel_adapter_sdk import (
    AdapterInfo,
    FileInput,
    MdlBuilder,
    SourceAsset,
    SourceColumn,
    SourceFiles,
    SourceSemanticModel,
    SourceSnapshot,
    normalise_fqn,
)


class ToyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    export: str


class ToyAdapter:
    info = AdapterInfo(
        kind="toy",
        display_name="Toy",
        version="0.1.0",
        capabilities=frozenset({"assets", "semantic"}),
        config_model=ToyConfig,
        file_inputs=(FileInput(name="export", required=True),),
    )

    def load(self, config: ToyConfig, files: SourceFiles) -> SourceSnapshot:
        data = json.loads(files.read("export"))
        assets = [
            SourceAsset(
                key=f"table.{t['name']}",
                fqn=normalise_fqn(f"Shop.{t['name']}"),
                description=t.get("doc"),
                columns=[SourceColumn(name=c) for c in t.get("columns", [])],
            )
            for t in sorted(data["tables"], key=lambda t: t["name"])
        ]
        b = MdlBuilder(data_source="duckdb")
        b.model("orders", schema="shop", table="orders", columns={"amount": "integer"})
        b.cube("sales", base="orders", measures=[("revenue", "SUM(amount)", "BIGINT")])
        sem = SourceSemanticModel(
            key="toy.sales", name="sales", mdl=b.build(), queryable_metrics=["revenue"]
        )
        return SourceSnapshot(
            kind="toy",
            fingerprint=files.fingerprint(),
            assets=assets,
            semantic_models=[sem],
        )


adapter = ToyAdapter()
