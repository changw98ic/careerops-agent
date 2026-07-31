from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from temporalio.client import Client

from careerops.application.real_crawl_batch import (
    MAX_CONCURRENT_SOURCE_WORKFLOWS,
    run_source_workflows,
)
from careerops.domain.crawl_plans import CrawlSource, CrawlSourceType
from careerops.workflows.s5_contracts import CrawlRunWorkflowResult


class _RecordingClient:
    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0
        self.source_ids: list[str] = []

    async def execute_workflow(self, _workflow: Any, request: Any, **_: Any) -> Any:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.source_ids.append(request.source_id)
        await asyncio.sleep(0.01)
        self.active -= 1
        return CrawlRunWorkflowResult(
            run_id=str(uuid4()),
            state="succeeded",
        )


@pytest.mark.asyncio
async def test_large_batch_uses_one_bounded_durable_workflow_per_source() -> None:
    owner_id = uuid4()
    sources = [
        CrawlSource(
            id=uuid4(),
            owner_id=owner_id,
            company_id=uuid4(),
            source_type=CrawlSourceType.GREENHOUSE,
            source_identifier=f"source-{index}",
            base_url=f"https://example.com/{index}",
        )
        for index in range(17)
    ]
    client = _RecordingClient()

    results = await run_source_workflows(
        cast("Client", client),
        sources,
        owner_id,
        task_queue="test",
    )

    assert len(results) == len(sources)
    assert set(client.source_ids) == {str(source.id) for source in sources}
    assert client.maximum_active == MAX_CONCURRENT_SOURCE_WORKFLOWS
    assert all(UUID(result.run_id) for result in results)
