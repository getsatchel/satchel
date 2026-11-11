"""Chunking for indexing (design §4/§5).

Markdown is split on headings and packed into chunks of roughly MIN..MAX tokens (tokens
approximated from characters). YAML-bodied items are rendered to readable text and indexed
as a single chunk.
"""

from __future__ import annotations

import re

from satchel.core.tokens import approx_tokens

MAX_TOKENS = 700
MIN_TOKENS = 250

_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")


def _sections(markdown: str) -> list[tuple[str, str]]:
    """Split into (heading path, text) sections, ignoring headings inside code fences."""
    sections: list[tuple[str, str]] = []
    path: list[tuple[int, str]] = []
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        text = "\n".join(buf).strip()
        if text:
            sections.append((" > ".join(h for _, h in path), text))
        buf.clear()

    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, m.group(2).strip()))
            buf.append(line)
        else:
            buf.append(line)
    flush()
    return sections


def _split_long(text: str) -> list[str]:
    """Split an oversized section on blank lines."""
    parts: list[str] = []
    cur: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        if cur and approx_tokens("\n\n".join([*cur, para])) > MAX_TOKENS:
            parts.append("\n\n".join(cur))
            cur = []
        cur.append(para)
    if cur:
        parts.append("\n\n".join(cur))
    return parts


def chunk_markdown(markdown: str, title: str | None = None) -> list[str]:
    """Return chunk texts; each is prefixed with ``title`` (when given) for context."""
    pieces: list[str] = []
    for _, text in _sections(markdown):
        pieces.extend(_split_long(text) if approx_tokens(text) > MAX_TOKENS else [text])
    chunks: list[str] = []
    cur = ""
    for p in pieces:
        full = approx_tokens(cur) >= MIN_TOKENS or approx_tokens(f"{cur}\n\n{p}") > MAX_TOKENS
        if cur and full:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    if title:
        chunks = [f"{title}\n\n{c}" for c in chunks]
    return chunks
