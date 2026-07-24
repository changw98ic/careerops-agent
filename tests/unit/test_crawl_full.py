"""Unit tests for crawl_full career page parsing and ego_save_raw_html.

Covers:
- parse_career_page with JobPosting JSON-LD (via JsonLdAdapter).
- parse_career_page regex fallback when no JSON-LD present.
- ego_save_raw_html subprocess invocation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts.crawl_full import ego_save_raw_html, parse_career_page


def _html_with_jsonld(jobs_json: str) -> str:
    """Wrap a JSON-LD string in a minimal HTML page."""
    return (
        "<html><head>"
        f'<script type="application/ld+json">{jobs_json}</script>'
        "</head><body></body></html>"
    )


# --- parse_career_page: JSON-LD path ---


class TestParseCareerPageJsonld:
    def test_single_jobposting(self) -> None:
        html = _html_with_jsonld(
            '{"@type":"JobPosting",'
            '"title":"Staff Engineer",'
            '"url":"https://example.com/jobs/staff",'
            '"description":"<p>Build platforms.</p>",'
            '"jobLocation":{"address":{"addressLocality":"Berlin"}}}'
        )

        jobs = parse_career_page(html, "Example", "https://example.com/careers")

        assert len(jobs) == 1
        job = jobs[0]
        assert job["company"] == "Example"
        assert job["title"] == "Staff Engineer"
        assert job["url"] == "https://example.com/jobs/staff"
        assert job["location"] == "Berlin"
        assert job["description"] == "<p>Build platforms.</p>"
        assert job["source_type"] == "career_page_jsonld"
        assert job["source_url"] == "https://example.com/careers"

    def test_multiple_jobpostings(self) -> None:
        # JsonLdAdapter only handles dicts, not lists — each item needs its
        # own <script> tag for the adapter to pick it up.
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

        jobs = parse_career_page(html, "Ex", "https://ex.com/careers")

        assert len(jobs) == 2
        assert jobs[0]["title"] == "SWE"
        assert jobs[1]["title"] == "PM"
        assert all(j["source_type"] == "career_page_jsonld" for j in jobs)

    def test_non_jobposting_type_ignored(self) -> None:
        html = _html_with_jsonld('{"@type":"WebPage","name":"Careers"}')

        jobs = parse_career_page(html, "Ex", "https://ex.com/careers")

        # No JSON-LD jobs found — falls through to regex (which also finds
        # nothing in this HTML without text "..." patterns).
        assert jobs == []

    def test_empty_title_skipped(self) -> None:
        html = _html_with_jsonld('{"@type":"JobPosting","title":"","url":"https://ex.com/1"}')

        jobs = parse_career_page(html, "Ex", "https://ex.com/careers")

        assert jobs == []

    def test_description_included_in_output(self) -> None:
        html = _html_with_jsonld(
            '{"@type":"JobPosting",'
            '"title":"Backend Engineer",'
            '"url":"https://ex.com/be",'
            '"description":"<p>Work on APIs.</p>"}'
        )

        jobs = parse_career_page(html, "Ex", "https://ex.com/careers")

        assert len(jobs) == 1
        assert jobs[0]["description"] == "<p>Work on APIs.</p>"

    def test_url_falls_back_to_page_url(self) -> None:
        html = _html_with_jsonld('{"@type":"JobPosting","title":"Eng"}')
        page_url = "https://ex.com/careers"

        jobs = parse_career_page(html, "Ex", page_url)

        assert len(jobs) == 1
        assert jobs[0]["url"] == page_url


# --- parse_career_page: regex fallback ---


class TestParseCareerPageFallbackRegex:
    def test_snapshot_text_with_role_keyword(self) -> None:
        snapshot = 'text "Senior Software Engineer" [ref=1]\nbutton "Apply"'

        jobs = parse_career_page(snapshot, "Acme", "https://acme.com/careers")

        assert len(jobs) == 1
        assert jobs[0]["title"] == "Senior Software Engineer"
        assert jobs[0]["source_type"] == "career_page"
        assert jobs[0]["company"] == "Acme"

    def test_noise_filtered(self) -> None:
        snapshot = 'text "Cookie Policy" [ref=1]\nbutton "Next"'

        jobs = parse_career_page(snapshot, "Acme", "https://acme.com/careers")

        assert jobs == []

    def test_no_role_keyword_filtered(self) -> None:
        snapshot = 'text "Welcome to our company page" [ref=1]'

        jobs = parse_career_page(snapshot, "Acme", "https://acme.com/careers")

        assert jobs == []

    def test_deduplicates_titles(self) -> None:
        snapshot = 'text "Senior Staff Engineer" [ref=1]\ntext "Senior Staff Engineer" [ref=2]'

        jobs = parse_career_page(snapshot, "Acme", "https://acme.com/careers")

        assert len(jobs) == 1

    def test_jsonld_takes_precedence_over_regex(self) -> None:
        """When HTML contains JSON-LD, regex fallback is not reached."""
        html = _html_with_jsonld(
            '{"@type":"JobPosting","title":"Platform Eng","url":"https://ex.com/pe"}'
        )
        # Also inject a regex-matching pattern (should be ignored)
        html += 'text "Staff Data Engineer" [ref=1]'

        jobs = parse_career_page(html, "Ex", "https://ex.com/careers")

        # Only the JSON-LD job should appear
        assert len(jobs) == 1
        assert jobs[0]["source_type"] == "career_page_jsonld"
        assert jobs[0]["title"] == "Platform Eng"


# --- ego_save_raw_html ---


class TestEgoSaveRawHtml:
    @patch("scripts.crawl_full.run_ego")
    def test_returns_true_on_success(self, mock_run_ego: MagicMock) -> None:
        mock_run_ego.return_value = "cliLog saved 12345"

        result = ego_save_raw_html("https://example.com", "/tmp/out.html", wait_seconds=5)

        assert result is True
        mock_run_ego.assert_called_once()
        script = mock_run_ego.call_args[0][0]
        assert "document.documentElement.outerHTML" in script
        assert "https://example.com" in script
        assert "/tmp/out.html" in script

    @patch("scripts.crawl_full.run_ego")
    def test_returns_false_on_failure(self, mock_run_ego: MagicMock) -> None:
        mock_run_ego.return_value = "ERROR: timeout"

        result = ego_save_raw_html("https://example.com", "/tmp/out.html")

        assert result is False

    @patch("scripts.crawl_full.run_ego")
    def test_script_uses_evaluate_not_snapshot_text(self, mock_run_ego: MagicMock) -> None:
        mock_run_ego.return_value = "cliLog saved 100"

        ego_save_raw_html("https://example.com", "/tmp/out.html")

        script = mock_run_ego.call_args[0][0]
        assert "evaluate" in script
        assert "snapshotText" not in script

    @patch("scripts.crawl_full.run_ego")
    def test_default_wait_seconds(self, mock_run_ego: MagicMock) -> None:
        mock_run_ego.return_value = "cliLog saved 100"

        ego_save_raw_html("https://example.com", "/tmp/out.html")

        script = mock_run_ego.call_args[0][0]
        assert "await wait(8)" in script

    @patch("scripts.crawl_full.run_ego")
    def test_custom_wait_seconds(self, mock_run_ego: MagicMock) -> None:
        mock_run_ego.return_value = "cliLog saved 100"

        ego_save_raw_html("https://example.com", "/tmp/out.html", wait_seconds=12)

        script = mock_run_ego.call_args[0][0]
        assert "await wait(12)" in script
