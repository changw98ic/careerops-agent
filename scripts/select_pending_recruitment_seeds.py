#!/usr/bin/env python3
"""Select prioritized recruitment seeds that still need raw bodies."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

OUTPUT_FILENAME = "pending_recruitment_seeds_priority_v2_2026-07-18.jsonl"
SELECTOR_VERSION = "pending_recruitment_seeds_v1"
ATS_HOST_PATTERNS = (
    "greenhouse.io",
    "job-boards.greenhouse.io",
    "boards.greenhouse.io",
    "lever.co",
    "jobs.lever.co",
    "ashbyhq.com",
    "jobs.ashbyhq.com",
    "workable.com",
    "smartrecruiters.com",
    "myworkdayjobs.com",
    "workdayjobs.com",
    "recruitee.com",
    "bamboohr.com",
    "jobvite.com",
    "icims.com",
    "teamtailor.com",
)
TERMINAL_NON_BODY_STATUSES = {
    "invalid_url",
    "unsupported_content_type",
    "empty_text",
    "worker_error",
}
RETAINABLE_STATUSES = {"storage_cap_reached", "aggregate_storage_cap_reached"}


@dataclass(frozen=True)
class JsonlReadResult:
    rows: tuple[dict[str, Any], ...]
    skipped_trailing_invalid_line: bool = False
    skipped_trailing_line_number: int | None = None
    skipped_invalid_line_count: int = 0


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


def read_jsonl_allow_trailing_partial(
    path: Path, *, allow_invalid_lines: bool = False
) -> JsonlReadResult:
    if not path.exists():
        return JsonlReadResult(rows=())
    if allow_invalid_lines:
        return _read_jsonl_with_repair(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: list[dict[str, Any]] = []
    skipped_invalid_line_count = 0
    for index, line in enumerate(lines):
        line_number = index + 1
        stripped = line.strip()
        if not stripped:
            continue
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError as error:
            if allow_invalid_lines:
                skipped_invalid_line_count += 1
                continue
            if index == len(lines) - 1:
                return JsonlReadResult(
                    rows=tuple(rows),
                    skipped_trailing_invalid_line=True,
                    skipped_trailing_line_number=line_number,
                    skipped_invalid_line_count=1,
                )
            raise ValueError(f"{path}:{line_number}: invalid JSONL: {error.msg}") from error
        if not isinstance(decoded, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        rows.append(decoded)
    return JsonlReadResult(rows=tuple(rows), skipped_invalid_line_count=skipped_invalid_line_count)


def _read_jsonl_with_repair(path: Path) -> JsonlReadResult:
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    rows: list[dict[str, Any]] = []
    skipped_invalid_line_count = 0
    position = 0
    length = len(text)
    while position < length:
        while position < length and text[position].isspace():
            position += 1
        if position >= length:
            break
        try:
            decoded, end = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            skipped_invalid_line_count += 1
            next_line = text.find("\n", position)
            if next_line == -1:
                break
            position = next_line + 1
            continue
        if not isinstance(decoded, dict):
            skipped_invalid_line_count += 1
        else:
            rows.append(decoded)
        position = end
    return JsonlReadResult(rows=tuple(rows), skipped_invalid_line_count=skipped_invalid_line_count)


def _row_url(row: dict[str, Any]) -> str:
    for key in ("url", "canonical_url", "requested_url"):
        value = row.get(key)
        if isinstance(value, str):
            canonical = canonicalize_url(value)
            if canonical:
                return canonical
    return ""


def _hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").rstrip(".").lower()


def _host_matches_domain(hostname: str, domain: str) -> bool:
    host = hostname.rstrip(".").lower()
    normalized_domain = domain.rstrip(".").lower()
    return bool(host and normalized_domain) and (
        host == normalized_domain or host.endswith(f".{normalized_domain}")
    )


def _is_known_ats_url(url: str) -> bool:
    hostname = _hostname(url)
    return any(hostname == host or hostname.endswith(f".{host}") for host in ATS_HOST_PATTERNS)


def _link_relationship(url: str, registrable_domain: str, fallback: object) -> str:
    if _host_matches_domain(_hostname(url), registrable_domain):
        return "first_party"
    if _is_known_ats_url(url):
        return "known_ats"
    return fallback if isinstance(fallback, str) and fallback else "first_party"


def _is_retryable_http_status(status_code: object) -> bool:
    return isinstance(status_code, int) and (status_code == 429 or 500 <= status_code <= 599)


def _is_retryable_failure(row: dict[str, Any]) -> bool:
    status = row.get("capture_status")
    return status == "network_error" or (
        status == "http_error" and _is_retryable_http_status(row.get("http_status"))
    )


def _is_terminal_non_body(row: dict[str, Any], retry_attempts: int, retry_rounds: int) -> bool:
    status = row.get("capture_status")
    if status in RETAINABLE_STATUSES:
        return False
    if status in TERMINAL_NON_BODY_STATUSES:
        return True
    if status == "http_error" and not _is_retryable_http_status(row.get("http_status")):
        return True
    return _is_retryable_failure(row) and retry_attempts > retry_rounds


def _receipt_state(
    receipt_rows: Iterable[dict[str, Any]], *, retry_rounds: int
) -> tuple[dict[str, dict[str, Any]], set[str], Counter[str]]:
    latest_by_url: dict[str, dict[str, Any]] = {}
    terminal_urls: set[str] = set()
    retry_attempts: Counter[str] = Counter()
    for row in receipt_rows:
        canonical = _row_url(row)
        if not canonical:
            continue
        latest_by_url[canonical] = row
        if _is_retryable_failure(row):
            retry_attempts[canonical] += 1
        else:
            retry_attempts[canonical] = 0
        if _is_terminal_non_body(row, retry_attempts[canonical], retry_rounds):
            terminal_urls.add(canonical)
        else:
            terminal_urls.discard(canonical)
    return latest_by_url, terminal_urls, retry_attempts


def _captured_raw_urls(raw_rows: Iterable[dict[str, Any]]) -> set[str]:
    captured: set[str] = set()
    for row in raw_rows:
        canonical = _row_url(row)
        if canonical:
            captured.add(canonical)
    return captured


def _discovered_seed_rows(raw_rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_row in raw_rows:
        parent_url = _row_url(raw_row)
        links = raw_row.get("extracted_links")
        if not parent_url or not isinstance(links, list):
            continue
        registrable_domain = str(raw_row.get("registrable_domain") or "").strip().lower()
        for link in links:
            if not isinstance(link, str):
                continue
            canonical = canonicalize_url(link)
            if not canonical or canonical == parent_url or canonical in seen:
                continue
            seen.add(canonical)
            rows.append(
                {
                    "canonical_url": canonical,
                    "priority_rank": int(raw_row.get("source_index") or 0),
                    "registrable_domain": registrable_domain,
                    "source_assessment": raw_row.get("source_assessment"),
                    "source_index": raw_row.get("source_index"),
                    "source_kind": "priority_v1_discovered_raw",
                    "source_parent_url": parent_url,
                    "source_raw_provenance": {
                        "body_sha256": raw_row.get("body_sha256"),
                        "captured_at": raw_row.get("captured_at"),
                        "canonical_url": raw_row.get("canonical_url"),
                        "depth": raw_row.get("depth"),
                        "raw_data_file": raw_row.get("raw_data_file"),
                    },
                    "source_record_id": raw_row.get("source_record_id"),
                    "source_relationship": _link_relationship(
                        canonical,
                        registrable_domain,
                        raw_row.get("source_relationship"),
                    ),
                    "source_sitemap_url": parent_url,
                    "target_kind": "discovered_recruitment_link",
                    "url": canonical,
                    "validation": {
                        "derived_from_captured_raw_extracted_link": True,
                        "selected_without_fetching": True,
                    },
                }
            )
    rows.sort(
        key=lambda row: (
            int(row.get("priority_rank") or 0),
            str(row.get("source_record_id") or ""),
            str(row.get("canonical_url") or ""),
        )
    )
    return rows


def _pending_reason(
    *,
    canonical_url: str,
    latest_receipts: dict[str, dict[str, Any]],
    captured_raw_urls: set[str],
    terminal_urls: set[str],
) -> str | None:
    if canonical_url in captured_raw_urls:
        return None
    if canonical_url in terminal_urls:
        return None
    latest = latest_receipts.get(canonical_url)
    if latest is None:
        return "never_scheduled"
    status = latest.get("capture_status")
    if status in RETAINABLE_STATUSES:
        return str(status)
    if _is_retryable_failure(latest):
        return "retryable_failure"
    if status == "captured":
        return "captured_receipt_without_raw_body"
    return "needs_raw_body"


def select_pending_seeds(
    prioritized_rows: Sequence[dict[str, Any]],
    receipt_rows: Sequence[dict[str, Any]],
    raw_rows: Sequence[dict[str, Any]],
    *,
    retry_rounds: int = 4,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    latest_receipts, terminal_urls, retry_attempts = _receipt_state(
        receipt_rows, retry_rounds=retry_rounds
    )
    captured_raw_urls = _captured_raw_urls(raw_rows)
    output: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    excluded_counts: Counter[str] = Counter()
    seen: set[str] = set()
    discovered_rows = _discovered_seed_rows(raw_rows)
    candidate_rows = [*prioritized_rows, *discovered_rows]

    for row in sorted(
        candidate_rows,
        key=lambda item: (
            int(item.get("priority_rank") or 0),
            str(item.get("source_kind") or ""),
            str(item.get("canonical_url") or item.get("url") or ""),
        ),
    ):
        canonical = _row_url(row)
        if not canonical or canonical in seen:
            excluded_counts["invalid_or_duplicate_seed"] += 1
            continue
        seen.add(canonical)
        if canonical in captured_raw_urls:
            excluded_counts["captured_raw_body"] += 1
            continue
        if canonical in terminal_urls:
            excluded_counts["terminal_or_exhausted_non_body"] += 1
            continue
        reason = _pending_reason(
            canonical_url=canonical,
            latest_receipts=latest_receipts,
            captured_raw_urls=captured_raw_urls,
            terminal_urls=terminal_urls,
        )
        if reason is None:
            excluded_counts["not_pending"] += 1
            continue
        latest = latest_receipts.get(canonical)
        pending_row = dict(row)
        pending_row["pending_selector_version"] = SELECTOR_VERSION
        pending_row["pending_reason"] = reason
        pending_row["previous_capture_status"] = (
            latest.get("capture_status") if latest is not None else None
        )
        pending_row["previous_retry_attempts"] = retry_attempts.get(canonical, 0)
        pending_row["previous_receipt"] = _receipt_provenance(latest) if latest else None
        output.append(pending_row)
        reason_counts[reason] += 1

    for index, row in enumerate(output, start=1):
        row["pending_rank"] = index

    summary = {
        "captured_raw_url_count": len(captured_raw_urls),
        "discovered_candidate_count": len(discovered_rows),
        "excluded_counts": dict(sorted(excluded_counts.items())),
        "input_seed_count": len(prioritized_rows),
        "pending_count": len(output),
        "pending_reason_counts": dict(sorted(reason_counts.items())),
        "receipt_url_count": len(latest_receipts),
        "retry_rounds": retry_rounds,
        "selector_version": SELECTOR_VERSION,
        "terminal_url_count": len(terminal_urls),
    }
    return output, summary


def _receipt_provenance(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    keys = (
        "capture_status",
        "captured_at",
        "canonical_url",
        "requested_url",
        "final_url",
        "http_status",
        "error_kind",
        "raw_data_file",
        "raw_jsonl_line_bytes",
        "storage_cap_bytes",
        "stored_jsonl_bytes_before_response",
    )
    return {key: row[key] for key in keys if key in row}


def _json_line(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def _write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(f"{_json_line(row)}\n")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def run_selector(
    *,
    prioritized_input: Path,
    receipt_input: Path,
    raw_input: Path,
    output_path: Path,
    retry_rounds: int = 4,
) -> dict[str, Any]:
    prioritized = read_jsonl_allow_trailing_partial(prioritized_input)
    receipts = read_jsonl_allow_trailing_partial(receipt_input)
    raw = read_jsonl_allow_trailing_partial(raw_input, allow_invalid_lines=True)
    output, summary = select_pending_seeds(
        list(prioritized.rows),
        list(receipts.rows),
        list(raw.rows),
        retry_rounds=retry_rounds,
    )
    _write_jsonl_atomic(output_path, output)
    summary.update(
        {
            "output_path": str(output_path),
            "prioritized_input": str(prioritized_input),
            "raw_input": str(raw_input),
            "receipt_input": str(receipt_input),
            "skipped_trailing_invalid_lines": {
                "prioritized": prioritized.skipped_trailing_invalid_line,
                "raw": raw.skipped_trailing_invalid_line,
                "receipts": receipts.skipped_trailing_invalid_line,
            },
            "skipped_invalid_line_counts": {
                "prioritized": prioritized.skipped_invalid_line_count,
                "raw": raw.skipped_invalid_line_count,
                "receipts": receipts.skipped_invalid_line_count,
            },
        }
    )
    return summary


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prioritized-input", type=Path, required=True)
    parser.add_argument("--receipt-input", type=Path, required=True)
    parser.add_argument("--raw-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--retry-rounds", type=int, default=4)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = run_selector(
            prioritized_input=args.prioritized_input,
            receipt_input=args.receipt_input,
            raw_input=args.raw_input,
            output_path=args.output,
            retry_rounds=args.retry_rounds,
        )
    except (OSError, ValueError) as error:
        print(f"pending recruitment seed selection failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
