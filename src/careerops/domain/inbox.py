"""Domain models for the job inbox projection (Section 6).

End-to-end-career-application-loop, Section 6 (job-discovery-inbox spec).

The inbox projection connects the job projection (canonical_jobs + job_postings
+ job_posting_versions with provenance) to the active profile version and
crawl-plan provenance. Each inbox item carries: canonical job, latest version,
provenance (crawl_run_id, plan_version_id, source), active profile version
used for filtering, and the filter decision.

Iron rules honored:

- **Hard-gate precedence** (Iron Rule 1): deterministic hard filters run FIRST;
  LLM matching runs ONLY after hard filters pass.
- **Model review-only** (Iron Rule 2): LLM output is review_only suggestions
  with evidence references. Model NEVER changes policy facts or application state.
- **Idempotent** (Iron Rule 3): favorite + ignore actions are idempotent.
- **Reversible merge** (Iron Rule 4): semantic matches are reviewable proposals only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

__all__ = [
    "BlockingReason",
    "FilterDecision",
    "FilterVerdict",
    "InboxItem",
    "InboxProvenance",
    "RequirementMatchResult",
    "SemanticRankingStatus",
]


class FilterVerdict(StrEnum):
    """Outcome of the deterministic hard-filter pass.

    ``RECOMMENDED`` means the job passed all hard filters and is eligible for
    the recommended tab. ``EXCLUDED`` means one or more hard filters rejected
    the job; ``blocking_reasons`` carries the human-readable reasons.
    """

    RECOMMENDED = "recommended"
    EXCLUDED = "excluded"


class BlockingReason(StrEnum):
    """Human-readable reasons a job was excluded by hard filters.

    Each reason maps to one deterministic filter rule. A job may have multiple
    blocking reasons (e.g. both location and remote fail).
    """

    ROLE_MISMATCH = "role_mismatch"
    LOCATION_EXCLUDED = "location_excluded"
    LOCATION_NOT_REQUIRED = "location_not_required"
    REMOTE_NOT_ELIGIBLE = "remote_not_eligible"
    REMOTE_UNKNOWN = "remote_unknown"
    AUTHORIZATION_MISMATCH = "authorization_mismatch"
    COMPENSATION_BELOW_MIN = "compensation_below_min"
    COMPENSATION_ABOVE_MAX = "compensation_above_max"
    SOURCE_INACTIVE = "source_inactive"
    SOURCE_BLOCKED = "source_blocked"
    EXCLUDED_COMPANY = "excluded_company"
    EXCLUDED_TITLE = "excluded_title"
    EXCLUDED_KEYWORD = "excluded_keyword"
    JOB_CLOSED = "job_closed"
    JOB_ARCHIVED = "job_archived"
    NO_ACTIVE_VERSION = "no_active_version"


class SemanticRankingStatus(StrEnum):
    """Status of the optional LLM semantic ranking step.

    ``AVAILABLE`` means the model produced a review-only ranking.
    ``UNAVAILABLE`` means the model was disabled, unavailable, or returned
    invalid output; deterministic filtering only.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class InboxProvenance:
    """Provenance for why a job appeared in the inbox.

    Links the job posting version back to the crawl run and plan version
    that produced it (Section 5 provenance). ``source_type`` and
    ``source_identifier`` identify the crawl source.
    """

    crawl_run_id: UUID | None = None
    plan_version_id: UUID | None = None
    source_type: str = ""
    source_identifier: str = ""
    captured_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FilterDecision:
    """The deterministic hard-filter outcome for one job against one profile.

    ``profile_version_id`` records which profile version was used; ``rules_version``
    records the filter ruleset version. ``blocking_reasons`` is empty when the
    job passes all filters. ``evidence_refs`` maps each blocking reason to the
    specific job field value that triggered it (e.g. ``{"location_excluded":
    "San Francisco, CA"}``). Semantic fields are optional review metadata and
    never participate in the hard-filter verdict.
    """

    verdict: FilterVerdict
    profile_version_id: UUID
    rules_version: str = ""
    blocking_reasons: tuple[BlockingReason, ...] = ()
    evidence_refs: dict[str, str] = field(default_factory=lambda: {})
    # Optional review-only semantic ranking. This is never used as a hard
    # filter or an application-state transition.
    semantic_ranking_status: SemanticRankingStatus = SemanticRankingStatus.UNAVAILABLE
    semantic_ranking_score: float | None = None
    semantic_ranking_reason: str = ""
    semantic_model_version: str = ""


@dataclass(frozen=True, slots=True)
class RequirementMatchResult:
    """Match result for a single job requirement against candidate evidence (6.5).

    Review-only: never authorizes application state changes. ``evidence_ids``
    reference CONFIRMED candidate evidence items that support this match.
    ``rules_version`` and ``model_version`` record provenance.
    """

    requirement_name: str
    match_level: str = "unsupported"  # strong / partial / transferable / unsupported
    evidence_ids: tuple[UUID, ...] = ()
    confidence: float = 0.0
    reason: str = ""
    rules_version: str = ""
    model_version: str = ""


@dataclass(frozen=True, slots=True)
class InboxItem:
    """One item in the job inbox projection.

    Connects the canonical job + latest posting version + provenance to the
    active profile version and the filter decision. For recommended jobs,
    optional requirement match results and semantic ranking are included.
    """

    canonical_job_id: UUID
    canonical_title: str
    company_id: UUID
    company_name: str = ""
    aggregate_state: str = "active"
    # Latest posting version data
    posting_id: UUID | None = None
    version_id: UUID | None = None
    structured_data: dict[str, Any] = field(default_factory=lambda: {})
    apply_url: str = ""
    # Provenance
    provenance: InboxProvenance = field(default_factory=InboxProvenance)
    # Filter decision
    filter_decision: FilterDecision = field(
        default_factory=lambda: FilterDecision(
            verdict=FilterVerdict.EXCLUDED,
            profile_version_id=UUID(int=0),
        )
    )
    # Requirement-level match results (only for recommended jobs)
    requirement_matches: tuple[RequirementMatchResult, ...] = ()
    # Optional LLM semantic ranking
    semantic_ranking_status: SemanticRankingStatus = SemanticRankingStatus.UNAVAILABLE
    semantic_ranking_score: float | None = None
    semantic_ranking_reason: str = ""
    # Application state (if user already acted on this job)
    application_state: str | None = None
    application_id: UUID | None = None
    # Timestamps
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
