"""Cube's ``/cubejs-api/v1/meta`` response: descriptions for members, and the cubes that exist
only in JavaScript models.

The response is read from a saved file (``meta``) or fetched from a running Cube (``meta_url``,
the only network access of this adapter). It enriches the YAML model, it never replaces it: Cube
does not expose a cube's SQL, so a cube known only from ``/meta`` gets no table link and no
queryable metrics, just glossary terms for its documented members.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from satchel_adapter_sdk import SourceTerm

from satchel.sources.adapters.cube.model import Model

MAX_META_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 20.0

Fetch = Callable[[str, str | None], bytes]


def meta_endpoint(url: str) -> str:
    """``http://host:4000`` -> ``http://host:4000/cubejs-api/v1/meta``."""
    base = url.strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"meta_url must be an http(s) URL, got {url!r}")
    if base.endswith("/meta"):
        return base
    if base.endswith("/cubejs-api/v1"):
        return f"{base}/meta"
    return f"{base}/cubejs-api/v1/meta"


def http_fetch(url: str, token: str | None) -> bytes:
    """GET the meta endpoint (the token is sent as Cube expects, in ``Authorization``)."""
    req = urllib.request.Request(meta_endpoint(url), headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310
            data: bytes = resp.read(MAX_META_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise ValueError(f"Cube /meta at {url} answered HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ValueError(f"cannot reach Cube /meta at {url}: {getattr(e, 'reason', e)}") from None
    if len(data) > MAX_META_BYTES:
        raise ValueError("the Cube /meta response is larger than 16 MB")
    return data


def resolve_token(token: str | None, token_env: str | None) -> str | None:
    if token:
        return token
    if token_env:
        value = os.environ.get(token_env)
        if not value:
            raise ValueError(f"meta_token_env names {token_env}, which is not set")
        return value
    return None


def apply_meta(model: Model, raw: bytes) -> list[SourceTerm]:
    """Fill missing descriptions of the YAML cubes from ``/meta``; return glossary terms for
    the documented members of cubes that only ``/meta`` knows (JavaScript models)."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        model.warn("unreadable_input", f"meta is not valid JSON ({e}); ignored")
        return []
    cubes = doc.get("cubes") if isinstance(doc, dict) else None
    if not isinstance(cubes, list):
        model.warn("unreadable_input", "meta has no cubes list; ignored")
        return []
    terms: list[SourceTerm] = []
    for mc in sorted((c for c in cubes if isinstance(c, dict) and c.get("name")),
                     key=lambda c: str(c["name"])):
        name = str(mc["name"])
        members = _members(mc)
        cube = model.cubes.get(name)
        if cube is None:
            if name in model.views:
                continue  # views repeat the members of their cubes
            model.warn("cube_meta_only",
                       f"cube {name} is only known from /meta (a JavaScript model): there is no "
                       "table link and its metrics are not queryable; its documented members "
                       "were imported as glossary terms", name)
            if _text(mc.get("description")):
                terms.append(SourceTerm(
                    key=f"cube.{name}", term=name, definition=str(mc["description"]).strip()))
            for _kind, member, desc in members:
                if desc:
                    terms.append(SourceTerm(
                        key=f"cube.{member}", term=member, definition=desc,
                    ))
            continue
        if not cube.description and _text(mc.get("description")):
            cube.description = str(mc["description"]).strip()
        by_dim = {d.name: d for d in cube.dimensions}
        by_measure = {m.name: m for m in cube.measures}
        for kind, member, desc in members:
            short = member.split(".", 1)[-1]
            target = (by_measure if kind == "measure" else by_dim).get(short)
            if target is not None and not target.description and desc:
                target.description = desc
        by_seg = {s.name: s for s in cube.segments}
        for s in mc.get("segments") or []:
            if isinstance(s, dict) and s.get("name"):
                short = str(s["name"]).split(".", 1)[-1]
                seg = by_seg.get(short)
                if seg is not None and not seg.description and _text(s.get("description")):
                    seg.description = str(s["description"]).strip()
    return terms


def _text(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _members(mc: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    out: list[tuple[str, str, str | None]] = []
    for kind, key in (("measure", "measures"), ("dimension", "dimensions")):
        for m in mc.get(key) or []:
            if isinstance(m, dict) and m.get("name"):
                out.append((kind, str(m["name"]), _text(m.get("description"))))
    return out
