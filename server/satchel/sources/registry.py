"""Adapter discovery through the ``satchel.sources`` entry-point group.

Built-in adapters (``satchel.sources.adapters.<kind>``) register through the same group in
``pyproject.toml`` as external packages (``pip install satchel-source-foo``). If the installed
package metadata predates a built-in adapter (an old editable install), the built-ins are also
loaded directly, so ``satchel source list`` never loses them.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any

from satchel_adapter_sdk import AdapterInfo, SourceAdapter

from satchel.install_hints import extra_hint

log = logging.getLogger("satchel.sources")

GROUP = "satchel.sources"
BUILTIN_PREFIX = "satchel.sources.adapters."
BUILTIN_FALLBACK = {
    "dbt": "satchel.sources.adapters.dbt:adapter",
    "cube": "satchel.sources.adapters.cube:adapter",
    "sqlmesh": "satchel.sources.adapters.sqlmesh:adapter",
}


@dataclass
class AdapterEntry:
    kind: str
    builtin: bool
    package: str  # distribution name, "satchel" for built-ins
    package_version: str | None
    adapter: SourceAdapter | None  # None when it failed to load
    error: str | None = None
    missing: list[str] = field(default_factory=list)  # optional modules the adapter needs

    @property
    def available(self) -> bool:
        return self.adapter is not None and not self.missing

    @property
    def install_hint(self) -> str | None:
        """How to get the missing optional dependencies, or ``None`` when nothing is missing."""
        if not self.missing:
            return None
        extras = ",".join(self.adapter.info.extras) if self.adapter else ""
        if self.builtin:
            return extra_hint(*(extras.split(",") if extras else [self.kind]))
        pkg = f"'{self.package}[{extras}]'" if extras else self.package
        return f"pip install {pkg}"

    @property
    def info(self) -> AdapterInfo:
        if self.adapter is None:
            raise LookupError(f"adapter {self.kind!r} failed to load: {self.error}")
        return self.adapter.info


def _instantiate(obj: Any) -> SourceAdapter:
    adapter = obj() if isinstance(obj, type) else obj
    if not isinstance(adapter, SourceAdapter) or not isinstance(
        getattr(adapter, "info", None), AdapterInfo
    ):
        raise TypeError("entry point is not a SourceAdapter (needs `info: AdapterInfo` and load())")
    return adapter


def missing_modules(adapter: SourceAdapter) -> list[str]:
    """Modules the adapter declared in ``info.requires`` that cannot be imported."""
    out: list[str] = []
    for mod in adapter.info.requires:
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append(mod)
    return out


def _load_target(target: str) -> SourceAdapter:
    module, _, attr = target.partition(":")
    return _instantiate(getattr(importlib.import_module(module), attr))


def discover() -> dict[str, AdapterEntry]:
    """All adapters, keyed by entry-point name. A broken one is listed with ``error`` set."""
    out: dict[str, AdapterEntry] = {}
    for ep in sorted(entry_points(group=GROUP), key=lambda e: e.name):
        builtin = ep.value.startswith(BUILTIN_PREFIX)
        dist = ep.dist
        pkg = dist.metadata["Name"] if dist else "unknown"
        ver = dist.version if dist else None
        try:
            adapter = _instantiate(ep.load())
            if adapter.info.kind != ep.name:
                raise ValueError(
                    f"entry point name {ep.name!r} differs from info.kind {adapter.info.kind!r}"
                )
            out[ep.name] = AdapterEntry(
                ep.name, builtin, pkg, ver, adapter, missing=missing_modules(adapter)
            )
        except Exception as e:  # noqa: BLE001 - a broken plugin must not break the CLI
            log.warning("source adapter %s failed to load: %s", ep.name, e)
            out[ep.name] = AdapterEntry(
                ep.name, builtin, pkg, ver, None, f"{type(e).__name__}: {e}"
            )
    for kind, target in BUILTIN_FALLBACK.items():
        if kind not in out:
            try:
                loaded = _load_target(target)
                out[kind] = AdapterEntry(
                    kind, True, "satchel", None, loaded, missing=missing_modules(loaded)
                )
            except Exception as e:  # noqa: BLE001
                out[kind] = AdapterEntry(kind, True, "satchel", None, None, str(e))
    return out


def get_entry(kind: str, *, require_available: bool = True) -> AdapterEntry:
    entries = discover()
    entry = entries.get(kind)
    if entry is None:
        known = ", ".join(sorted(entries)) or "none installed"
        raise LookupError(f"unknown source kind {kind!r} (installed adapters: {known})")
    if entry.adapter is None:
        raise LookupError(f"source adapter {kind!r} failed to load: {entry.error}")
    if require_available and entry.missing:
        raise LookupError(
            f"source adapter {kind!r} is unavailable: it needs {', '.join(entry.missing)}. "
            f"Install it with: {entry.install_hint}"
        )
    return entry
