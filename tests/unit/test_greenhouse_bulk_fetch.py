from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from careerops.adapters.http_fetcher import FetchedResponse
from careerops.infrastructure.temporal.m1_crawl_sink import RealCrawlActivitySink
from careerops.workflows.m1_contracts import CrawlJobSourceInput


@pytest.mark.asyncio
async def test_content_true_list_does_not_refetch_every_job_detail() -> None:
    calls: list[str] = []
    list_url = (
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    )

    def fetcher(url: str) -> FetchedResponse:
        calls.append(url)
        return FetchedResponse(
            status_code=200,
            final_url=url,
            fetched_at=datetime(2026, 7, 31, tzinfo=UTC),
            response_hash="a" * 64,
            body=json.dumps(
                {
                    "jobs": [
                        {
                            "id": 1,
                            "title": "Engineer",
                            "location": {"name": "Remote"},
                            "absolute_url": "https://example.test/jobs/1",
                            "content": "<p>Complete job description.</p>",
                        }
                    ]
                }
            ),
        )

    def generic_fetcher(url: str) -> FetchedResponse:
        raise AssertionError(f"generic 1 MB fetcher used for public ATS: {url}")

    result = await RealCrawlActivitySink(
        fetcher=generic_fetcher,
        public_ats_fetcher=fetcher,
    ).crawl_source_with_signals(
        CrawlJobSourceInput(
            source_id="source-1",
            company_id="company-1",
            company_name="Acme",
            source_type="greenhouse",
            base_url=list_url,
        )
    )

    assert calls == [list_url]
    assert result.postings[0].structured_data["description"] == (
        "<p>Complete job description.</p>"
    )
