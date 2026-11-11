"""Install hints must name satchel-server: PyPI's `satchel` is an unrelated project."""

from __future__ import annotations

import re
from pathlib import Path

from satchel.install_hints import DIST, extra_hint, git_spec, installed_version, pypi_spec


def test_the_distribution_is_satchel_server() -> None:
    assert DIST == "satchel-server"
    assert installed_version()  # the dev environment installs the project under that name


def test_pypi_spec_pins_the_running_version() -> None:
    assert pypi_spec("aws", "gcp") == f"satchel-server[aws,gcp]=={installed_version()}"
    assert pypi_spec(pin=False) == "satchel-server"


def test_git_spec() -> None:
    assert git_spec("aws") == (f"satchel-server[aws] @ git+https://github.com/getsatchel/satchel"
                               f"@v{installed_version()}#subdirectory=server")
    assert git_spec(ref="") == (
        "satchel-server @ git+https://github.com/getsatchel/satchel#subdirectory=server")


def test_extra_hint() -> None:
    hint = extra_hint("bigquery")
    assert f'uv tool install --force "satchel-server[bigquery]=={installed_version()}"' in hint
    assert 'pip install "satchel-server[bigquery]' in hint


def test_no_hint_installs_pypi_satchel() -> None:
    root = Path(__file__).resolve().parents[1] / "satchel"
    bare = re.compile(
        r"(pip|uv tool) install (-U |--force )?['\"]?satchel(\[[^\]]*\])?(['\"\s)]|$)")
    hits = [f"{p.relative_to(root)}:{n}" for p in root.rglob("*.py")
            for n, line in enumerate(p.read_text().splitlines(), 1) if bare.search(line)]
    assert hits == []
