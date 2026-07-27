"""Candidate-scoped, review-only Agent run contracts.

Agent runs are the durable boundary between user-triggered work and the shared
structured model runtime.  They store input identities and bounded outcome
metadata, never raw prompts, raw provider responses, credentials, or browser
session material.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class AgentCapability(StrEnum):
    JOB_MATCHING = "job_matching"
    RESUME_REVIEW = "resume_review"
    INTERVIEW_PREPARATION = "interview_preparation"


class AgentRunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    ABSTAINED = "abstained"
    STALE = "stale"
    CANCELLED = "cancelled"
    REVIEWED = "reviewed"


class AgentReviewDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EDITED = "edited"


def _empty_identities() -> dict[str, str]:
    return {}


def _empty_result() -> dict[str, object]:
    return {}


@dataclass(frozen=True, slots=True)
class AgentRun:
    """Immutable-input Agent execution record with a mutable lifecycle."""

    id: UUID
    candidate_id: UUID
    capability: AgentCapability
    state: AgentRunState = AgentRunState.PENDING
    idempotency_key: str = ""
    input_hash: str = ""
    input_identities: Mapping[str, str] = field(default_factory=_empty_identities)
    evidence_ids: tuple[UUID, ...] = ()
    schema_version: str = ""
    prompt_version: str = ""
    model_id: str = ""
    trace_id: str = ""
    result: Mapping[str, object] = field(default_factory=_empty_result)
    error_category: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    review_decision: AgentReviewDecision | None = None
    reviewed_by: str = ""
    review_note: str = ""
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    reviewed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AgentRunReview:
    id: UUID
    run_id: UUID
    candidate_id: UUID
    decision: AgentReviewDecision
    actor_id: str
    note: str = ""
    edited_result: Mapping[str, object] = field(default_factory=_empty_result)
    created_at: datetime | None = None


class AgentRunRepository(Protocol):
    def create(self, run: AgentRun) -> AgentRun: ...

    def get(self, candidate_id: UUID, run_id: UUID) -> AgentRun | None: ...

    def find_by_idempotency(
        self, candidate_id: UUID, capability: AgentCapability, idempotency_key: str
    ) -> AgentRun | None: ...

    def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        capability: AgentCapability | None = None,
        limit: int = 50,
    ) -> list[AgentRun]: ...

    def update(self, run: AgentRun) -> AgentRun: ...

    def claim(self, candidate_id: UUID, run_id: UUID, now: datetime) -> AgentRun | None: ...

    def add_review(self, review: AgentRunReview) -> AgentRunReview: ...

    def record_review(self, review: AgentRunReview, run: AgentRun) -> AgentRun: ...

    def list_reviews(self, candidate_id: UUID, run_id: UUID) -> list[AgentRunReview]: ...
