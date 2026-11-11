"""Cheap token estimation (no tokenizer dependency)."""

from __future__ import annotations


def approx_tokens(text: str) -> int:
    """Roughly 4 characters per token; good enough for budgeting and chunk sizing."""
    return max(1, (len(text) + 3) // 4)
