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
