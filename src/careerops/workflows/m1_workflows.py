"""M1 Workflows: CompanyDiscovery, CrawlJobSource, and RawDocumentPurge.

All I/O is performed in Activities. Workflow code is deterministic.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from careerops.workflows.m1_contracts import (
    CRAWL_JOB_SOURCE_ACTIVITY,
    DISCOVER_COMPANY_SOURCES_ACTIVITY,
    INGEST_POSTING_ACTIVITY,
    PURGE_RAW_DOCUMENT_ACTIVITY,
    CompanyDiscoveryInput,
    CompanyDiscoveryResult,
    CrawledPostingRecord,
    CrawlJobSourceInput,
    CrawlJobSourceResult,
    RawDocumentPurgeInput,
    RawDocumentPurgeResult,
)

_ACTIVITY_TIMEOUT = timedelta(seconds=60)
_CRAWL_TIMEOUT = timedelta(seconds=120)
_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)


@workflow.defn
class CompanyDiscoveryWorkflow:
    """Discovers job sources for a company and registers them."""

    @workflow.run
    async def run(self, request: CompanyDiscoveryInput) -> CompanyDiscoveryResult:
        result = await workflow.execute_activity(
            DISCOVER_COMPANY_SOURCES_ACTIVITY,
            request,
            result_type=CompanyDiscoveryResult,
            start_to_close_timeout=_ACTIVITY_TIMEOUT,
            retry_policy=_RETRY_POLICY,
        )
        return result


@workflow.defn
class CrawlJobSourceWorkflow:
    """Crawls a job source, ingests postings with dedup and versioning."""

    @workflow.run
    async def run(self, request: CrawlJobSourceInput) -> CrawlJobSourceResult:
        crawled = await workflow.execute_activity(
            CRAWL_JOB_SOURCE_ACTIVITY,
            request,
            result_type=list[CrawledPostingRecord],
            start_to_close_timeout=_CRAWL_TIMEOUT,
            retry_policy=_RETRY_POLICY,
        )

        ingested = 0
        new_postings = 0
        new_versions = 0
        errors: list[str] = []

        for record in crawled:
            try:
                ingest_result = await workflow.execute_activity(
                    INGEST_POSTING_ACTIVITY,
                    record,
                    result_type=dict[str, bool],
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    retry_policy=_RETRY_POLICY,
                )
                ingested += 1
                if ingest_result.get("is_new_posting"):
                    new_postings += 1
                if ingest_result.get("is_new_version"):
                    new_versions += 1
            except Exception as exc:
                errors.append(f"{record.external_id}: {exc}")

        return CrawlJobSourceResult(
            source_id=request.source_id,
            postings_crawled=len(crawled),
            postings_ingested=ingested,
            new_postings=new_postings,
            new_versions=new_versions,
            errors=tuple(errors),
        )


@workflow.defn
class RawDocumentPurgeWorkflow:
    """Purges expired raw documents after persisting evidence snippets.

    Before deleting full content, persists long-term evidence snippet
    (URL, fetched_at, content hash, decision snippet). Verifies no
    dangling version references remain.
    """

    @workflow.run
    async def run(self, request: RawDocumentPurgeInput) -> RawDocumentPurgeResult:
        if request.dry_run:
            return RawDocumentPurgeResult(dry_run=True)

        purge_result = await workflow.execute_activity(
            PURGE_RAW_DOCUMENT_ACTIVITY,
            request,
            result_type=RawDocumentPurgeResult,
            start_to_close_timeout=_CRAWL_TIMEOUT,
            retry_policy=_RETRY_POLICY,
        )
        return purge_result
