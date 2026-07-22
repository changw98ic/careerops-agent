#!/usr/bin/env python3
"""Select candidate inventory rows not present in merged recruitment triage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_CANDIDATES_PATH = Path(
    "datasets/private/agent-company-seeds/agent_company_candidates_2026-07-18.jsonl"
)
DEFAULT_TRIAGE_PATH = Path(
    "datasets/private/agent-company-seeds/recruitment_homepage_triage_merged_2026-07-18.jsonl"
)
DEFAULT_OUTPUT_PATH = Path(
    "datasets/private/agent-company-seeds/agent_company_candidates_untriaged_2026-07-18.jsonl"
)

REQUIRED_CANDIDATE_FIELDS = (
    "record_id",
    "candidate_entity_name",
    "normalized_name",
    "registrable_domain",
    "representative_website",
    "candidate_type",
    "candidate_state",
    "website_fetch_status",
    "company_terms_status",
    "source_usage_status",
    "source_evidence",
    "personal_data_included",
    "collection_boundary",
    "captured_at",
)

REQUIRED_STRING_FIELDS = tuple(
    field
    for field in REQUIRED_CANDIDATE_FIELDS
    if field not in {"source_evidence", "personal_data_included"}
)


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


def _require_record_id(row: dict[str, Any], path: Path, line_number: int) -> str:
    record_id = row.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        raise ValueError(f"{path}:{line_number}: expected a non-empty record_id")
    return record_id


def _validate_candidate_row(row: dict[str, Any], path: Path, line_number: int) -> str:
    record_id = _require_record_id(row, path, line_number)
    missing = [field for field in REQUIRED_CANDIDATE_FIELDS if field not in row]
    if missing:
        raise ValueError(f"{path}:{line_number}: missing required field(s): {', '.join(missing)}")

    for field in REQUIRED_STRING_FIELDS:
        value = row[field]
        if not isinstance(value, str) or not value:
            raise ValueError(f"{path}:{line_number}: expected non-empty string field {field}")

    if not isinstance(row["source_evidence"], list) or not row["source_evidence"]:
        raise ValueError(f"{path}:{line_number}: expected non-empty source_evidence list")
    if not isinstance(row["personal_data_included"], bool):
        raise ValueError(f"{path}:{line_number}: expected boolean personal_data_included")
    return record_id


def select_untriaged_candidates(
    candidates_path: Path,
    triage_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidate_rows = _load_jsonl(candidates_path)
    triage_rows = _load_jsonl(triage_path)

    candidate_record_ids: set[str] = set()
    triage_record_ids: set[str] = set()
    for line_number, row in enumerate(triage_rows, start=1):
        record_id = _require_record_id(row, triage_path, line_number)
        triage_record_ids.add(record_id)

    untriaged_rows: list[dict[str, Any]] = []
    for line_number, row in enumerate(candidate_rows, start=1):
        record_id = _validate_candidate_row(row, candidates_path, line_number)
        if record_id in candidate_record_ids:
            raise ValueError(
                f"{candidates_path}:{line_number}: duplicate candidate record_id {record_id}"
            )
        candidate_record_ids.add(record_id)
        if record_id not in triage_record_ids:
            untriaged_rows.append(row)

    triage_ids_not_in_candidates = triage_record_ids - candidate_record_ids
    summary = {
        "candidate_input_jsonl": str(candidates_path),
        "triage_input_jsonl": str(triage_path),
        "candidate_rows_read": len(candidate_rows),
        "candidate_unique_record_ids": len(candidate_record_ids),
        "triage_rows_read": len(triage_rows),
        "triage_unique_record_ids": len(triage_record_ids),
        "triaged_candidate_record_ids": len(candidate_record_ids & triage_record_ids),
        "untriaged_candidate_rows": len(untriaged_rows),
        "triage_record_ids_not_in_candidates": len(triage_ids_not_in_candidates),
        "triage_record_ids_not_in_candidates_examples": sorted(triage_ids_not_in_candidates)[:20],
    }
    return untriaged_rows, summary


def write_untriaged_candidates(
    candidates_path: Path,
    triage_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    rows, summary = select_untriaged_candidates(candidates_path, triage_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {**summary, "output_jsonl": str(output_path)}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES_PATH)
    parser.add_argument("--triage", type=Path, default=DEFAULT_TRIAGE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = write_untriaged_candidates(args.candidates, args.triage, args.output)
    except (OSError, ValueError) as error:
        print(f"selection failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
