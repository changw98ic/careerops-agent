"""Canonical career-page JSON-LD parsing (real-autonomous-career-loop Phase 4).

The old ``scripts/crawl_full.parse_career_page`` mixed JSON-LD parsing, a regex
fallback over ego snapshot text, and a subprocess ``ego_save_raw_html`` wrapper
into one divergent script. Phase 4 folds the reusable JSON-LD extraction into
the existing :class:`JsonLdAdapter` (the adapter the production crawl sink
uses), deletes the parallel script path, and retargets this suite at the
canonical adapter so there is one career-page parse contract.

What moved where:
- JSON-LD extraction -> ``JsonLdAdapter.list_jobs`` (tested here).
- ego HTML capture -> ``EgoBrowserExecutor`` / ``EgoBrowserTool`` (covered by
  ``test_ego_browser_executor.py``), not a one-off subprocess wrapper.
- The snapshot-text regex fallback is dropped: a page the JSON-LD adapter
  cannot read is now handled by the Tier 2 ``CrawlAgent`` + ``LLMJobExtractor``
  fallback, not a second regex parser.
"""

from __future__ import annotations

from careerops.adapters.job_sources import JsonLdAdapter

_ADAPTER = JsonLdAdapter()


def _html_with_jsonld(jobs_json: str) -> str:
    """Wrap a JSON-LD string in a minimal HTML page."""
    return (
        "<html><head>"
        f'<script type="application/ld+json">{jobs_json}</script>'
        "</head><body></body></html>"
    )


class TestJsonLdAdapterCareerPage:
    def test_single_jobposting(self) -> None:
        html = _html_with_jsonld(
            '{"@type":"JobPosting",'
            '"title":"Staff Engineer",'
            '"url":"https://example.com/jobs/staff",'
            '"description":"<p>Build platforms.</p>",'
            '"jobLocation":{"address":{"addressLocality":"Berlin"}}}'
        )

        result = _ADAPTER.list_jobs(html)

        assert len(result.jobs) == 1
        record = result.jobs[0]
        assert record.title == "Staff Engineer"
        assert record.url == "https://example.com/jobs/staff"
        assert record.location == "Berlin"
        assert record.description == "<p>Build platforms.</p>"
        assert record.external_id  # sha256(url)[:16], non-empty
        assert result.parser_version == "json-ld-v1"

    def test_multiple_jobpostings_each_script(self) -> None:
        # JsonLdAdapter parses one JobPosting object per <script> tag.
        html = (
            "<html><head>"
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"SWE","url":"https://ex.com/1"}'
            "</script>"
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"PM","url":"https://ex.com/2"}'
            "</script>"
            "</head><body></body></html>"
        )

        result = _ADAPTER.list_jobs(html)

        assert len(result.jobs) == 2
        titles = {r.title for r in result.jobs}
        assert titles == {"SWE", "PM"}

    def test_non_jobposting_type_ignored(self) -> None:
        html = _html_with_jsonld('{"@type":"WebPage","name":"Careers"}')

        result = _ADAPTER.list_jobs(html)

        assert result.jobs == ()

    def test_description_included(self) -> None:
        html = _html_with_jsonld(
            '{"@type":"JobPosting",'
            '"title":"Backend Engineer",'
            '"url":"https://ex.com/be",'
            '"description":"<p>Work on APIs.</p>"}'
        )

        result = _ADAPTER.list_jobs(html)

        assert len(result.jobs) == 1
        assert result.jobs[0].description == "<p>Work on APIs.</p>"

    def test_location_extracted_from_job_location_address(self) -> None:
        html = _html_with_jsonld(
            '{"@type":"JobPosting",'
            '"title":"Frontend Engineer",'
            '"url":"https://ex.com/fe",'
            '"jobLocation":{"address":{"addressLocality":"Tokyo"}}}'
        )

        result = _ADAPTER.list_jobs(html)

        assert result.jobs[0].location == "Tokyo"

    def test_no_jsonld_returns_empty(self) -> None:
        # A page with no JSON-LD: the adapter returns no jobs (the production
        # sink then falls back to the Tier 2 agent, not a regex parser).
        result = _ADAPTER.list_jobs("<html><body>no structured data here</body></html>")

        assert result.jobs == ()

    def test_external_id_derived_from_url(self) -> None:
        # The adapter derives a stable external_id from the posting URL so the
        # sink's (source_id, external_id) dedup is stable across crawls.
        import hashlib

        url = "https://ex.com/j/dedup"
        html = _html_with_jsonld(f'{{"@type":"JobPosting","title":"Eng","url":"{url}"}}')

        record = _ADAPTER.list_jobs(html).jobs[0]

        assert record.external_id == hashlib.sha256(url.encode()).hexdigest()[:16]
