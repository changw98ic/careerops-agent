from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_structured_job_catalog.py"
SPEC = importlib.util.spec_from_file_location("build_structured_job_catalog", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
job_catalog = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = job_catalog
SPEC.loader.exec_module(job_catalog)


def _signal(
    raw_path: Path, *, url: str = "https://example.com/jobs/backend?tracking=1"
) -> dict[str, object]:
    return {
        "classification": "actual_job_posting",
        "evidence": [{"rule": "schema_org_jobposting"}],
        "provenance": {
            "body_bytes": 2_000,
            "body_truncated": False,
            "canonical_url": url,
            "captured_at": "2026-07-19T00:00:00Z",
            "raw_byte_offset": 0,
            "raw_file": str(raw_path),
            "registrable_domain": "example.com",
        },
    }


def _write_raw(
    path: Path, response_text: str, *, url: str = "https://example.com/jobs/backend?tracking=1"
) -> None:
    path.write_text(
        json.dumps(
            {
                "canonical_url": url,
                "final_url": url,
                "response_text": response_text,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_build_catalog_extracts_structured_job_details_and_remote(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw.jsonl"
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Backend Engineer",
        "description": (
            "<p>Build resilient services. Apply at recruiting@example.com.</p>"
            "<h2>Requirements</h2><ul>"
            "<li>Python experience</li><li>Distributed systems</li></ul>"
            "<h2>Responsibilities</h2><p>Own backend architecture.</p>"
        ),
        "applicationContact": {"email": "jobs@example.com"},
        "hiringOrganization": {"@type": "Organization", "name": "Example Labs"},
        "jobLocation": {
            "@type": "Place",
            "address": {"addressLocality": "Berlin", "addressCountry": "DE"},
        },
        "jobLocationType": "TELECOMMUTE",
        "applicantLocationRequirements": {"@type": "Country", "name": "Germany"},
        "employmentType": ["FULL_TIME"],
        "datePosted": "2026-07-18",
        "validThrough": "2026-08-18",
    }
    _write_raw(
        raw_path,
        f'<script type="application/ld+json">{json.dumps(posting)}</script>',
    )

    catalog = job_catalog.build_catalog([_signal(raw_path)], max_body_bytes=1_000_000)

    assert catalog["metadata"]["schema_version"] == "structured_job_catalog_v2"
    assert catalog["stats"]["exported_jobs"] == 1
    job = catalog["jobs"][0]
    assert job["title"] == "Backend Engineer"
    assert job["company"] == "Example Labs"
    assert job["location"] == "Berlin, DE"
    assert job["employment_types"] == ["FULL_TIME"]
    assert job["remote_status"] == "remote"
    assert job["remote_confidence"] == "structured"
    assert job["remote_scope"] == ["Germany"]
    assert "[公开联系邮箱见卡片]" in job["description"]
    assert job["public_contact_emails"] == [
        {"email": "jobs@example.com", "source": "结构化职位联系人"},
        {"email": "recruiting@example.com", "source": "职位简介中的投递说明"},
    ]
    assert "Python experience" in job["requirements"][0]
    assert "Own backend architecture" in job["responsibilities"][0]
    assert job["public_url"] == "https://example.com/jobs/backend"


def test_build_catalog_extracts_contextual_company_domain_mailto(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw.jsonl"
    posting = {
        "@type": "JobPosting",
        "title": "Platform Engineer",
        "description": "Build a reliable platform.",
    }
    _write_raw(
        raw_path,
        (
            '<p>To apply, email <a href="mailto:jobs@example.com">'
            "jobs@example.com</a>.</p>"
            f'<script type="application/ld+json">{json.dumps(posting)}</script>'
        ),
    )

    catalog = job_catalog.build_catalog([_signal(raw_path)], max_body_bytes=1_000_000)

    assert catalog["jobs"][0]["public_contact_emails"] == [
        {"email": "jobs@example.com", "source": "公开职位页投递链接"}
    ]


def test_build_catalog_handles_graph_and_deduplicates_repeated_posting(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw.jsonl"
    posting = {
        "@type": ["https://schema.org/JobPosting"],
        "title": "Data Engineer",
        "description": "Build data products.",
    }
    _write_raw(
        raw_path,
        f'<script type="application/ld+json">{json.dumps({"@graph": [posting, posting]})}</script>',
    )

    catalog = job_catalog.build_catalog([_signal(raw_path)], max_body_bytes=1_000_000)

    assert catalog["stats"]["exported_jobs"] == 1
    assert catalog["stats"]["duplicate_jobs"] == 1
    assert catalog["jobs"][0]["remote_status"] == "unknown"


def test_build_catalog_skips_unsafe_extracted_content(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw.jsonl"
    synthetic_token = "ghp_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    _write_raw(
        raw_path,
        f"""
        <script type="application/ld+json">
        {{"@type":"JobPosting","title":"Security Engineer",
         "description":"{synthetic_token}"}}
        </script>
        """,
    )

    catalog = job_catalog.build_catalog([_signal(raw_path)], max_body_bytes=1_000_000)

    assert catalog["jobs"] == []
    assert catalog["stats"]["skipped_unsafe"] == 1


def test_select_references_excludes_truncated_and_non_schema_rows(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw.jsonl"
    _write_raw(raw_path, "<script type='application/ld+json'>{}</script>")
    truncated = _signal(raw_path)
    truncated["provenance"]["body_truncated"] = True  # type: ignore[index]
    wrong_rule = _signal(raw_path)
    wrong_rule["evidence"] = [{"rule": "qualifications"}]

    references, stats = job_catalog.select_references(
        [truncated, wrong_rule], max_body_bytes=1_000_000
    )

    assert references == []
    assert stats["skipped_truncated"] == 1
    assert stats["schema_candidates"] == 1


def test_plain_text_decodes_escaped_markup_and_normalizes_json_list_values() -> None:
    value = (
        "&amp;lt;p&amp;gt;Build &amp;lt;strong&amp;gt;well"
        "&amp;lt;/strong&amp;gt;.&amp;lt;/p&amp;gt;"
    )
    assert job_catalog._plain_text(value, 100) == "Build well."
    assert job_catalog._employment_types('["FULL_TIME"]') == ["FULL_TIME"]


def test_plain_text_directs_public_email_addresses_to_contact_card() -> None:
    value = "Apply via jobs@example.com"

    assert job_catalog._plain_text(value, 100) == "Apply via [公开联系邮箱见卡片]"


def test_description_email_requires_recruiting_context() -> None:
    assert job_catalog._contextual_description_emails("Apply via jobs@example.com") == [
        "jobs@example.com"
    ]
    assert job_catalog._contextual_description_emails(
        "Please send your resume to jobs@example.com."
    ) == ["jobs@example.com"]
    privacy_text = "Privacy questions: privacy@example.com"
    accommodation_text = (
        "If you need an accommodation due to a disability, please email us at "
        "accessibility@example.com."
    )
    alternative_application_text = (
        "If you are unable to use this online application and need an alternative method "
        "to apply, please contact access@example.com for assistance."
    )
    question_text = "If you have questions, please email hiring.manager@example.com."
    sender_text = "Add alerts@example.com as an approved sender for job notifications."
    technical_issue_text = (
        "If you experience technical issues while submitting your application, contact "
        "support@example.com. Unsolicited applications are not accepted."
    )

    assert job_catalog._contextual_description_emails(privacy_text) == []
    assert job_catalog._contextual_description_emails(accommodation_text) == []
    assert job_catalog._contextual_description_emails(alternative_application_text) == []
    assert job_catalog._contextual_description_emails(question_text) == []
    assert job_catalog._contextual_description_emails(sender_text) == []
    assert job_catalog._contextual_description_emails(technical_issue_text) == []


def test_mailto_contact_requires_application_context_and_company_domain() -> None:
    support_html = '<a href="mailto:support@example.com">Support</a>'
    external_html = 'Apply via <a href="mailto:jobs@agency.example">jobs@agency.example</a>'
    careers_feedback_html = (
        "Do you have a question or feedback about Careers? Email our "
        '<a href="mailto:team@example.com">team</a>.'
    )

    assert job_catalog._mailto_contact_emails(support_html, "example.com") == []
    assert job_catalog._mailto_contact_emails(external_html, "example.com") == []
    assert job_catalog._mailto_contact_emails(careers_feedback_html, "example.com") == []


def test_only_structured_application_contact_fields_are_exported() -> None:
    assert job_catalog._public_contact_emails({"email": "support@example.com"}, "", ()) == []
    assert job_catalog._public_contact_emails({"applicationEmail": "jobs@example.com"}, "", ()) == [
        {"email": "jobs@example.com", "source": "结构化职位联系人"}
    ]


def test_plain_text_removes_json_escaped_closing_tags() -> None:
    value = r"Intro <\/path><p>Job body</p>"

    assert job_catalog._plain_text(value, 100) == "Intro Job body"


def test_plain_text_omits_widget_chrome_artifacts() -> None:
    value = 'Jobs <\\/path>","library":"icons" data-widget_type="nav-menu"> Navigation'

    assert job_catalog._plain_text(value, 200) == ""
    encoded_value = 'Jobs &lt;\\/path&gt; data-widget_type="nav-menu"&gt; Navigation'
    assert job_catalog._plain_text(encoded_value, 200) == ""


def test_explicit_description_work_mode_is_available_when_structured_mode_is_absent() -> None:
    mode, confidence, _ = job_catalog._remote_fields(
        {}, "This hybrid work model lets you split your time between the office and home."
    )
    assert (mode, confidence) == ("hybrid", "description_explicit")
