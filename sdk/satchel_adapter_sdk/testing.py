"""Conformance kit: run it against your adapter's own fixtures.

    from satchel_adapter_sdk.testing import assert_conformant

    def test_conformance(update_golden):
        assert_conformant(adapter, config, files, golden=Path("tests/golden/snapshot.json"),
                          update_golden=update_golden)

``update_golden`` comes from the ``--update-golden`` pytest flag; enable it with
``pytest_plugins = ["satchel_adapter_sdk.pytest_plugin"]`` in your ``conftest.py``.

Each ``check_*`` function raises ``AssertionError`` with a message that says what to fix;
:func:`assert_conformant` runs them all and reports every failure at once.
"""

from __future__ import annotations

import os
import re
import socket
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from satchel_adapter_sdk.adapter import KIND_PATTERN, SourceAdapter, SourceFiles
from satchel_adapter_sdk.ir import SourceSnapshot, is_compatible, normalise_fqn
from satchel_adapter_sdk.mdl import validate_mdl

FORBIDDEN_MODULES = ("satchel", "sqlalchemy", "psycopg", "psycopg2", "fastapi", "alembic")

__all__ = [
    "ConformanceError",
    "assert_conformant",
    "check_capabilities",
    "check_deterministic",
    "check_fingerprint_stable",
    "check_fqns_normalised",
    "check_golden",
    "check_info",
    "check_ir_version",
    "check_mdl_valid",
    "check_no_server_imports",
    "check_no_network",
    "check_snapshot_valid",
    "check_unique_keys",
    "golden_update_requested",
]


class ConformanceError(AssertionError):
    pass


def golden_update_requested() -> bool:
    return os.environ.get("SATCHEL_UPDATE_GOLDEN") == "1"


def check_info(adapter: SourceAdapter) -> None:
    info = adapter.info
    assert re.match(KIND_PATTERN, info.kind), f"kind {info.kind!r} must match {KIND_PATTERN}"
    assert info.display_name.strip(), "info.display_name must not be empty"
    assert re.match(r"^\d+\.\d+\.\d+", info.version), "info.version must be semver"
    assert info.capabilities, "info.capabilities must declare at least one capability"
    assert issubclass(info.config_model, BaseModel), "info.config_model must be a pydantic model"
    info.config_schema()  # must be JSON-schema serialisable (drives CLI flags and UI forms)
    fields = set(info.config_model.model_fields)
    for f in info.file_inputs:
        assert f.name in fields, (
            f"file input {f.name!r} must also be a field of the config model (its path)"
        )


def check_snapshot_valid(snap: SourceSnapshot) -> None:
    again = SourceSnapshot.model_validate(snap.model_dump(mode="json"))
    assert again == snap, "snapshot does not survive a JSON round trip"


def check_ir_version(snap: SourceSnapshot, adapter: SourceAdapter | None = None) -> None:
    assert snap.ir_version, "snapshot.ir_version is not set"
    assert is_compatible(snap.ir_version), (
        f"snapshot IR version {snap.ir_version} is not readable by this SDK"
    )
    if adapter is not None:
        assert snap.kind == adapter.info.kind, (
            f"snapshot.kind {snap.kind!r} must equal info.kind {adapter.info.kind!r}"
        )


def check_deterministic(adapter: SourceAdapter, config: Any, files: SourceFiles) -> None:
    a = adapter.load(config, files)
    b = adapter.load(config, files)
    assert a.canonical_json() == b.canonical_json(), (
        "two loads of the same input differ: sort your output (assets by key, sets to lists) "
        "and keep timestamps and random ids out of the snapshot"
    )


def check_fingerprint_stable(adapter: SourceAdapter, config: Any, files: SourceFiles) -> None:
    a = adapter.load(config, files).fingerprint
    b = adapter.load(config, SourceFiles({n: files.read(n) for n in files.names()})).fingerprint
    assert a and a == b, "fingerprint must be non-empty and identical for identical input"


def check_fqns_normalised(snap: SourceSnapshot) -> None:
    for a in snap.assets:
        assert a.fqn == normalise_fqn(a.fqn), (
            f"asset {a.key}: fqn {a.fqn!r} is not normalised (expected {normalise_fqn(a.fqn)!r})"
        )
        for r in a.relationships:
            assert r.to_asset == normalise_fqn(r.to_asset), (
                f"asset {a.key}: relationship target {r.to_asset!r} is not normalised"
            )
        names = [c.name for c in a.columns]
        assert len(names) == len(set(names)), f"asset {a.key}: duplicate column names"
    for t in snap.terms:
        for ref in t.related_assets:
            assert ref == normalise_fqn(ref), f"term {t.key}: related asset {ref!r} not normalised"


def check_unique_keys(snap: SourceSnapshot) -> None:
    for label, keys in (
        ("asset", [a.key for a in snap.assets]),
        ("semantic model", [m.key for m in snap.semantic_models]),
        ("query", [q.key for q in snap.queries]),
        ("term", [t.key for t in snap.terms]),
    ):
        dup = sorted({k for k in keys if keys.count(k) > 1})
        assert not dup, f"duplicate {label} keys: {dup}"
    fqns = [a.fqn for a in snap.assets]
    dup = sorted({k for k in fqns if fqns.count(k) > 1})
    assert not dup, f"two assets share an fqn: {dup} (merge them or skip one with a warning)"


def check_mdl_valid(snap: SourceSnapshot) -> None:
    for m in snap.semantic_models:
        errors = validate_mdl(m.mdl)
        assert not errors, f"semantic model {m.key}: {'; '.join(errors)}"


def check_capabilities(snap: SourceSnapshot, adapter: SourceAdapter) -> None:
    caps = adapter.info.capabilities
    for label, cap, rows in (
        ("assets", "assets", snap.assets),
        ("semantic models", "semantic", snap.semantic_models),
        ("queries", "queries", snap.queries),
        ("terms", "glossary", snap.terms),
    ):
        assert not rows or cap in caps, (
            f"the snapshot has {label} but info.capabilities does not declare {cap!r}"
        )
    if any(a.depends_on for a in snap.assets):
        assert "lineage" in caps, "assets carry depends_on but 'lineage' is not declared"


@contextmanager
def _no_network() -> Iterator[None]:
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def blocked(*_a: Any, **_k: Any) -> Any:
        raise AssertionError(
            "the adapter touched the network; declare the 'network' capability or remove the call"
        )

    socket.socket.connect = blocked  # type: ignore[method-assign]
    socket.getaddrinfo = blocked
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.getaddrinfo = real_getaddrinfo


def check_no_network(adapter: SourceAdapter, config: Any, files: SourceFiles) -> None:
    if "network" in adapter.info.capabilities:
        return
    with _no_network():
        adapter.load(config, files)


def check_no_server_imports(adapter: SourceAdapter, config: Any, files: SourceFiles) -> None:
    """A pure adapter must not pull in the server, a database driver or a web framework."""
    before = set(sys.modules)
    adapter.load(config, files)
    pulled = {
        m.split(".")[0] for m in set(sys.modules) - before if m.split(".")[0] in FORBIDDEN_MODULES
    }
    assert not pulled, f"the adapter imported server-side modules: {sorted(pulled)}"


def check_golden(
    snap: SourceSnapshot, golden: Path, *, update_golden: bool | None = None
) -> None:
    """Compare the snapshot to a committed golden file; write it when updating."""
    update = golden_update_requested() if update_golden is None else update_golden
    text = snap.canonical_json()
    if update or not golden.exists():
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(text, encoding="utf-8")
        if not update:
            raise AssertionError(
                f"golden file {golden} did not exist and was created; review and commit it"
            )
        return
    want = golden.read_text(encoding="utf-8")
    assert text == want, (
        f"snapshot differs from {golden}. If the change is intended, re-run with "
        "--update-golden (or SATCHEL_UPDATE_GOLDEN=1) and review the diff."
    )


def assert_conformant(
    adapter: SourceAdapter,
    config: Any,
    files: SourceFiles,
    *,
    golden: Path | None = None,
    update_golden: bool | None = None,
    extra_checks: list[Callable[[SourceSnapshot], None]] | None = None,
) -> SourceSnapshot:
    """Run every conformance check; raise one :class:`ConformanceError` listing all failures.
    Returns the snapshot so the caller can add adapter-specific assertions."""
    failures: list[str] = []

    def run(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{name}: {e}")

    run("info", lambda: check_info(adapter))
    snap = adapter.load(config, files)
    run("snapshot_valid", lambda: check_snapshot_valid(snap))
    run("ir_version", lambda: check_ir_version(snap, adapter))
    run("deterministic", lambda: check_deterministic(adapter, config, files))
    run("fingerprint_stable", lambda: check_fingerprint_stable(adapter, config, files))
    run("fqns_normalised", lambda: check_fqns_normalised(snap))
    run("unique_keys", lambda: check_unique_keys(snap))
    run("mdl_valid", lambda: check_mdl_valid(snap))
    run("capabilities", lambda: check_capabilities(snap, adapter))
    run("no_network", lambda: check_no_network(adapter, config, files))
    run("no_server_imports", lambda: check_no_server_imports(adapter, config, files))
    if golden is not None:
        run("golden", lambda: check_golden(snap, golden, update_golden=update_golden))
    for fn in extra_checks or []:
        run(getattr(fn, "__name__", "extra"), lambda fn=fn: fn(snap))  # type: ignore[misc]
    if failures:
        raise ConformanceError("adapter is not conformant:\n  - " + "\n  - ".join(failures))
    return snap
