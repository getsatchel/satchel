from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import yaml

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "jaffle-shop"
EXAMPLE_CONTEXT = EXAMPLE / "context"


def copy_example(tmp_path: Path, *, gate: bool = False) -> Path:
    """Copy the example context repo. The eval publish gate (``publish:`` in the config) is
    stripped unless ``gate=True`` so tests that publish are not blocked by it."""
    dest = tmp_path / "context"
    shutil.copytree(EXAMPLE_CONTEXT, dest)
    if not gate:
        cfg = dest / "satchel.config.yaml"
        data = yaml.safe_load(cfg.read_text())
        data.pop("publish", None)
        cfg.write_text(yaml.safe_dump(data, sort_keys=False))
    return dest


def unique_slug(prefix: str = "ws") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def build_jaffle_db(path: Path) -> Path:
    """Build the example warehouse (examples/jaffle-shop/seed.py) at ``path``."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("jaffle_seed", EXAMPLE / "seed.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.build_database(path)
    return path


class MemoryBucket:
    """In-memory stand-in for a GCS or S3 bucket (``satchel.data.objstore.BucketLike``)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.range_reads: list[tuple[str, int, int]] = []

    def put(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.objects[name] = bytes(data)

    def size(self, name: str) -> int | None:
        return len(self.objects[name]) if name in self.objects else None

    def read_range(self, name: str, start: int, end: int) -> bytes:
        self.range_reads.append((name, start, end))
        return self.objects[name][start:end]

    def delete(self, name: str) -> bool:
        return self.objects.pop(name, None) is not None

    def list(self, prefix: str, limit: int) -> list[tuple[str, int]]:
        return sorted((k, len(v)) for k, v in self.objects.items() if k.startswith(prefix))[:limit]


def use_test_engines(monkeypatch: Any, app_engine: Any) -> None:
    """Point CLI code (``get_engine`` / ``get_admin_engine``) at the test container's app engine
    and its paired satchel_worker engine."""
    from satchel.store import db

    monkeypatch.setattr(db, "get_engine", lambda: app_engine)
    monkeypatch.setattr(db, "get_admin_engine", lambda: db.admin_engine_for(app_engine))
