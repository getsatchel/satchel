"""Parse a §4 context repository (folder) into validated, typed items. No database access."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter
import yaml
from pydantic import BaseModel

from satchel.core.item_types import (
    AssetDoc,
    Caveat,
    GlossaryTerm,
    GoldenQuery,
    Guide,
    ItemValidationError,
    WorkspaceRule,
    normalized_meta,
    validate_item,
)
from satchel.core.render import render_yaml_item
from satchel.sync.chunking import chunk_markdown
from satchel.sync.sqlrefs import extract_tables

REPO_CONFIG = "satchel.config.yaml"

# link relations: (asset name as written in the repo, relation)
LinkSpec = tuple[str, str]


@dataclass
class ParsedItem:
    key: str
    type: str
    body: str
    meta: dict[str, Any]
    chunk_texts: list[str]
    scope_kind: str = "workspace"
    scope_ref: str | None = None  # domain name / connection name / asset fqn
    links: list[LinkSpec] = field(default_factory=list)
    scan_text: str | None = None  # markdown text scanned for mentions of known tables
    connection: str | None = None  # connection name (asset docs, golden queries)
    asset_table: str | None = None  # asset_doc: fqn of the documented table
    asset_columns: list[str] = field(default_factory=list)


@dataclass
class ParsedRepo:
    config: dict[str, Any]
    items: list[ParsedItem]
    skipped: list[str]
    errors: list[str]


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "item"


def _read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _entries(raw: Any, list_key: str, single_marker: str) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if isinstance(raw, dict):
        if isinstance(raw.get(list_key), list):
            return [e for e in raw[list_key] if isinstance(e, dict)]
        if single_marker in raw:
            return [raw]
    raise ItemValidationError(f"expected a mapping with '{single_marker}' or a '{list_key}' list")


def _markdown_item(
    rel: str, item_type: str, text: str, title: str | None
) -> tuple[ParsedItem, BaseModel]:
    post = frontmatter.loads(text)
    model = validate_item(item_type, post.content.strip(), dict(post.metadata))
    assert isinstance(model, WorkspaceRule | Guide | Caveat)
    meta = model.model_dump(mode="json", exclude={"body"})
    body = model.body
    item = ParsedItem(
        key=rel,
        type=item_type,
        body=body,
        meta=meta,
        chunk_texts=chunk_markdown(body, title),
        scan_text=body,
    )
    return item, model


def _dialect_for(config: dict[str, Any], connection: str | None) -> str | None:
    conns = config.get("connections") or {}
    if connection and isinstance(conns.get(connection), dict):
        return str(conns[connection].get("kind") or "") or None
    if len(conns) == 1:
        only = next(iter(conns.values()))
        return str(only.get("kind") or "") or None if isinstance(only, dict) else None
    return None


def _semantic_item(path: Path, rel: str) -> ParsedItem:
    from satchel.data.semantic import SemanticError, parse_mdl, summarize

    name = path.name.lower()
    if name.endswith((".mdl.json", ".mdl.yaml", ".mdl.yml")):
        fmt = "wren_mdl"
    elif name.endswith((".ossie.yaml", ".ossie.yml")):
        fmt = "ossie"
    else:
        raise ItemValidationError("semantic files must end in .mdl.json/.mdl.yaml or .ossie.yaml")
    text = path.read_text("utf-8")
    validate_item("semantic_model", text, {"format": fmt})
    links: list[LinkSpec] = []
    chunks = [text[:2000]]
    if fmt == "wren_mdl":
        try:
            mdl = parse_mdl(text)
        except SemanticError as e:
            raise ItemValidationError(str(e)) from None
        chunks = [summarize(mdl)]
        from satchel.data.semantic import model_tables

        for m in mdl.get("models") or []:
            links += [(t, "uses") for t in model_tables(mdl, m["name"])]
    return ParsedItem(
        key=rel, type="semantic_model", body=text, meta={"format": fmt}, chunk_texts=chunks,
        links=links,
    )


def _parse_one(path: Path, rel: str, config: dict[str, Any]) -> list[ParsedItem]:
    parts = rel.split("/")
    top = parts[0]
    suffix = path.suffix.lower()
    is_md = suffix == ".md"
    is_yaml = suffix in {".yaml", ".yml"}

    if rel == "workspace.md" or (top == "domains" and len(parts) >= 3 and parts[-1] == "rules.md"):
        item, _ = _markdown_item(rel, "workspace_rule", path.read_text("utf-8"), None)
        if top == "domains":
            item.scope_kind, item.scope_ref = "domain", parts[1]
            item.meta["domain"] = parts[1]
        return [item]

    if top == "guides" and is_md:
        text = path.read_text("utf-8")
        item, model = _markdown_item(rel, "guide", text, None)
        assert isinstance(model, Guide)
        item.chunk_texts = chunk_markdown(item.body, f"Guide: {model.name}. {model.description}")
        return [item]

    if top == "caveats" and is_md:
        item, model = _markdown_item(rel, "caveat", path.read_text("utf-8"), "Caveat")
        assert isinstance(model, Caveat)
        item.links = [(a, "applies_to") for a in model.applies_to]
        return [item]

    if top == "glossary" and is_yaml:
        entries = _entries(_read_yaml(path), "terms", "term")
        multi = len(entries) > 1
        out = []
        for e in entries:
            body = yaml.safe_dump(e, sort_keys=False, allow_unicode=True)
            model = validate_item("glossary_term", body)
            assert isinstance(model, GlossaryTerm)
            out.append(
                ParsedItem(
                    key=f"{rel}#{slugify(model.term)}" if multi else rel,
                    type="glossary_term",
                    body=body,
                    meta=normalized_meta(
                        "glossary_term", body, {"term": model.term, "synonyms": model.synonyms}
                    ),
                    chunk_texts=[render_yaml_item("glossary_term", model)],
                    links=[(a, "mentions") for a in model.related_assets],
                )
            )
        return out

    if top == "queries" and is_yaml:
        entries = _entries(_read_yaml(path), "queries", "question")
        multi = len(entries) > 1
        out = []
        for e in entries:
            body = yaml.safe_dump(e, sort_keys=False, allow_unicode=True)
            model = validate_item("golden_query", body)
            assert isinstance(model, GoldenQuery)
            tables = extract_tables(model.sql, _dialect_for(config, model.connection)) if (
                model.sql
            ) else []
            out.append(
                ParsedItem(
                    key=f"{rel}#{slugify(model.question)}" if multi else rel,
                    type="golden_query",
                    body=body,
                    meta=normalized_meta(
                        "golden_query",
                        body,
                        {"question": model.question, "connection": model.connection},
                    ),
                    chunk_texts=[render_yaml_item("golden_query", model)],
                    scope_kind="connection" if model.connection else "workspace",
                    scope_ref=model.connection,
                    links=[(t, "uses") for t in tables],
                    connection=model.connection,
                )
            )
        return out

    if top == "semantic" and (is_yaml or suffix == ".json"):
        return [_semantic_item(path, rel)]

    if top == "assets" and is_yaml:
        raw = _read_yaml(path)
        if not isinstance(raw, dict):
            raise ItemValidationError("asset file must be a YAML mapping")
        raw = dict(raw)
        conn = parts[1] if len(parts) >= 4 else None
        table = raw.pop("table", None) or raw.pop("asset", None)
        if not table:
            if len(parts) < 3:
                raise ItemValidationError("asset file needs a 'table' field")
            table = f"{parts[-2]}.{Path(parts[-1]).stem}"
        raw["asset"] = table
        cols = raw.get("columns")
        if isinstance(cols, dict):
            raw["columns"] = [{"name": n, **(spec or {})} for n, spec in cols.items()]
        model = validate_item("asset_doc", yaml.safe_dump(raw, sort_keys=False))
        assert isinstance(model, AssetDoc)
        body = yaml.safe_dump(
            model.model_dump(mode="json", exclude_defaults=True),
            sort_keys=False,
            allow_unicode=True,
        )
        return [
            ParsedItem(
                key=rel,
                type="asset_doc",
                body=body,
                meta=normalized_meta(
                    "asset_doc", body, {"asset": model.asset, "endorsed": model.endorsed}
                ),
                chunk_texts=[render_yaml_item("asset_doc", model)],
                scope_kind="asset",
                scope_ref=model.asset,
                links=[(model.asset, "defines")]
                + [(f"{model.asset}.{c.name}", "defines") for c in model.columns],
                connection=conn,
                asset_table=model.asset,
                asset_columns=[c.name for c in model.columns],
            )
        ]

    return []


def parse_repo(root: Path) -> ParsedRepo:
    cfg_path = root / REPO_CONFIG
    config: dict[str, Any] = {}
    if cfg_path.exists():
        loaded = _read_yaml(cfg_path)
        if isinstance(loaded, dict):
            config = loaded
    items: list[ParsedItem] = []
    skipped: list[str] = []
    errors: list[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        hidden = any(seg.startswith(".") for seg in rel.split("/"))
        if rel == REPO_CONFIG or hidden:
            continue
        try:
            parsed = _parse_one(path, rel, config)
        except (ItemValidationError, yaml.YAMLError, ValueError) as e:
            errors.append(f"{rel}: {e}")
            continue
        if parsed:
            items.extend(parsed)
        else:
            skipped.append(rel)
    keys = [i.key for i in items]
    for k in {k for k in keys if keys.count(k) > 1}:
        errors.append(f"{k}: duplicate item key")
    return ParsedRepo(config, items, skipped, errors)
