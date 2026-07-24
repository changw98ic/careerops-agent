"""Real ``CrawlActivitySink`` backed by http_fetcher + JobSourceAdapter.

Implements the ``CrawlActivitySink`` protocol from ``m1_activities.py`` by
fetching the source URL via ``http_fetcher.fetch`` and running the matching
``JobSourceAdapter.list_jobs`` parser. Each ``RawJobRecord`` is mapped to a
``CrawledPostingRecord`` with the fetch provenance attached.

``ingest_posting`` is a v1 stub (returns ``is_new_posting=True``); real
ingestion writes to ``job_postings`` + ``job_posting_versions`` which is
handled by the LangGraph crawl node path in v1.
"""

from __future__ import annotations

from collections.abc import Callable
from json import JSONDecodeError, loads

from careerops.adapters.http_fetcher import FetchedResponse, fetch
from careerops.adapters.job_sources import (
    AshbyAdapter,
    GreenhouseAdapter,
    JobSourceAdapter,
    JsonLdAdapter,
    LeverAdapter,
    SitemapAdapter,
    StaticHtmlAdapter,
)
from careerops.workflows.m1_contracts import (
    CrawledPostingRecord,
    CrawlJobSourceInput,
)

FetcherFn = Callable[[str], FetchedResponse]

_ADAPTER_REGISTRY: dict[str, JobSourceAdapter] = {
    "greenhouse": GreenhouseAdapter(),
    "lever": LeverAdapter(),
    "ashby": AshbyAdapter(),
    "json_ld": JsonLdAdapter(),
    "sitemap": SitemapAdapter(),
    "static_html": StaticHtmlAdapter(),
}


def _parse_body(body: str) -> object:
    """Parse a fetch body into the form its adapter expects."""
    stripped = body.lstrip()
    if stripped and stripped[0] in "{[":
        try:
            return loads(body)
        except (JSONDecodeError, ValueError):
            return body
    return body


class RealCrawlActivitySink:
    """Activity-side crawl adapter backed by http_fetcher + adapters.

    For each ``CrawlJobSourceInput``, fetches the ``base_url``, runs the
    list adapter matching ``source_type``, and returns the crawled postings.
    """

    def __init__(
        self,
        adapters: dict[str, JobSourceAdapter] | None = None,
        *,
        fetcher: FetcherFn | None = None,
    ) -> None:
        self._adapters = adapters or dict(_ADAPTER_REGISTRY)
        # Allow injecting a fake fetcher for tests.
        self._fetch: FetcherFn = fetcher or fetch

    async def crawl_source(self, request: CrawlJobSourceInput) -> list[CrawledPostingRecord]:
        adapter = self._adapters.get(request.source_type)
        if adapter is None:
            return []

        resp: FetchedResponse = self._fetch(request.base_url)
        result = adapter.list_jobs(_parse_body(resp.body))

        postings: list[CrawledPostingRecord] = []
        for record in result.jobs:
            postings.append(
                CrawledPostingRecord(
                    external_id=record.external_id,
                    canonical_url=record.url or request.base_url,
                    source_url=resp.final_url or request.base_url,
                    structured_data={
                        "title": record.title,
                        "location": record.location,
                        "description": record.description,
                        **record.raw_data,
                    },
                    parser_version=adapter.parser_version,
                )
            )
        return postings

    async def ingest_posting(self, record: CrawledPostingRecord) -> dict[str, object]:
        # v1 stub: the real ingestion path is the LangGraph crawl node.
        del record
        return {"is_new_posting": True, "is_new_version": True}
