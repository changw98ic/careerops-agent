"""Integration tests for M1 Temporal workflows with real crawl activity.

Uses ``temporalio.testing.WorkflowEnvironment`` (test-server) to run
``CrawlJobSourceWorkflow`` end-to-end with a mock fetcher.

Requires ``CAREEROPS_TEST_DATABASE_URL`` for the test DB (for migration
verification) and the Temporal SDK test server.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from careerops.adapters.http_fetcher import FetchedResponse
from careerops.infrastructure.temporal.m1_activities import M1CrawlActivities
from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink
from careerops.workflows.m1_contracts import (
    CrawlJobSourceInput,
)
from careerops.workflows.m1_workflows import CrawlJobSourceWorkflow

pytestmark = pytest.mark.integration


_GREENHOUSE_LIST_RESPONSE = json.dumps(
    {
        "jobs": [
            {
                "id": "12345",
                "title": "Senior Engineer",
                "location": {"name": "San Francisco, CA"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
            },
            {
                "id": "12346",
                "title": "Staff Engineer",
                "location": {"name": "Remote"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/12346",
            },
        ]
    }
)


def _fake_fetcher(url: str) -> FetchedResponse:
    return FetchedResponse(
        status_code=200,
        final_url=url,
        fetched_at=datetime.now(tz=UTC),
        response_hash="abc123" + "0" * 58,
        body=_GREENHOUSE_LIST_RESPONSE,
    )


class TestCrawlJobSourceWorkflow:
    """CrawlJobSourceWorkflow with RealCrawlActivitySink."""

    @pytest.mark.asyncio
    async def test_workflow_produces_postings(self) -> None:
        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)
        activities = M1CrawlActivities(sink=sink)

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = env.client
            task_queue = "test-m1-crawl"

            async with Worker(
                client,
                task_queue=task_queue,
                workflows=[CrawlJobSourceWorkflow],
                activities=[
                    activities.crawl_job_source,
                    activities.ingest_posting,
                ],
            ):
                result = await client.execute_workflow(
                    CrawlJobSourceWorkflow.run,
                    CrawlJobSourceInput(
                        source_id="src-1",
                        company_id="comp-1",
                        company_name="Acme",
                        source_type="greenhouse",
                        base_url="https://boards.greenhouse.io/acme/jobs",
                    ),
                    id=f"test-crawl-{__import__('uuid').uuid4().hex}",
                    task_queue=task_queue,
                )

                assert result.source_id == "src-1"
                assert result.postings_crawled == 2
                assert result.postings_ingested == 2
                assert result.new_postings == 2
                assert len(result.errors) == 0

    @pytest.mark.asyncio
    async def test_workflow_unknown_source_type(self) -> None:
        sink = RealCrawlActivitySink(fetcher=_fake_fetcher)
        activities = M1CrawlActivities(sink=sink)

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = env.client
            task_queue = "test-m1-unknown"

            async with Worker(
                client,
                task_queue=task_queue,
                workflows=[CrawlJobSourceWorkflow],
                activities=[
                    activities.crawl_job_source,
                    activities.ingest_posting,
                ],
            ):
                result = await client.execute_workflow(
                    CrawlJobSourceWorkflow.run,
                    CrawlJobSourceInput(
                        source_id="src-2",
                        company_id="comp-2",
                        company_name="Beta",
                        source_type="unknown_source",
                        base_url="https://example.com/jobs",
                    ),
                    id=f"test-crawl-unknown-{__import__('uuid').uuid4().hex}",
                    task_queue=task_queue,
                )

                assert result.source_id == "src-2"
                assert result.postings_crawled == 0
                assert result.postings_ingested == 0
