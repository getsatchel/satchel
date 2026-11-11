import pytest

from satchel.core.item_types import ITEM_TYPES, ItemValidationError, validate_item


def test_registry_has_all_types() -> None:
    assert len(ITEM_TYPES) == 9


@pytest.mark.parametrize(
    ("t", "body", "meta"),
    [
        ("workspace_rule", "Always use net revenue.", {}),
        (
            "guide",
            "# Refunds\nText",
            {"name": "refunds", "description": "How refunds work", "tags": ["fin"]},
        ),
        ("glossary_term", "term: revenue\nsynonyms: [sales]\ndefinition: net of refunds\n", {}),
        ("golden_query", "question: revenue last month?\nsql: select 1\n", {}),
        ("asset_doc", "asset: db.orders\ngrain: one row per order\ncolumns:\n  - name: id\n", {}),
        ("endorsement", "asset: db.orders\nendorsed: true\n", {}),
        ("semantic_model", "models: []", {"format": "ossie"}),
        ("caveat", "Missing refunds", {"applies_to": ["db.orders"], "valid_to": "2023-02-01"}),
        ("user_memory", "Prefers CTEs", {"user_id": "u1"}),
    ],
)
def test_good(t: str, body: str, meta: dict[str, object]) -> None:
    validate_item(t, body, meta)


@pytest.mark.parametrize(
    ("t", "body", "meta"),
    [
        ("nope", "x", {}),
        ("guide", "text", {"name": "x"}),  # missing description
        ("glossary_term", "term: x\n", {}),  # missing definition
        ("golden_query", "question: q\nsql: s\nmetric_query: {a: 1}\n", {}),  # both
        ("golden_query", "question: q\n", {}),  # neither
        ("semantic_model", "x", {"format": "cube"}),
        ("caveat", "x", {"applies_to": []}),
        (
            "caveat",
            "x",
            {"applies_to": ["a"], "valid_from": "2024-01-01", "valid_to": "2023-01-01"},
        ),
        ("endorsement", "- not a mapping", {}),
        ("asset_doc", "asset: [", {}),
    ],
)
def test_bad(t: str, body: str, meta: dict[str, object]) -> None:
    with pytest.raises(ItemValidationError):
        validate_item(t, body, meta)
