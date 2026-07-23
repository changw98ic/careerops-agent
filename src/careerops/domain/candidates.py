"""Domain models for M2: candidates, evidence, matching, remote eligibility, compensation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID


class EvidenceKind(StrEnum):
    SKILL = "skill"
    PROJECT = "project"
    EXPERIENCE = "experience"
    CERTIFICATION = "certification"
    EDUCATION = "education"


class MatchLevel(StrEnum):
    STRONG = "strong"
    PARTIAL = "partial"
    TRANSFERABLE = "transferable"
    UNSUPPORTED = "unsupported"
    HARD_FAIL = "hard_fail"


class RemoteEligibilityVerdict(StrEnum):
    ELIGIBLE = "eligible"
    NOT_ELIGIBLE = "not_eligible"
    REVIEW_REQUIRED = "review_required"
    UNKNOWN = "unknown"


class MatchTier(StrEnum):
    APPLY_NOW = "apply_now"
    STRONG_CANDIDATE = "strong_candidate"
    WORTH_EXPLORING = "worth_exploring"
    STRETCH = "stretch"
    NOT_RECOMMENDED = "not_recommended"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class Candidate:
    """A job-seeking candidate with evidence-backed skills."""

    id: UUID
    display_name: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """A single piece of candidate evidence (skill, project, etc.).

    Idempotency key: (repository, commit_sha, path, symbol, content_hash).
    """

    id: UUID
    candidate_id: UUID
    kind: EvidenceKind
    name: str
    description: str = ""
    repository: str = ""
    commit_sha: str = ""
    path: str = ""
    symbol: str = ""
    content_hash: str = ""
    source_url: str = ""
    verified: bool = False
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobRequirement:
    """A requirement extracted from a job posting."""

    name: str
    is_hard_requirement: bool = False
    category: str = "skill"
    source_text: str = ""


@dataclass(frozen=True, slots=True)
class RequirementMatch:
    """Match result for a single requirement against candidate evidence."""

    requirement_name: str
    level: MatchLevel
    evidence_ids: tuple[UUID, ...] = ()
    confidence: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Aggregate match result between a candidate and a canonical job."""

    id: UUID
    candidate_id: UUID
    canonical_job_id: UUID
    tier: MatchTier
    overall_score: float
    requirement_matches: tuple[RequirementMatch, ...] = ()
    geographic_blocked: bool = False
    remote_verdict: RemoteEligibilityVerdict = RemoteEligibilityVerdict.UNKNOWN
    rules_version: str = ""
    input_hash: str = ""
    output_hash: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RemoteEligibility:
    """Assessment of whether a job is eligible for remote work from China/Chengdu."""

    canonical_job_id: UUID
    verdict: RemoteEligibilityVerdict
    confidence: float = 0.0
    evidence_spans: tuple[str, ...] = ()
    structured_fields: dict[str, str] = field(default_factory=lambda: {})
    reason: str = ""
    rules_version: str = ""


@dataclass(frozen=True, slots=True)
class CompensationRecord:
    """Normalized compensation data for a job posting."""

    id: UUID
    canonical_job_id: UUID
    currency: str = ""
    amount_min: float | None = None
    amount_max: float | None = None
    period: str = ""
    fx_rate: float | None = None
    fx_effective_date: date | None = None
    normalized_amount_min: float | None = None
    normalized_amount_max: float | None = None
    normalized_currency: str = "CNY"
    score: str = "unknown"
    source_text: str = ""
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FxSnapshot:
    """A user-provided static FX rate with an effective date."""

    from_currency: str
    to_currency: str
    rate: float
    effective_date: date
