"""Per-workspace curation settings (``workspace.settings.curation``), design §7.1.

Every field has a default, so a workspace with no ``curation`` key behaves like Hex's defaults
(at least 2 users, 2 sessions and 3 signals). Invalid values fall back to the default rather
than breaking the nightly job.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("satchel.curation")


class CurationConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Gap detection thresholds (behavioural clusters only; hygiene findings bypass them).
    min_users: int = Field(default=2, ge=1)
    min_sessions: int = Field(default=2, ge=1)
    min_signals: int = Field(default=3, ge=1)
    window_days: int = Field(default=30, ge=1, le=365)
    # Cosine similarity a signal needs to join a cluster, and for two clusters to merge when
    # they also share a primary asset.
    similarity: float = Field(default=0.72, ge=0.0, le=1.0)
    merge_similarity: float = Field(default=0.8, ge=0.0, le=1.0)
    # Hygiene.
    dead_item_days: int = Field(default=60, ge=1)
    duplicate_similarity: float = Field(default=0.95, ge=0.0, le=1.0)
    contradiction_min_similarity: float = Field(default=0.8, ge=0.0, le=1.0)
    contradiction_pairs: int = Field(default=10, ge=0)
    memory_min_users: int = Field(default=3, ge=2)
    memory_similarity: float = Field(default=0.9, ge=0.0, le=1.0)
    # Drafting.
    max_drafts: int = Field(default=10, ge=0)
    instructions: str = ""


def load_config(workspace_settings: dict[str, Any] | None) -> CurationConfig:
    raw = (workspace_settings or {}).get("curation")
    if not isinstance(raw, dict):
        return CurationConfig()
    try:
        return CurationConfig.model_validate(raw)
    except ValidationError as e:
        log.warning("invalid workspace.settings.curation, using defaults: %s", e)
        return CurationConfig()
