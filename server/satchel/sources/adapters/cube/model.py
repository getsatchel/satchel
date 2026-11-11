"""Tolerant reader for Cube data-model files (YAML ``cubes:`` and ``views:``).

Only YAML is read; JavaScript models would need a JS runtime and are reported as skipped (the
optional ``/cubejs-api/v1/meta`` input still describes them). A cube or view that does not
validate becomes a warning, never a failed import. Keys are snake_case as in Cube's YAML
documentation; camelCase spellings are accepted too.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator
from satchel_adapter_sdk import SourceWarning


class _T(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


def _text(v: Any) -> str | None:
    return None if v is None else str(v)


class Dimension(_T):
    name: str
    sql: str | None = None
    type: str = "string"
    title: str | None = None
    description: str | None = None
    primary_key: bool = Field(
        default=False, validation_alias=AliasChoices("primary_key", "primaryKey")
    )
    sub_query: bool = Field(
        default=False, validation_alias=AliasChoices("sub_query", "subQuery")
    )
    public: bool = True
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sql", mode="before")
    @classmethod
    def _sql(cls, v: Any) -> str | None:
        return _text(v)


class MeasureFilter(_T):
    sql: str

    @field_validator("sql", mode="before")
    @classmethod
    def _sql(cls, v: Any) -> str:
        return str(v)


class Measure(_T):
    name: str
    sql: str | None = None
    type: str
    title: str | None = None
    description: str | None = None
    filters: list[MeasureFilter] = Field(default_factory=list)
    rolling_window: dict[str, Any] | None = Field(
        default=None, validation_alias=AliasChoices("rolling_window", "rollingWindow")
    )
    multi_stage: bool = Field(
        default=False, validation_alias=AliasChoices("multi_stage", "multiStage")
    )
    format: str | None = None
    public: bool = True
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sql", mode="before")
    @classmethod
    def _sql(cls, v: Any) -> str | None:
        return _text(v)


class Join(_T):
    name: str
    relationship: str = "many_to_one"
    sql: str

    @field_validator("sql", mode="before")
    @classmethod
    def _sql(cls, v: Any) -> str:
        return str(v)


class Segment(_T):
    name: str
    sql: str
    title: str | None = None
    description: str | None = None

    @field_validator("sql", mode="before")
    @classmethod
    def _sql(cls, v: Any) -> str:
        return str(v)


class Cube(_T):
    name: str
    sql_table: str | None = Field(
        default=None, validation_alias=AliasChoices("sql_table", "sqlTable")
    )
    sql: str | None = None
    extends: str | None = None
    title: str | None = None
    description: str | None = None
    data_source: str | None = Field(
        default=None, validation_alias=AliasChoices("data_source", "dataSource")
    )
    public: bool = True
    meta: dict[str, Any] = Field(default_factory=dict)
    dimensions: list[Dimension] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)
    joins: list[Join] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    file: str = ""  # path of the model file it came from (set by the reader)

    @field_validator("sql_table", "sql", mode="before")
    @classmethod
    def _sql(cls, v: Any) -> str | None:
        return _text(v)


class ViewCube(_T):
    join_path: str = Field(validation_alias=AliasChoices("join_path", "joinPath"))
    includes: Any = None
    excludes: Any = None
    prefix: bool = False
    alias: str | None = None


class View(_T):
    name: str
    title: str | None = None
    description: str | None = None
    cubes: list[ViewCube] = Field(default_factory=list)
    file: str = ""


class Model:
    """All cubes and views found in the model files, extends resolved."""

    def __init__(self) -> None:
        self.cubes: dict[str, Cube] = {}
        self.views: dict[str, View] = {}
        self.warnings: list[SourceWarning] = []

    def warn(self, code: str, message: str, key: str | None = None) -> None:
        self.warnings.append(SourceWarning(code=code, message=message, key=key))

    # ---- reading -------------------------------------------------------------------------

    def add_file(self, path: str, data: bytes) -> None:
        if path.lower().endswith(".js"):
            self.warn("js_model_skipped",
                      f"{path}: JavaScript data models are not read; use YAML models or pass "
                      "--meta-url / --meta (the /cubejs-api/v1/meta response)", path)
            return
        try:
            doc = yaml.safe_load(data.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            self.warn("invalid_yaml",
                      f"{path}: not readable as YAML ({str(e).splitlines()[0]}); Jinja templated "
                      "models must be rendered first", path)
            return
        if doc is None:
            return
        if not isinstance(doc, dict):
            self.warn("invalid_yaml", f"{path}: expected a mapping with cubes: or views:", path)
            return
        for raw in _list(doc.get("cubes")):
            self._add(Cube, raw, path, self.cubes, "cube")
        for raw in _list(doc.get("views")):
            self._add(View, raw, path, self.views, "view")

    def _add(self, cls: type[Cube] | type[View], raw: Any, path: str,
             into: dict[str, Any], what: str) -> None:
        name = raw.get("name") if isinstance(raw, dict) else None
        try:
            obj = cls.model_validate(raw)
        except ValidationError as e:
            err = e.errors()[0]
            where = ".".join(str(p) for p in err["loc"])
            self.warn("invalid_node", f"{path}: {what} {name or '?'}: {where}: {err['msg']}", path)
            return
        obj.file = path
        if obj.name in into:
            self.warn("duplicate_node",
                      f"{what} {obj.name!r} is defined in {into[obj.name].file} and {path}; the "
                      "first (by path) is used", obj.name)
            return
        into[obj.name] = obj

    # ---- extends -------------------------------------------------------------------------

    def resolve_extends(self) -> None:
        """Merge ``extends: base`` (members of the child win); unknown bases are warned."""
        done: dict[str, Cube] = {}

        def resolve(name: str, chain: tuple[str, ...]) -> Cube:
            if name in done:
                return done[name]
            cube = self.cubes[name]
            if cube.extends:
                if cube.extends in chain or cube.extends not in self.cubes:
                    why = "is circular" if cube.extends in chain else "is not defined"
                    self.warn("extends", f"cube {name} extends {cube.extends!r}, which {why}",
                              name)
                else:
                    base = resolve(cube.extends, (*chain, name))
                    cube = _merge(base, cube)
            done[name] = cube
            return cube

        for n in sorted(self.cubes):
            resolve(n, ())
        self.cubes = {n: done[n] for n in sorted(done)}


def _list(v: Any) -> list[Any]:
    return list(v) if isinstance(v, list) else []


def _merge(base: Cube, child: Cube) -> Cube:
    def by_name[T: Dimension | Measure | Join | Segment](a: list[T], b: list[T]) -> list[T]:
        merged = {x.name: x for x in a}
        merged.update({x.name: x for x in b})
        return list(merged.values())

    data = child.model_dump()
    for field in ("sql_table", "sql", "title", "description", "data_source"):
        if data.get(field) is None:
            data[field] = getattr(base, field)
    if child.sql and not child.sql_table:
        data["sql_table"] = None
    data["meta"] = {**base.meta, **child.meta}
    data["dimensions"] = by_name(base.dimensions, child.dimensions)
    data["measures"] = by_name(base.measures, child.measures)
    data["joins"] = by_name(base.joins, child.joins)
    data["segments"] = by_name(base.segments, child.segments)
    return Cube.model_validate(data)
