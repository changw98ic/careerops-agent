#!/usr/bin/env python3
"""Incrementally filter appended depth3 recruitment page captures.

This is a thin tail-reader around ``filter_recruitment_pages_live_v1``. It keeps
an output-local byte-offset state so later runs can continue without rescanning
the large raw JSONL file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aggregate_recruitment_company_signals as company_rollup
import filter_recruitment_pages_live_v1 as live_filter

DEFAULT_RAW_PATH = live_filter.DEFAULT_RAW_PATH
DEFAULT_OUTPUT_DIR = Path("datasets/derived/recruitment_pages/2026-07-18/live_filter_delta_v1")
DEFAULT_BOOTSTRAP_SIGNALS = Path(
    "datasets/derived/recruitment_pages/2026-07-18/live_filter_v1/recruitment_page_signals.jsonl"
)
DEFAULT_BOOTSTRAP_SUMMARY = Path(
    "datasets/derived/recruitment_pages/2026-07-18/live_filter_v1/summary.json"
)

FILTER_VERSION = "live_filter_delta_v1"
STATE_FILENAME = "incremental_state.json"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"{path}: expected JSON object")
    return decoded


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


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    state = _read_json(path)
    if not isinstance(state.get("next_byte_offset"), int):
        raise ValueError(f"{path}: missing integer next_byte_offset")
    if not isinstance(state.get("next_line_number"), int):
        raise ValueError(f"{path}: missing integer next_line_number")
    return state


def _bootstrap_from_live_filter(
    raw_path: Path,
    signals_path: Path,
    summary_path: Path,
) -> dict[str, Any]:
    if not signals_path.exists():
        raise FileNotFoundError(f"bootstrap signals not found: {signals_path}")
    if not summary_path.exists():
        raise FileNotFoundError(f"bootstrap summary not found: {summary_path}")

    summary = _read_json(summary_path)
    scanned = summary.get("total_raw_records_scanned")
    if not isinstance(scanned, int) or scanned < 0:
        raise ValueError(f"{summary_path}: missing integer total_raw_records_scanned")

    last_row: dict[str, Any] | None = None
    for _line_number, row in live_filter.iter_jsonl(signals_path):
        provenance = row.get("provenance")
        if not isinstance(provenance, dict):
            continue
        raw_file = provenance.get("raw_file")
        raw_line_number = provenance.get("raw_line_number")
        raw_byte_offset = provenance.get("raw_byte_offset")
        if raw_file != str(raw_path) or not isinstance(raw_line_number, int):
            continue
        if not isinstance(raw_byte_offset, int):
            continue
        if raw_line_number == scanned:
            last_row = row

    if last_row is None:
        next_byte_offset = _byte_offset_after_complete_lines(raw_path, scanned)
        return {
            "bootstrap_mode": "summary_line_count",
            "bootstrap_source": str(summary_path),
            "next_byte_offset": next_byte_offset,
            "next_line_number": scanned + 1,
        }

    provenance = last_row["provenance"]
    offset = int(provenance["raw_byte_offset"])
    with raw_path.open("rb") as handle:
        handle.seek(offset)
        line = handle.readline()
        if not line.endswith(b"\n"):
            raise ValueError(f"{raw_path}: bootstrap endpoint line is incomplete at byte {offset}")
        return {
            "bootstrap_mode": "live_filter_v1_endpoint",
            "bootstrap_source": str(signals_path),
            "next_byte_offset": handle.tell(),
            "next_line_number": scanned + 1,
        }


def _byte_offset_after_complete_lines(raw_path: Path, line_count: int) -> int:
    if line_count < 0:
        raise ValueError("line_count must be non-negative")
    with raw_path.open("rb") as handle:
        for line_number in range(1, line_count + 1):
            line = handle.readline()
            if not line:
                raise ValueError(f"{raw_path}: ended before bootstrap line {line_number}")
            if not line.endswith(b"\n"):
                raise ValueError(f"{raw_path}: bootstrap line {line_number} is incomplete")
        return handle.tell()


def _initial_state(
    *,
    raw_path: Path,
    state_path: Path,
    bootstrap_signals: Path,
    bootstrap_summary: Path,
    start_at_beginning: bool,
) -> dict[str, Any]:
    existing = _load_state(state_path)
    if existing is not None:
        return existing
    stat = raw_path.stat()
    if start_at_beginning:
        return {
            "bootstrap_mode": "start_at_beginning",
            "created_at": _now(),
            "filter_version": FILTER_VERSION,
            "last_completed_at": None,
            "next_byte_offset": 0,
            "next_line_number": 1,
            "raw_device": stat.st_dev,
            "raw_file": str(raw_path),
            "raw_inode": stat.st_ino,
        }
    bootstrap = _bootstrap_from_live_filter(raw_path, bootstrap_signals, bootstrap_summary)
    return {
        "created_at": _now(),
        "filter_version": FILTER_VERSION,
        "raw_file": str(raw_path),
        "raw_inode": stat.st_ino,
        "raw_device": stat.st_dev,
        "last_completed_at": None,
        **bootstrap,
    }


def run_delta_filter(
    *,
    raw_path: Path,
    output_dir: Path,
    bootstrap_signals: Path,
    bootstrap_summary: Path,
    start_at_beginning: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals_path = output_dir / live_filter.SIGNALS_FILENAME
    summary_path = output_dir / live_filter.SUMMARY_FILENAME
    representative_path = output_dir / live_filter.REPRESENTATIVE_FILENAME
    manifest_path = output_dir / live_filter.MANIFEST_FILENAME
    state_path = output_dir / STATE_FILENAME
    rollup_dir = output_dir / "company_rollup"

    state = _initial_state(
        raw_path=raw_path,
        state_path=state_path,
        bootstrap_signals=bootstrap_signals,
        bootstrap_summary=bootstrap_summary,
        start_at_beginning=start_at_beginning,
    )
    stat = raw_path.stat()
    if state.get("raw_file") != str(raw_path):
        raise ValueError(f"{state_path}: state raw_file does not match {raw_path}")
    if state.get("raw_inode") not in {None, stat.st_ino}:
        raise ValueError(f"{state_path}: raw file inode changed; refusing to continue")

    start_offset = int(state["next_byte_offset"])
    start_line_number = int(state["next_line_number"])
    if start_offset > stat.st_size:
        raise ValueError(f"{state_path}: next_byte_offset is beyond raw file size")

    snapshot_size = stat.st_size
    existing_keys = live_filter.load_existing_signal_keys(signals_path)
    seen_keys = set(existing_keys)
    scanned = 0
    invalid_json_lines = 0
    appended_rows: list[dict[str, Any]] = []
    duplicate_positive_records = 0
    classification_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    http_status_counts: Counter[str] = Counter()
    source_assessment_counts: Counter[str] = Counter()
    representative: dict[str, list[dict[str, Any]]] = defaultdict(list)
    next_offset = start_offset
    next_line_number = start_line_number
    stopped_at_partial_line = False

    with raw_path.open("rb") as raw_handle:
        raw_handle.seek(start_offset)
        while raw_handle.tell() < snapshot_size:
            raw_byte_offset = raw_handle.tell()
            limit = snapshot_size - raw_byte_offset
            line = raw_handle.readline(limit + 1)
            if not line:
                break
            if raw_handle.tell() > snapshot_size or not line.endswith(b"\n"):
                stopped_at_partial_line = True
                raw_handle.seek(raw_byte_offset)
                break

            scanned += 1
            current_line_number = next_line_number
            next_line_number += 1
            next_offset = raw_handle.tell()
            stripped = line.strip()
            if not stripped:
                status_counts["blank_line"] += 1
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                invalid_json_lines += 1
                status_counts["invalid_json"] += 1
                continue
            if not isinstance(row, dict):
                invalid_json_lines += 1
                status_counts["non_object_json"] += 1
                continue

            classification, evidence, score, links = live_filter.classify_record(row)
            classification_counts[classification] += 1
            http_status_counts[str(row.get("http_status"))] += 1
            source_assessment_counts[str(row.get("source_assessment"))] += 1

            if classification == "no_signal":
                status_counts["no_signal"] += 1
                continue

            status_counts["positive_signal"] += 1
            derived = live_filter.derived_row(
                row,
                raw_path=raw_path,
                raw_line_number=current_line_number,
                raw_byte_offset=raw_byte_offset,
                classification=classification,
                evidence=evidence,
                score=score,
                links=links,
            )
            derived["filter_version"] = FILTER_VERSION
            key = derived["signal_key"]
            if key in seen_keys:
                duplicate_positive_records += 1
                continue
            seen_keys.add(key)
            appended_rows.append(derived)

            bucket = representative[classification]
            if len(bucket) < live_filter.REPRESENTATIVE_PER_CLASS:
                bucket.append(
                    {
                        "classification": classification,
                        "evidence": derived["evidence"],
                        "links": links,
                        "provenance": derived["provenance"],
                        "signal_score": score,
                    }
                )

    _append_jsonl(signals_path, appended_rows)
    rollup_summary = (
        company_rollup.run_rollup(signals_path, rollup_dir)
        if signals_path.exists()
        else {
            "company_count": 0,
            "input_path": str(signals_path),
            "output_jsonl": company_rollup.OUTPUT_JSONL,
            "rollup_version": company_rollup.ROLLUP_VERSION,
            "status_counts": {},
            "total_page_signals": 0,
        }
    )

    completed_at = _now()
    new_state = {
        **state,
        "last_completed_at": completed_at,
        "last_run": {
            "bytes_processed": next_offset - start_offset,
            "completed_at": completed_at,
            "duplicate_positive_records_skipped_this_run": duplicate_positive_records,
            "invalid_json_lines": invalid_json_lines,
            "new_signal_records_appended": len(appended_rows),
            "raw_snapshot_size": snapshot_size,
            "started_at_byte_offset": start_offset,
            "started_at_line_number": start_line_number,
            "stopped_at_partial_line": stopped_at_partial_line,
            "total_raw_records_scanned": scanned,
        },
        "next_byte_offset": next_offset,
        "next_line_number": next_line_number,
        "raw_device": stat.st_dev,
        "raw_file": str(raw_path),
        "raw_inode": stat.st_ino,
    }
    _write_json_atomic(state_path, new_state)

    total_signal_records = (
        sum(1 for _line_number, _row in live_filter.iter_jsonl(signals_path))
        if signals_path.exists()
        else 0
    )
    summary = {
        "classification_counts": dict(sorted(classification_counts.items())),
        "company_count": rollup_summary["company_count"],
        "duplicate_positive_records_skipped_this_run": duplicate_positive_records,
        "existing_signal_records_before_run": len(existing_keys),
        "filter_version": FILTER_VERSION,
        "generated_at": completed_at,
        "http_status_counts": dict(sorted(http_status_counts.items())),
        "invalid_json_lines": invalid_json_lines,
        "new_signal_records_appended": len(appended_rows),
        "notes": [
            (
                "Incremental run reads only complete raw JSONL lines at or after "
                "state.next_byte_offset."
            ),
            "Raw captures and shared collector scripts are not modified.",
        ],
        "output_dir": str(output_dir),
        "positive_signal_records_seen_this_run": status_counts["positive_signal"],
        "raw_file": str(raw_path),
        "raw_snapshot_size": snapshot_size,
        "signals_file": str(signals_path),
        "source_assessment_counts": dict(sorted(source_assessment_counts.items())),
        "state_file": str(state_path),
        "status_counts": dict(sorted(status_counts.items())),
        "stopped_at_partial_line": stopped_at_partial_line,
        "total_bytes_processed": next_offset - start_offset,
        "total_raw_records_scanned": scanned,
        "total_signal_records_after_run": total_signal_records,
        "watermark": {
            "next_byte_offset": next_offset,
            "next_line_number": next_line_number,
            "started_at_byte_offset": start_offset,
            "started_at_line_number": start_line_number,
        },
    }
    _write_json_atomic(summary_path, summary)
    _write_json_atomic(representative_path, dict(sorted(representative.items())))
    _write_json_atomic(
        manifest_path,
        {
            "created_at": completed_at,
            "filter_version": FILTER_VERSION,
            "idempotency_key": "signal_key",
            "outputs": {
                "company_rollup": str(rollup_dir),
                "representative_signals": str(representative_path),
                "signals": str(signals_path),
                "state": str(state_path),
                "summary": str(summary_path),
            },
            "raw_file": str(raw_path),
        },
    )
    return summary


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW_PATH, help="Raw JSONL path.")
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Delta output directory."
    )
    parser.add_argument(
        "--bootstrap-signals",
        type=Path,
        default=DEFAULT_BOOTSTRAP_SIGNALS,
        help="Existing live-filter signals used once to initialize the byte watermark.",
    )
    parser.add_argument(
        "--bootstrap-summary",
        type=Path,
        default=DEFAULT_BOOTSTRAP_SUMMARY,
        help="Existing live-filter summary used once to initialize the line watermark.",
    )
    parser.add_argument(
        "--start-at-beginning",
        action="store_true",
        help=(
            "Initialize a new output state at byte 0 instead of bootstrapping from "
            "an existing live-filter run. Existing state always takes precedence."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = run_delta_filter(
            raw_path=args.raw,
            output_dir=args.output_dir,
            bootstrap_signals=args.bootstrap_signals,
            bootstrap_summary=args.bootstrap_summary,
            start_at_beginning=args.start_at_beginning,
        )
    except (OSError, ValueError) as error:
        print(f"delta filter failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
