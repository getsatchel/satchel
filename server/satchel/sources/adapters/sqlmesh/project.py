"""SQLMesh models -> ``SourceAsset``: description, columns (declared types, descriptions, audits as
facts), grain, kind, owner, tags and the lineage found in the query."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
import yaml
from satchel_adapter_sdk import SourceAsset, SourceColumn, SourceRelationship, normalise_fqn
from sqlglot import exp
from sqlglot.errors import SqlglotError

from satchel.sources.adapters.sqlmesh import parse as P
from satchel.sources.adapters.sqlmesh.parse import Call, ParseError

AUDIT_SUFFIX = "_non_blocking"
SKIP_DIRS = {"audits", "macros", "tests", "seeds", "logs"}
_MACRO = re.compile(r"@\{?\w")
_PART = r"(?:[\w$]+|\"[^\"]+\"|`[^`]+`)"
_FROM_JOIN = re.compile(rf"(?i)\b(?:from|join)\s+({_PART}(?:\.{_PART})*)")


@dataclass
class Model:
    """One SQLMesh model as read from a file."""

    name: str
    path: str
    props: dict[str, Any]
    raw: dict[str, str] = field(default_factory=dict)
    query: str = ""
    python: bool = False

    @property
    def fqn(self) -> str:
        return normalise_fqn(self.name)


def _strs(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list | tuple):
        out: list[str] = []
        for x in v:
            out += _strs(x)
        return out
    return []


def _text(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _col_names(v: Any) -> list[str]:
    """A column list argument: ``a``, ``(a, b)`` or ``[a, b]``."""
    return [c.strip().strip('"`') for c in _strs(v) if c.strip()]


class Project:
    """All models of a project folder, ``config.yaml`` defaults applied."""

    def __init__(self, dialect: str | None = None) -> None:
        self.models: dict[str, Model] = {}
        self.defaults: dict[str, Any] = {}
        self.dialect = dialect
        self.warnings: list[tuple[str, str, str | None]] = []

    def warn(self, code: str, message: str, key: str | None = None) -> None:
        self.warnings.append((code, message, key))

    # ---- reading -------------------------------------------------------------------------

    def read(self, files: dict[str, bytes]) -> None:
        """``files``: relative path -> bytes of the project folder."""
        for path in sorted(files):
            low = path.lower()
            parts = path.split("/")
            if len(parts) == 1 and low in ("config.yaml", "config.yml"):
                self._config(path, files[path])
        for path in sorted(files):
            low = path.lower()
            if any(p in SKIP_DIRS for p in path.split("/")[:-1]):
                continue
            try:
                text = files[path].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if low.endswith(".sql") and P.has_model_block(text):
                self._sql(path, text)
            elif low.endswith(".py") and "@model" in text:
                self._python(path, text)

    def _config(self, path: str, data: bytes) -> None:
        try:
            cfg = yaml.safe_load(data.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            self.warn("invalid_config", f"{path}: not readable as YAML ({str(e).splitlines()[0]})",
                      path)
            return
        md = cfg.get("model_defaults") if isinstance(cfg, dict) else None
        if isinstance(md, dict):
            self.defaults = md
            if self.dialect is None and isinstance(md.get("dialect"), str):
                self.dialect = md["dialect"]

    def _add(self, name: Any, path: str, props: dict[str, Any], raw: dict[str, str],
             query: str, python: bool) -> None:
        if not isinstance(name, str) or not name.strip():
            self.warn("invalid_model", f"{path}: the model has no readable name", path)
            return
        m = Model(name.strip(), path, props, raw, query, python)
        if m.fqn in self.models:
            self.warn("duplicate_model",
                      f"model {m.fqn} is defined in {self.models[m.fqn].path} and {path}; the "
                      "first (by path) is used", m.fqn)
            return
        self.models[m.fqn] = m

    def _sql(self, path: str, text: str) -> None:
        try:
            block = P.parse_model(text)
        except ParseError as e:
            self.warn("invalid_model", f"{path}: cannot read the MODEL block ({e})", path)
            return
        name = block.props.get("name")
        self._add(name, path, block.props, block.raw, block.query, False)

    def _python(self, path: str, text: str) -> None:
        try:
            found = P.python_models(text)
        except ParseError as e:
            self.warn("invalid_model", f"{path}: {e}", path)
            return
        for props in found:
            self._add(props.get("name"), path, props, {}, "", True)

    # ---- assets --------------------------------------------------------------------------

    def assets(self) -> list[SourceAsset]:
        usable = {f: m for f, m in self.models.items() if self._kind(m)[0] != "EMBEDDED"
                  and self._enabled(m)}
        grains: dict[str, list[str]] = {}
        built = {f: self._build(m, usable) for f, m in sorted(usable.items())}
        for f, a in built.items():
            if len(a.grain) == 1:
                grains.setdefault(a.grain[0].lower(), []).append(f)
        for f, a in built.items():
            self._references(usable[f], a, grains, usable)
        return sorted(built.values(), key=lambda a: a.key)

    @staticmethod
    def _kind(m: Model) -> tuple[str, dict[str, Any]]:
        k = m.props.get("kind")
        if isinstance(k, Call):
            return k.name.upper(), k.kwargs
        if isinstance(k, dict):
            return str(k.get("name", "")).upper(), k
        return (str(k).upper() if isinstance(k, str) else "FULL"), {}

    @staticmethod
    def _enabled(m: Model) -> bool:
        return m.props.get("enabled") is not False

    def _dialect(self, m: Model) -> str | None:
        d = m.props.get("dialect")
        d = d if isinstance(d, str) else self.dialect
        return d.split(",")[0].strip() if d else None

    def _build(self, m: Model, usable: dict[str, Model]) -> SourceAsset:
        props = m.props
        cols: dict[str, SourceColumn] = {}

        def col(name: str) -> SourceColumn:
            c = cols.get(name.lower())
            if c is None:
                c = cols[name.lower()] = SourceColumn(name=name)
            return c

        declared = props.get("columns")
        if m.python and isinstance(declared, dict):
            for n, t in declared.items():
                col(str(n)).data_type = str(t).lower()
        elif "columns" in m.raw:
            for n, t, required in P.parse_columns(m.raw["columns"]):
                declared_col = col(n)
                declared_col.data_type = t.lower()
                declared_col.not_null = declared_col.not_null or required
        deps: list[str] = []
        if m.query:
            names, tables = self._query(m)
            for n in names:
                col(n)
            deps = tables
        cd = props.get("column_descriptions")
        if isinstance(cd, dict):
            for n, d in cd.items():
                if _text(d):
                    col(str(n)).description = str(d).strip()
        grain_cols = self._grain(props)
        combo: list[str] = []
        for a in props.get("audits") or []:
            if not isinstance(a, Call):
                continue
            name = a.name.lower().removesuffix(AUDIT_SUFFIX)
            kw = {k.lower(): v for k, v in a.kwargs.items()}
            if kw.get("where"):
                continue  # a filtered audit does not hold for every row
            if name == "not_null":
                for cn_ in _col_names(kw.get("columns")):
                    col(cn_).not_null = True
            elif name == "unique_values":
                for cn_ in _col_names(kw.get("columns")):
                    col(cn_).unique = True
            elif name == "unique_combination_of_columns":
                combo = _col_names(kw.get("columns"))
            elif name == "accepted_values":
                cn = _col_names(kw.get("column"))
                vals = [str(v) for v in _strs(kw.get("is_in"))]
                if cn and vals:
                    col(cn[0]).enum_values = vals
        if not grain_cols:
            grain_cols = combo or [c.name for c in cols.values() if c.unique and c.not_null][:1]
        by_lc = {c.name.lower(): c.name for c in cols.values()}
        grain_cols = [by_lc.get(g.lower(), g) for g in grain_cols]

        kind, kw = self._kind(m)
        owner = _text(props.get("owner")) or _text(self.defaults.get("owner"))
        description = _text(props.get("description"))
        short: dict[str, str] = {}
        for f in usable:
            short.setdefault(".".join(f.split(".")[-2:]), f)
        depends: set[str] = set()
        for table in [*deps, *_strs(props.get("depends_on"))]:
            n = normalise_fqn(table)
            target = n if n in usable else short.get(".".join(n.split(".")[-2:]))
            if target and target != m.fqn:
                depends.add(target)
        meta: dict[str, Any] = {
            "model": m.name, "kind": kind, "path": m.path, "python": m.python or None,
            "dialect": self._dialect(m), "cron": _text(props.get("cron")),
            "start": str(props["start"]) if props.get("start") is not None else None,
            "time_column": next(iter(_strs(kw.get("time_column"))), None),
            "audits": sorted({a.name for a in props.get("audits") or [] if isinstance(a, Call)}),
        }
        parts = normalise_fqn(m.name).split(".")
        return SourceAsset(
            key=f"model.{m.fqn}", fqn=".".join(parts[-2:]) if len(parts) > 1 else parts[0],
            kind="view" if kind == "VIEW" else "table",
            name=parts[-1], database=".".join(parts[:-2]) or None, description=description,
            columns=sorted(cols.values(), key=lambda c: c.name.lower()),
            grain=grain_cols, tags=sorted(set(_strs(props.get("tags")))), owner=owner,
            depends_on=[f"model.{d}" for d in sorted(depends)],
            source_meta={k: v for k, v in meta.items() if v not in (None, "", [])},
        )

    @staticmethod
    def _grain(props: dict[str, Any]) -> list[str]:
        g = props.get("grain", props.get("grains"))
        if isinstance(g, list) and g and isinstance(g[0], list):
            g = g[0]  # grains ((a, b), (c)): the first
        return _col_names(g)

    def _query(self, m: Model) -> tuple[list[str], list[str]]:
        """Output column names and the tables the query reads, or empty when it cannot be
        parsed (macros)."""
        body = m.query.strip().rstrip(";")
        if not body:
            return [], []
        dialect = self._dialect(m)
        if not _MACRO.search(body):
            for read in (dialect, None) if dialect else (None,):
                try:
                    tree = sqlglot.parse_one(body, read=read)
                except (SqlglotError, ValueError):
                    continue
                if tree is None:
                    continue
                ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
                tables = [
                    ".".join(p for p in (t.catalog, t.db, t.name) if p)
                    for t in tree.find_all(exp.Table) if t.name.lower() not in ctes or t.db
                ]
                names: list[str] = []
                if isinstance(tree, exp.Query):
                    names = [n for n in tree.named_selects if n and n != "*"]
                return names, tables
        self.warn("query_unparsed",
                  f"model {m.fqn}: the query uses macros or syntax that was not parsed, so its "
                  "columns and upstream tables are not inferred (declare `columns`)", m.fqn)
        tables = [t.strip() for t in _FROM_JOIN.findall(body)]
        return [], tables

    def _references(
        self, m: Model, a: SourceAsset, grains: dict[str, list[str]], usable: dict[str, Model]
    ) -> None:
        """``references (customer_id)``: a column named like another model's single-column
        grain is a join hint to it."""
        refs = m.props.get("references")
        if refs is None:
            return
        names: list[str] = []
        for r in refs if isinstance(refs, list) else [refs]:
            if isinstance(r, Call):
                names += _col_names(r.args[0] if r.args else None) if r.args else []
            elif isinstance(r, str | list):
                names += _col_names(r)
        seen: set[tuple[str, str]] = set()
        for n in names:
            for target in grains.get(n.lower(), []):
                if target == m.fqn or (n.lower(), target) in seen:
                    continue
                seen.add((n.lower(), target))
                ta = usable[target]
                parts = normalise_fqn(ta.name).split(".")
                a.relationships.append(SourceRelationship(
                    column=n, to_asset=".".join(parts[-2:]) if len(parts) > 1 else parts[0],
                    to_column=n))
        a.relationships.sort(key=lambda r: (r.column, r.to_asset))
