#!/usr/bin/env python3
"""Snapshot and filter priority_sharded_v1 shard outputs.

This script reads only existing shard outputs, copies complete newline-terminated
raw JSONL lines into an owned derived snapshot, then runs the existing page
signal filter and company rollup on that snapshot.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aggregate_recruitment_company_signals as company_rollup
import filter_recruitment_pages_live_v1 as live_filter

DEFAULT_SHARD_ROOT = Path(
    "datasets/raw/recruitment_pages/2026-07-18/priority_sharded_v1/run_tmux/outputs"
)
DEFAULT_OUTPUT_DIR = Path(
    "datasets/derived/recruitment_pages/2026-07-18/priority_sharded_v1_filter_v1"
)

DEFAULT_FILTER_VERSION = "priority_sharded_v1_filter_v1"
SNAPSHOT_FILENAME = "priority_sharded_v1_raw_snapshot.jsonl"
MERGE_MANIFEST_FILENAME = "priority_sharded_v1_snapshot_manifest.json"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_line(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


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


def _iter_shard_dirs(shard_root: Path) -> list[Path]:
    return sorted(path for path in shard_root.glob("shard-*") if path.is_dir())


def _stable_file_sizes(paths: Iterable[Path], wait_seconds: float) -> tuple[bool, dict[str, int]]:
    before = {str(path): path.stat().st_size for path in paths if path.exists()}
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    after = {str(path): path.stat().st_size for path in paths if path.exists()}
    return before == after, after


def _copy_complete_jsonl_lines(
    *,
    source_path: Path,
    output_handle: Any | None = None,
) -> dict[str, Any]:
    complete_lines = 0
    invalid_json_lines = 0
    incomplete_tail_bytes = 0
    bytes_copied = 0
    snapshot_size = source_path.stat().st_size if source_path.exists() else 0
    if not source_path.exists():
        return {
            "bytes_copied": 0,
            "complete_lines": 0,
            "incomplete_tail_bytes": 0,
            "invalid_json_lines": 0,
            "snapshot_size": 0,
        }

    with source_path.open("rb") as handle:
        while handle.tell() < snapshot_size:
            offset = handle.tell()
            line = handle.readline(snapshot_size - offset + 1)
            if not line:
                break
            if handle.tell() > snapshot_size or not line.endswith(b"\n"):
                incomplete_tail_bytes = max(0, snapshot_size - offset)
                break
            complete_lines += 1
            bytes_copied += len(line)
            if output_handle is not None:
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError:
                    invalid_json_lines += 1
                    continue
                if not isinstance(decoded, dict):
                    invalid_json_lines += 1
                    continue
                output_handle.write(_json_line(decoded) + "\n")

    return {
        "bytes_copied": bytes_copied,
        "complete_lines": complete_lines,
        "incomplete_tail_bytes": incomplete_tail_bytes,
        "invalid_json_lines": invalid_json_lines,
        "snapshot_size": snapshot_size,
    }


def build_snapshot(
    *,
    filter_version: str,
    shard_root: Path,
    output_dir: Path,
    stable_wait_seconds: float,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / SNAPSHOT_FILENAME
    manifest_path = output_dir / MERGE_MANIFEST_FILENAME
    temp_snapshot_path = snapshot_path.with_name(f".{snapshot_path.name}.{os.getpid()}.tmp")

    shard_dirs = _iter_shard_dirs(shard_root)
    raw_paths = [path / "recruitment_pages_raw.jsonl" for path in shard_dirs]
    receipt_paths = [path / "recruitment_page_receipts.jsonl" for path in shard_dirs]
    stable, stable_sizes = _stable_file_sizes([*raw_paths, *receipt_paths], stable_wait_seconds)

    shard_stats: list[dict[str, Any]] = []
    total_raw_complete_lines = 0
    total_receipt_complete_lines = 0
    total_raw_bytes_copied = 0
    total_invalid_raw_lines = 0
    try:
        with temp_snapshot_path.open("w", encoding="utf-8") as output_handle:
            for shard_dir, raw_path, receipt_path in zip(
                shard_dirs,
                raw_paths,
                receipt_paths,
                strict=True,
            ):
                raw_stats = _copy_complete_jsonl_lines(
                    source_path=raw_path,
                    output_handle=output_handle,
                )
                receipt_stats = _copy_complete_jsonl_lines(source_path=receipt_path)
                total_raw_complete_lines += int(raw_stats["complete_lines"])
                total_receipt_complete_lines += int(receipt_stats["complete_lines"])
                total_raw_bytes_copied += int(raw_stats["bytes_copied"])
                total_invalid_raw_lines += int(raw_stats["invalid_json_lines"])
                shard_stats.append(
                    {
                        "raw": raw_stats,
                        "receipt": receipt_stats,
                        "shard": shard_dir.name,
                        "shard_dir": str(shard_dir),
                    }
                )
        os.replace(temp_snapshot_path, snapshot_path)
    finally:
        if temp_snapshot_path.exists():
            temp_snapshot_path.unlink()

    manifest = {
        "created_at": _now(),
        "filter_version": filter_version,
        "raw_complete_lines": total_raw_complete_lines,
        "raw_invalid_json_lines_skipped": total_invalid_raw_lines,
        "raw_snapshot": str(snapshot_path),
        "raw_snapshot_bytes": snapshot_path.stat().st_size,
        "receipt_complete_lines": total_receipt_complete_lines,
        "shard_count": len(shard_dirs),
        "shard_root": str(shard_root),
        "shards": shard_stats,
        "stable_sizes": stable,
        "stable_sizes_after_wait": stable_sizes,
        "stable_wait_seconds": stable_wait_seconds,
        "total_source_raw_bytes_copied": total_raw_bytes_copied,
    }
    _write_json_atomic(manifest_path, manifest)
    return manifest


def _rewrite_filter_version(signals_path: Path, *, filter_version: str) -> int:
    if not signals_path.exists():
        return 0
    rows = []
    with signals_path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            if isinstance(row, dict):
                row["filter_version"] = filter_version
                rows.append(row)
    temp_path = signals_path.with_name(f".{signals_path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(_json_line(row) + "\n")
        os.replace(temp_path, signals_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return len(rows)


def run_pipeline(
    *,
    filter_version: str,
    shard_root: Path,
    output_dir: Path,
    stable_wait_seconds: float,
) -> dict[str, Any]:
    manifest = build_snapshot(
        filter_version=filter_version,
        shard_root=shard_root,
        output_dir=output_dir,
        stable_wait_seconds=stable_wait_seconds,
    )
    snapshot_path = Path(str(manifest["raw_snapshot"]))
    filter_summary = live_filter.run_filter(snapshot_path, output_dir)
    signals_path = output_dir / live_filter.SIGNALS_FILENAME
    signal_rows = _rewrite_filter_version(signals_path, filter_version=filter_version)
    rollup_summary = company_rollup.run_rollup(signals_path, output_dir / "company_rollup")
    summary = {
        **filter_summary,
        "company_count": rollup_summary["company_count"],
        "filter_version": filter_version,
        "generated_at": _now(),
        "raw_snapshot": str(snapshot_path),
        "rollup_summary": rollup_summary,
        "shard_snapshot_manifest": str(output_dir / MERGE_MANIFEST_FILENAME),
        "signal_rows_after_filter_version_rewrite": signal_rows,
    }
    _write_json_atomic(output_dir / live_filter.SUMMARY_FILENAME, summary)
    return summary


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-root", type=Path, default=DEFAULT_SHARD_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--filter-version", default=DEFAULT_FILTER_VERSION)
    parser.add_argument("--stable-wait-seconds", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = run_pipeline(
            filter_version=args.filter_version,
            shard_root=args.shard_root,
            output_dir=args.output_dir,
            stable_wait_seconds=args.stable_wait_seconds,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"priority sharded snapshot filter failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
