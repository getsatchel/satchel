from pathlib import Path

from satchel_adapter_sdk import SourceFiles
from satchel_adapter_sdk.testing import assert_conformant
from satchel_source_example import ExampleConfig, adapter

HERE = Path(__file__).parent
FILES = SourceFiles.from_paths({"export": HERE / "fixtures" / "export.json"})


def test_conformance(update_golden: bool) -> None:
    """The SDK's conformance kit: valid, deterministic, normalised, pure, matches the golden file.
    Run `pytest --update-golden` after an intended change and review the diff."""
    assert_conformant(
        adapter, ExampleConfig(), FILES,
        golden=HERE / "golden" / "snapshot.json", update_golden=update_golden,
    )


def test_adapter_specifics() -> None:
    snap = adapter.load(ExampleConfig(), FILES)
    assert [a.fqn for a in snap.assets] == ["shop.customers", "shop.orders"]
    assert snap.assets[1].columns[0].data_type == "integer"
