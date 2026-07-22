#!/usr/bin/env python3
"""Merge recruitment page signal JSONL files with URL/provenance dedupe."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

OUTPUT_JSONL = "recruitment_page_signals.jsonl"
SUMMARY_JSON = "summary.json"
MANIFEST_JSON = "manifest.json"
MERGE_VERSION = "priority_all_v1"
DEDUPE_STRATEGY = "company/source plus canonical/final/requested URL; raw provenance fallback"
CLASSIFICATION_PRECEDENCE = (
    "actual_job_posting",
    "job_listing",
    "career_portal",
    "recruitment_mention",
)
CLASSIFICATION_RANK = {
    classification: len(CLASSIFICATION_PRECEDENCE) - index
    for index, classification in enumerate(CLASSIFICATION_PRECEDENCE)
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSONL: {error.msg}") from error
            if not isinstance(decoded, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            rows.append(decoded)
    return rows


def _write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _write_json_atomic(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _provenance(row: dict[str, Any]) -> dict[str, Any]:
    provenance = row.get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def _text_field(row: dict[str, Any], provenance: dict[str, Any], key: str) -> str:
    value = provenance.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    value = row.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _dedupe_key(row: dict[str, Any]) -> tuple[str, str, str]:
    provenance = _provenance(row)
    source = _text_field(row, provenance, "source_record_id")
    if not source:
        source = _text_field(row, provenance, "registrable_domain")
    source = source.lower()
    url = (
        _text_field(row, provenance, "canonical_url")
        or _text_field(row, provenance, "final_url")
        or _text_field(row, provenance, "requested_url")
    ).rstrip("/")
    if url:
        return ("url", source, url.lower())
    raw_file = _text_field(row, provenance, "raw_file")
    raw_line = provenance.get("raw_line_number")
    body_sha = _text_field(row, provenance, "body_sha256")
    return ("raw", raw_file, f"{raw_line}:{body_sha}")


def _row_rank(row: dict[str, Any]) -> tuple[int, int, int]:
    classification = row.get("classification")
    score = row.get("signal_score")
    evidence = row.get("evidence")
    return (
        CLASSIFICATION_RANK.get(classification, 0) if isinstance(classification, str) else 0,
        score if isinstance(score, int) else -1,
        len(evidence) if isinstance(evidence, list) else 0,
    )


def merge_page_signals(input_paths: Sequence[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    source_counts: Counter[str] = Counter()
    duplicate_count = 0
    replaced_duplicate_count = 0
    total_input_rows = 0

    for input_path in input_paths:
        for row in _load_jsonl(input_path):
            total_input_rows += 1
            source_counts[str(input_path)] += 1
            key = _dedupe_key(row)
            existing = rows_by_key.get(key)
            if existing is None:
                rows_by_key[key] = row
                continue
            duplicate_count += 1
            if _row_rank(row) > _row_rank(existing):
                rows_by_key[key] = row
                replaced_duplicate_count += 1

    merged_rows = [rows_by_key[key] for key in sorted(rows_by_key)]
    classification_counts = Counter(
        str(row.get("classification")) for row in merged_rows if row.get("classification")
    )
    summary = {
        "merge_version": MERGE_VERSION,
        "input_paths": [str(path) for path in input_paths],
        "input_row_counts": dict(sorted(source_counts.items())),
        "total_input_rows": total_input_rows,
        "merged_signal_rows": len(merged_rows),
        "duplicate_rows_removed": duplicate_count,
        "replaced_duplicate_rows": replaced_duplicate_count,
        "dedupe_strategy": DEDUPE_STRATEGY,
        "classification_counts": dict(sorted(classification_counts.items())),
    }
    return merged_rows, summary


def run_merge(input_paths: Sequence[Path], output_dir: Path) -> dict[str, Any]:
    merged_rows, summary = merge_page_signals(input_paths)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / OUTPUT_JSONL
    summary_path = output_dir / SUMMARY_JSON
    manifest_path = output_dir / MANIFEST_JSON
    _write_jsonl_atomic(output_path, merged_rows)
    _write_json_atomic(summary_path, summary)
    _write_json_atomic(
        manifest_path,
        {
            "merge_version": MERGE_VERSION,
            "inputs": [str(path) for path in input_paths],
            "outputs": {
                "signals": str(output_path),
                "summary": str(summary_path),
            },
            "dedupe_key": DEDUPE_STRATEGY,
        },
    )
    return {**summary, "output_path": str(output_path)}


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    result = run_merge(args.input, args.output_dir)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
