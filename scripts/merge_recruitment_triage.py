#!/usr/bin/env python3
"""Merge recruitment homepage triage JSONL files deterministically."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SelectedRow:
    row: dict[str, Any]
    source_path: str
    source_line: int
    input_index: int
    first_seen_order: int


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
            record_id = decoded.get("record_id")
            if not isinstance(record_id, str) or not record_id:
                raise ValueError(f"{path}:{line_number}: expected a non-empty record_id")
            rows.append(decoded)
    return rows


def _score(row: dict[str, Any]) -> float:
    value = row.get("score", 0)
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def merge_triage_rows(input_paths: list[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected_by_record_id: dict[str, SelectedRow] = {}
    source_counter: Counter[str] = Counter()
    selected_source_counter: Counter[str] = Counter()
    classification_counter: Counter[str] = Counter()
    assessment_counter: Counter[str] = Counter()
    duplicate_record_ids: Counter[str] = Counter()
    rows_read = 0
    replacements_by_higher_score = 0
    duplicate_rows_kept_by_existing_score = 0

    for input_index, path in enumerate(input_paths):
        rows = _load_jsonl(path)
        source_counter[str(path)] = len(rows)
        for source_line, row in enumerate(rows, start=1):
            rows_read += 1
            record_id = str(row["record_id"])
            existing = selected_by_record_id.get(record_id)
            if existing is None:
                selected_by_record_id[record_id] = SelectedRow(
                    row=row,
                    source_path=str(path),
                    source_line=source_line,
                    input_index=input_index,
                    first_seen_order=len(selected_by_record_id),
                )
                continue

            duplicate_record_ids[record_id] += 1
            if _score(row) > _score(existing.row):
                selected_by_record_id[record_id] = SelectedRow(
                    row=row,
                    source_path=str(path),
                    source_line=source_line,
                    input_index=input_index,
                    first_seen_order=existing.first_seen_order,
                )
                replacements_by_higher_score += 1
            else:
                duplicate_rows_kept_by_existing_score += 1

    selected_rows = sorted(
        selected_by_record_id.values(),
        key=lambda selected: selected.first_seen_order,
    )
    output_rows = [selected.row for selected in selected_rows]
    for selected in selected_rows:
        row = selected.row
        selected_source_counter[selected.source_path] += 1
        classification_counter[str(row.get("classification", "missing"))] += 1
        assessment_counter[str(row.get("employer_hiring_assessment", "missing"))] += 1

    summary = {
        "input_files": [str(path) for path in input_paths],
        "input_row_counts": dict(source_counter),
        "rows_read": rows_read,
        "output_rows": len(output_rows),
        "duplicate_rows": rows_read - len(output_rows),
        "duplicate_record_ids": len(duplicate_record_ids),
        "replacements_by_higher_score": replacements_by_higher_score,
        "duplicate_rows_kept_by_existing_score": duplicate_rows_kept_by_existing_score,
        "classification_counts": dict(sorted(classification_counter.items())),
        "assessment_counts": dict(sorted(assessment_counter.items())),
        "selected_row_counts_by_source": dict(sorted(selected_source_counter.items())),
        "duplicate_record_id_examples": sorted(duplicate_record_ids)[:20],
    }
    return output_rows, summary


def write_merged_triage(
    input_paths: list[Path],
    output_path: Path,
    summary_path: Path,
) -> dict[str, Any]:
    output_rows, summary = merge_triage_rows(input_paths)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        for row in output_rows:
            output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    summary = {
        **summary,
        "output_jsonl": str(output_path),
        "summary_json": str(summary_path),
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        action="append",
        required=True,
        help="Input triage JSONL path. Pass once per source, in preferred tie-break order.",
    )
    parser.add_argument("--output", type=Path, required=True, help="Merged triage JSONL path.")
    parser.add_argument("--summary", type=Path, required=True, help="Merge summary JSON path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = write_merged_triage(args.input, args.output, args.summary)
    except (OSError, ValueError) as error:
        print(f"merge failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
