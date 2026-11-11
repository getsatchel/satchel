"""Persistence for eval suites: versioned by (workspace, key); a new version only when the
content changes."""

from __future__ import annotations

import fnmatch
import uuid
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from satchel.evals.suite import Suite, SuiteError, parse_suite, spec_hash, suite_spec
from satchel.store.models import EvalSuite

DEFAULT_GLOB = "evals/*.yaml"


def save_suite(
    s: Session, workspace_id: uuid.UUID, suite: Suite, *, managed_by: str = "ui",
    created_by: uuid.UUID | None = None,
) -> tuple[EvalSuite, bool]:
    """Insert a new version of ``suite`` unless the latest is identical. -> (row, created)."""
    spec = suite_spec(suite)
    h = spec_hash(spec)
    latest = latest_suite(s, workspace_id, suite.id)
    if latest is not None and latest.content_hash == h:
        if latest.managed_by != managed_by:
            latest.managed_by = managed_by
        return latest, False
    row = EvalSuite(
        workspace_id=workspace_id, key=suite.id,
        version_seq=(latest.version_seq if latest else 0) + 1,
        name=suite.name or suite.id, spec=spec, content_hash=h, managed_by=managed_by,
        created_by=created_by,
    )
    s.add(row)
    s.flush()
    return row, True


def latest_suite(s: Session, workspace_id: uuid.UUID, key: str) -> EvalSuite | None:
    return s.scalars(
        select(EvalSuite)
        .where(EvalSuite.workspace_id == workspace_id, EvalSuite.key == key)
        .order_by(EvalSuite.version_seq.desc())
    ).first()


def latest_suites(s: Session, workspace_id: uuid.UUID) -> list[EvalSuite]:
    newest = (
        select(EvalSuite.key, func.max(EvalSuite.version_seq).label("v"))
        .where(EvalSuite.workspace_id == workspace_id)
        .group_by(EvalSuite.key)
        .subquery()
    )
    return list(
        s.scalars(
            select(EvalSuite)
            .join(newest, (EvalSuite.key == newest.c.key) & (EvalSuite.version_seq == newest.c.v))
            .where(EvalSuite.workspace_id == workspace_id)
            .order_by(EvalSuite.key)
        )
    )


def suite_of(row: EvalSuite) -> Suite:
    return parse_suite(row.spec)


def import_repo_suites(
    s: Session, workspace_id: uuid.UUID, root: Path, config: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Import ``evals:`` globs (default ``evals/*.yaml``) from a context repo, marked git-managed.

    -> (imported keys with version, errors). ``satchel.config.yaml``'s ``evals`` is a glob or list.
    """
    globs = eval_globs(config)
    files = sorted(
        p for p in root.rglob("*.y*ml")
        if p.is_file() and not any(seg.startswith(".") for seg in p.relative_to(root).parts)
        and any(fnmatch.fnmatch(p.relative_to(root).as_posix(), g) for g in globs)
    )
    done: list[str] = []
    errors: list[str] = []
    for p in files:
        rel = p.relative_to(root).as_posix()
        try:
            suite = parse_suite(yaml.safe_load(p.read_text(encoding="utf-8")))
        except (SuiteError, yaml.YAMLError) as e:
            errors.append(f"{rel}: {e}")
            continue
        row, created = save_suite(s, workspace_id, suite, managed_by="git")
        done.append(f"{row.key}@v{row.version_seq}{'' if created else ' (unchanged)'}")
    return done, errors


def eval_globs(config: dict[str, Any]) -> list[str]:
    cfg = config.get("evals")
    if isinstance(cfg, str):
        return [cfg]
    if isinstance(cfg, list):
        return [str(g) for g in cfg]
    if isinstance(cfg, dict) and cfg.get("paths"):
        return [str(g) for g in cfg["paths"]]
    return [DEFAULT_GLOB]
