#!/usr/bin/env python3
"""Build append-safe missing recruitment seed JSONL files.

This helper is intentionally offline: it reads additional-seed JSONL plus one or
more existing collector receipt JSONL files and writes the unique additional
seed rows that do not yet have a depth-0 receipt in any receipt input. It does
not read triage inputs, fetch URLs, or modify crawler outputs.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any


def _load_collector_module() -> Any:
    script_path = Path(__file__).with_name("collect_recruitment_pages.py")
    spec = importlib.util.spec_from_file_location("collect_recruitment_pages", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load collector module from {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collector = _load_collector_module()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: expected JSON object")
            rows.append(row)
    return rows


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: expected JSON object")
            yield row


def _depth0_receipt_urls(receipt_rows: Iterable[dict[str, Any]]) -> set[str]:
    seen: set[str] = set()
    for row in receipt_rows:
        if row.get("depth") != 0:
            continue
        canonical_url = row.get("canonical_url")
        if not isinstance(canonical_url, str):
            continue
        canonical = collector.canonicalize_url(canonical_url)
        if canonical:
            seen.add(canonical)
    return seen


def _depth0_receipt_urls_from_paths(receipt_paths: Iterable[Path]) -> tuple[set[str], int]:
    seen: set[str] = set()
    row_count = 0
    for path in receipt_paths:
        for row in _iter_jsonl(path):
            row_count += 1
            if row.get("depth") != 0:
                continue
            canonical_url = row.get("canonical_url")
            if not isinstance(canonical_url, str):
                continue
            canonical = collector.canonicalize_url(canonical_url)
            if canonical:
                seen.add(canonical)
    return seen, row_count


def _select_missing_with_seen_depth0_urls(
    seed_rows: Iterable[dict[str, Any]],
    seen_depth0_urls: set[str],
    *,
    receipt_input_rows: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    seed_rows_list = list(seed_rows)
    selected_requests = collector.select_additional_seed_urls(seed_rows_list, set())
    request_urls = {request.canonical_url for request in selected_requests}
    missing_urls = request_urls - seen_depth0_urls

    emitted_urls: set[str] = set()
    missing_rows: list[dict[str, Any]] = []
    duplicate_selected_rows = 0
    ineligible_or_duplicate_rows = 0
    for row in seed_rows_list:
        href = row.get("url") or row.get("canonical_url")
        canonical = collector.canonicalize_url(href) if isinstance(href, str) else None
        if canonical is None or canonical not in request_urls:
            ineligible_or_duplicate_rows += 1
            continue
        if canonical not in missing_urls:
            continue
        if canonical in emitted_urls:
            duplicate_selected_rows += 1
            continue
        missing_rows.append(row)
        emitted_urls.add(canonical)

    summary = {
        "additional_seed_input_rows": len(seed_rows_list),
        "eligible_unique_additional_seed_urls": len(request_urls),
        "receipt_input_rows": receipt_input_rows,
        "depth0_receipt_unique_urls": len(seen_depth0_urls),
        "missing_seed_rows": len(missing_rows),
        "missing_unique_urls": len(emitted_urls),
        "already_has_depth0_receipt_urls": len(request_urls & seen_depth0_urls),
        "ineligible_or_duplicate_seed_rows": ineligible_or_duplicate_rows,
        "duplicate_missing_seed_rows_skipped": duplicate_selected_rows,
    }
    return missing_rows, summary


def select_missing_additional_seeds(
    seed_rows: Iterable[dict[str, Any]],
    receipt_rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return unique additional seed rows without a depth-0 receipt.

    Selection uses collect_recruitment_pages.select_additional_seed_urls so the
    same eligibility and canonicalization rules are applied as the crawler. The
    emitted rows are the original seed rows, not synthesized collector requests.
    """

    receipt_rows_list = list(receipt_rows)
    seen_depth0_urls = _depth0_receipt_urls(receipt_rows_list)
    return _select_missing_with_seen_depth0_urls(
        seed_rows,
        seen_depth0_urls,
        receipt_input_rows=len(receipt_rows_list),
    )


def _write_jsonl_no_overwrite(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _write_summary_no_overwrite(path: Path, summary: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing summary: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _path_is_reserved(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _validate_new_output_paths(output: Path, summary_output: Path | None) -> None:
    """Reject all conflicting targets before creating either output artifact."""

    if summary_output is not None and output.resolve() == summary_output.resolve():
        raise ValueError("output and summary output paths must differ")
    targets = [output]
    if summary_output is not None:
        targets.append(summary_output)
    conflicts = [str(path) for path in targets if _path_is_reserved(path)]
    if conflicts:
        raise FileExistsError(
            "refusing to overwrite existing output target(s): " + ", ".join(conflicts)
        )


def build_missing_seed_file(
    *,
    additional_seeds: Path,
    receipts: Sequence[Path],
    output: Path,
    summary_output: Path | None = None,
) -> dict[str, Any]:
    _validate_new_output_paths(output, summary_output)
    seed_rows = _read_jsonl(additional_seeds)
    receipt_paths = list(receipts)
    seen_depth0_urls, receipt_input_rows = _depth0_receipt_urls_from_paths(receipt_paths)
    missing_rows, summary = _select_missing_with_seen_depth0_urls(
        seed_rows,
        seen_depth0_urls,
        receipt_input_rows=receipt_input_rows,
    )
    summary = {
        **summary,
        "additional_seeds": str(additional_seeds),
        "receipts": [str(path) for path in receipt_paths],
        "output": str(output),
        "summary_output": str(summary_output) if summary_output is not None else None,
    }
    _write_jsonl_no_overwrite(output, missing_rows)
    if summary_output is not None:
        _write_summary_no_overwrite(summary_output, summary)
    return summary


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--additional-seeds", type=Path, required=True)
    parser.add_argument(
        "--receipts",
        type=Path,
        action="append",
        required=True,
        help="Collector receipt JSONL. May be provided multiple times.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = build_missing_seed_file(
            additional_seeds=args.additional_seeds,
            receipts=args.receipts,
            output=args.output,
            summary_output=args.summary_output,
        )
    except (FileExistsError, OSError, ValueError, RuntimeError) as error:
        print(f"build missing recruitment seed file failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
