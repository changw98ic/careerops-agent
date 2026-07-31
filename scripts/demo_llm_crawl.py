"""Offline demo of the LLM crawl path (real-autonomous-career-loop Phase 4).

Proves the full fake-driven path end to end with NO real provider, NO real
browser and NO database::

    ReAct loop (LLM decides action)
      -> LLM extraction (bounded schema -> RawJobRecord[])
        -> _to_crawled_posting (CrawledPostingRecord)
          -> ingest_posting (records parser_version provenance)

A ``FakeBrowserTool`` serves a local HTML fixture via ``capture_html`` and
returns empty ``capture_network``. A ``FakeStructuredModelClient`` drives both
halves of the model call surface: ``task_type="crawl_react"`` tells the ReAct
loop to extract immediately, and ``task_type="job_extraction"`` returns a
bounded set of demo postings. An in-memory sink records the
``parser_version`` of every newly ingested version.

Run::

    uv run python scripts/demo_llm_crawl.py     # or:  make demo-llm-crawl
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

from careerops.application.crawl_agent import CrawlAgent
from careerops.application.crawl_agent_service import CrawlAgentService
from careerops.application.llm_job_extraction import LLMJobExtractor
from careerops.infrastructure.ego_tool import CapturedApiCall, PageFetchResult
from careerops.model_gateway.base import StructuredModelRequest, StructuredModelResponse
from careerops.workflows.m1_contracts import CrawledPostingRecord

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_career_page.html"
_SOURCE_URL = "https://demo.test/careers"
_EXPECTED_PROVENANCE = "llm-extraction"

# Bounded set of postings the fake extractor returns for the fixture. These are
# the same kind of objects a real provider would emit against job_extraction.json.
_DEMO_JOBS: list[dict[str, str]] = [
    {
        "title": "Senior Engineer",
        "location": "Remote",
        "url": "https://demo.test/j/1",
        "description": "Lead a small team building the core platform end to end.",
    },
    {
        "title": "Platform Engineer",
        "location": "Berlin",
        "url": "https://demo.test/j/2",
        "description": "Own infrastructure, reliability and developer experience.",
    },
    {
        "title": "Product Designer",
        "location": "Remote",
        "url": "https://demo.test/j/3",
        "description": "Design end-to-end product flows and the design system.",
    },
]


class FakeStructuredModelClient:
    """Structured model fake that branches on ``request.task_type``.

    - ``crawl_react``    -> instruct the ReAct loop to ``extract`` on the spot.
    - ``job_extraction`` -> return the bounded demo posting list.
    """

    @property
    def is_enabled(self) -> bool:
        return True

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:
        if request.task_type == "crawl_react":
            result: dict[str, object] = {
                "thought": "Real job titles are visible on the page. Extracting now.",
                "action": "extract",
                "target": "",
            }
        elif request.task_type == "job_extraction":
            result = {"jobs": _DEMO_JOBS}
        else:
            result = {}
        return StructuredModelResponse(
            task_type=request.task_type,
            result=result,
            model_id="demo-fake-model",
            prompt_version="demo-v1",
            is_review_only=False,
            trace_id=request.trace_id,
        )


class FakeBrowserTool:
    """In-memory ``BrowserTool``: serves a local fixture, no network at all."""

    def __init__(self, html: str) -> None:
        self._html = html
        self._url = ""

    @property
    def is_ready(self) -> bool:
        return True

    def navigate(self, url: str, *, wait_s: float = 8.0) -> dict[str, str]:
        self._url = url
        return {"url": url, "title": "Demo Careers"}

    def capture_html(self) -> str:
        return self._html

    def run_page_script(self, script: str) -> str:
        # The ReAct loop asks for a compact page-state snapshot; give it the
        # visible text so the (fake) model has something concrete to "see".
        return (
            f"URL: {self._url}\nTITLE: Demo Careers\n\n"
            "VISIBLE TEXT:\nOpen Positions at Demo Corp\n"
            "- Senior Engineer\n- Platform Engineer\n- Product Designer"
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


def _content_hash(structured_data: dict[str, str]) -> str:
    """Deterministic SHA-256 hex digest, mirroring m1_crawl_sink._content_hash."""
    canonical = json.dumps(structured_data, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class InMemoryAsyncSink:
    """In-memory ingest sink mirroring ``m1_crawl_sink.ingest_posting`` dedup.

    Recording the ``parser_version`` of every NEW version row lets the demo
    print the provenance attached to each LLM-extracted posting.

    Dedup mirrors the real sink:
    - posting: unique on ``(source_id, external_id)``.
    - version: unique on ``(posting_id, content_hash)``.
    """

    def __init__(self) -> None:
        self._postings: dict[tuple[str, str], UUID] = {}
        self._versions: set[tuple[UUID, str]] = set()
        self.parser_versions: list[str] = []
        self.all_versions: list[CrawledPostingRecord] = []

    async def ingest_posting(
        self,
        record: CrawledPostingRecord,
        *,
        crawl_run_id: UUID | None = None,
        plan_version_id: UUID | None = None,
    ) -> dict[str, bool]:
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
            self.parser_versions.append(record.parser_version)
            self.all_versions.append(record)

        return {"is_new_posting": is_new_posting, "is_new_version": is_new_version}


def main() -> int:
    html = _FIXTURE.read_text(encoding="utf-8")
    fake_browser = FakeBrowserTool(html)
    fake_model = FakeStructuredModelClient()
    extractor = LLMJobExtractor(fake_model)
    agent = CrawlAgent(fake_browser, extractor, model_client=fake_model)
    sink = InMemoryAsyncSink()
    service = CrawlAgentService(sink=sink, agent=agent)  # type: ignore[arg-type]

    print("=" * 64)
    print("LLM crawl demo: ReAct -> extract -> _to_crawled_posting -> ingest")
    print("=" * 64)

    counters = asyncio.run(
        service.crawl_and_ingest(
            owner_id=uuid4(),
            source_id=uuid4(),
            source_url=_SOURCE_URL,
            source_name="demo",
        )
    )

    print("-" * 64)
    print(
        f"counts: discovered={counters.discovered} "
        f"updated={counters.updated} failed={counters.failed}"
    )
    print(f"ingested parser_versions: {sink.parser_versions}")

    provenance_ok = bool(sink.parser_versions) and all(
        pv == _EXPECTED_PROVENANCE for pv in sink.parser_versions
    )
    if provenance_ok:
        print(f"provenance OK: every posting tagged '{_EXPECTED_PROVENANCE}' (OpenSpec 4.3)")
    else:
        seen = sorted(set(sink.parser_versions))
        print(
            f"provenance note: OpenSpec 4.3 expects '{_EXPECTED_PROVENANCE}' but got "
            f"{seen}; the tag is emitted by crawl_agent_service._to_crawled_posting."
        )

    print("-" * 64)
    # The path is proven when at least one posting was ingested end to end.
    return 0 if counters.discovered + counters.updated > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
