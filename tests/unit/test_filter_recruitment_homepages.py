from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts/filter_recruitment_homepages.py"
SPEC = importlib.util.spec_from_file_location("filter_recruitment_homepages", SCRIPT_PATH)
assert SPEC is not None
filter_recruitment_homepages = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = filter_recruitment_homepages
assert SPEC.loader is not None
SPEC.loader.exec_module(filter_recruitment_homepages)

classify_page = filter_recruitment_homepages.classify_page
extract_page_text = filter_recruitment_homepages.extract_page_text
run_filter = filter_recruitment_homepages.run_filter
triage_row = filter_recruitment_homepages._triage_row


def _triage_html(
    html: str,
    *,
    registrable_domain: str = "example.com",
    final_url: str = "https://example.com/",
) -> dict[str, object]:
    return triage_row(
        {
            "record_id": "agent-ecosystem-domain--example.com",
            "candidate_entity_name": "Example",
            "registrable_domain": registrable_domain,
            "requested_url": f"https://{registrable_domain}/",
            "final_url": final_url,
        },
        {"response_text": html},
    )


def test_extract_page_text_ignores_hidden_code_and_keeps_links() -> None:
    page = extract_page_text(
        """
        <html>
          <head><style>.x{}</style><script>{"text":"We're hiring"}</script></head>
          <body>
            <noscript>Careers</noscript>
            <a href="/careers">Careers</a>
            <p>Build reliable software.</p>
          </body>
        </html>
        """,
        "https://example.com/",
    )

    assert "We're hiring" not in page.visible_text
    assert page.visible_text == "Careers Build reliable software."
    assert page.anchors[0].href == "https://example.com/careers"


def test_open_roles_take_precedence_over_career_portal() -> None:
    page = extract_page_text(
        '<a href="/careers">Careers</a><main>We are hiring. View open roles today.</main>',
        "https://example.com/",
    )

    label, evidence, score = classify_page(page)

    assert label == "open_roles"
    assert score == 100
    assert evidence[0].category == "open_roles"


def test_known_ats_host_is_career_portal() -> None:
    page = extract_page_text(
        '<a href="https://boards.greenhouse.io/acme">Openings</a>',
        "https://example.com/",
    )

    label, evidence, _score = classify_page(page)

    assert label == "career_portal"
    assert any(item.rule == "known_ats_host" for item in evidence)


def test_chinese_open_role_signal() -> None:
    page = extract_page_text(
        "<main>\u52a0\u5165\u6211\u4eec\uff0c\u67e5\u770b\u804c\u4f4d"
        "\u5e76\u7533\u8bf7\u804c\u4f4d\u3002</main>"
    )

    label, evidence, _score = classify_page(page)

    assert label == "open_roles"
    assert any(item.rule == "chinese_open_roles" for item in evidence)


def test_false_positive_technical_job_queue_is_no_evidence() -> None:
    page = extract_page_text(
        "<main>Our agent processes each cron job in a durable job queue.</main>"
    )

    label, evidence, score = classify_page(page)

    assert label == "no_recruitment_evidence"
    assert evidence == []
    assert score == 0


def test_bare_apply_now_product_cta_is_no_evidence() -> None:
    page = extract_page_text("<main>Start a free trial.</main><button>Apply now</button>")

    label, evidence, score = classify_page(page)

    assert label == "no_recruitment_evidence"
    assert evidence == []
    assert score == 0


def test_false_positive_staffing_vendor_is_no_evidence() -> None:
    page = extract_page_text(
        "<main>We are a recruiting agency and staffing vendor for enterprise teams.</main>"
    )

    label, evidence, _score = classify_page(page)

    assert label == "no_recruitment_evidence"
    assert evidence == []


def test_missing_raw_row_has_consistent_no_recruitment_assessment() -> None:
    row = triage_row(
        {
            "record_id": "agent-ecosystem-domain--missing.example",
            "candidate_entity_name": "Missing",
            "registrable_domain": "missing.example",
            "requested_url": "https://missing.example/",
            "final_url": "https://missing.example/",
        },
        None,
    )

    assert row["classification"] == "no_recruitment_evidence"
    assert row["employer_hiring_assessment"] == "no_recruitment_evidence"
    assert row["warnings"] == ["raw_row_not_found"]


def test_run_filter_resolves_raw_file_and_writes_private_outputs(tmp_path: Path) -> None:
    raw_rel = Path("raw/homepages.jsonl")
    raw_path = tmp_path / raw_rel
    raw_path.parent.mkdir(parents=True)
    html = '<a href="/careers">Careers</a><main>Search jobs with us.</main>'
    body_sha = hashlib.sha256(html.encode()).hexdigest()
    raw_row = {
        "record_id": "agent-ecosystem-domain--example.com",
        "candidate_entity_name": "Example",
        "registrable_domain": "example.com",
        "requested_url": "https://example.com/",
        "final_url": "https://example.com/",
        "http_status": "200",
        "content_type": "text/html",
        "body_sha256": body_sha,
        "response_text": html,
    }
    raw_path.write_text(json.dumps(raw_row) + "\n", encoding="utf-8")

    index_path = tmp_path / "index.jsonl"
    index_row = {
        **{key: value for key, value in raw_row.items() if key != "response_text"},
        "http_status": 200,
        "selected_run": "primary",
        "raw_file": str(raw_rel),
    }
    index_path.write_text(json.dumps(index_row) + "\n", encoding="utf-8")
    output_path = tmp_path / "private/triage.jsonl"
    summary_path = tmp_path / "private/summary.json"

    summary = run_filter(index_path, output_path, summary_path, tmp_path)

    output_row = json.loads(output_path.read_text(encoding="utf-8"))
    assert summary["classification_counts"]["open_roles"] == 1
    assert output_row["classification"] == "open_roles"
    assert output_row["raw_file"] == str(raw_rel)
    assert output_row["evidence"][0]["location"] in {"visible_text", "anchor"}
    assert output_row["employer_hiring_assessment"] == "first_party_career_entry"
    assert output_row["links"] == [
        {
            "text": "Careers",
            "href": "https://example.com/careers",
            "relationship": "first_party",
        }
    ]
    assert summary["assessment_counts"]["first_party_career_entry"] == 1
    assert json.loads(summary_path.read_text(encoding="utf-8"))["total_records"] == 1


def test_run_filter_links_include_recruitment_evidence_href_without_career_route(
    tmp_path: Path,
) -> None:
    raw_rel = Path("raw/homepages.jsonl")
    raw_path = tmp_path / raw_rel
    raw_path.parent.mkdir(parents=True)
    html = (
        '<a href="/company#opportunities">We are hiring</a>'
        '<a href="/company#opportunities">Hiring</a>'
        '<a href="/demo">Apply now</a>'
    )
    body_sha = hashlib.sha256(html.encode()).hexdigest()
    raw_row = {
        "record_id": "agent-ecosystem-domain--example.com",
        "candidate_entity_name": "Example",
        "registrable_domain": "example.com",
        "requested_url": "https://example.com/",
        "final_url": "https://example.com/",
        "http_status": "200",
        "content_type": "text/html",
        "body_sha256": body_sha,
        "response_text": html,
    }
    raw_path.write_text(json.dumps(raw_row) + "\n", encoding="utf-8")

    index_path = tmp_path / "index.jsonl"
    index_row = {
        **{key: value for key, value in raw_row.items() if key != "response_text"},
        "http_status": 200,
        "selected_run": "primary",
        "raw_file": str(raw_rel),
    }
    index_path.write_text(json.dumps(index_row) + "\n", encoding="utf-8")
    output_path = tmp_path / "private/triage.jsonl"
    summary_path = tmp_path / "private/summary.json"

    run_filter(index_path, output_path, summary_path, tmp_path)

    output_row = json.loads(output_path.read_text(encoding="utf-8"))
    assert output_row["classification"] == "open_roles"
    assert output_row["employer_hiring_assessment"] == "self_hiring_text_only_needs_verification"
    assert output_row["links"] == [
        {
            "text": "We are hiring",
            "href": "https://example.com/company#opportunities",
            "relationship": "first_party",
        }
    ]


def test_employer_hiring_assessment_requires_self_signal_and_first_party_career_link() -> None:
    row = _triage_html('<main>We are hiring.</main><a href="/careers">Careers</a>')

    assert row["classification"] == "open_roles"
    assert row["employer_hiring_assessment"] == "likely_self_hiring"
    assert row["links"] == [
        {
            "text": "Careers",
            "href": "https://example.com/careers",
            "relationship": "first_party",
        }
    ]


def test_employer_hiring_assessment_marks_job_board_style_first_party_route_as_entry_only() -> None:
    row = _triage_html('<a href="/jobs">Search jobs</a>')

    assert row["classification"] == "open_roles"
    assert row["employer_hiring_assessment"] == "first_party_career_entry"


def test_employer_hiring_assessment_marks_external_job_page_as_ambiguous() -> None:
    row = _triage_html('<a href="https://jobs.example.net/jobs">Search jobs</a>')

    assert row["classification"] == "open_roles"
    assert row["employer_hiring_assessment"] == "third_party_or_ambiguous_signal"
    assert row["links"] == [
        {
            "text": "Search jobs",
            "href": "https://jobs.example.net/jobs",
            "relationship": "external",
        }
    ]


def test_employer_hiring_assessment_marks_external_ats_for_verification() -> None:
    row = _triage_html('<a href="https://boards.greenhouse.io/acme">Careers</a>')

    assert row["classification"] == "career_portal"
    assert row["employer_hiring_assessment"] == "external_ats_needs_verification"
    assert row["links"] == [
        {
            "text": "Careers",
            "href": "https://boards.greenhouse.io/acme",
            "relationship": "known_ats",
        }
    ]


def test_ats_vendor_customer_subdomain_is_not_treated_as_first_party() -> None:
    row = _triage_html(
        '<main>We are hiring.</main><a href="https://huaweicanada.recruitee.com/jobs">Careers</a>',
        registrable_domain="recruitee.com",
        final_url="https://recruitee.com/",
    )

    assert row["employer_hiring_assessment"] == "external_ats_needs_verification"
    assert row["links"] == [
        {
            "text": "Careers",
            "href": "https://huaweicanada.recruitee.com/jobs",
            "relationship": "known_ats",
        }
    ]
