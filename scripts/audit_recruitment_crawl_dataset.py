#!/usr/bin/env python3
"""Audit recruitment crawl outputs without echoing crawl payloads.

The audit is intentionally read-only and streams JSONL files line by line. It
reports counts, statuses, structural errors, and hashed seed identifiers only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

RAW_FILENAME = "recruitment_pages_raw.jsonl"
RECEIPT_FILENAME = "recruitment_page_receipts.jsonl"
REPAIR_SEED_KEY_FIELDS = ("canonical_url", "url", "requested_url", "source_seed_url")
HASH_LENGTH = 16


@dataclass(frozen=True)
class CrawlOutputPair:
    output_dir: Path
    raw_path: Path
    receipt_path: Path


@dataclass(frozen=True)
class JsonlScanResult:
    rows: int
    blank_lines: int
    invalid_rows: list[dict[str, Any]]


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    netloc = hostname
    if (
        port
        and not (parsed.scheme.lower() == "http" and port == 80)
        and not (parsed.scheme.lower() == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def _seed_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def _safe_error(path: Path, line_number: int, message: str) -> dict[str, Any]:
    return {"path": str(path), "line": line_number, "error": message}


def scan_jsonl_structure(path: Path) -> JsonlScanResult:
    rows = 0
    blank_lines = 0
    invalid_rows: list[dict[str, Any]] = []
    if not path.exists():
        return JsonlScanResult(
            rows=0,
            blank_lines=0,
            invalid_rows=[{"path": str(path), "line": None, "error": "missing file"}],
        )
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                blank_lines += 1
                continue
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError as error:
                invalid_rows.append(_safe_error(path, line_number, f"invalid JSON: {error.msg}"))
                continue
            if not isinstance(decoded, dict):
                invalid_rows.append(_safe_error(path, line_number, "expected a JSON object"))
                continue
            rows += 1
    return JsonlScanResult(rows=rows, blank_lines=blank_lines, invalid_rows=invalid_rows)


def _extract_seed_key(row: dict[str, Any]) -> str:
    for field in REPAIR_SEED_KEY_FIELDS:
        value = row.get(field)
        if isinstance(value, str):
            canonical = canonicalize_url(value)
            if canonical:
                return canonical
    return ""


def _iter_output_pairs(input_paths: Iterable[Path]) -> Iterator[CrawlOutputPair]:
    seen: set[Path] = set()
    for input_path in input_paths:
        path = input_path.expanduser()
        if not path.exists():
            continue
        candidates: list[Path]
        if path.is_dir() and (path / RAW_FILENAME).exists() and (path / RECEIPT_FILENAME).exists():
            candidates = [path]
        elif path.is_dir():
            candidates = [
                child
                for child in path.rglob("*")
                if child.is_dir()
                and (child / RAW_FILENAME).exists()
                and (child / RECEIPT_FILENAME).exists()
            ]
        else:
            candidates = []
        for output_dir in sorted(candidates):
            resolved = output_dir.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield CrawlOutputPair(
                output_dir=output_dir,
                raw_path=output_dir / RAW_FILENAME,
                receipt_path=output_dir / RECEIPT_FILENAME,
            )


def discover_output_pairs(input_paths: Sequence[Path]) -> list[CrawlOutputPair]:
    return list(_iter_output_pairs(input_paths))


def load_repair_seed_hashes(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "provided": False,
            "path": None,
            "seed_count": 0,
            "duplicate_seed_count": 0,
            "missing_key_rows": 0,
            "invalid_rows": [],
            "seed_hashes": set(),
        }
    seed_hashes: set[str] = set()
    duplicate_seed_count = 0
    missing_key_rows = 0
    invalid_rows: list[dict[str, Any]] = []
    if not path.exists():
        return {
            "provided": True,
            "path": str(path),
            "seed_count": 0,
            "duplicate_seed_count": 0,
            "missing_key_rows": 0,
            "invalid_rows": [{"path": str(path), "line": None, "error": "missing file"}],
            "seed_hashes": seed_hashes,
        }
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError as error:
                invalid_rows.append(_safe_error(path, line_number, f"invalid JSON: {error.msg}"))
                continue
            if not isinstance(decoded, dict):
                invalid_rows.append(_safe_error(path, line_number, "expected a JSON object"))
                continue
            seed_key = _extract_seed_key(decoded)
            if not seed_key:
                missing_key_rows += 1
                continue
            seed_id = _seed_hash(seed_key)
            if seed_id in seed_hashes:
                duplicate_seed_count += 1
            seed_hashes.add(seed_id)
    return {
        "provided": True,
        "path": str(path),
        "seed_count": len(seed_hashes),
        "duplicate_seed_count": duplicate_seed_count,
        "missing_key_rows": missing_key_rows,
        "invalid_rows": invalid_rows,
        "seed_hashes": seed_hashes,
    }


def audit_output_pair(pair: CrawlOutputPair) -> dict[str, Any]:
    raw_scan = scan_jsonl_structure(pair.raw_path)
    receipt_scan = scan_jsonl_structure(pair.receipt_path)
    status_counts: Counter[str] = Counter()
    terminal_counts: Counter[str] = Counter()
    captured_receipt_count = 0
    terminal_receipts_by_seed: dict[str, Counter[str]] = defaultdict(Counter)
    receipt_missing_key_rows = 0

    if not receipt_scan.invalid_rows:
        with pair.receipt_path.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                decoded = json.loads(stripped)
                if not isinstance(decoded, dict):
                    continue
                status_value = decoded.get("capture_status")
                status = (
                    status_value if isinstance(status_value, str) and status_value else "<missing>"
                )
                status_counts[status] += 1
                if status == "captured":
                    captured_receipt_count += 1
                # With --retry-rounds 0, a receipt is the final result of the
                # one permitted attempt. Content-type and empty-body outcomes
                # are terminal too, even though they did not yield a raw body.
                if status != "<missing>":
                    terminal_counts[status] += 1
                    seed_key = _extract_seed_key(decoded)
                    if seed_key:
                        terminal_receipts_by_seed[_seed_hash(seed_key)][status] += 1
                    else:
                        receipt_missing_key_rows += 1

    errors = [*raw_scan.invalid_rows, *receipt_scan.invalid_rows]
    if raw_scan.rows != captured_receipt_count:
        errors.append(
            {
                "path": str(pair.output_dir),
                "line": None,
                "error": "raw row count does not match captured receipt count",
                "raw_rows": raw_scan.rows,
                "captured_receipts": captured_receipt_count,
            }
        )

    return {
        "output_dir": str(pair.output_dir),
        "raw": {
            "path": str(pair.raw_path),
            "rows": raw_scan.rows,
            "blank_lines": raw_scan.blank_lines,
        },
        "receipts": {
            "path": str(pair.receipt_path),
            "rows": receipt_scan.rows,
            "blank_lines": receipt_scan.blank_lines,
            "captured_count": captured_receipt_count,
            "status_counts": dict(sorted(status_counts.items())),
            "terminal_status_counts": dict(sorted(terminal_counts.items())),
            "terminal_missing_key_rows": receipt_missing_key_rows,
        },
        "errors": errors,
        "terminal_receipts_by_seed": terminal_receipts_by_seed,
    }


def audit_dataset(
    input_paths: Sequence[Path], repair_seed_path: Path | None = None
) -> dict[str, Any]:
    pairs = discover_output_pairs(input_paths)
    repair_seed_audit = load_repair_seed_hashes(repair_seed_path)
    all_terminal_by_seed: dict[str, Counter[str]] = defaultdict(Counter)
    reports: list[dict[str, Any]] = []
    total_errors = 0
    aggregate_status_counts: Counter[str] = Counter()
    aggregate_terminal_counts: Counter[str] = Counter()
    total_raw_rows = 0
    total_receipt_rows = 0
    total_captured_receipts = 0

    for pair in pairs:
        pair_report = audit_output_pair(pair)
        terminal_by_seed = pair_report.pop("terminal_receipts_by_seed")
        for seed_id, status_counter in terminal_by_seed.items():
            all_terminal_by_seed[seed_id].update(status_counter)
        aggregate_status_counts.update(pair_report["receipts"]["status_counts"])
        aggregate_terminal_counts.update(pair_report["receipts"]["terminal_status_counts"])
        total_raw_rows += int(pair_report["raw"]["rows"])
        total_receipt_rows += int(pair_report["receipts"]["rows"])
        total_captured_receipts += int(pair_report["receipts"]["captured_count"])
        total_errors += len(pair_report["errors"])
        reports.append(pair_report)

    missing_terminal_seed_hashes: list[str] = []
    repair_terminal_status_counts: Counter[str] = Counter()
    seed_hashes = repair_seed_audit.pop("seed_hashes")
    for seed_id in sorted(seed_hashes):
        statuses = all_terminal_by_seed.get(seed_id)
        if not statuses:
            missing_terminal_seed_hashes.append(seed_id)
            continue
        repair_terminal_status_counts.update(statuses)

    repair_seed_audit["terminal_semantics"] = {
        "retry_rounds_0_terminal_capture_status_rule": "any non-empty capture_status",
        "note": (
            "Every non-empty capture_status is a terminal receipt for a retry_rounds=0 "
            "repair pass, including captured, HTTP/network errors, unsupported content, "
            "and empty-body outcomes; counts are reported separately."
        ),
    }
    repair_seed_audit["terminal_status_counts"] = dict(
        sorted(repair_terminal_status_counts.items())
    )
    repair_seed_audit["missing_terminal_seed_hashes"] = missing_terminal_seed_hashes
    repair_seed_audit["missing_terminal_seed_count"] = len(missing_terminal_seed_hashes)
    total_errors += len(repair_seed_audit["invalid_rows"]) + len(missing_terminal_seed_hashes)

    return {
        "ok": total_errors == 0,
        "input_count": len(input_paths),
        "output_pair_count": len(pairs),
        "summary": {
            "raw_rows": total_raw_rows,
            "receipt_rows": total_receipt_rows,
            "captured_receipts": total_captured_receipts,
            "status_counts": dict(sorted(aggregate_status_counts.items())),
            "terminal_status_counts": dict(sorted(aggregate_terminal_counts.items())),
            "error_count": total_errors,
        },
        "outputs": reports,
        "repair_seed_audit": repair_seed_audit,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit recruitment crawl output directories. Inputs may be crawl output "
            "directories or roots that contain shard directories."
        )
    )
    parser.add_argument(
        "inputs", nargs="+", type=Path, help="Output directories or recursive roots."
    )
    parser.add_argument(
        "--repair-seeds",
        type=Path,
        default=None,
        help="Optional repair seed JSONL to verify against terminal receipts.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write JSON report to this path instead of stdout.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = audit_dataset(args.inputs, repair_seed_path=args.repair_seeds)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output is None:
        print(rendered)
    else:
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
