"""Domain models for companies and job sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class TermsStatus(StrEnum):
    UNKNOWN = "unknown"
    ALLOWED = "allowed"
    BLOCKED = "blocked"


class JobSourceState(StrEnum):
    PENDING_REVIEW = "pending_review"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"


class SourceType(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    JSON_LD = "json_ld"
    SITEMAP = "sitemap"
    STATIC_HTML = "static_html"


@dataclass(frozen=True, slots=True)
class Company:
    id: UUID
    name: str
    normalized_name: str
    official_domains: tuple[str, ...] = ()
    terms_status: TermsStatus = TermsStatus.UNKNOWN
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobSource:
    id: UUID
    company_id: UUID
    source_type: SourceType
    source_identifier: str
    base_url: str
    state: JobSourceState = JobSourceState.PENDING_REVIEW
    verified_at: datetime | None = None
    last_discovery_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CompanyDiscoveryResult:
    company_id: UUID
    discovered_sources: tuple[DiscoveredSource, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveredSource:
    source_type: SourceType
    source_identifier: str
    base_url: str
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class CompanyAggregate:
    company: Company
    sources: tuple[JobSource, ...] = field(default_factory=tuple)
