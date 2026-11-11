"""Install hints must never point at PyPI's `satchel`, which is an unrelated project."""

from __future__ import annotations

import re
from pathlib import Path

from satchel.install_hints import extra_hint, git_spec, release_ref

SPEC = re.compile(r"^satchel(\[[a-z,]+\])? @ git\+https://github\.com/getsatchel/satchel"
                  r"(@v?[\w.]+)?#subdirectory=server$")


def test_git_spec_pins_the_running_version() -> None:
    assert release_ref().startswith("@v")
    spec = git_spec("aws", "gcp")
    assert SPEC.match(spec)
    assert spec.startswith("satchel[aws,gcp] @ ") and f"satchel{release_ref()}#" in spec
    assert git_spec(ref="") == "satchel @ git+https://github.com/getsatchel/satchel#subdirectory=server"


def test_extra_hint_installs_from_git() -> None:
    hint = extra_hint("bigquery")
    assert 'uv tool install --force "satchel[bigquery] @ git+' in hint
    assert 'pip install "satchel[bigquery] @ git+' in hint


def test_no_bare_pypi_install_hints_in_the_server() -> None:
    root = Path(__file__).resolve().parents[1] / "satchel"
    bare = re.compile(r"pip install (-U )?['\"]?satchel(\[[^\]]*\])?['\"]?(\s|$|\))")
    hits = [f"{p.relative_to(root)}:{n}" for p in root.rglob("*.py")
            for n, line in enumerate(p.read_text().splitlines(), 1) if bare.search(line)]
    assert hits == []
