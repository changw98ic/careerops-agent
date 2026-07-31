"""Domain models for canonical jobs, postings, versions, and merge decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class AggregateState(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    ARCHIVED = "archived"


class PostingSourceState(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class MergeDecisionKind(StrEnum):
    MERGE = "merge"
    SPLIT = "split"
    ROLLBACK = "rollback"


class ActorType(StrEnum):
    RULE = "rule"
    HUMAN = "human"
    MIGRATION = "migration"


class AliasType(StrEnum):
    TITLE = "title"
    LOCATION = "location"
    URL = "url"


@dataclass(frozen=True, slots=True)
class CanonicalJob:
    id: UUID
    company_id: UUID
    canonical_title: str
    normalized_title: str
    aggregate_state: AggregateState = AggregateState.ACTIVE
    primary_posting_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobPosting:
    id: UUID
    source_id: UUID
    external_id: str
    canonical_url: str
    source_state: PostingSourceState = PostingSourceState.ACTIVE
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    closed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobPostingVersion:
    id: UUID
    job_posting_id: UUID
    raw_snapshot_id: UUID | None = None
    content_hash: str = ""
    source_url: str = ""
    parser_version: str = ""
    structured_data: dict[str, Any] = field(default_factory=lambda: {})
    changed_fields: tuple[str, ...] = ()
    captured_at: datetime | None = None
    crawl_run_id: UUID | None = None
    plan_version_id: UUID | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobMergeDecision:
    id: UUID
    job_posting_id: UUID
    from_canonical_job_id: UUID | None = None
    to_canonical_job_id: UUID | None = None
    decision_kind: MergeDecisionKind = MergeDecisionKind.MERGE
    rule: str = ""
    reason: str = ""
    score: float | None = None
    actor_type: ActorType = ActorType.RULE
    actor_id: str | None = None
    algorithm_version: str | None = None
    supersedes_decision_id: UUID | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobAlias:
    id: UUID
    canonical_job_id: UUID
    alias_type: AliasType
    normalized_value: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NormalizedJobData:
    """Structured data extracted from a raw job posting."""

    title: str
    location: str = ""
    remote_eligible: bool | None = None
    description_text: str = ""
    apply_url: str = ""
    salary_currency: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_period: str | None = None
    skills: tuple[str, ...] = ()
    employment_type: str | None = None


@dataclass(frozen=True, slots=True)
class DedupCandidate:
    """A candidate match between two postings for dedup evaluation."""

    posting_id: UUID
    canonical_job_id: UUID
    score: float
    rule: str
    reason: str
