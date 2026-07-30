"""LLM extraction provenance contract: fail-closed + llm-extraction provenance.

Pins the canonical Tier 2 extraction provenance (real-autonomous-career-loop,
spec 4.3):
- ``LLMJobExtractor.extract`` returns ``[]`` when the model client is disabled or
  raises (fail-closed), and ``[]`` when the result has no ``jobs`` key / a
  non-list ``jobs`` value.
- Each extracted ``RawJobRecord`` carries ``provenance='llm-extraction'`` and a
  sha256-derived ``external_id``.
- The shared sink helper ``_to_crawled_posting`` (in
  ``infrastructure.temporal.m1_crawl_sink``) carries that provenance into
  ``parser_version`` on the ``CrawledPostingRecord`` — the value
  ``RealCrawlActivitySink.ingest_posting`` writes to ``job_posting_versions`` —
  and fills ``structured_data`` with title/location/description/apply_url.

The helper previously lived on a never-created ``crawl_agent_service`` module;
Phase 4 folds it into the crawl sink so there is one ingest contract.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from careerops.adapters.job_sources import RawJobRecord
from careerops.application.llm_job_extraction import LLMJobExtractor
from careerops.infrastructure.temporal.m1_crawl_sink import _to_crawled_posting
from careerops.model_gateway.base import StructuredModelRequest, StructuredModelResponse

# Bounded set of postings a real provider would emit against job_extraction.json.
_JOBS_PAYLOAD = [
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
]

_SOURCE_URL = "https://demo.test/careers"


class _FakeModelClient:
    """Minimal ``StructuredModelClient`` fake for provenance tests."""

    def __init__(
        self,
        *,
        enabled: bool,
        result: dict[str, object],
        raise_on_invoke: bool = False,
    ) -> None:
        self._enabled = enabled
        self._result = result
        self._raise = raise_on_invoke

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:
        if self._raise:
            raise RuntimeError("provider unavailable")
        return StructuredModelResponse(
            task_type=request.task_type,
            result=self._result,
            model_id="test-fake-model",
            prompt_version="job_extraction-v1",
            is_review_only=True,
            trace_id=request.trace_id,
        )


def _expected_external_id(title: str, url: str) -> str:
    """Mirror the extractor's sha256(title|url)[:16] derivation."""
    return hashlib.sha256(f"{title}|{url}".encode()).hexdigest()[:16]


class TestLLMExtractionProvenance:
    def test_extract_tags_records_with_llm_extraction_provenance(self) -> None:
        client = _FakeModelClient(enabled=True, result={"jobs": _JOBS_PAYLOAD})
        extractor = LLMJobExtractor(client)

        records = extractor.extract("<html>careers</html>", source_url=_SOURCE_URL)

        assert len(records) == len(_JOBS_PAYLOAD)
        for raw, record in zip(_JOBS_PAYLOAD, records, strict=True):
            assert record.provenance == "llm-extraction"
            assert record.title == raw["title"]
            assert record.location == raw["location"]
            assert record.url == raw["url"]
            assert record.description == raw["description"]
            # external_id is sha256(title|url)[:16] — distinct per posting.
            assert record.external_id == _expected_external_id(raw["title"], raw["url"])
        assert len({r.external_id for r in records}) == len(records)

    def test_extract_derives_external_id_from_source_url_when_detail_link_missing(
        self,
    ) -> None:
        # A posting with no detail url: the extractor resolves url to source_url,
        # so the sha256 external_id is derived from title|source_url (not title|"").
        client = _FakeModelClient(
            enabled=True, result={"jobs": [{"title": "Staff Engineer", "location": "NYC"}]},
        )
        extractor = LLMJobExtractor(client)

        records = extractor.extract("<html>careers</html>", source_url=_SOURCE_URL)

        assert len(records) == 1
        record = records[0]
        assert record.url == _SOURCE_URL
        assert record.external_id == _expected_external_id("Staff Engineer", _SOURCE_URL)

    def test_extract_fail_closed_when_client_disabled(self) -> None:
        client = _FakeModelClient(enabled=False, result={"jobs": _JOBS_PAYLOAD})
        extractor = LLMJobExtractor(client)

        assert extractor.extract("<html>careers</html>", source_url=_SOURCE_URL) == []

    def test_extract_fail_closed_when_invoke_raises(self) -> None:
        client = _FakeModelClient(
            enabled=True, result={"jobs": _JOBS_PAYLOAD}, raise_on_invoke=True
        )
        extractor = LLMJobExtractor(client)

        assert extractor.extract("<html>careers</html>", source_url=_SOURCE_URL) == []

    def test_extract_fail_closed_when_result_has_no_jobs_key(self) -> None:
        client = _FakeModelClient(enabled=True, result={"not_jobs": []})
        extractor = LLMJobExtractor(client)

        assert extractor.extract("<html>careers</html>", source_url=_SOURCE_URL) == []

    def test_extract_fail_closed_when_jobs_is_not_a_list(self) -> None:
        client = _FakeModelClient(enabled=True, result={"jobs": "not-a-list"})
        extractor = LLMJobExtractor(client)

        assert extractor.extract("<html>careers</html>", source_url=_SOURCE_URL) == []

    def test_to_crawled_posting_carries_llm_extraction_provenance(self) -> None:
        # End-to-end pin: extract() -> _to_crawled_posting() preserves the
        # llm-extraction provenance into parser_version (spec 4.3) and fills the
        # structured_data the sink persists to job_posting_versions.
        client = _FakeModelClient(enabled=True, result={"jobs": _JOBS_PAYLOAD})
        extractor = LLMJobExtractor(client)
        source_id = uuid4()
        now = datetime.now(tz=UTC)

        records = extractor.extract("<html>careers</html>", source_url=_SOURCE_URL)
        assert records  # sanity

        posting = _to_crawled_posting(records[0], source_id, _SOURCE_URL, now)

        assert posting.parser_version == "llm-extraction"
        assert posting.source_id == str(source_id)
        assert posting.source_url == _SOURCE_URL
        assert posting.canonical_url == records[0].url
        assert posting.external_id == records[0].external_id
        assert posting.fetched_at == now.isoformat()
        assert posting.structured_data == {
            "title": records[0].title,
            "location": records[0].location,
            "description": records[0].description,
            "apply_url": records[0].url,
        }

    def test_to_crawled_posting_falls_back_to_source_url_for_canonical_url(self) -> None:
        # A record whose url is empty (LLM returned no detail link): canonical_url
        # falls back to the source url and apply_url stays "", but parser_version
        # still reflects the record's llm-extraction provenance.
        record = RawJobRecord(
            external_id="abc123",
            title="Backend Engineer",
            location="Tokyo",
            url="",
            description="Build services.",
            provenance="llm-extraction",
        )
        now = datetime.now(tz=UTC)

        posting = _to_crawled_posting(record, uuid4(), _SOURCE_URL, now)

        assert posting.parser_version == "llm-extraction"
        assert posting.canonical_url == _SOURCE_URL
        assert posting.structured_data == {
            "title": "Backend Engineer",
            "location": "Tokyo",
            "description": "Build services.",
            "apply_url": "",
        }
