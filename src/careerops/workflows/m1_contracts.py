"""Workflow contracts for M1 discovery and crawl workflows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

DISCOVER_COMPANY_SOURCES_ACTIVITY = "careerops.m1.discover_company_sources"
CRAWL_JOB_SOURCE_ACTIVITY = "careerops.m1.crawl_job_source"
INGEST_POSTING_ACTIVITY = "careerops.m1.ingest_posting"
PURGE_RAW_DOCUMENT_ACTIVITY = "careerops.m1.purge_raw_document"


class WorkflowPhase(StrEnum):
    STARTING = "starting"
    DISCOVERING = "discovering"
    CRAWLING = "crawling"
    INGESTING = "ingesting"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CompanyDiscoveryInput:
    company_id: str
    company_name: str
    official_domains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveredSourceRecord:
    source_type: str
    source_identifier: str
    base_url: str


@dataclass(frozen=True, slots=True)
class CompanyDiscoveryResult:
    company_id: str
    discovered_sources: tuple[DiscoveredSourceRecord, ...] = ()
    sources_registered: int = 0


@dataclass(frozen=True, slots=True)
class CrawlJobSourceInput:
    source_id: str
    company_id: str
    company_name: str
    source_type: str
    base_url: str


@dataclass(frozen=True, slots=True)
class CrawledPostingRecord:
    source_id: str
    external_id: str
    canonical_url: str
    source_url: str
    structured_data: dict[str, str]
    parser_version: str
    fetched_at: str = ""


@dataclass(frozen=True, slots=True)
class CrawlJobSourceResult:
    source_id: str
    postings_crawled: int = 0
    postings_ingested: int = 0
    new_postings: int = 0
    new_versions: int = 0
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RawDocumentPurgeInput:
    batch_size: int = 50
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class PurgedDocumentRecord:
    content_object_id: str
    blob_id: str
    evidence_persisted: bool
    dangling_references: int


@dataclass(frozen=True, slots=True)
class RawDocumentPurgeResult:
    documents_purged: int = 0
    evidence_snippets_persisted: int = 0
    dangling_references_found: int = 0
    errors: tuple[str, ...] = ()
    dry_run: bool = False
