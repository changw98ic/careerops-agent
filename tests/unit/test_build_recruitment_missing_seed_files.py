from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "build_recruitment_missing_seed_files.py"
    spec = importlib.util.spec_from_file_location("build_recruitment_missing_seed_files", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed(url: str, **extra: Any) -> dict[str, Any]:
    return {
        "canonical_url": url,
        "registrable_domain": "example.com",
        "source_assessment": "first_party_career_entry",
        "source_record_id": "agent-ecosystem-domain--example-com",
        "source_relationship": "first_party",
        "url": url,
        **extra,
    }


def _receipt(url: str, *, depth: int = 0, status: str = "network_error") -> dict[str, Any]:
    return {
        "canonical_url": url,
        "capture_status": status,
        "captured_at": "2026-07-19T00:00:00Z",
        "depth": depth,
        "requested_url": url,
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_select_missing_uses_depth0_receipts_only_and_emits_original_rows() -> None:
    module = _load_module()
    missing_url = "https://example.com/careers"
    seen_depth0 = "https://example.com/jobs"
    seen_depth1_only = "https://example.com/open-positions"
    duplicate = "https://example.com/positions"
    rows = [
        _seed(missing_url, custom="kept"),
        _seed(seen_depth0),
        _seed(seen_depth1_only),
        _seed(duplicate, copy=1),
        _seed(duplicate, copy=2),
        _seed("https://external.invalid/careers", registrable_domain="example.com"),
    ]
    receipts = [
        _receipt(seen_depth0, depth=0, status="http_error"),
        _receipt(seen_depth1_only, depth=1, status="captured"),
    ]

    missing, summary = module.select_missing_additional_seeds(rows, receipts)

    assert missing == [
        _seed(missing_url, custom="kept"),
        _seed(seen_depth1_only),
        _seed(duplicate, copy=1),
    ]
    assert summary["additional_seed_input_rows"] == 6
    assert summary["eligible_unique_additional_seed_urls"] == 4
    assert summary["depth0_receipt_unique_urls"] == 1
    assert summary["missing_seed_rows"] == 3
    assert summary["duplicate_missing_seed_rows_skipped"] == 1


def test_build_missing_seed_file_refuses_to_overwrite_and_writes_summary(tmp_path: Path) -> None:
    module = _load_module()
    additional = tmp_path / "additional.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    output = tmp_path / "missing.jsonl"
    summary = tmp_path / "summary.json"
    _write_jsonl(additional, [_seed("https://example.com/careers")])
    _write_jsonl(receipts, [])

    result = module.build_missing_seed_file(
        additional_seeds=additional,
        receipts=[receipts],
        output=output,
        summary_output=summary,
    )

    assert result["missing_seed_rows"] == 1
    assert [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()] == [
        _seed("https://example.com/careers")
    ]
    assert json.loads(summary.read_text(encoding="utf-8"))["missing_seed_rows"] == 1

    try:
        module.build_missing_seed_file(
            additional_seeds=additional,
            receipts=[receipts],
            output=output,
        )
    except FileExistsError as error:
        assert "refusing to overwrite" in str(error)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected output overwrite refusal")


def test_build_missing_seed_file_preflights_summary_before_writing_output(tmp_path: Path) -> None:
    module = _load_module()
    additional = tmp_path / "additional.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    output = tmp_path / "missing.jsonl"
    summary = tmp_path / "summary.json"
    _write_jsonl(additional, [_seed("https://example.com/careers")])
    _write_jsonl(receipts, [])
    summary.write_text("existing summary\n", encoding="utf-8")

    try:
        module.build_missing_seed_file(
            additional_seeds=additional,
            receipts=[receipts],
            output=output,
            summary_output=summary,
        )
    except FileExistsError as error:
        assert "summary.json" in str(error)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected summary overwrite refusal")

    assert not output.exists()
    assert summary.read_text(encoding="utf-8") == "existing summary\n"


def test_cli_returns_error_without_overwriting_existing_output(tmp_path: Path) -> None:
    script = Path(__file__).parents[2] / "scripts" / "build_recruitment_missing_seed_files.py"
    additional = tmp_path / "additional.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    output = tmp_path / "missing.jsonl"
    _write_jsonl(additional, [_seed("https://example.com/careers")])
    _write_jsonl(receipts, [])
    output.write_text("existing\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--additional-seeds",
            str(additional),
            "--receipts",
            str(receipts),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "refusing to overwrite" in completed.stderr
    assert output.read_text(encoding="utf-8") == "existing\n"


def test_build_missing_seed_file_excludes_urls_seen_in_any_receipt_file(tmp_path: Path) -> None:
    module = _load_module()
    additional = tmp_path / "additional.jsonl"
    receipts_a = tmp_path / "receipts-a.jsonl"
    receipts_b = tmp_path / "receipts-b.jsonl"
    output = tmp_path / "missing.jsonl"
    seen_a = "https://example.com/careers"
    seen_b = "https://example.com/jobs"
    depth1_only = "https://example.com/open-positions"
    _write_jsonl(additional, [_seed(seen_a), _seed(seen_b), _seed(depth1_only)])
    _write_jsonl(receipts_a, [_receipt(seen_a, depth=0)])
    _write_jsonl(receipts_b, [_receipt(seen_b, depth=0), _receipt(depth1_only, depth=1)])

    result = module.build_missing_seed_file(
        additional_seeds=additional,
        receipts=[receipts_a, receipts_b],
        output=output,
    )

    assert result["receipt_input_rows"] == 3
    assert result["depth0_receipt_unique_urls"] == 2
    assert result["receipts"] == [str(receipts_a), str(receipts_b)]
    assert [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()] == [
        _seed(depth1_only)
    ]


def test_cli_accepts_repeated_receipts(tmp_path: Path) -> None:
    script = Path(__file__).parents[2] / "scripts" / "build_recruitment_missing_seed_files.py"
    additional = tmp_path / "additional.jsonl"
    receipts_a = tmp_path / "receipts-a.jsonl"
    receipts_b = tmp_path / "receipts-b.jsonl"
    output = tmp_path / "missing.jsonl"
    seen_a = "https://example.com/careers"
    seen_b = "https://example.com/jobs"
    missing = "https://example.com/positions"
    _write_jsonl(additional, [_seed(seen_a), _seed(seen_b), _seed(missing)])
    _write_jsonl(receipts_a, [_receipt(seen_a, depth=0)])
    _write_jsonl(receipts_b, [_receipt(seen_b, depth=0)])

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--additional-seeds",
            str(additional),
            "--receipts",
            str(receipts_a),
            "--receipts",
            str(receipts_b),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    summary = json.loads(completed.stdout)
    assert summary["missing_seed_rows"] == 1
    assert summary["receipts"] == [str(receipts_a), str(receipts_b)]
    assert [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()] == [
        _seed(missing)
    ]
