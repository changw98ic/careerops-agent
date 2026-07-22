from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_audit_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "audit_recruitment_crawl_dataset.py"
    spec = importlib.util.spec_from_file_location("audit_recruitment_crawl_dataset", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, rows: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_crawl_output(
    output_dir: Path, *, raw_rows: list[dict[str, Any]], receipts: list[dict[str, Any]]
) -> None:
    audit = _load_audit_module()
    _write_jsonl(output_dir / audit.RAW_FILENAME, raw_rows)
    _write_jsonl(output_dir / audit.RECEIPT_FILENAME, receipts)


def test_audit_accepts_multiple_inputs_and_recurses_to_output_pairs(tmp_path: Path) -> None:
    audit = _load_audit_module()
    shard = tmp_path / "batch" / "outputs" / "shard-00"
    direct = tmp_path / "direct"
    _write_crawl_output(
        shard,
        raw_rows=[{"canonical_url": "https://alpha.example/careers"}],
        receipts=[
            {
                "canonical_url": "https://alpha.example/careers",
                "capture_status": "captured",
            },
            {
                "canonical_url": "https://beta.example/careers",
                "capture_status": "http_error",
            },
        ],
    )
    _write_crawl_output(
        direct,
        raw_rows=[],
        receipts=[
            {
                "canonical_url": "https://gamma.example/careers",
                "capture_status": "network_error",
            }
        ],
    )

    report = audit.audit_dataset([tmp_path / "batch", direct])

    assert report["ok"] is True
    assert report["output_pair_count"] == 2
    assert report["summary"]["raw_rows"] == 1
    assert report["summary"]["captured_receipts"] == 1
    assert report["summary"]["status_counts"] == {
        "captured": 1,
        "http_error": 1,
        "network_error": 1,
    }
    assert report["summary"]["terminal_status_counts"] == {
        "captured": 1,
        "http_error": 1,
        "network_error": 1,
    }


def test_repair_seed_audit_reports_terminal_statuses_separately(tmp_path: Path) -> None:
    audit = _load_audit_module()
    output_dir = tmp_path / "crawl"
    _write_crawl_output(
        output_dir,
        raw_rows=[{"canonical_url": "https://alpha.example/careers"}],
        receipts=[
            {
                "canonical_url": "https://alpha.example/careers",
                "capture_status": "captured",
            },
            {
                "canonical_url": "https://beta.example/careers",
                "capture_status": "http_error",
            },
            {
                "canonical_url": "https://gamma.example/careers",
                "capture_status": "network_error",
            },
            {
                "canonical_url": "https://delta.example/careers",
                "capture_status": "unsupported_content_type",
            },
        ],
    )
    repair_seeds = tmp_path / "repair.jsonl"
    _write_jsonl(
        repair_seeds,
        [
            {"url": "https://alpha.example/careers"},
            {"canonical_url": "https://beta.example/careers"},
            {"requested_url": "https://gamma.example/careers"},
            {"url": "https://delta.example/careers"},
        ],
    )

    report = audit.audit_dataset([output_dir], repair_seed_path=repair_seeds)

    assert report["ok"] is True
    assert report["repair_seed_audit"]["seed_count"] == 4
    assert report["repair_seed_audit"]["missing_terminal_seed_count"] == 0
    assert report["repair_seed_audit"]["terminal_status_counts"] == {
        "captured": 1,
        "http_error": 1,
        "network_error": 1,
        "unsupported_content_type": 1,
    }
    assert (
        report["repair_seed_audit"]["terminal_semantics"][
            "retry_rounds_0_terminal_capture_status_rule"
        ]
        == "any non-empty capture_status"
    )


def test_audit_flags_count_mismatch_and_missing_repair_terminal(tmp_path: Path) -> None:
    audit = _load_audit_module()
    output_dir = tmp_path / "crawl"
    _write_crawl_output(
        output_dir,
        raw_rows=[
            {"canonical_url": "https://alpha.example/careers"},
            {"canonical_url": "https://beta.example/careers"},
        ],
        receipts=[
            {
                "canonical_url": "https://alpha.example/careers",
                "capture_status": "captured",
            }
        ],
    )
    repair_seeds = tmp_path / "repair.jsonl"
    _write_jsonl(
        repair_seeds,
        [
            {"url": "https://alpha.example/careers"},
            {"url": "https://missing.example/careers"},
        ],
    )

    report = audit.audit_dataset([output_dir], repair_seed_path=repair_seeds)

    assert report["ok"] is False
    assert report["summary"]["error_count"] == 2
    assert report["outputs"][0]["errors"][0]["error"] == (
        "raw row count does not match captured receipt count"
    )
    missing_hashes = report["repair_seed_audit"]["missing_terminal_seed_hashes"]
    assert missing_hashes == [audit._seed_hash("https://missing.example/careers")]


def test_invalid_jsonl_is_reported_without_row_content(tmp_path: Path) -> None:
    audit = _load_audit_module()
    output_dir = tmp_path / "crawl"
    output_dir.mkdir()
    (output_dir / audit.RAW_FILENAME).write_text(
        '{"canonical_url": "https://secret.example/a"}\n',
        encoding="utf-8",
    )
    (output_dir / audit.RECEIPT_FILENAME).write_text(
        '{"canonical_url": "https://secret.example/a", "capture_status": "captured"}\n'
        '{"canonical_url": "https://leaked.example/b", "capture_status": \n',
        encoding="utf-8",
    )

    report = audit.audit_dataset([output_dir])
    rendered = json.dumps(report, sort_keys=True)

    assert report["ok"] is False
    assert "invalid JSON" in report["outputs"][0]["errors"][0]["error"]
    assert "secret.example" not in rendered
    assert "leaked.example" not in rendered


def test_main_writes_report_and_returns_nonzero_for_failed_audit(tmp_path: Path) -> None:
    audit = _load_audit_module()
    output_dir = tmp_path / "crawl"
    _write_crawl_output(
        output_dir,
        raw_rows=[{"canonical_url": "https://alpha.example/careers"}],
        receipts=[],
    )
    report_path = tmp_path / "report.json"

    result = audit.main([str(output_dir), "--output", str(report_path)])

    assert result == 1
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["ok"] is False
    assert written["summary"]["raw_rows"] == 1
