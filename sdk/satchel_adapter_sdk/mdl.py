"""Wren MDL helpers, so adapter authors never touch Wren internals.

Satchel's semantic engine consumes a Wren MDL manifest: ``models`` (a table or ``refSql`` plus
columns), ``relationships`` and ``cubes`` (measures, dimensions and time dimensions over a base
model). :class:`MdlBuilder` assembles one and :func:`validate_mdl` checks the structural
rules Satchel relies on (unique names, the cube's base model exists, relationship models exist).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

JoinType = Literal["ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "MANY_TO_MANY"]


class _Mdl(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class MdlTableReference(_Mdl):
    schema_: str | None = Field(default=None, alias="schema")
    table: str
    catalog: str | None = None


class MdlColumn(_Mdl):
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    description: str | None = None
    is_calculated: bool | None = Field(default=None, alias="isCalculated")
    expression: str | None = None
    relationship: str | None = None


class MdlModel(_Mdl):
    name: str = Field(min_length=1)
    table_reference: MdlTableReference | None = Field(default=None, alias="tableReference")
    ref_sql: str | None = Field(default=None, alias="refSql")
    primary_key: str | None = Field(default=None, alias="primaryKey")
    columns: list[MdlColumn] = Field(default_factory=list)
    description: str | None = None


class MdlRelationship(_Mdl):
    name: str = Field(min_length=1)
    models: list[str]
    join_type: JoinType = Field(alias="joinType")
    condition: str


class MdlMember(_Mdl):
    name: str = Field(min_length=1)
    expression: str = Field(min_length=1)
    type: str = Field(min_length=1)
    description: str | None = None


class MdlCube(_Mdl):
    name: str = Field(min_length=1)
    base_object: str = Field(alias="baseObject")
    measures: list[MdlMember] = Field(default_factory=list)
    dimensions: list[MdlMember] = Field(default_factory=list)
    time_dimensions: list[MdlMember] = Field(default_factory=list, alias="timeDimensions")
    description: str | None = None


class Mdl(_Mdl):
    catalog: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    data_source: str | None = Field(default=None, alias="dataSource")
    models: list[MdlModel] = Field(default_factory=list)
    relationships: list[MdlRelationship] = Field(default_factory=list)
    cubes: list[MdlCube] = Field(default_factory=list)


def validate_mdl(data: dict[str, Any]) -> list[str]:
    """Return a list of problems (empty = valid). Never raises."""
    try:
        mdl = Mdl.model_validate(data)
    except ValidationError as e:
        return [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
    errors: list[str] = []
    models = {m.name for m in mdl.models}
    for label, names in (
        ("model", [m.name for m in mdl.models]),
        ("relationship", [r.name for r in mdl.relationships]),
        ("cube", [c.name for c in mdl.cubes]),
    ):
        for dup in sorted({n for n in names if names.count(n) > 1}):
            errors.append(f"duplicate {label} name {dup!r}")
    for m in mdl.models:
        if m.table_reference is None and not m.ref_sql:
            errors.append(f"model {m.name!r} needs tableReference or refSql")
    for r in mdl.relationships:
        for mn in r.models:
            if mn not in models:
                errors.append(f"relationship {r.name!r} refers to unknown model {mn!r}")
    for c in mdl.cubes:
        if c.base_object not in models:
            errors.append(f"cube {c.name!r} has unknown baseObject {c.base_object!r}")
        members = [x.name for x in (*c.measures, *c.dimensions, *c.time_dimensions)]
        for dup in sorted({n for n in members if members.count(n) > 1}):
            errors.append(f"cube {c.name!r} has duplicate member {dup!r}")
        if not c.measures:
            errors.append(f"cube {c.name!r} has no measures")
    return errors


class MdlBuilder:
    """Fluent builder for a Wren MDL manifest.

    >>> b = MdlBuilder(data_source="duckdb")
    >>> b.model("orders", schema="analytics", table="orders", columns={"amount": "integer"})
    >>> b.cube("sales", base="orders", measures=[("revenue", "SUM(amount)", "DOUBLE")])
    >>> mdl = b.build()
    """

    def __init__(
        self, *, data_source: str | None = None, catalog: str = "satchel", schema: str = "public"
    ) -> None:
        self._top: dict[str, Any] = {"catalog": catalog, "schema": schema}
        if data_source:
            self._top["dataSource"] = data_source
        self._models: list[dict[str, Any]] = []
        self._relationships: list[dict[str, Any]] = []
        self._cubes: list[dict[str, Any]] = []

    def model(
        self,
        name: str,
        *,
        table: str | None = None,
        schema: str | None = None,
        ref_sql: str | None = None,
        columns: dict[str, str] | None = None,
        primary_key: str | None = None,
        description: str | None = None,
    ) -> MdlBuilder:
        m: dict[str, Any] = {"name": name}
        if ref_sql:
            m["refSql"] = ref_sql
        elif table:
            ref: dict[str, Any] = {"table": table}
            if schema:
                ref["schema"] = schema
            m["tableReference"] = ref
        if primary_key:
            m["primaryKey"] = primary_key
        if description:
            m["description"] = description
        m["columns"] = [{"name": n, "type": t} for n, t in (columns or {}).items()]
        self._models.append(m)
        return self

    def relationship(
        self, name: str, models: tuple[str, str], join_type: JoinType, condition: str
    ) -> MdlBuilder:
        self._relationships.append(
            {"name": name, "models": list(models), "joinType": join_type, "condition": condition}
        )
        return self

    def cube(
        self,
        name: str,
        *,
        base: str,
        measures: Sequence[tuple[str, str, str] | tuple[str, str, str, str | None]],
        dimensions: Sequence[tuple[str, str, str] | tuple[str, str, str, str | None]] | None = None,
        time_dimensions: Sequence[tuple[str, str, str] | tuple[str, str, str, str | None]]
        | None = None,
        description: str | None = None,
    ) -> MdlBuilder:
        """Members are ``(name, expression, type[, description])`` tuples."""

        def members(rows: Sequence[Any] | None) -> list[dict[str, Any]]:
            out = []
            for row in rows or []:
                d: dict[str, Any] = {"name": row[0], "expression": row[1], "type": row[2]}
                if len(row) > 3 and row[3]:
                    d["description"] = row[3]
                out.append(d)
            return out

        c: dict[str, Any] = {"name": name, "baseObject": base, "measures": members(measures)}
        if dimensions:
            c["dimensions"] = members(dimensions)
        if time_dimensions:
            c["timeDimensions"] = members(time_dimensions)
        if description:
            c["description"] = description
        self._cubes.append(c)
        return self

    def build(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            **self._top,
            "models": self._models,
            "relationships": self._relationships,
            "cubes": self._cubes,
        }
        errors = validate_mdl(out)
        if errors:
            raise ValueError("invalid MDL: " + "; ".join(errors))
        return out
