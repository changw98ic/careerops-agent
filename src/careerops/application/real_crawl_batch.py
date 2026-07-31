"""Durable, bounded execution for a large set of crawl sources."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from careerops.domain.crawl_plans import CrawlSource
from careerops.workflows.s5_contracts import (
    CrawlRunWorkflowResult,
    ScheduledCrawlWorkflowInput,
)
from careerops.workflows.s5_workflows import CrawlScheduledWorkflow

MAX_CONCURRENT_SOURCE_WORKFLOWS = 4


async def run_source_workflows(
    client: Client,
    sources: list[CrawlSource],
    candidate_id: UUID,
    *,
    task_queue: str,
) -> list[CrawlRunWorkflowResult]:
    """Execute one recoverable production workflow per source.

    Deterministic workflow IDs let a caller reconnect after its own process or
    container restarts. Bounded concurrency prevents a large catalog from
    overwhelming the database or remote providers.
    """

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SOURCE_WORKFLOWS)

    async def run_one(source: CrawlSource) -> CrawlRunWorkflowResult:
        workflow_id = f"acceptance-crawl-v2:{candidate_id}:{source.id}"
        async with semaphore:
            try:
                result = await client.execute_workflow(
                    CrawlScheduledWorkflow.run,
                    ScheduledCrawlWorkflowInput(
                        owner_id=str(candidate_id),
                        source_id=str(source.id),
                    ),
                    id=workflow_id,
                    task_queue=task_queue,
                    execution_timeout=timedelta(hours=2),
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                result = await client.get_workflow_handle(
                    workflow_id,
                    result_type=CrawlRunWorkflowResult,
                ).result()
            if result.state != "succeeded" or not result.run_id:
                raise RuntimeError(
                    f"source {source.id} ended in {result.state}: "
                    f"{result.error_category or result.skip_reason}"
                )
            return result

    return await asyncio.gather(*(run_one(source) for source in sources))
