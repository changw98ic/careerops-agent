from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts/merge_recruitment_page_signals.py"
SPEC = importlib.util.spec_from_file_location("merge_recruitment_page_signals", SCRIPT_PATH)
assert SPEC is not None
merge_recruitment_page_signals = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = merge_recruitment_page_signals
assert SPEC.loader is not None
SPEC.loader.exec_module(merge_recruitment_page_signals)

merge_page_signals = merge_recruitment_page_signals.merge_page_signals
run_merge = merge_recruitment_page_signals.run_merge


def _signal(
    *,
    source_record_id: str = "agent-ecosystem-domain--example.com",
    domain: str = "example.com",
    url: str = "https://example.com/careers/",
    classification: str = "career_portal",
    score: int = 70,
    raw_file: str = "raw.jsonl",
    raw_line_number: int = 1,
    body_sha256: str = "abc",
) -> dict[str, object]:
    return {
        "signal_key": f"{source_record_id}:{url}:{body_sha256}",
        "classification": classification,
        "signal_score": score,
        "evidence": [{"category": classification}],
        "provenance": {
            "source_record_id": source_record_id,
            "registrable_domain": domain,
            "canonical_url": url,
            "final_url": url,
            "requested_url": url,
            "raw_file": raw_file,
            "raw_line_number": raw_line_number,
            "body_sha256": body_sha256,
        },
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_merge_dedupes_same_company_url_and_keeps_stronger_row(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _write_jsonl(
        first,
        [
            _signal(url="https://example.com/careers/", classification="career_portal", score=70),
        ],
    )
    _write_jsonl(
        second,
        [
            _signal(
                url="https://example.com/careers",
                classification="actual_job_posting",
                score=100,
                raw_file="raw2.jsonl",
                body_sha256="def",
            ),
        ],
    )

    rows, summary = merge_page_signals([first, second])

    assert len(rows) == 1
    assert rows[0]["classification"] == "actual_job_posting"
    assert summary["total_input_rows"] == 2
    assert summary["merged_signal_rows"] == 1
    assert summary["duplicate_rows_removed"] == 1
    assert summary["replaced_duplicate_rows"] == 1


def test_merge_keeps_same_url_for_different_companies(tmp_path: Path) -> None:
    input_path = tmp_path / "signals.jsonl"
    _write_jsonl(
        input_path,
        [
            _signal(source_record_id="record:a", domain="a.example", url="https://jobs.example/x"),
            _signal(source_record_id="record:b", domain="b.example", url="https://jobs.example/x"),
        ],
    )

    rows, summary = merge_page_signals([input_path])

    assert len(rows) == 2
    assert summary["duplicate_rows_removed"] == 0


def test_merge_uses_raw_provenance_fallback_without_url(tmp_path: Path) -> None:
    input_path = tmp_path / "signals.jsonl"
    row = _signal(raw_file="raw.jsonl", raw_line_number=7, body_sha256="same")
    provenance = row["provenance"]
    assert isinstance(provenance, dict)
    for key in ("canonical_url", "final_url", "requested_url"):
        provenance.pop(key)
    duplicate = json.loads(json.dumps(row))
    duplicate["signal_score"] = 80
    _write_jsonl(input_path, [row, duplicate])

    rows, summary = merge_page_signals([input_path])

    assert len(rows) == 1
    assert rows[0]["signal_score"] == 80
    assert summary["duplicate_rows_removed"] == 1


def test_run_merge_writes_outputs(tmp_path: Path) -> None:
    input_path = tmp_path / "signals.jsonl"
    output_dir = tmp_path / "out"
    _write_jsonl(input_path, [_signal()])

    result = run_merge([input_path], output_dir)

    output_rows = [
        json.loads(line)
        for line in (output_dir / "recruitment_page_signals.jsonl").read_text().splitlines()
    ]
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result["merged_signal_rows"] == 1
    assert output_rows[0]["classification"] == "career_portal"
    assert summary["merged_signal_rows"] == 1
    assert manifest["merge_version"] == "priority_all_v1"
