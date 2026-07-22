from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts/filter_recruitment_pages_live_v1.py"
SPEC = importlib.util.spec_from_file_location("filter_recruitment_pages_live_v1", SCRIPT_PATH)
assert SPEC is not None
filter_live = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = filter_live
assert SPEC.loader is not None
SPEC.loader.exec_module(filter_live)

classify_record = filter_live.classify_record
run_filter = filter_live.run_filter
validate_outputs = filter_live.validate_outputs


def _raw_row(
    response_text: str,
    *,
    final_url: str = "https://example.com/careers/software-engineer",
    content_type: str = "text/html",
    source_record_id: str = "agent-ecosystem-domain--example.com",
) -> dict[str, object]:
    return {
        "body_bytes": len(response_text.encode()),
        "body_sha256": hashlib.sha256(response_text.encode()).hexdigest(),
        "body_truncated": False,
        "canonical_url": final_url,
        "captured_at": "2026-07-18T13:05:18Z",
        "content_type": content_type,
        "depth": 1,
        "discovered_from": "https://example.com/careers",
        "final_url": final_url,
        "http_status": 200,
        "registrable_domain": "example.com",
        "requested_url": final_url,
        "source_assessment": "first_party_career_entry",
        "source_index": 1,
        "source_record_id": source_record_id,
        "source_relationship": "first_party",
        "response_text": response_text,
    }


def test_schema_org_jobposting_is_actual_job_posting() -> None:
    row = _raw_row(
        """
        <html><head>
        <script type="application/ld+json">{"@type":"JobPosting","title":"Agent Engineer"}</script>
        </head><body><main>Apply for this job. Requirements include Python.</main></body></html>
        """
    )

    classification, evidence, score, _links = classify_record(row)

    assert classification == "actual_job_posting"
    assert score == 100
    assert evidence[0].rule == "schema_org_jobposting"


def test_job_url_with_detail_signals_is_actual_job_posting() -> None:
    row = _raw_row(
        """
        <main>
          <h1>Software Engineer</h1>
          <h2>Responsibilities</h2>
          <p>Build product systems.</p>
          <h2>Qualifications</h2>
          <p>Python and distributed systems.</p>
        </main>
        """
    )

    classification, evidence, _score, _links = classify_record(row)

    assert classification == "actual_job_posting"
    assert any(item.rule == "job_url_with_detail_signals" for item in evidence)


def test_json_payload_strings_are_filtered() -> None:
    row = _raw_row(
        json.dumps(
            {
                "title": "Machine Learning Engineer",
                "description": "Job description and responsibilities",
                "apply": "Apply for this job",
            }
        ),
        content_type="application/json",
    )

    classification, evidence, _score, _links = classify_record(row)

    assert classification == "actual_job_posting"
    assert {item.rule for item in evidence} >= {"job_description", "responsibilities"}


def test_technical_job_queue_false_positive_is_no_signal() -> None:
    row = _raw_row(
        "<main>Our platform runs every cron job through a durable job queue.</main>",
        final_url="https://example.com/blog/job-queue",
    )

    classification, evidence, score, links = classify_record(row)

    assert classification == "no_signal"
    assert evidence == []
    assert score == 0
    assert links == []


def test_run_filter_is_append_safe_and_validates_outputs(tmp_path: Path) -> None:
    raw_path = tmp_path / "raw.jsonl"
    rows = [
        _raw_row(
            '<main>We are hiring. View open roles.</main><a href="/careers">Careers</a>',
            final_url="https://example.com/careers",
        ),
        _raw_row(
            "<main>Our platform runs background jobs.</main>",
            final_url="https://example.com/blog/background-jobs",
            source_record_id="agent-ecosystem-domain--example.com--blog",
        ),
    ]
    raw_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    output_dir = tmp_path / "derived"

    first_summary = run_filter(raw_path, output_dir)
    second_summary = run_filter(raw_path, output_dir)
    validation = validate_outputs(output_dir)

    assert first_summary["new_signal_records_appended"] == 1
    assert second_summary["new_signal_records_appended"] == 0
    assert second_summary["duplicate_positive_records_skipped_this_run"] == 1
    assert validation["signal_rows"] == 1
    output_row = json.loads((output_dir / "recruitment_page_signals.jsonl").read_text())
    assert output_row["classification"] == "job_listing"
    assert output_row["provenance"]["raw_line_number"] == 1
    assert "response_text" not in output_row
