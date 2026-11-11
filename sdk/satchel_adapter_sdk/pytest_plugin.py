"""Optional pytest plugin: ``pytest_plugins = ["satchel_adapter_sdk.pytest_plugin"]``.

Adds ``--update-golden`` and an ``update_golden`` fixture for the conformance kit."""

from __future__ import annotations

import pytest

from satchel_adapter_sdk.testing import golden_update_requested


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden", action="store_true", default=False,
        help="rewrite adapter golden snapshots instead of comparing against them",
    )


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden")) or golden_update_requested()
