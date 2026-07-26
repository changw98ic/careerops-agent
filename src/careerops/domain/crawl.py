"""Domain models for crawl policy and raw document lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class CrawlDecision(StrEnum):
    ALLOW = "allow"
    DENY_BLOCKED = "deny_blocked"
    DENY_RATE_LIMITED = "deny_rate_limited"
    DENY_TERMS_UNKNOWN = "deny_terms_unknown"
    DENY_SSRF = "deny_ssrf"


class PurgeState(StrEnum):
    PENDING = "pending"
    EVIDENCE_PERSISTED = "evidence_persisted"
    PURGED = "purged"
    FAILED = "failed"


class CrawlPlanState(StrEnum):
    """Lifecycle state of the user-managed crawl plan resource.

    Plan *versions* are immutable (copy-on-write per design Decision 2): editing
    an active plan creates a new version and preserves the old one for
    provenance. This state therefore describes the current plan head, not any
    individual historical version. A ``PAUSED`` plan keeps its prior postings,
    evidence, and run history and simply schedules no new runs; ``ARCHIVED``
    covers plans retired or superseded, retained for provenance but no longer
    schedulable.
    """

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


# Phase 0 deliberately ships NO ``ALLOWED_TRANSITIONS`` table for
# ``CrawlPlanState``. Plan *versions* are immutable copy-on-write (design
# Decision 2), so head-state moves (draft -> active, active <-> paused,
# -> archived) are enforced by the plan service in a later gate, not by a
# static table here. Adding one prematurely would risk codifying transitions
# the service layer has not validated against the crawl-plan-management spec.


class CrawlRunState(StrEnum):
    """Lifecycle state of a single crawl run execution.

    Every run binds to one immutable plan-version snapshot, source set, run
    identifier, and reports a terminal status (crawl-plan-management spec).
    ``TIMEOUT`` is terminal and distinct from ``FAILED`` so callers can tell
    exceeded time/limit budgets apart from source/parser failures; both disable
    blind retry per the safe-fallback policy. ``CANCELLED`` covers user
    cancellation and the coalesce/skip outcome when a scheduled trigger overlaps
    an already-running source/plan.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


# Legal crawl-run state transitions: from_state -> allowed to_states.
# Terminal states (SUCCEEDED/FAILED/CANCELLED/TIMEOUT) admit no further moves, so
# a finished run is never silently resurrected. Rejecting illegal transitions is
# the contract; enforcement (state-machine service) arrives in a later gate.
ALLOWED_TRANSITIONS: dict[CrawlRunState, frozenset[CrawlRunState]] = {
    CrawlRunState.PENDING: frozenset({CrawlRunState.RUNNING, CrawlRunState.CANCELLED}),
    CrawlRunState.RUNNING: frozenset(
        {
            CrawlRunState.SUCCEEDED,
            CrawlRunState.FAILED,
            CrawlRunState.CANCELLED,
            CrawlRunState.TIMEOUT,
        }
    ),
    CrawlRunState.SUCCEEDED: frozenset(),
    CrawlRunState.FAILED: frozenset(),
    CrawlRunState.CANCELLED: frozenset(),
    CrawlRunState.TIMEOUT: frozenset(),
}


@dataclass(frozen=True, slots=True)
class CrawlPolicyInput:
    source_url: str
    terms_status: str
    domain: str
    is_rate_limited: bool = False


@dataclass(frozen=True, slots=True)
class CrawlPolicyDecision:
    decision: CrawlDecision
    reason: str


@dataclass(frozen=True, slots=True)
class EvidenceSnippet:
    """Long-term evidence preserved before raw content purge."""

    source_url: str
    fetched_at: datetime
    content_hash: str
    decision_snippet: str
    snippet_hash: str


@dataclass(frozen=True, slots=True)
class RawDocumentPurgeCandidate:
    content_object_id: UUID
    blob_id: UUID
    object_key: str
    sha256: str
    source_url: str
    fetched_at: datetime
    retention_until: datetime


@dataclass(frozen=True, slots=True)
class PurgeResult:
    content_object_id: UUID
    blob_id: UUID
    state: PurgeState
    evidence_persisted: bool
    dangling_references: int = 0
