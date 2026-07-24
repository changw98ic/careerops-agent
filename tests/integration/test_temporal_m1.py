"""Integration tests for M1 Temporal workflows with real crawl activity.

Uses ``temporalio.testing.WorkflowEnvironment`` (test-server) to run
``CrawlJobSourceWorkflow`` end-to-end with a mock fetcher and a real
PostgreSQL database for ``ingest_posting`` writes.

Requires ``CAREEROPS_TEST_DATABASE_URL`` for the test DB and the Temporal
SDK test server.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from careerops.adapters.http_fetcher import FetchedResponse
from careerops.infrastructure.database.schema import (
    companies,
    job_posting_versions,
    job_postings,
    job_sources,
)
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


# --- Deterministic UUIDs derived from test strings (matches sink logic). ---

_COMPANY_ID = uuid5(NAMESPACE_URL, "company:acme-test")
_SOURCE_ID = uuid5(NAMESPACE_URL, "source:greenhouse:acme-test")


@pytest.fixture()
def db_engine() -> Engine:
    url = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required")
    return create_engine(url)


@pytest.fixture()
def _seed_prerequisites(db_engine: Engine) -> None:
    """Insert the company and job_source rows required by FK constraints."""
    with db_engine.begin() as conn:
        conn.execute(
            pg_insert(companies)
            .values(
                id=_COMPANY_ID,
                name="Acme Test",
                normalized_name="acme test",
            )
            .on_conflict_do_nothing(index_elements=[companies.c.normalized_name])
        )
        conn.execute(
            pg_insert(job_sources)
            .values(
                id=_SOURCE_ID,
                company_id=_COMPANY_ID,
                source_type="greenhouse",
                source_identifier="acme-test",
                base_url="https://boards.greenhouse.io/acme/jobs",
                state="active",
            )
            .on_conflict_do_nothing(
                index_elements=[
                    job_sources.c.company_id,
                    job_sources.c.source_type,
                    job_sources.c.source_identifier,
                ]
            )
        )


@pytest.fixture(autouse=True)
def _truncate_postings(db_engine: Engine) -> None:
    """Clean up job_postings rows before each test.

    ``job_posting_versions`` is an append-only table (DELETE rejected by
    trigger), so we use ``TRUNCATE ... CASCADE`` which bypasses row-level
    triggers.  This is safe in the disposable test database.
    """
    with db_engine.begin() as conn:
        conn.execute(
            sa.text("TRUNCATE careerops.job_posting_versions, careerops.job_postings CASCADE")
        )


class TestCrawlJobSourceWorkflow:
    """CrawlJobSourceWorkflow with RealCrawlActivitySink writing to DB."""

    @pytest.mark.asyncio
    async def test_workflow_produces_postings(
        self,
        db_engine: Engine,
        _seed_prerequisites: None,
    ) -> None:
        sink = RealCrawlActivitySink(fetcher=_fake_fetcher, engine=db_engine)
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
                        source_id=str(_SOURCE_ID),
                        company_id=str(_COMPANY_ID),
                        company_name="Acme Test",
                        source_type="greenhouse",
                        base_url="https://boards.greenhouse.io/acme/jobs",
                    ),
                    id=f"test-crawl-{uuid4().hex}",
                    task_queue=task_queue,
                )

                assert result.source_id == str(_SOURCE_ID)
                assert result.postings_crawled == 2
                assert result.postings_ingested == 2
                assert result.new_postings == 2
                assert result.new_versions == 2
                assert len(result.errors) == 0

        # Verify rows actually landed in Postgres.
        with db_engine.begin() as conn:
            posting_count = conn.scalar(
                sa.select(sa.func.count())
                .select_from(job_postings)
                .where(job_postings.c.source_id == _SOURCE_ID)
            )
            assert posting_count == 2

            version_count = conn.scalar(
                sa.select(sa.func.count())
                .select_from(job_posting_versions)
                .join(job_postings)
                .where(job_postings.c.source_id == _SOURCE_ID)
            )
            assert version_count == 2

    @pytest.mark.asyncio
    async def test_workflow_dedup_same_content(
        self,
        db_engine: Engine,
        _seed_prerequisites: None,
    ) -> None:
        """Running the same crawl twice produces no new postings or versions."""
        sink = RealCrawlActivitySink(fetcher=_fake_fetcher, engine=db_engine)
        activities = M1CrawlActivities(sink=sink)

        wf_input = CrawlJobSourceInput(
            source_id=str(_SOURCE_ID),
            company_id=str(_COMPANY_ID),
            company_name="Acme Test",
            source_type="greenhouse",
            base_url="https://boards.greenhouse.io/acme/jobs",
        )

        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = env.client
            task_queue = "test-m1-dedup"

            async with Worker(
                client,
                task_queue=task_queue,
                workflows=[CrawlJobSourceWorkflow],
                activities=[
                    activities.crawl_job_source,
                    activities.ingest_posting,
                ],
            ):
                # First run.
                result1 = await client.execute_workflow(
                    CrawlJobSourceWorkflow.run,
                    wf_input,
                    id=f"test-dedup-1-{uuid4().hex}",
                    task_queue=task_queue,
                )
                assert result1.new_postings == 2
                assert result1.new_versions == 2

                # Second run -- same content, same source.
                result2 = await client.execute_workflow(
                    CrawlJobSourceWorkflow.run,
                    wf_input,
                    id=f"test-dedup-2-{uuid4().hex}",
                    task_queue=task_queue,
                )
                assert result2.postings_crawled == 2
                assert result2.postings_ingested == 2
                assert result2.new_postings == 0
                assert result2.new_versions == 0

        # Still exactly 2 postings and 2 versions in the DB.
        with db_engine.begin() as conn:
            posting_count = conn.scalar(
                sa.select(sa.func.count())
                .select_from(job_postings)
                .where(job_postings.c.source_id == _SOURCE_ID)
            )
            assert posting_count == 2

            version_count = conn.scalar(
                sa.select(sa.func.count())
                .select_from(job_posting_versions)
                .join(job_postings)
                .where(job_postings.c.source_id == _SOURCE_ID)
            )
            assert version_count == 2

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
                    id=f"test-crawl-unknown-{uuid4().hex}",
                    task_queue=task_queue,
                )

                assert result.source_id == "src-2"
                assert result.postings_crawled == 0
                assert result.postings_ingested == 0
