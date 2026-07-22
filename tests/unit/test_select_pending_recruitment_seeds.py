from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_selector_module() -> ModuleType:
    script = Path(__file__).parents[2] / "scripts" / "select_pending_recruitment_seeds.py"
    spec = importlib.util.spec_from_file_location("select_pending_recruitment_seeds", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed(url: str, rank: int) -> dict[str, Any]:
    return {
        "canonical_url": url,
        "priority_rank": rank,
        "registrable_domain": "example.com",
        "source_assessment": "first_party_career_entry",
        "source_record_id": "agent-ecosystem-domain--example.com",
        "source_relationship": "first_party",
        "url": url,
    }


def _receipt(url: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "canonical_url": url,
        "capture_status": status,
        "captured_at": "2026-07-18T00:00:00Z",
        "requested_url": url,
        **extra,
    }


def test_select_pending_excludes_captured_and_terminal_but_keeps_storage_cap() -> None:
    selector = _load_selector_module()
    seeds = [
        _seed("https://example.com/careers/captured", 1),
        _seed("https://example.com/careers/invalid", 2),
        _seed("https://example.com/careers/capped", 3),
        _seed("https://example.com/careers/never", 4),
    ]
    receipts = [
        _receipt("https://example.com/careers/invalid", "invalid_url"),
        _receipt("https://example.com/careers/capped", "storage_cap_reached"),
    ]
    raw_rows = [{"canonical_url": "https://example.com/careers/captured"}]

    pending, summary = selector.select_pending_seeds(seeds, receipts, raw_rows)

    assert [row["canonical_url"] for row in pending] == [
        "https://example.com/careers/capped",
        "https://example.com/careers/never",
    ]
    assert [row["pending_reason"] for row in pending] == [
        "storage_cap_reached",
        "never_scheduled",
    ]
    assert summary["excluded_counts"] == {
        "captured_raw_body": 1,
        "terminal_or_exhausted_non_body": 1,
    }


def test_retryable_failures_are_pending_until_exhausted() -> None:
    selector = _load_selector_module()
    retryable = "https://example.com/careers/retryable"
    exhausted = "https://example.com/careers/exhausted"
    seeds = [_seed(retryable, 1), _seed(exhausted, 2)]
    receipts = [
        _receipt(retryable, "network_error"),
        *[_receipt(exhausted, "network_error") for _ in range(5)],
    ]

    pending, summary = selector.select_pending_seeds(seeds, receipts, [], retry_rounds=4)

    assert [row["canonical_url"] for row in pending] == [retryable]
    assert pending[0]["pending_reason"] == "retryable_failure"
    assert summary["terminal_url_count"] == 1
    assert summary["excluded_counts"]["terminal_or_exhausted_non_body"] == 1


def test_read_jsonl_allow_trailing_partial_skips_only_final_invalid_line(tmp_path: Path) -> None:
    selector = _load_selector_module()
    path = tmp_path / "append.jsonl"
    path.write_text('{"ok": 1}\n{"partial": ', encoding="utf-8")

    result = selector.read_jsonl_allow_trailing_partial(path)

    assert list(result.rows) == [{"ok": 1}]
    assert result.skipped_trailing_invalid_line is True
    assert result.skipped_trailing_line_number == 2
    assert result.skipped_invalid_line_count == 1


def test_raw_reader_can_skip_append_corrupted_lines_without_modifying_file(tmp_path: Path) -> None:
    selector = _load_selector_module()
    path = tmp_path / "raw.jsonl"
    path.write_text('{"ok": 1}\n{"bad": \n{"ok": 2}\n', encoding="utf-8")

    result = selector.read_jsonl_allow_trailing_partial(path, allow_invalid_lines=True)

    assert list(result.rows) == [{"ok": 1}, {"ok": 2}]
    assert result.skipped_invalid_line_count == 1
    assert path.read_text(encoding="utf-8") == '{"ok": 1}\n{"bad": \n{"ok": 2}\n'


def test_raw_reader_accepts_json_string_newlines_inside_object(tmp_path: Path) -> None:
    selector = _load_selector_module()
    path = tmp_path / "raw.jsonl"
    path.write_text(
        json.dumps({"canonical_url": "https://example.com/a", "response_text": "a\nb"}) + "\n",
        encoding="utf-8",
    )

    result = selector.read_jsonl_allow_trailing_partial(path, allow_invalid_lines=True)

    assert len(result.rows) == 1
    assert result.rows[0]["response_text"] == "a\nb"
    assert result.skipped_invalid_line_count == 0


def test_discovered_raw_links_are_added_when_they_still_need_bodies() -> None:
    selector = _load_selector_module()
    parent = "https://example.com/careers"
    child = "https://example.com/careers/designer"
    seeds = [_seed(parent, 1)]
    receipts = [_receipt(parent, "captured")]
    raw_rows = [
        {
            "body_sha256": "0" * 64,
            "canonical_url": parent,
            "captured_at": "2026-07-18T00:00:00Z",
            "depth": 0,
            "extracted_links": [child],
            "registrable_domain": "example.com",
            "source_assessment": "first_party_career_entry",
            "source_index": 7,
            "source_record_id": "agent-ecosystem-domain--example.com",
            "source_relationship": "first_party",
        }
    ]

    pending, summary = selector.select_pending_seeds(seeds, receipts, raw_rows)

    assert [row["canonical_url"] for row in pending] == [child]
    assert pending[0]["pending_reason"] == "never_scheduled"
    assert pending[0]["source_kind"] == "priority_v1_discovered_raw"
    assert pending[0]["source_parent_url"] == parent
    assert pending[0]["source_sitemap_url"] == parent
    assert summary["discovered_candidate_count"] == 1


def test_run_selector_writes_deterministic_pending_output(tmp_path: Path) -> None:
    selector = _load_selector_module()
    prioritized = tmp_path / "prioritized.jsonl"
    receipts = tmp_path / "receipts.jsonl"
    raw = tmp_path / "raw.jsonl"
    output = tmp_path / "pending.jsonl"
    prioritized.write_text(
        "\n".join(
            [
                json.dumps(_seed("https://example.com/careers/never", 2)),
                json.dumps(_seed("https://example.com/careers/captured", 1)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    receipts.write_text("", encoding="utf-8")
    raw.write_text(
        json.dumps({"canonical_url": "https://example.com/careers/captured"}) + "\n",
        encoding="utf-8",
    )

    first_summary = selector.run_selector(
        prioritized_input=prioritized,
        receipt_input=receipts,
        raw_input=raw,
        output_path=output,
    )
    first_output = output.read_text(encoding="utf-8")
    second_summary = selector.run_selector(
        prioritized_input=prioritized,
        receipt_input=receipts,
        raw_input=raw,
        output_path=output,
    )
    second_output = output.read_text(encoding="utf-8")

    assert first_output == second_output
    assert first_summary["pending_count"] == 1
    assert second_summary["pending_count"] == 1
    row = json.loads(first_output)
    assert row["pending_rank"] == 1
    assert row["pending_reason"] == "never_scheduled"
