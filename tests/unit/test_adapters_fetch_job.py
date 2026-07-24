"""Unit tests for detail-source adapters and RawJobRecord.description.

Covers v0.4 §2.3:
- GreenhouseDetailAdapter.fetch_job parses the JD body from ``content``.
- JsonLdAdapter.list_jobs maps JobPosting ``description`` to RawJobRecord.
- RawJobRecord.description defaults to "".
"""

from __future__ import annotations

from datetime import UTC, datetime

from careerops.adapters import (
    AshbyDetailAdapter,
    GreenhouseDetailAdapter,
    JsonLdAdapter,
    RawJobRecord,
)


def _html_block(jsonld: str) -> str:
    return f'<html><head><script type="application/ld+json">{jsonld}</script></head></html>'


class TestRawJobRecordDescription:
    def test_description_defaults_to_empty_string(self) -> None:
        record = RawJobRecord(external_id="gh-123", title="Engineer")

        assert record.description == ""

    def test_description_is_distinct_from_raw_data(self) -> None:
        record = RawJobRecord(
            external_id="gh-123",
            title="Engineer",
            description="<p>JD body</p>",
            raw_data={"content": "<p>JD body</p>"},
        )

        assert record.description == "<p>JD body</p>"
        assert record.raw_data == {"content": "<p>JD body</p>"}


class TestGreenhouseDetailAdapterFetchJob:
    def test_parses_content_into_description(self) -> None:
        adapter = GreenhouseDetailAdapter()
        detail = {
            "id": 12345,
            "title": "Senior Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
            "location": {"name": "Remote"},
            "content": "<p>We are looking for a senior engineer.</p>",
        }
        fetched_at = datetime(2026, 7, 24, tzinfo=UTC)

        record = adapter.fetch_job(
            detail,
            source_url="https://boards.greenhouse.io/acme/jobs/12345",
            fetched_at=fetched_at,
        )

        assert record.description == "<p>We are looking for a senior engineer.</p>"
        assert record.description != ""
        assert record.external_id == "12345"
        assert record.title == "Senior Engineer"
        assert record.location == "Remote"
        assert record.url == "https://boards.greenhouse.io/acme/jobs/12345"
        assert record.raw_data is detail

    def test_source_type_and_parser_version_pinned(self) -> None:
        adapter = GreenhouseDetailAdapter()

        assert adapter.source_type == "greenhouse_detail"
        assert adapter.parser_version == "greenhouse-detail-v1"

    def test_url_falls_back_to_source_url_when_missing(self) -> None:
        adapter = GreenhouseDetailAdapter()
        fetched_url = "https://boards.greenhouse.io/acme/jobs/777"

        record = adapter.fetch_job(
            {"id": 777, "title": "Eng", "content": "<p>JD</p>"},
            source_url=fetched_url,
            fetched_at=datetime(2026, 7, 24, tzinfo=UTC),
        )

        assert record.url == fetched_url

    def test_empty_content_yields_empty_description(self) -> None:
        adapter = GreenhouseDetailAdapter()

        record = adapter.fetch_job(
            {"id": 1, "title": "Eng"},
            source_url="https://boards.greenhouse.io/acme/jobs/1",
            fetched_at=datetime(2026, 7, 24, tzinfo=UTC),
        )

        assert record.description == ""

    def test_non_dict_response_returns_empty_record(self) -> None:
        adapter = GreenhouseDetailAdapter()
        fetched_url = "https://boards.greenhouse.io/acme/jobs/9"

        record = adapter.fetch_job(
            "not-json",
            source_url=fetched_url,
            fetched_at=datetime(2026, 7, 24, tzinfo=UTC),
        )

        assert record.external_id == ""
        assert record.title == ""
        assert record.description == ""
        assert record.url == fetched_url


class TestAshbyDetailAdapterFetchJob:
    def test_parses_description_plain_into_description(self) -> None:
        adapter = AshbyDetailAdapter()
        detail = {
            "id": "ashby-1",
            "title": "Reliability Engineer",
            "location": "Remote (Global)",
            "url": "https://careers.ashbyhq.com/acme/ashby-1",
            "descriptionPlain": "We are looking for a reliability engineer.",
            "description": "<p>We are looking for a reliability engineer.</p>",
        }
        fetched_at = datetime(2026, 7, 24, tzinfo=UTC)

        record = adapter.fetch_job(
            detail,
            source_url="https://careers.ashbyhq.com/acme/ashby-1",
            fetched_at=fetched_at,
        )

        # descriptionPlain preferred over description
        assert record.description == "We are looking for a reliability engineer."
        assert record.external_id == "ashby-1"
        assert record.title == "Reliability Engineer"
        assert record.location == "Remote (Global)"
        assert record.url == "https://careers.ashbyhq.com/acme/ashby-1"
        assert record.raw_data is detail

    def test_falls_back_to_description_when_plain_missing(self) -> None:
        adapter = AshbyDetailAdapter()

        record = adapter.fetch_job(
            {"id": "a", "title": "Eng", "description": "<p>JD body</p>"},
            source_url="https://careers.ashbyhq.com/acme/a",
            fetched_at=datetime(2026, 7, 24, tzinfo=UTC),
        )

        assert record.description == "<p>JD body</p>"

    def test_source_type_and_parser_version_pinned(self) -> None:
        adapter = AshbyDetailAdapter()

        assert adapter.source_type == "ashby_detail"
        assert adapter.parser_version == "ashby-detail-v1"

    def test_url_falls_back_to_source_url_when_missing(self) -> None:
        adapter = AshbyDetailAdapter()
        fetched_url = "https://careers.ashbyhq.com/acme/9"

        record = adapter.fetch_job(
            {"id": "9", "title": "Eng", "descriptionPlain": "JD"},
            source_url=fetched_url,
            fetched_at=datetime(2026, 7, 24, tzinfo=UTC),
        )

        assert record.url == fetched_url

    def test_non_dict_response_returns_empty_record(self) -> None:
        adapter = AshbyDetailAdapter()
        fetched_url = "https://careers.ashbyhq.com/acme/x"

        record = adapter.fetch_job(
            "not-json",
            source_url=fetched_url,
            fetched_at=datetime(2026, 7, 24, tzinfo=UTC),
        )

        assert record.external_id == ""
        assert record.description == ""
        assert record.url == fetched_url


class TestJsonLdAdapterDescriptionMapping:
    def test_list_maps_jobposting_description_field(self) -> None:
        adapter = JsonLdAdapter()
        page = _html_block(
            '{"@type":"JobPosting",'
            '"title":"Staff Engineer",'
            '"url":"https://example.com/jobs/staff",'
            '"description":"<p>Own the platform roadmap.</p>",'
            '"jobLocation":{"address":{"addressLocality":"Berlin"}}}'
        )

        result = adapter.list_jobs(page)

        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job.description == "<p>Own the platform roadmap.</p>"
        assert job.title == "Staff Engineer"
        assert job.url == "https://example.com/jobs/staff"
        assert job.location == "Berlin"

    def test_missing_description_defaults_to_empty(self) -> None:
        adapter = JsonLdAdapter()
        page = _html_block(
            '{"@type":"JobPosting","title":"Eng","url":"https://example.com/jobs/eng"}'
        )

        result = adapter.list_jobs(page)

        assert len(result.jobs) == 1
        assert result.jobs[0].description == ""
