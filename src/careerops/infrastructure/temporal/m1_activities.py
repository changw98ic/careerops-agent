"""M1 Temporal activities: all I/O for discovery, crawl, ingest, and purge."""

from __future__ import annotations

from typing import Protocol

from temporalio import activity

from careerops.workflows.m1_contracts import (
    CRAWL_JOB_SOURCE_ACTIVITY,
    DISCOVER_COMPANY_SOURCES_ACTIVITY,
    INGEST_POSTING_ACTIVITY,
    PURGE_RAW_DOCUMENT_ACTIVITY,
    CompanyDiscoveryInput,
    CompanyDiscoveryResult,
    CrawledPostingRecord,
    CrawlJobSourceInput,
    RawDocumentPurgeInput,
    RawDocumentPurgeResult,
)


class DiscoveryActivitySink(Protocol):
    """Activity-side adapter for company discovery I/O."""

    async def discover_sources(self, request: CompanyDiscoveryInput) -> CompanyDiscoveryResult: ...


class CrawlActivitySink(Protocol):
    """Activity-side adapter for crawl I/O."""

    async def crawl_source(self, request: CrawlJobSourceInput) -> list[CrawledPostingRecord]: ...

    async def ingest_posting(
        self, record: CrawledPostingRecord
    ) -> dict[str, bool | str]: ...


class PurgeActivitySink(Protocol):
    """Activity-side adapter for raw document purge I/O."""

    async def purge_documents(self, request: RawDocumentPurgeInput) -> RawDocumentPurgeResult: ...


class NoOpDiscoverySink:
    """M1 discovery sink that returns empty results without external I/O."""

    async def discover_sources(self, request: CompanyDiscoveryInput) -> CompanyDiscoveryResult:
        return CompanyDiscoveryResult(company_id=request.company_id)


class NoOpCrawlSink:
    """M1 crawl sink that returns empty results without external I/O."""

    async def crawl_source(self, request: CrawlJobSourceInput) -> list[CrawledPostingRecord]:
        return []

    async def ingest_posting(
        self, record: CrawledPostingRecord
    ) -> dict[str, bool | str]:
        return {"is_new_posting": True, "is_new_version": True}


class NoOpPurgeSink:
    """M1 purge sink that returns empty results without external I/O."""

    async def purge_documents(self, request: RawDocumentPurgeInput) -> RawDocumentPurgeResult:
        return RawDocumentPurgeResult()


class M1DiscoveryActivities:
    def __init__(self, sink: DiscoveryActivitySink | None = None) -> None:
        self._sink = sink or NoOpDiscoverySink()

    @activity.defn(name=DISCOVER_COMPANY_SOURCES_ACTIVITY)
    async def discover_company_sources(
        self, request: CompanyDiscoveryInput
    ) -> CompanyDiscoveryResult:
        activity.logger.info("discovering sources for company %s", request.company_id)
        return await self._sink.discover_sources(request)


class M1CrawlActivities:
    def __init__(self, sink: CrawlActivitySink | None = None) -> None:
        self._sink = sink or NoOpCrawlSink()

    @activity.defn(name=CRAWL_JOB_SOURCE_ACTIVITY)
    async def crawl_job_source(self, request: CrawlJobSourceInput) -> list[CrawledPostingRecord]:
        activity.logger.info("crawling source %s", request.source_id)
        return await self._sink.crawl_source(request)

    @activity.defn(name=INGEST_POSTING_ACTIVITY)
    async def ingest_posting(
        self, record: CrawledPostingRecord
    ) -> dict[str, bool | str]:
        activity.logger.info("ingesting posting %s", record.external_id)
        return await self._sink.ingest_posting(record)


class M1PurgeActivities:
    def __init__(self, sink: PurgeActivitySink | None = None) -> None:
        self._sink = sink or NoOpPurgeSink()

    @activity.defn(name=PURGE_RAW_DOCUMENT_ACTIVITY)
    async def purge_raw_documents(self, request: RawDocumentPurgeInput) -> RawDocumentPurgeResult:
        activity.logger.info("purging expired raw documents")
        return await self._sink.purge_documents(request)
