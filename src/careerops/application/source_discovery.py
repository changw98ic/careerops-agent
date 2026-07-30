"""Source discovery normalizer (real-autonomous-career-loop Phase 5.4).

Normalizes ATS / JsonLd / Sitemap crawl results into the canonical
``job_sources`` registry.  Each discovered source is identified by
``(company_id, source_type, source_identifier)`` — the business unique key
enforced by ``uq_job_sources_company_type_identifier``.

Design:
- The normalizer receives raw ``DiscoveredSourceRecord`` from the existing
  crawl adapters (ATS / JsonLd / Sitemap) and maps them to a ``CrawlSource``.
- Deduplication uses the existing ``CrawlSourceRepository.upsert_by_identity``
  (which performs ``ON CONFLICT DO UPDATE`` on the business key) so two
  discovery events for the same source collapse to ONE row.
- New sources start ``PENDING_REVIEW`` / disabled — consistent with the crawl
  readiness gate (Phase 8) that keeps new sources paused until reviewed.
- ``source_identifier`` is the normalized URL hostname + path (no query
  string, no trailing slash) so that ``https://acme.com/careers?lang=en``
  and ``https://acme.com/careers?lang=zh`` collapse to the same identity.
- ``executor_mode`` defaults to ``HTTP`` for structured sources
  (Greenhouse/Lever/Ashby/JsonLd/Sitemap) and ``EGO`` for sources that
  need browser rendering.  The caller can override.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from careerops.domain.crawl_plans import (
    CrawlExecutorMode,
    CrawlPolicyStatus,
    CrawlSource,
    CrawlSourceRepository,
    CrawlSourceState,
    CrawlSourceType,
)

__all__ = [
    "DiscoveredSource",
    "normalize_source_identifier",
    "discover_and_register",
]


# Source types that are structured (Tier 1 — no browser needed).
_STRUCTURED_SOURCE_TYPES: frozenset[CrawlSourceType] = frozenset(
    {
        CrawlSourceType.GREENHOUSE,
        CrawlSourceType.LEVER,
        CrawlSourceType.ASHBY,
    }
)


@dataclass(frozen=True, slots=True)
class DiscoveredSource:
    """A raw discovered source from an ATS / JsonLd / Sitemap adapter.

    Carries enough information to register into the canonical ``job_sources``
    registry.  ``source_type`` is a string (the adapter key) that maps to
    :class:`CrawlSourceType`; unknown types are rejected.
    """

    base_url: str
    source_type: str
    source_identifier: str = ""
    executor_mode: str = ""


def normalize_source_identifier(url: str) -> str:
    """Derive a stable, dedup-friendly source identifier from a URL.

    Strips query/fragment, normalizes trailing slashes, lowercases the
    hostname.  Two URLs that differ only in query parameters collapse to
    the same identifier.
    """
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    path = re.sub(r"/+$", "", parts.path)
    return f"{hostname}{path}"


def _infer_executor_mode(source_type: CrawlSourceType) -> CrawlExecutorMode:
    """Infer the executor mode from the source type."""
    if source_type in _STRUCTURED_SOURCE_TYPES:
        return CrawlExecutorMode.HTTP
    # OFFICIAL (meta-type) and unknown types default to EGO (browser rendering).
    return CrawlExecutorMode.EGO


def discover_and_register(
    repository: CrawlSourceRepository,
    discovered: DiscoveredSource,
    *,
    owner_id: UUID,
    company_id: UUID,
    now: datetime | None = None,
) -> CrawlSource:
    """Normalize and upsert a discovered source into the registry.

    Dedup by ``(company_id, source_type, source_identifier)`` — the
    ``uq_job_sources_company_type_identifier`` unique key.  On conflict the
    EXISTING row's id wins; mutable discovery fields (base_url,
    executor_mode, adapter_version, last_discovery_at) are refreshed.

    New sources start ``PENDING_REVIEW`` / disabled.
    """
    ts = now or datetime.now(tz=UTC)

    # Map source_type string to enum; reject unknown types.
    try:
        st = CrawlSourceType(discovered.source_type)
    except ValueError:
        raise ValueError(f"unsupported source type: {discovered.source_type}")

    # Resolve source_identifier: use the caller-supplied one or derive from URL.
    identifier = discovered.source_identifier or normalize_source_identifier(
        discovered.base_url
    )

    # Resolve executor mode: caller-supplied or inferred from source type.
    if discovered.executor_mode:
        em = CrawlExecutorMode(discovered.executor_mode)
    else:
        em = _infer_executor_mode(st)

    source = CrawlSource(
        id=uuid4(),
        owner_id=owner_id,
        company_id=company_id,
        source_type=st,
        source_identifier=identifier,
        base_url=discovered.base_url,
        executor_mode=em,
        state=CrawlSourceState.PENDING_REVIEW,
        trust_status=CrawlPolicyStatus.UNKNOWN,
        terms_status=CrawlPolicyStatus.UNKNOWN,
        robots_status=CrawlPolicyStatus.UNKNOWN,
        adapter_version="",
        enabled=False,
        last_discovery_at=ts,
    )

    return repository.upsert_by_identity(source)
