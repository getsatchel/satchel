"""Pydantic request/response models for the curator REST API (the UI generates TS from these)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ScopeKind = Literal["workspace", "domain", "connection", "asset"]
Redaction = Literal["none", "redacted", "metadata_only"]


class Page(BaseModel):
    total: int
    limit: int
    offset: int


# ---- search / packs (M1) -----------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    types: list[str] | None = None
    k: int = Field(default=10, ge=1, le=100)


class SearchHitOut(BaseModel):
    key: str
    type: str
    seq: int
    tag: str
    score: float
    sources: dict[str, int]
    snippet: str


class PackRequest(BaseModel):
    question: str = Field(min_length=1)
    budget_tokens: int = Field(default=4000, ge=100, le=100_000)


class PackEntryOut(BaseModel):
    key: str
    type: str
    seq: int
    tag: str
    tokens: int
    score: float | None
    text: str


class PackOut(BaseModel):
    release_id: uuid.UUID | None
    budget_tokens: int
    used_tokens: int
    text: str
    entries: list[PackEntryOut]


# ---- library -------------------------------------------------------------------------------


class ItemUsage(BaseModel):
    served_30d: int = 0
    thumbs_up: int = 0
    thumbs_down: int = 0


class ItemSummary(BaseModel):
    key: str
    type: str
    state: str
    managed_by: str
    source_key: str | None = None
    scope_kind: str
    current_seq: int | None = None
    published_seq: int | None = None
    has_changes: bool = False
    updated_at: datetime | None = None
    usage: ItemUsage = Field(default_factory=ItemUsage)


class ItemPage(Page):
    items: list[ItemSummary]


class VersionSummary(BaseModel):
    seq: int
    content_hash: str
    author_user_id: uuid.UUID | None
    provenance: dict[str, Any]
    created_at: datetime
    is_current: bool
    is_published: bool


class VersionOut(VersionSummary):
    key: str
    body: str
    meta: dict[str, Any]


class DocRefOut(BaseModel):
    """One of the asset docs that describe the same table (several sources, or a hand-written
    doc and a source). ``source`` is the source name, or ``git`` / ``ui`` for hand-written."""

    source: str
    key: str
    primary: bool = False


class ItemDetail(ItemSummary):
    primary: bool | None = Field(
        default=None,
        description="asset_doc only: True when this doc is the one retrieval serves for its "
        "table, False when another doc outranks it (see also_described_by). null otherwise.",
    )
    also_described_by: list[DocRefOut] = Field(
        default_factory=list,
        description="asset_doc only: the other docs of the same table, each flagged primary.",
    )
    body: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    content_hash: str | None = None
    versions: list[VersionSummary] = Field(default_factory=list)


class DiffOut(BaseModel):
    key: str
    from_seq: int | None
    to_seq: int | None
    unified: str
    additions: int
    deletions: int
    meta_changed: bool


# ---- workbench -----------------------------------------------------------------------------


class CreateItemRequest(BaseModel):
    key: str = Field(min_length=1, max_length=300, description="Stable path, e.g. guides/x.md")
    type: str
    body: str
    meta: dict[str, Any] = Field(default_factory=dict)
    scope_kind: ScopeKind = "workspace"
    scope_id: uuid.UUID | None = Field(
        default=None, description="Domain / connection / asset id (required unless workspace)"
    )


class UpdateItemRequest(BaseModel):
    body: str
    meta: dict[str, Any] = Field(default_factory=dict)


class UpdateOverlayRequest(BaseModel):
    overlay: dict[str, Any] = Field(
        description="The analyst overlay of a source-managed item (replaces the previous one). "
        "asset_doc: description, notes, caveats, endorsed, columns {name: {notes}}; other "
        "types: notes."
    )


class ChangeOut(BaseModel):
    key: str
    type: str
    kind: Literal["create", "update", "deprecate"]
    managed_by: str
    from_seq: int | None
    to_seq: int | None
    diff: DiffOut


class ChangesOut(BaseModel):
    changes: list[ChangeOut]


class PreviewRequest(BaseModel):
    keys: list[str] | None = Field(
        default=None, description="Draft item keys to include; null = every draft change"
    )
    note: str | None = None


class ReleaseOut(BaseModel):
    id: uuid.UUID
    seq: int
    kind: str
    note: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    parent_release_id: uuid.UUID | None
    item_count: int
    is_current: bool = False
    ref: str | None = Field(
        default=None, description="Pull-request previews: the git ref it was built from")


class PreviewOut(ReleaseOut):
    changed_keys: list[str]
    removed_keys: list[str]


class ReleasePage(Page):
    releases: list[ReleaseOut]


# ---- proposals -----------------------------------------------------------------------------


class ProposalOut(BaseModel):
    key: str
    type: str
    op: str
    target: str | None
    rationale: str | None
    body: str
    meta: dict[str, Any]
    proposed_by: uuid.UUID | None
    proposed_by_label: str | None
    session_id: uuid.UUID | None
    session_question: str | None
    created_at: datetime
    target_managed_by: str | None
    diff: DiffOut | None


class ProposalList(BaseModel):
    proposals: list[ProposalOut]


class AcceptProposalRequest(BaseModel):
    key: str | None = Field(default=None, description="New key for a create_item proposal")
    body: str | None = Field(default=None, description="Edited body (edit and accept)")
    meta: dict[str, Any] | None = None


class RejectProposalRequest(BaseModel):
    reason: str = Field(min_length=1)


class ProposalResult(BaseModel):
    proposal_key: str
    decision: Literal["accepted", "rejected"]
    item_key: str | None
    item_seq: int | None


# ---- observability ---------------------------------------------------------------------------


class DayClientCount(BaseModel):
    day: date
    client: str
    sessions: int


class ToolStat(BaseModel):
    tool: str
    calls: int
    errors: int
    avg_latency_ms: float | None


class SignalStat(BaseModel):
    kind: str
    count: int


class FeedbackCounts(BaseModel):
    positive: int
    negative: int
    # The split: people clicking in the result widget vs the agent relaying what they said.
    human_positive: int = 0
    human_negative: int = 0
    agent_positive: int = 0
    agent_negative: int = 0


class ServedItemStat(BaseModel):
    key: str
    type: str
    served: int
    thumbs_up: int
    thumbs_down: int


class ObservabilitySummary(BaseModel):
    days: int
    total_sessions: int
    sessions_per_day: list[DayClientCount]
    top_tools: list[ToolStat]
    signals_by_kind: list[SignalStat]
    feedback: FeedbackCounts
    most_served_items: list[ServedItemStat]


class SessionRowOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    user_label: str | None
    client: str | None
    status: str
    started_at: datetime
    ended_at: datetime | None
    question: str | None
    sensitive: bool
    tool_call_count: int
    signal_count: int
    thumbs_up: int
    thumbs_down: int


class SessionPage(Page):
    sessions: list[SessionRowOut]


class ServedOut(BaseModel):
    item_key: str
    item_type: str
    seq: int
    rank: int
    score: float | None


class ToolCallOut(BaseModel):
    id: int
    tool: str
    created_at: datetime
    latency_ms: int | None
    has_error: bool
    error: str | None
    args: dict[str, Any] | None
    result_meta: dict[str, Any]
    served: list[ServedOut]


class SignalOut(BaseModel):
    id: int
    kind: str
    source: str
    created_at: datetime
    payload: dict[str, Any] | None
    item_keys: list[str]


class FeedbackOut(BaseModel):
    id: int
    tool_call_id: int | None
    user_id: uuid.UUID
    rating: int
    comment: str | None
    created_at: datetime
    source: Literal["agent", "human"] = "agent"
    client: str | None = None


class SessionDetail(SessionRowOut):
    redaction: Redaction
    release_seq: int | None
    timeline: list[ToolCallOut]
    signals: list[SignalOut]
    feedback: list[FeedbackOut]


class SensitiveRequest(BaseModel):
    sensitive: bool


# ---- catalog ---------------------------------------------------------------------------------


class ConnectionOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    default_for_ai: bool
    has_secret: bool
    secret: Literal["stored", "env"] | None = Field(
        None, description="'stored' (encrypted, set in the UI), 'env' (env:NAME, CLI) or null.")
    config: dict[str, Any] | None = Field(
        None, description="Host, database, user... (admins only; never a secret).")
    asset_count: int


class AssetNode(BaseModel):
    id: uuid.UUID
    connection: str | None
    kind: str
    fqn: str
    name: str
    data_type: str | None
    dropped: bool
    has_doc: bool
    doc_item_key: str | None
    primary: DocRefOut | None = Field(
        default=None, description="The doc retrieval serves for this asset (doc_item_key)."
    )
    also_described_by: list[DocRefOut] = Field(
        default_factory=list, description="Other docs of this asset, kept as provenance."
    )
    endorsed: bool
    excluded_from_ai: bool
    children: list[AssetNode] = Field(default_factory=list)


class AssetPage(Page):
    assets: list[AssetNode]


class AssetLinkedItem(BaseModel):
    key: str
    type: str
    state: str
    relation: str


class AssetDetail(AssetNode):
    stats: dict[str, Any]
    first_seen_at: datetime
    last_seen_at: datetime
    linked_items: list[AssetLinkedItem]


# ---- me --------------------------------------------------------------------------------------


class MeOut(BaseModel):
    user_id: uuid.UUID | None
    email: str | None
    name: str | None
    role: str
    workspace_id: uuid.UUID
    workspace_slug: str
    workspace_name: str
    csrf_token: str | None
    auth: str


# ---- suggestions (M4) ------------------------------------------------------------------------

SuggestionStatus = Literal["open", "completed", "dismissed"]


class SuggestionSummary(BaseModel):
    id: uuid.UUID
    title: str
    summary: str
    status: SuggestionStatus
    source: Literal["signals", "hygiene", "golden"]
    evidence_count: int
    distinct_users: int
    distinct_sessions: int
    change_count: int
    pending_changes: int
    drafted: bool
    draft_note: str | None
    created_at: datetime
    updated_at: datetime


class SuggestionList(BaseModel):
    total: int
    limit: int
    offset: int
    suggestions: list[SuggestionSummary]


class SuggestionChangeOut(BaseModel):
    id: uuid.UUID
    ord: int
    op: str
    item_type: str | None
    target_key: str | None  # existing item being changed
    target_managed_by: str | None
    target_asset: str | None
    proposed_key: str | None  # key of the item that accepting creates
    proposed_body: str
    proposed_meta: dict[str, Any]
    current_body: str | None  # the target's current body, for the diff
    current_meta: dict[str, Any] | None
    rationale: str
    status: Literal["pending", "accepted", "rejected", "applied"]
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    result: dict[str, Any]
    diff: DiffOut


class EvidenceSignal(BaseModel):
    id: int
    kind: str
    source: str
    message: str | None  # None when the session is sensitive and you are not an admin
    created_at: datetime


class EvidenceSession(BaseModel):
    session_id: uuid.UUID | None  # None = signals with no session (hygiene findings)
    user_label: str | None
    client: str | None
    question: str | None
    sensitive: bool
    redaction: Redaction
    started_at: datetime | None
    signals: list[EvidenceSignal]


class SuggestionDetail(SuggestionSummary):
    dismiss_reason: str | None
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    draft_model: str | None
    signal_kinds: list[SignalStat]
    evidence: list[EvidenceSession]
    evidence_truncated: bool
    changes: list[SuggestionChangeOut]


class AcceptChangeRequest(BaseModel):
    """Optional edits made before accepting ("edit and accept")."""

    body: str | None = None
    meta: dict[str, Any] | None = None
    key: str | None = None  # for a new item


class RejectChangeRequest(BaseModel):
    reason: str | None = None


class DismissRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class ChangeDecision(BaseModel):
    change: SuggestionChangeOut
    suggestion: SuggestionSummary
    item_key: str | None
    item_seq: int | None
    edit_in_git: bool
    message: str | None


class CurateRunRequest(BaseModel):
    use_llm: bool = True


class CurateRunOut(BaseModel):
    workspace: str
    llm: str
    embedded: int
    hygiene: dict[str, int]
    hygiene_notes: list[str]
    golden_candidates: int
    signals_considered: int
    clusters_found: int
    below_threshold: int
    suggestions_created: int
    suggestions_extended: int
    drafted: int
    changes_proposed: int
    invalid_dropped: int
    draft_failed: int
    undrafted_left: int
    errors: list[str]
