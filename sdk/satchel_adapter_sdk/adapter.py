"""The adapter contract: what an adapter package must provide."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from satchel_adapter_sdk.ir import SourceSnapshot

Maturity = Literal["experimental", "beta", "stable"]
Capability = Literal["assets", "semantic", "queries", "glossary", "lineage", "network"]
KIND_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"


class FileInput(BaseModel):
    """A file the adapter reads. The host loads it (from the path in ``config[name]`` or an
    upload) so the adapter itself never opens files or sockets."""

    name: str
    description: str = ""
    required: bool = False
    multiple: bool = Field(
        default=False,
        description="The path may be a file or a folder (a Cube data model, a SQLMesh project). "
        "The host reads every matching file under it and hands them over as "
        "'<name>/<relative path>'; read them with SourceFiles.group(name).",
    )
    extensions: tuple[str, ...] = Field(
        default=(),
        description="With multiple: only read files with these lower-case suffixes, e.g. "
        "('.yml', '.yaml'). Empty means every file.",
    )


class AdapterInfo(BaseModel):
    """The adapter's manifest. Its ``config_model`` JSON schema drives the CLI flags of
    ``satchel source add`` and the UI form."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: str = Field(pattern=KIND_PATTERN)
    display_name: str
    version: str
    maturity: Maturity = "experimental"
    capabilities: frozenset[Capability]
    config_model: type[BaseModel]
    file_inputs: tuple[FileInput, ...] = ()
    extras: tuple[str, ...] = Field(
        default=(), description="pip extras the adapter needs, e.g. ('cube',)"
    )
    requires: tuple[str, ...] = Field(
        default=(),
        description="Importable top-level modules the adapter needs at load time (an optional "
        "dependency). When one is missing the host lists the adapter as unavailable with an "
        "install hint instead of failing; import them lazily inside load().",
    )
    description: str = ""
    homepage: str | None = None

    def config_schema(self) -> dict[str, Any]:
        return self.config_model.model_json_schema()


class SourceFiles:
    """Named input files, as bytes. Built by the host; adapters only read."""

    def __init__(self, data: Mapping[str, bytes] | None = None) -> None:
        self._data = dict(data or {})

    @classmethod
    def from_paths(cls, paths: Mapping[str, str | Path | None]) -> SourceFiles:
        return cls({n: Path(p).read_bytes() for n, p in paths.items() if p})

    def has(self, name: str) -> bool:
        return name in self._data

    def names(self) -> list[str]:
        return sorted(self._data)

    def read(self, name: str) -> bytes:
        try:
            return self._data[name]
        except KeyError:
            raise FileNotFoundError(f"input file {name!r} was not provided") from None

    def read_text(self, name: str) -> str:
        return self.read(name).decode("utf-8")

    def read_json(self, name: str) -> Any:
        return json.loads(self.read(name))

    def group(self, name: str) -> dict[str, bytes]:
        """The files of a ``FileInput(multiple=True)``: ``{relative path: bytes}``, sorted by
        path. A plain file stored under exactly ``name`` is returned as ``{name: bytes}``."""
        prefix = f"{name}/"
        out = {k[len(prefix):]: v for k, v in self._data.items() if k.startswith(prefix)}
        if name in self._data:
            out[name] = self._data[name]
        return dict(sorted(out.items()))

    def has_group(self, name: str) -> bool:
        return bool(self.group(name))

    def fingerprint(self, names: Iterable[str] | None = None) -> str:
        """sha256 over (name, bytes) of the given inputs (default: all), order independent."""
        h = hashlib.sha256()
        for n in sorted(names if names is not None else self._data):
            if n in self._data:
                h.update(n.encode() + b"\0" + hashlib.sha256(self._data[n]).digest())
        return h.hexdigest()


@runtime_checkable
class SourceAdapter(Protocol):
    """``load`` is pure: deterministic, no database, and no network unless the ``network``
    capability is declared. Installed adapters run in-process as trusted code."""

    info: AdapterInfo

    def load(self, config: Any, files: SourceFiles) -> SourceSnapshot: ...
