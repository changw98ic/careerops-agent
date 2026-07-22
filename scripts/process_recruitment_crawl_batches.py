#!/usr/bin/env python3
"""Incrementally derive recruitment signals from active or completed crawl outputs.

The runner is local-only: it never starts a crawler or makes network requests. It
reads a stable byte snapshot from each raw JSONL file, so a concurrently running
collector can continue appending while only complete records are processed. Each
source gets its own watermark and derived output before all available signals are
merged into one deterministic company rollup.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import aggregate_recruitment_company_signals as company_rollup  # noqa: E402
import filter_recruitment_pages_delta_v1 as delta_filter  # noqa: E402
import filter_recruitment_pages_live_v1 as live_filter  # noqa: E402
import merge_recruitment_page_signals as page_signal_merge  # noqa: E402

RAW_FILENAME = "recruitment_pages_raw.jsonl"
SHARDED_PLAN_FILENAME = "sharded_recruitment_crawl_plan.json"
PROCESSING_MANIFEST_FILENAME = "processing_manifest.json"
PROCESSING_SUMMARY_FILENAME = "processing_summary.json"
PROCESSING_LOCK_FILENAME = ".processing.lock"
PROCESSING_VERSION = "recruitment_crawl_processing_v1"


@dataclass(frozen=True)
class CrawlSource:
    """A raw collector output eligible for incremental processing."""

    source_dir: Path
    raw_path: Path
    origin: str


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _read_json_object(path: Path) -> dict[str, Any]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return decoded


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _resolve_path(path: Path, workspace_root: Path) -> Path:
    return (path if path.is_absolute() else workspace_root / path).resolve()


def _source_output_dir(output_dir: Path, source_dir: Path) -> Path:
    source_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source_dir.name).strip("-") or "crawl"
    source_digest = hashlib.sha256(str(source_dir).encode("utf-8")).hexdigest()[:12]
    return output_dir / "sources" / f"{source_name}-{source_digest}"


def _raw_file_for_source(source_dir: Path) -> Path:
    raw_path = source_dir / RAW_FILENAME
    raw_stat = raw_path.lstat()
    if raw_path.is_symlink():
        raise ValueError(f"crawl raw file must not be a symlink: {raw_path}")
    if not stat.S_ISREG(raw_stat.st_mode):
        raise ValueError(f"crawl raw path is not a regular file: {raw_path}")
    resolved_raw_path = raw_path.resolve()
    if not resolved_raw_path.is_relative_to(source_dir):
        raise ValueError(f"crawl raw file escapes its output directory: {raw_path}")
    return resolved_raw_path


def _direct_source(source_dir: Path, *, origin: str) -> CrawlSource:
    if not source_dir.is_dir():
        raise ValueError(f"crawl output directory does not exist: {source_dir}")
    try:
        raw_path = _raw_file_for_source(source_dir)
    except FileNotFoundError as error:
        raise ValueError(f"crawl output has no {RAW_FILENAME}: {source_dir}") from error
    return CrawlSource(source_dir=source_dir, raw_path=raw_path, origin=origin)


def _sources_from_sharded_batch(
    batch_dir: Path,
    *,
    workspace_root: Path,
) -> tuple[list[CrawlSource], list[dict[str, str]]]:
    plan_path = batch_dir / SHARDED_PLAN_FILENAME
    if not plan_path.is_file():
        raise ValueError(f"sharded batch has no plan file: {plan_path}")
    plan = _read_json_object(plan_path)
    shards = plan.get("shards")
    if not isinstance(shards, list):
        raise ValueError(f"{plan_path}: missing shards list")

    batch_root = batch_dir.resolve()
    sources: list[CrawlSource] = []
    skipped: list[dict[str, str]] = []
    for index, shard in enumerate(shards):
        if not isinstance(shard, dict):
            raise ValueError(f"{plan_path}: shard {index} is not an object")
        output_value = shard.get("output_dir")
        if not isinstance(output_value, str) or not output_value:
            raise ValueError(f"{plan_path}: shard {index} has no output_dir")
        output_dir = _resolve_path(Path(output_value), workspace_root)
        if not output_dir.is_relative_to(batch_root):
            raise ValueError(f"{plan_path}: shard {index} output_dir escapes the batch directory")
        try:
            raw_path = _raw_file_for_source(output_dir)
        except FileNotFoundError:
            skipped.append(
                {
                    "reason": "raw_output_not_created",
                    "source_dir": str(output_dir),
                }
            )
            continue
        sources.append(
            CrawlSource(
                source_dir=output_dir,
                raw_path=raw_path,
                origin=f"sharded_batch:{batch_dir}",
            )
        )
    return sources, skipped


def discover_sources(
    *,
    crawl_outputs: Sequence[Path],
    sharded_batches: Sequence[Path],
    workspace_root: Path,
) -> tuple[list[CrawlSource], list[dict[str, str]]]:
    """Resolve direct and sharded collector outputs without following unsafe plans."""

    sources: dict[Path, CrawlSource] = {}
    skipped: list[dict[str, str]] = []
    for value in crawl_outputs:
        source = _direct_source(_resolve_path(value, workspace_root), origin="crawl_output")
        sources[source.source_dir] = source
    for value in sharded_batches:
        batch_sources, batch_skipped = _sources_from_sharded_batch(
            _resolve_path(value, workspace_root),
            workspace_root=workspace_root,
        )
        skipped.extend(batch_skipped)
        for source in batch_sources:
            sources[source.source_dir] = source
    return [sources[key] for key in sorted(sources)], skipped


@contextmanager
def _processing_lock(output_dir: Path) -> Iterator[None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / PROCESSING_LOCK_FILENAME
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(f"another processing run already owns {output_dir}") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_delta_output(filter_dir: Path) -> dict[str, Any]:
    signals_path = filter_dir / live_filter.SIGNALS_FILENAME
    summary_path = filter_dir / live_filter.SUMMARY_FILENAME
    if not summary_path.is_file():
        raise ValueError(f"delta filter did not produce a summary: {summary_path}")
    summary = _read_json_object(summary_path)
    total_signals = summary.get("total_signal_records_after_run")
    if not isinstance(total_signals, int) or total_signals < 0:
        raise ValueError(f"{summary_path}: missing non-negative total signal count")
    if not signals_path.exists():
        if total_signals != 0:
            raise ValueError(f"{summary_path}: signals are missing despite a non-zero signal count")
        return {"signal_rows": 0, "valid": True}
    return live_filter.validate_outputs(filter_dir)


def _existing_signal_inputs(output_dir: Path) -> list[Path]:
    sources_dir = output_dir / "sources"
    if not sources_dir.is_dir():
        return []
    return sorted(
        path for path in sources_dir.glob(f"*/{live_filter.SIGNALS_FILENAME}") if path.is_file()
    )


def process_sources(
    *,
    sources: Sequence[CrawlSource],
    output_dir: Path,
    skipped_sources: Sequence[dict[str, str]] = (),
) -> dict[str, Any]:
    """Run source-local delta filters, then merge and roll up all retained signals."""

    if not sources:
        raise ValueError("no readable crawl outputs were supplied")

    output_dir = output_dir.resolve()
    if any(
        output_dir.is_relative_to(source.source_dir) or source.source_dir.is_relative_to(output_dir)
        for source in sources
    ):
        raise ValueError("processing output directory must not overlap a crawl output directory")
    with _processing_lock(output_dir):
        processed_sources: list[dict[str, Any]] = []
        for source in sources:
            filter_dir = _source_output_dir(output_dir, source.source_dir)
            filter_summary = delta_filter.run_delta_filter(
                raw_path=source.raw_path,
                output_dir=filter_dir,
                bootstrap_signals=filter_dir / live_filter.SIGNALS_FILENAME,
                bootstrap_summary=filter_dir / live_filter.SUMMARY_FILENAME,
                start_at_beginning=True,
            )
            validation = _validate_delta_output(filter_dir)
            processed_sources.append(
                {
                    "crawl_output": str(source.source_dir),
                    "filter_output": str(filter_dir),
                    "filter_summary": filter_summary,
                    "origin": source.origin,
                    "raw_file": str(source.raw_path),
                    "validation": validation,
                }
            )

        signal_inputs = _existing_signal_inputs(output_dir)
        merged_dir = output_dir / "merged"
        merge_summary = page_signal_merge.run_merge(signal_inputs, merged_dir)
        rollup_dir = output_dir / "company_rollup"
        rollup_summary = company_rollup.run_rollup(
            merged_dir / page_signal_merge.OUTPUT_JSONL,
            rollup_dir,
        )
        completed_at = _now()
        manifest = {
            "created_at": completed_at,
            "local_only": True,
            "outputs": {
                "company_rollup": str(rollup_dir),
                "merged_signals": str(merged_dir / page_signal_merge.OUTPUT_JSONL),
                "summary": str(output_dir / PROCESSING_SUMMARY_FILENAME),
            },
            "processing_version": PROCESSING_VERSION,
            "merged_signal_inputs": [str(path) for path in signal_inputs],
            "source_filter_dirs": [item["filter_output"] for item in processed_sources],
            "source_raw_files": [item["raw_file"] for item in processed_sources],
        }
        summary = {
            "completed_at": completed_at,
            "company_rollup": rollup_summary,
            "local_only": True,
            "merge": merge_summary,
            "processed_sources": processed_sources,
            "processing_version": PROCESSING_VERSION,
            "signal_input_count": len(signal_inputs),
            "skipped_sources": list(skipped_sources),
            "notes": [
                "Only newline-terminated JSONL records within each source byte snapshot are read.",
                "Raw crawl artifacts are never modified by this runner.",
                "This local transformation does not launch crawls or authorize recurring crawling.",
            ],
        }
        _write_json_atomic(output_dir / PROCESSING_MANIFEST_FILENAME, manifest)
        _write_json_atomic(output_dir / PROCESSING_SUMMARY_FILENAME, summary)
    return summary


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--crawl-output",
        action="append",
        default=[],
        type=Path,
        help="Direct collector output directory. May be provided multiple times.",
    )
    parser.add_argument(
        "--sharded-batch",
        action="append",
        default=[],
        type=Path,
        help="Sharded batch directory containing sharded_recruitment_crawl_plan.json.",
    )
    parser.add_argument("--output-dir", required=True, type=Path, help="Derived output directory.")
    parser.add_argument(
        "--workspace-root",
        default=Path.cwd(),
        type=Path,
        help="Base for relative crawl-output and sharded-batch paths (default: current directory).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve eligible sources and print them without writing derived output.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.crawl_output and not args.sharded_batch:
        print("provide at least one --crawl-output or --sharded-batch", file=sys.stderr)
        return 2
    try:
        workspace_root = args.workspace_root.resolve()
        sources, skipped = discover_sources(
            crawl_outputs=args.crawl_output,
            sharded_batches=args.sharded_batch,
            workspace_root=workspace_root,
        )
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "sources": [
                            {
                                "origin": source.origin,
                                "raw_file": str(source.raw_path),
                                "source_dir": str(source.source_dir),
                            }
                            for source in sources
                        ],
                        "skipped_sources": skipped,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        summary = process_sources(
            sources=sources,
            output_dir=_resolve_path(args.output_dir, workspace_root),
            skipped_sources=skipped,
        )
    except (OSError, ValueError) as error:
        print(f"recruitment data processing failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
