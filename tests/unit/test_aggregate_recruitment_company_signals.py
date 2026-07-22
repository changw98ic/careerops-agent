from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_rollup_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "aggregate_recruitment_company_signals.py"
    spec = importlib.util.spec_from_file_location("aggregate_recruitment_company_signals", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _signal(
    *,
    record_id: str,
    domain: str,
    classification: str,
    url: str,
    score: int,
    raw_line_number: int,
    evidence_categories: list[str],
) -> dict[str, Any]:
    return {
        "classification": classification,
        "evidence": [
            {"category": category, "location": "visible_text", "rule": category}
            for category in evidence_categories
        ],
        "filter_version": "live_filter_v1",
        "provenance": {
            "body_bytes": 123,
            "body_sha256": f"{raw_line_number:064d}"[-64:],
            "canonical_url": url,
            "captured_at": f"2026-07-18T00:00:{raw_line_number:02d}Z",
            "depth": 0,
            "final_url": url,
            "raw_byte_offset": raw_line_number * 100,
            "raw_file": "raw.jsonl",
            "raw_line_number": raw_line_number,
            "registrable_domain": domain,
            "requested_url": url,
            "source_assessment": "first_party_career_entry",
            "source_record_id": record_id,
            "source_relationship": "first_party",
        },
        "signal_key": f"{record_id}-{raw_line_number}",
        "signal_score": score,
    }


def test_aggregate_company_signals_uses_status_precedence_and_counts_pages() -> None:
    rollup = _load_rollup_module()
    rows = [
        _signal(
            record_id="agent-ecosystem-domain--alpha.com",
            domain="alpha.com",
            classification="career_portal",
            url="https://alpha.com/careers",
            score=70,
            raw_line_number=2,
            evidence_categories=["career_portal", "recruitment_mention"],
        ),
        _signal(
            record_id="agent-ecosystem-domain--alpha.com",
            domain="alpha.com",
            classification="actual_job_posting",
            url="https://alpha.com/jobs/eng",
            score=100,
            raw_line_number=3,
            evidence_categories=["actual_job_posting", "job_listing"],
        ),
        _signal(
            record_id="agent-ecosystem-domain--beta.com",
            domain="beta.com",
            classification="recruitment_mention",
            url="https://beta.com/about",
            score=35,
            raw_line_number=1,
            evidence_categories=["recruitment_mention"],
        ),
    ]

    output = rollup.aggregate_company_signals(rows)

    assert [row["company_key"] for row in output] == [
        "record:agent-ecosystem-domain--alpha.com",
        "record:agent-ecosystem-domain--beta.com",
    ]
    alpha = output[0]
    assert alpha["status"] == "actual_job_posting"
    assert alpha["page_count"] == 2
    assert alpha["classification_counts"] == {
        "actual_job_posting": 1,
        "career_portal": 1,
        "job_listing": 0,
        "recruitment_mention": 0,
    }
    assert alpha["evidence_counts"]["job_listing"] == 1
    assert alpha["has_actual_job_posting"] is True
    assert alpha["has_career_portal"] is True
    assert alpha["best_urls"]["actual_job_posting"] == "https://alpha.com/jobs/eng"
    assert [item["raw_line_number"] for item in alpha["raw_provenance"]] == [2, 3]


def test_run_rollup_writes_deterministic_jsonl_and_summary(tmp_path: Path) -> None:
    rollup = _load_rollup_module()
    input_path = tmp_path / "signals.jsonl"
    output_dir = tmp_path / "rollup"
    rows = [
        _signal(
            record_id="agent-ecosystem-domain--beta.com",
            domain="beta.com",
            classification="job_listing",
            url="https://beta.com/jobs",
            score=85,
            raw_line_number=2,
            evidence_categories=["job_listing"],
        ),
        _signal(
            record_id="agent-ecosystem-domain--alpha.com",
            domain="alpha.com",
            classification="career_portal",
            url="https://alpha.com/careers",
            score=70,
            raw_line_number=1,
            evidence_categories=["career_portal"],
        ),
    ]
    input_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    first_summary = rollup.run_rollup(input_path, output_dir)
    first_output = (output_dir / rollup.OUTPUT_JSONL).read_text(encoding="utf-8")
    second_summary = rollup.run_rollup(input_path, output_dir)
    second_output = (output_dir / rollup.OUTPUT_JSONL).read_text(encoding="utf-8")

    assert first_output == second_output
    assert first_summary["company_count"] == 2
    assert second_summary["company_count"] == 2
    assert first_summary["status_counts"] == {"career_portal": 1, "job_listing": 1}
    output_rows = [json.loads(line) for line in first_output.splitlines()]
    assert [row["status"] for row in output_rows] == ["job_listing", "career_portal"]
