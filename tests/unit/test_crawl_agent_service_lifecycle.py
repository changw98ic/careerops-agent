"""Canonical Tier 2 crawl->ingest lifecycle (real-autonomous-career-loop Phase 4).

Proves the merged production crawl chain with NO real provider, NO real browser
and NO database::

    CrawlAgent.crawl (ReAct loop decides ``extract``)
      -> LLMJobExtractor.extract (bounded schema -> RawJobRecord[], provenance='llm-extraction')
        -> ingest_crawled_records (m1_crawl_sink: _to_crawled_posting -> sink.ingest_posting,
           crawl_run_id / plan_version_id propagated, CrawlCounters accumulated)

Phase 4 folds the former ``CrawlAgentService.crawl_and_ingest`` into the crawl
sink module (``ingest_crawled_records`` + ``_to_crawled_posting``), so this test
exercises the ONE ingest contract the execution service also uses — there is no
parallel ``crawl_agent_service`` module.

A ``FakeBrowserTool`` serves canned career-page HTML via ``capture_html`` and an
empty ``capture_network``. A ``FakeStructuredModelClient`` (``is_enabled=True``)
drives both model call surfaces: ``task_type="crawl_agent"`` tells the ReAct
loop to extract immediately, and ``task_type="job_extraction"`` returns a bounded
set of postings. The async ingest fake mirrors the idempotent dedup of
``RealCrawlActivitySink.ingest_posting`` and records the propagated run/plan ids
plus the ``parser_version`` provenance on every NEW version row.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from uuid import UUID, uuid4

from careerops.application.crawl_agent import CrawlAgent
from careerops.application.llm_job_extraction import LLMJobExtractor
from careerops.infrastructure.ego_tool import CapturedApiCall, PageFetchResult
from careerops.infrastructure.temporal.m1_crawl_sink import CrawlCounters, ingest_crawled_records
from careerops.model_gateway.base import StructuredModelRequest, StructuredModelResponse
from careerops.workflows.m1_contracts import CrawledPostingRecord

_SOURCE_URL = "https://example.test/careers"
_CAREER_HTML = (
    "<html><head><title>Example Careers</title></head>"
    "<body><h1>Open Positions</h1>"
    "<ul><li>Senior Engineer</li><li>Platform Engineer</li></ul>"
    "</body></html>"
)

# Bounded set of postings the fake model returns for job_extraction.json.
_JOBS: list[dict[str, str]] = [
    {
        "title": "Senior Engineer",
        "location": "Remote",
        "url": "https://example.test/j/1",
        "description": "Lead a small team building the core platform end to end.",
    },
    {
        "title": "Platform Engineer",
        "location": "Berlin",
        "url": "https://example.test/j/2",
        "description": "Own infrastructure, reliability and developer experience.",
    },
]


def _content_hash(structured_data: dict[str, str]) -> str:
    """Deterministic SHA-256 hex digest, mirroring m1_crawl_sink._content_hash."""
    canonical = json.dumps(structured_data, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class FakeStructuredModelClient:
    """Structured model fake that branches on ``request.task_type``.

    - ``crawl_agent``    -> instruct the ReAct loop to ``extract`` on the spot.
    - ``job_extraction`` -> return the bounded posting list.
    """

    @property
    def is_enabled(self) -> bool:
        return True

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:
        if request.task_type == "crawl_agent":
            result: dict[str, object] = {"thought": "", "action": "extract", "target": ""}
        elif request.task_type == "job_extraction":
            result = {"jobs": _JOBS}
        else:
            result = {}
        return StructuredModelResponse(
            task_type=request.task_type,
            result=result,
            model_id="fake-model",
            prompt_version="test-v1",
            is_review_only=False,
            trace_id=request.trace_id,
        )


class FakeBrowserTool:
    """In-memory ``BrowserTool``: serves canned HTML, no network at all."""

    def __init__(self, html: str) -> None:
        self._html = html
        self._url = ""

    @property
    def is_ready(self) -> bool:
        return True

    def navigate(self, url: str, *, wait_s: float = 8.0) -> dict[str, str]:
        self._url = url
        return {"url": url, "title": "Example Careers"}

    def capture_html(self) -> str:
        return self._html

    def run_page_script(self, script: str) -> str:
        # The ReAct loop asks for a compact page-state snapshot; give it the
        # visible text so the (fake) model has something concrete to "see".
        return (
            f"URL: {self._url}\nTITLE: Example Careers\n\n"
            "VISIBLE TEXT:\nOpen Positions\n- Senior Engineer\n- Platform Engineer"
        )

    def capture_network(
        self,
        *,
        url_contains: str = "",
        trigger_script: str = "",
        wait_s: float = 6.0,
    ) -> list[CapturedApiCall]:
        return []

    def fetch_in_page(
        self,
        url: str,
        *,
        method: str = "GET",
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> PageFetchResult:
        return PageFetchResult(status=200, body="{}")


@dataclasses.dataclass
class IngestedVersionRecord:
    """A single ingested version with its provenance fields (mirrors m1_crawl_sink)."""

    posting_id: UUID
    source_id: UUID
    external_id: str
    parser_version: str
    crawl_run_id: UUID | None
    plan_version_id: UUID | None


class ProvenanceTrackingIngest:
    """Async ingest fake mirroring ``RealCrawlActivitySink.ingest_posting`` dedup.

    Dedup mirrors the real sink:
    - posting: unique on ``(source_id, external_id)``.
    - version: unique on ``(posting_id, content_hash)``.

    Every NEW version row records the propagated ``crawl_run_id`` /
    ``plan_version_id`` and the ``parser_version`` provenance so the test can
    assert both propagation and the 'llm-extraction' tag.
    """

    def __init__(self) -> None:
        self._postings: dict[tuple[str, str], UUID] = {}
        self._versions: set[tuple[UUID, str]] = set()
        self.all_versions: list[IngestedVersionRecord] = []
        # Every CrawledPostingRecord handed to ingest_posting, in call order.
        self.calls: list[CrawledPostingRecord] = []

    async def ingest_posting(
        self,
        record: CrawledPostingRecord,
        *,
        crawl_run_id: UUID | None = None,
        plan_version_id: UUID | None = None,
    ) -> dict[str, bool]:
        self.calls.append(record)

        posting_key = (record.source_id, record.external_id)
        is_new_posting = posting_key not in self._postings
        if is_new_posting:
            posting_id = uuid4()
            self._postings[posting_key] = posting_id
        else:
            posting_id = self._postings[posting_key]

        version_key = (posting_id, _content_hash(record.structured_data))
        is_new_version = version_key not in self._versions
        if is_new_version:
            self._versions.add(version_key)
            self.all_versions.append(
                IngestedVersionRecord(
                    posting_id=posting_id,
                    source_id=UUID(record.source_id),
                    external_id=record.external_id,
                    parser_version=record.parser_version,
                    crawl_run_id=crawl_run_id,
                    plan_version_id=plan_version_id,
                )
            )

        return {"is_new_posting": is_new_posting, "is_new_version": is_new_version}


class TestCrawlAgentIngestLifecycle:
    """ReAct -> extract -> _to_crawled_posting -> ingest_posting, all with fakes."""

    def test_react_extract_ingest_propagates_run_id_and_provenance(self) -> None:
        source_id = uuid4()
        crawl_run_id = uuid4()
        plan_version_id = uuid4()

        fake_browser = FakeBrowserTool(_CAREER_HTML)
        fake_model = FakeStructuredModelClient()
        extractor = LLMJobExtractor(fake_model)
        agent = CrawlAgent(fake_browser, extractor, model_client=fake_model)
        sink = ProvenanceTrackingIngest()

        # Phase 1: the agent crawls (ReAct decides extract -> LLM extraction).
        records = agent.crawl(_SOURCE_URL, wait_s=0, max_pages=1)
        assert len(records) == len(_JOBS)

        # Phase 2: ingest through the canonical sink helper (one contract).
        counters = asyncio.run(
            ingest_crawled_records(
                sink,  # type: ignore[arg-type]
                records,
                source_id=source_id,
                source_url=_SOURCE_URL,
                crawl_run_id=crawl_run_id,
                plan_version_id=plan_version_id,
            )
        )

        # Phase 3: every canned posting was discovered (all are new postings).
        assert isinstance(counters, CrawlCounters)
        assert counters.discovered == len(_JOBS)
        assert counters.updated == 0
        assert counters.failed == 0

        # Phase 4: ingest_posting was called once per crawled record.
        assert len(sink.calls) == len(_JOBS)

        # Phase 5: every CrawledPostingRecord carries the llm-extraction
        # provenance (OpenSpec 4.3) — parser_version IS the provenance column
        # written to job_posting_versions by RealCrawlActivitySink.
        for record in sink.calls:
            assert record.parser_version == "llm-extraction"
            assert record.source_id == str(source_id)
            assert record.source_url == _SOURCE_URL

        # Phase 6: crawl_run_id / plan_version_id propagated to every new
        # version row, one record per crawled posting.
        assert len(sink.all_versions) == len(_JOBS)
        for version in sink.all_versions:
            assert version.crawl_run_id == crawl_run_id
            assert version.plan_version_id == plan_version_id
            assert version.parser_version == "llm-extraction"
            assert version.source_id == source_id
