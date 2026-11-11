import typing as t

import pandas as pd
from sqlmesh import ExecutionContext, model
from sqlmesh.core.model.kind import ModelKindName


@model(
    "sales.enriched",
    owner="ds@example.com",
    kind=dict(name=ModelKindName.FULL),
    description="Python model: enriched orders",
    grain="order_id",
    tags=["ml"],
    columns={"order_id": "int", "score": "double"},
    column_descriptions={"score": "Model score between 0 and 1."},
)
def execute(context: ExecutionContext, **kwargs: t.Any) -> pd.DataFrame:
    raise RuntimeError("never executed by satchel")
