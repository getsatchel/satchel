"""Implicit signal extractors for ``run_sql`` (design §6.2).

Run synchronously after each ``run_sql`` for now (the worker takes this over later). SQLGlot
extracts tables and columns and they are resolved to ``asset`` ids. Each extractor returns
``SignalSpec`` rows; the caller writes them with ``source='implicit'``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession
from sqlglot import exp
from sqlglot.errors import SqlglotError

from satchel.core.item_types import yaml_item_dict
from satchel.curation.releases import release_items_stmt
from satchel.data.gateway import DIALECTS
from satchel.data.semantic import (
    PublishedModel,
    SemanticError,
    load_published,
    merge_manifests,
    model_tables,
)
from satchel.sources.precedence import group_docs, load_sources
from satchel.store.models import Asset, ContextItem, Signal, ToolCall

RETRY_STORM_THRESHOLD = 3
UNENDORSED_COLUMN_OVERLAP = 0.5


@dataclass
class SignalSpec:
    kind: str
    payload: dict[str, Any]
    asset_ids: list[uuid.UUID] = field(default_factory=list)
    item_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass
class SqlRun:
    sql: str
    dialect: str
    connection_id: uuid.UUID
    error: str | None = None
    error_kind: str | None = None  # "execution" | "guardrail"


def normalise_error(msg: str) -> str:
    """Stable form of a DB error: literals, numbers and positions removed."""
    m = msg.strip().splitlines()[0] if msg.strip() else ""
    m = re.sub(r"'[^']*'", "'?'", m)
    m = re.sub(r"\b\d+(\.\d+)?\b", "N", m)
    m = re.sub(r"\s+", " ", m)
    return m.lower()[:300]


# ----------------------------------------------------------------------------- resolution


@dataclass
class Refs:
    tables: list[Asset]
    columns: list[Asset]


class _Catalog:
    """Catalog lookup for a connection: loads tables eagerly and columns lazily per table."""

    def __init__(self, s: OrmSession, connection_id: uuid.UUID) -> None:
        self._s = s
        self._conn_id = connection_id
        self.tables = list(
            s.scalars(
                select(Asset).where(
                    Asset.connection_id == connection_id,
                    Asset.kind == "table",
                    Asset.dropped_at.is_(None),
                )
            )
        )
        self._cols: dict[uuid.UUID, dict[str, Asset]] = {}

    def load_columns(self, table_ids: set[uuid.UUID]) -> None:
        missing = [tid for tid in table_ids if tid not in self._cols]
        if not missing:
            return
        for tid in missing:
            self._cols[tid] = {}
        for a in self._s.scalars(
            select(Asset).where(
                Asset.connection_id == self._conn_id,
                Asset.kind == "column",
                Asset.parent_id.in_(missing),
                Asset.dropped_at.is_(None),
            )
        ):
            if a.parent_id:
                self._cols.setdefault(a.parent_id, {})[a.fqn.rsplit(".", 1)[1].lower()] = a

    def cols_of(self, table_id: uuid.UUID) -> dict[str, Asset]:
        if table_id not in self._cols:
            self.load_columns({table_id})
        return self._cols.get(table_id, {})

    def table(self, name: str, db: str = "") -> Asset | None:
        n = f"{db}.{name}".lower() if db else name.lower()
        exact = [t for t in self.tables if t.fqn.lower() == n]
        if exact:
            return exact[0]
        suffix = [t for t in self.tables if t.fqn.lower().endswith("." + n)]
        return suffix[0] if len(suffix) == 1 else None


def _tables_of(tree: exp.Expr) -> list[exp.Table]:
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return [t for t in tree.find_all(exp.Table)
            if t.name and (t.db or t.name.lower() not in ctes)]


def resolve_refs(cat: _Catalog, tree: exp.Expr) -> Refs:
    aliases: dict[str, Asset] = {}
    tables: dict[uuid.UUID, Asset] = {}
    for tref in _tables_of(tree):
        a = cat.table(tref.name, tref.db)
        if a is None:
            continue
        tables[a.id] = a
        aliases[tref.alias_or_name.lower()] = a
    cat.load_columns(set(tables))
    cols: dict[uuid.UUID, Asset] = {}
    for c in tree.find_all(exp.Column):
        if not c.name:
            continue
        name = c.name.lower()
        if c.table:
            ta = aliases.get(c.table.lower())
            cand = [cat.cols_of(ta.id).get(name)] if ta else []
        else:
            cand = [cat.cols_of(ta.id).get(name) for ta in tables.values()]
        found = [x for x in cand if x is not None]
        if len(found) == 1:
            cols[found[0].id] = found[0]
    return Refs(list(tables.values()), list(cols.values()))


# ----------------------------------------------------------------------------- doc state


@dataclass
class _Docs:
    docs: dict[uuid.UUID, tuple[ContextItem, dict[str, Any]]]
    endorsed: set[uuid.UUID]


def _load_docs(s: OrmSession, cat: _Catalog, release_id: uuid.UUID | None = None) -> _Docs:
    docs: dict[uuid.UUID, tuple[ContextItem, dict[str, Any]]] = {}
    endorsed: set[uuid.UUID] = set()
    rows = list(
        s.execute(
            release_items_stmt(release_id).where(
                ContextItem.type.in_(["asset_doc", "endorsement"])
            )
        )
    )
    # several sources may document one table; the primary doc is the one that counts (M7b)
    groups = group_docs(((i, v) for i, v in rows if i.type == "asset_doc"), load_sources(s))
    for scope_id, g in groups.items():
        item, ver = g.primary.item, g.primary.version
        d = yaml_item_dict(item.type, ver.body, ver.meta) if ver is not None else {}
        if not d:
            continue
        docs[scope_id] = (item, d)
        if d.get("endorsed"):
            endorsed.add(scope_id)
    for item, ver in rows:
        if item.type != "endorsement":
            continue
        d = yaml_item_dict(item.type, ver.body, ver.meta)
        if d and d.get("endorsed") and d.get("asset"):
            a = cat.table(str(d["asset"]))
            if a is not None:
                endorsed.add(a.id)
    return _Docs(docs, endorsed)


# ----------------------------------------------------------------------------- extractors


def _undocumented(refs: Refs, docs: _Docs) -> SignalSpec | None:
    missing_tables = [t for t in refs.tables if t.id not in docs.docs]
    by_id = {t.id: t for t in refs.tables}
    missing_cols: list[Asset] = []
    for c in refs.columns:
        if c.parent_id in docs.docs and c.parent_id in by_id:
            documented = {
                str(x.get("name", "")).lower() for x in docs.docs[c.parent_id][1].get("columns", [])
                if isinstance(x, dict)
            }
            if c.fqn.rsplit(".", 1)[1].lower() not in documented:
                missing_cols.append(c)
    if not missing_tables and not missing_cols:
        return None
    ids = [a.id for a in missing_tables + missing_cols]
    return SignalSpec(
        "undocumented_asset",
        {"tables": [t.fqn for t in missing_tables], "columns": [c.fqn for c in missing_cols]},
        asset_ids=ids,
    )


def _name_key(fqn: str) -> str:
    bare = fqn.rsplit(".", 1)[-1].lower()
    bare = re.sub(r"[^a-z0-9]", "", bare)
    return bare[:-1] if bare.endswith("s") else bare


def _unendorsed(refs: Refs, cat: _Catalog, docs: _Docs) -> list[SignalSpec]:
    out: list[SignalSpec] = []
    endorsed = [t for t in cat.tables if t.id in docs.endorsed]
    unendorsed_refs = [t for t in refs.tables if t.id not in docs.endorsed]
    if not unendorsed_refs or not endorsed:
        return out
    cat.load_columns({t.id for t in unendorsed_refs} | {e.id for e in endorsed})
    for t in unendorsed_refs:
        mine = set(cat.cols_of(t.id))
        preferred: list[tuple[Asset, str]] = []
        for e in endorsed:
            if e.id == t.id:
                continue
            theirs = set(cat.cols_of(e.id))
            overlap = len(mine & theirs) / len(mine) if mine else 0.0
            if overlap >= UNENDORSED_COLUMN_OVERLAP:
                preferred.append((e, f"shares {overlap:.0%} of its columns"))
            elif _name_key(e.fqn) == _name_key(t.fqn):
                preferred.append((e, "matching table name"))
        if preferred:
            out.append(SignalSpec(
                "unendorsed_used",
                {"used": t.fqn, "preferred": [{"table": e.fqn, "reason": r} for e, r in preferred]},
                asset_ids=[t.id, *[e.id for e, _ in preferred]],
                item_ids=[docs.docs[e.id][0].id for e, _ in preferred if e.id in docs.docs],
            ))
    return out


def _conjuncts(e: exp.Expr | None) -> set[str]:
    if e is None:
        return set()
    parts: list[exp.Expr] = []

    def walk(n: exp.Expr) -> None:
        if isinstance(n, exp.Paren):
            walk(n.this)
        elif isinstance(n, exp.And):
            walk(n.left)
            walk(n.right)
        else:
            parts.append(n)

    walk(e)
    out = set()
    for p in parts:
        p = p.copy()
        for c in list(p.find_all(exp.Column)):
            c.set("table", None)
        for i in list(p.find_all(exp.Identifier)):
            i.set("this", i.name.lower())
        for n in list(p.find_all(exp.In)):
            n.set("expressions", sorted(n.expressions, key=lambda x: x.sql()))
        out.add(p.sql())
    return out


def _agg_signature(node: exp.Expr) -> tuple[str, str] | None:
    aggs = list(node.find_all(exp.AggFunc))
    if not aggs:
        return None
    a = aggs[0]
    arg = a.this
    if isinstance(arg, exp.Distinct):
        arg = arg.expressions[0] if arg.expressions else None
    col = "*" if arg is None or isinstance(arg, exp.Star) else (
        arg.name.lower() if isinstance(arg, exp.Column) else arg.sql().lower())
    return type(a).__name__, col


def _same_table(a: str, b: str) -> bool:
    """``orders`` matches ``analytics.orders``; two qualified names must be equal."""
    if a == b:
        return True
    return ("." not in a or "." not in b) and a.rsplit(".", 1)[-1] == b.rsplit(".", 1)[-1]


def _own_select(n: exp.Expr) -> exp.Select | None:
    p = n.parent
    while p is not None and not isinstance(p, exp.Select):
        p = p.parent
    return p


def _diverging(
    tree: exp.Expr, published: list[PublishedModel], item_ids: dict[str, uuid.UUID]
) -> list[SignalSpec]:
    try:
        mdl = merge_manifests(published)
    except SemanticError:
        return []
    out: list[SignalSpec] = []
    for cube in mdl.get("cubes") or []:
        base = next((m for m in mdl.get("models") or [] if m["name"] == cube["baseObject"]), None)
        if base is None:
            continue
        src = {t.lower() for t in model_tables(mdl, base["name"])}
        model_where: set[str] = set()
        if base.get("refSql"):
            try:
                ref = sqlglot.parse_one(base["refSql"])
                model_where = _conjuncts(ref.args["where"].this) if ref.args.get("where") else set()
            except SqlglotError:
                pass
        for measure in cube.get("measures") or []:
            try:
                sig = _agg_signature(sqlglot.parse_one(measure["expression"]))
            except SqlglotError:
                continue
            if sig is None:
                continue
            for sel in tree.find_all(exp.Select):
                names = {
                    (f"{t.db}.{t.name}" if t.db else t.name).lower()
                    for t in sel.find_all(exp.Table) if _own_select(t) is sel
                }
                if not any(_same_table(n, t) for n in names for t in src):
                    continue
                mine = list(sel.expressions)
                hit = [e for e in mine if _agg_signature(e) == sig]
                if not hit:
                    continue
                where = _conjuncts(sel.args["where"].this) if sel.args.get("where") else set()
                missing = sorted(model_where - where)
                if missing:
                    out.append(SignalSpec(
                        "metric_divergence",
                        {"metric": f"{cube['name']}.{measure['name']}",
                         "adhoc_aggregate": hit[0].sql()[:200],
                         "missing_filters": missing,
                         "metric_expression": measure["expression"]},
                        item_ids=[i for k, i in item_ids.items()],
                    ))
    return out


def _seen(s: OrmSession, session_id: uuid.UUID, kind: str, key: str) -> bool:
    return s.scalar(
        select(func.count()).select_from(Signal).where(
            Signal.session_id == session_id, Signal.kind == kind,
            Signal.payload["dedupe"].astext == key)
    ) not in (0, None)


def extract_sql_signals(
    s: OrmSession,
    *,
    session_id: uuid.UUID,
    run: SqlRun,
    current_tool_call_id: int | None,
    release_id: uuid.UUID | None = None,
) -> list[SignalSpec]:
    specs: list[SignalSpec] = []
    if run.error and run.error_kind == "execution":
        specs.append(SignalSpec("sql_error", {
            "message": normalise_error(run.error), "raw": run.error[:500], "sql": run.sql[:2000]}))
    if run.error:
        prior = s.scalar(
            select(func.count()).select_from(ToolCall).where(
                ToolCall.session_id == session_id, ToolCall.tool == "run_sql",
                ToolCall.error.is_not(None),
                ToolCall.id != (current_tool_call_id or -1))
        ) or 0
        if prior + 1 >= RETRY_STORM_THRESHOLD:
            specs.append(SignalSpec("retry_storm", {
                "failed_attempts": prior + 1, "last_error": normalise_error(run.error)}))
    if run.error_kind != "guardrail":
        try:
            tree = sqlglot.parse_one(run.sql, read=DIALECTS.get(run.dialect))
        except SqlglotError:
            tree = None
        if tree is not None:
            cat = _Catalog(s, run.connection_id)
            refs = resolve_refs(cat, tree)
            docs = _load_docs(s, cat, release_id=release_id)
            u = _undocumented(refs, docs)
            if u:
                specs.append(u)
            specs += _unendorsed(refs, cat, docs)
            published = load_published(s, release_id=release_id)
            specs += _diverging(tree, published, {p.key: p.item_id for p in published})
    # one signal per (kind, key) per session
    final: list[SignalSpec] = []
    for sp in specs:
        key = _dedupe_key(sp)
        if sp.kind != "sql_error" and _seen(s, session_id, sp.kind, key):
            continue
        sp.payload["dedupe"] = key
        final.append(sp)
    return final


def _dedupe_key(sp: SignalSpec) -> str:
    if sp.kind == "retry_storm":
        return "session"
    if sp.kind == "metric_divergence":
        return f"{sp.payload['metric']}|{'|'.join(sp.payload['missing_filters'])}"
    if sp.kind == "unendorsed_used":
        return str(sp.payload["used"])
    return "|".join(sorted(str(a) for a in sp.asset_ids)) or sp.kind
