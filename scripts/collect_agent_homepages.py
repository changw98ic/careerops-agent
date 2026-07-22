#!/usr/bin/env python3
"""Collect public homepage text from a JSONL candidate inventory.

This is a one-shot, resumable data-acquisition utility. It deliberately writes
raw response text and receipts outside the tracked repository by default; it is
not wired into the product workflow or scheduler.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import socket
import sys
from collections import Counter, deque
from collections.abc import Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, getproxies, proxy_bypass

RAW_FILENAME = "http_homepages.jsonl"
RECEIPT_FILENAME = "http_homepage_receipts.jsonl"
REPAIR_FILENAME_SUFFIX = ".repair.jsonl"
DEFAULT_USER_AGENT = "CareerOpsHomepageCollector/0.1"
DEFAULT_MAX_STORED_BYTES = 80 * 1024 * 1024 * 1024
TEXT_CONTENT_TYPE_PREFIXES = ("text/",)
TEXT_CONTENT_TYPES = {
    "application/json",
    "application/xhtml+xml",
    "application/xml",
}
LOCAL_HOSTNAMES = {"localhost"}
SANDBOX_EGRESS_ALIAS_NETWORK = ipaddress.ip_network("198.18.0.0/15")


@dataclass(frozen=True)
class CollectorConfig:
    timeout_seconds: float = 20.0
    max_bytes: int = 1_000_000
    user_agent: str = DEFAULT_USER_AGENT
    allow_sandbox_egress_alias: bool = False


@dataclass(frozen=True)
class FetchOutcome:
    raw_row: dict[str, Any] | None
    receipt: dict[str, Any]


class UnsafeTargetError(ValueError):
    """Raised when a URL points at a local or non-public network target."""


class _PublicRedirectHandler(HTTPRedirectHandler):
    def __init__(self, proxies: dict[str, str], *, allow_sandbox_egress_alias: bool) -> None:
        self._proxies = proxies
        self._allow_sandbox_egress_alias = allow_sandbox_egress_alias

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        _ensure_public_http_url(
            urljoin(req.full_url, newurl),
            self._proxies,
            allow_sandbox_egress_alias=self._allow_sandbox_egress_alias,
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _is_supported_text_content_type(content_type: str) -> bool:
    return content_type.startswith(TEXT_CONTENT_TYPE_PREFIXES) or content_type in TEXT_CONTENT_TYPES


def _is_public_ip_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as error:
        raise UnsafeTargetError(f"target resolved to invalid address {address!r}") from error
    return parsed.is_global


def _is_allowed_dns_address(address: str, *, allow_sandbox_egress_alias: bool) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as error:
        raise UnsafeTargetError(f"target resolved to invalid address {address!r}") from error
    return parsed.is_global or (
        allow_sandbox_egress_alias and parsed in SANDBOX_EGRESS_ALIAS_NETWORK
    )


def _proxy_applies(parsed_url: Any, hostname: str, proxies: dict[str, str]) -> bool:
    if proxy_bypass(hostname):
        return False
    return bool(proxies.get(parsed_url.scheme) or proxies.get("all"))


def _ensure_public_http_url(
    url: str,
    proxies: dict[str, str] | None = None,
    *,
    allow_sandbox_egress_alias: bool = False,
) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise UnsafeTargetError("URL must use http or https and include a host")
    if parsed.username or parsed.password:
        raise UnsafeTargetError("URL must not include credentials")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeTargetError("URL must include a host")
    normalized_hostname = hostname.rstrip(".").lower()
    if normalized_hostname in LOCAL_HOSTNAMES or normalized_hostname.endswith(".localhost"):
        raise UnsafeTargetError("URL host is local")

    try:
        literal_address = ipaddress.ip_address(normalized_hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        if not _is_public_ip_address(str(literal_address)):
            raise UnsafeTargetError("URL host is not public")
        return

    try:
        port = parsed.port
    except ValueError as error:
        raise UnsafeTargetError("URL port is invalid") from error
    if proxies is None:
        proxies = getproxies()
    if _proxy_applies(parsed, hostname, proxies):
        return

    checked_address = False
    for family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
        hostname, port, type=socket.SOCK_STREAM
    ):
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        checked_address = True
        if not _is_allowed_dns_address(
            str(sockaddr[0]), allow_sandbox_egress_alias=allow_sandbox_egress_alias
        ):
            raise UnsafeTargetError("URL host resolves to a non-public address")
    if not checked_address:
        raise UnsafeTargetError("URL host did not resolve to a public address")


def _json_line(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _captured_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
                message = f"{path}:{line_number}: invalid JSONL: {error.msg}"
                raise ValueError(message) from error
            if not isinstance(decoded, dict):
                message = f"{path}:{line_number}: expected a JSON object"
                raise ValueError(message)
            rows.append(decoded)
    return rows


def _repair_trailing_jsonl_line(path: Path) -> bool:
    if not path.exists():
        return False
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return False

    line_start = data.rfind(b"\n") + 1
    trailing_line = data[line_start:]
    try:
        decoded = json.loads(trailing_line.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError):
        repair_path = path.with_name(f"{path.name}{REPAIR_FILENAME_SUFFIX}")
        repair_record = {
            "source_file": path.name,
            "line_number": data[:line_start].count(b"\n") + 1,
            "quarantined_at": _captured_at(),
            "reason": "truncated_trailing_jsonl_line",
            "raw_fragment": trailing_line.decode("utf-8", errors="replace"),
        }
        with repair_path.open("a", encoding="utf-8") as repair_handle:
            _append_json_line(repair_handle, repair_record)
        path.write_bytes(data[:line_start])
        return True
    if not isinstance(decoded, dict):
        message = f"{path}:{data[:line_start].count(b'\n') + 1}: expected a JSON object"
        raise ValueError(message)
    path.write_bytes(data + b"\n")
    return True


def _load_output_jsonl(path: Path) -> list[dict[str, Any]]:
    _repair_trailing_jsonl_line(path)
    return _load_jsonl(path)


def _is_retryable_http_status(status_code: object) -> bool:
    return isinstance(status_code, int) and (status_code == 429 or 500 <= status_code <= 599)


def _receipt_is_retryable_failure(receipt: dict[str, Any]) -> bool:
    status = receipt.get("capture_status")
    return status == "network_error" or (
        status == "http_error" and _is_retryable_http_status(receipt.get("http_status"))
    )


def _resume_state(receipt_path: Path, *, retry_rounds: int) -> tuple[set[str], Counter[str]]:
    completed: set[str] = set()
    retryable_failure_counts: Counter[str] = Counter()
    if not receipt_path.exists():
        return completed, retryable_failure_counts
    for row in _load_output_jsonl(receipt_path):
        record_id = row.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            continue
        if _receipt_is_retryable_failure(row):
            completed.discard(record_id)
            retryable_failure_counts[record_id] += 1
            if retryable_failure_counts[record_id] > retry_rounds:
                completed.add(record_id)
        else:
            completed.add(record_id)
            retryable_failure_counts[record_id] = 0
    return completed, retryable_failure_counts


def _synthesize_receipt_from_raw_row(raw_row: dict[str, Any]) -> dict[str, Any] | None:
    record_id = raw_row.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        return None
    captured_at = raw_row.get("captured_at")
    receipt: dict[str, Any] = {
        "record_id": record_id,
        "source_index": raw_row.get("source_index"),
        "requested_url": raw_row.get("requested_url"),
        "capture_status": "captured",
        "captured_at": (
            captured_at if isinstance(captured_at, str) and captured_at else _captured_at()
        ),
        "raw_data_file": RAW_FILENAME,
        "attempt_number": 1,
        "max_attempts": 1,
    }
    for key in (
        "final_url",
        "http_status",
        "content_type",
        "body_bytes",
        "body_truncated",
        "body_sha256",
    ):
        if key in raw_row:
            receipt[key] = raw_row[key]
    return receipt


def _existing_output_record_ids(
    raw_path: Path, receipt_path: Path, *, retry_rounds: int
) -> tuple[set[str], set[str], Counter[str], int]:
    completed_ids, retryable_failure_counts = _resume_state(receipt_path, retry_rounds=retry_rounds)
    raw_ids: set[str] = set()
    synthesized_receipts: list[dict[str, Any]] = []
    if raw_path.exists():
        for raw_row in _load_output_jsonl(raw_path):
            record_id = raw_row.get("record_id")
            if not isinstance(record_id, str) or not record_id:
                continue
            raw_ids.add(record_id)
            if record_id in completed_ids:
                continue
            receipt = _synthesize_receipt_from_raw_row(raw_row)
            if receipt is not None:
                synthesized_receipts.append(receipt)
                completed_ids.add(record_id)
                retryable_failure_counts[record_id] = 0
    if synthesized_receipts:
        with receipt_path.open("a", encoding="utf-8") as receipt_handle:
            for receipt in synthesized_receipts:
                _append_json_line(receipt_handle, receipt)
    return completed_ids, raw_ids, retryable_failure_counts, len(synthesized_receipts)


def _record_identity(record: dict[str, Any], source_index: int) -> tuple[str, str]:
    record_id = record.get("record_id")
    requested_url = record.get("representative_website")
    if not isinstance(record_id, str) or not record_id:
        raise ValueError(f"candidate at index {source_index} has no non-empty record_id")
    if not isinstance(requested_url, str) or not requested_url:
        raise ValueError(f"candidate {record_id} has no non-empty representative_website")
    return record_id, requested_url


def _base_receipt(
    record: dict[str, Any],
    source_index: int,
    requested_url: str,
    status: str,
    *,
    attempt_number: int,
    max_attempts: int,
) -> dict[str, Any]:
    return {
        "record_id": record["record_id"],
        "source_index": source_index,
        "requested_url": requested_url,
        "capture_status": status,
        "captured_at": _captured_at(),
        "raw_data_file": RAW_FILENAME if status == "captured" else None,
        "attempt_number": attempt_number,
        "max_attempts": max_attempts,
    }


def fetch_candidate(
    record: dict[str, Any],
    source_index: int,
    config: CollectorConfig,
    *,
    attempt_number: int = 1,
    max_attempts: int = 1,
) -> FetchOutcome:
    """Fetch one candidate homepage and return only serializable output."""

    try:
        record_id, requested_url = _record_identity(record, source_index)
    except ValueError as error:
        receipt = {
            "record_id": str(record.get("record_id") or f"source-index-{source_index}"),
            "source_index": source_index,
            "requested_url": record.get("representative_website"),
            "capture_status": "invalid_record",
            "captured_at": _captured_at(),
            "error_kind": type(error).__name__,
            "raw_data_file": None,
            "attempt_number": attempt_number,
            "max_attempts": max_attempts,
        }
        return FetchOutcome(raw_row=None, receipt=receipt)

    try:
        proxies = getproxies()
        _ensure_public_http_url(
            requested_url,
            proxies,
            allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
        )
    except UnsafeTargetError as error:
        receipt = _base_receipt(
            record,
            source_index,
            requested_url,
            "invalid_url",
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )
        receipt.update({"error_kind": type(error).__name__, "raw_data_file": None})
        return FetchOutcome(raw_row=None, receipt=receipt)
    except OSError as error:
        receipt = _base_receipt(
            record,
            source_index,
            requested_url,
            "network_error",
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )
        receipt.update({"error_kind": type(error).__name__, "raw_data_file": None})
        return FetchOutcome(raw_row=None, receipt=receipt)

    request = Request(
        requested_url,
        headers={
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.1",
            "User-Agent": config.user_agent,
        },
        method="GET",
    )
    try:
        opener = build_opener(
            _PublicRedirectHandler(
                proxies,
                allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
            )
        )
        with opener.open(request, timeout=config.timeout_seconds) as response:
            raw_body = response.read(config.max_bytes + 1)
            body_truncated = len(raw_body) > config.max_bytes
            if body_truncated:
                raw_body = raw_body[: config.max_bytes]
            content_type = response.headers.get_content_type().lower()
            final_url = response.geturl()
            status_code = response.getcode()
            if not _is_supported_text_content_type(content_type):
                receipt = _base_receipt(
                    record,
                    source_index,
                    requested_url,
                    "unsupported_content_type",
                    attempt_number=attempt_number,
                    max_attempts=max_attempts,
                )
                receipt.update(
                    {
                        "final_url": final_url,
                        "http_status": status_code,
                        "content_type": content_type,
                        "body_bytes": len(raw_body),
                        "body_truncated": body_truncated,
                        "raw_data_file": None,
                    }
                )
                return FetchOutcome(raw_row=None, receipt=receipt)
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as error:
        receipt = _base_receipt(
            record,
            source_index,
            requested_url,
            "http_error",
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )
        receipt.update(
            {
                "http_status": error.code,
                "final_url": error.geturl(),
                "error_kind": type(error).__name__,
                "raw_data_file": None,
            }
        )
        return FetchOutcome(raw_row=None, receipt=receipt)
    except UnsafeTargetError as error:
        receipt = _base_receipt(
            record,
            source_index,
            requested_url,
            "invalid_url",
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )
        receipt.update({"error_kind": type(error).__name__, "raw_data_file": None})
        return FetchOutcome(raw_row=None, receipt=receipt)
    except (OSError, TimeoutError, URLError) as error:
        receipt = _base_receipt(
            record,
            source_index,
            requested_url,
            "network_error",
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )
        receipt.update({"error_kind": type(error).__name__, "raw_data_file": None})
        return FetchOutcome(raw_row=None, receipt=receipt)

    response_text = raw_body.decode(charset, errors="replace")
    if not response_text.strip():
        receipt = _base_receipt(
            record,
            source_index,
            requested_url,
            "empty_text",
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )
        receipt.update(
            {
                "final_url": final_url,
                "http_status": status_code,
                "content_type": content_type,
                "body_bytes": len(raw_body),
                "body_truncated": body_truncated,
                "raw_data_file": None,
            }
        )
        return FetchOutcome(raw_row=None, receipt=receipt)

    raw_row = {
        "record_id": record_id,
        "candidate_entity_name": record.get("candidate_entity_name"),
        "registrable_domain": record.get("registrable_domain"),
        "source_index": source_index,
        "requested_url": requested_url,
        "final_url": final_url,
        "http_status": status_code,
        "content_type": content_type,
        "body_bytes": len(raw_body),
        "body_truncated": body_truncated,
        "body_sha256": hashlib.sha256(raw_body).hexdigest(),
        "response_text": response_text,
        "capture_method": "concurrent HTTP GET; response text only; no browser interaction",
    }
    receipt = _base_receipt(
        record,
        source_index,
        requested_url,
        "captured",
        attempt_number=attempt_number,
        max_attempts=max_attempts,
    )
    receipt.update(
        {
            "final_url": final_url,
            "http_status": status_code,
            "content_type": content_type,
            "body_bytes": len(raw_body),
            "body_truncated": body_truncated,
            "body_sha256": raw_row["body_sha256"],
        }
    )
    raw_row["captured_at"] = receipt["captured_at"]
    return FetchOutcome(raw_row=raw_row, receipt=receipt)


def _append_json_line(handle: Any, row: dict[str, Any]) -> None:
    handle.write(f"{_json_line(row)}\n")
    handle.flush()


def collect_candidates(
    records: Iterable[dict[str, Any]],
    output_dir: Path,
    *,
    config: CollectorConfig,
    workers: int,
    limit: int | None = None,
    resume: bool = True,
    start_index: int = 0,
    retry_rounds: int = 4,
    max_stored_bytes: int = DEFAULT_MAX_STORED_BYTES,
) -> Counter[str]:
    """Collect candidates concurrently, appending a receipt for every attempted row."""

    if workers < 1:
        raise ValueError("workers must be at least 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1 when provided")
    if start_index < 0:
        raise ValueError("start_index must be at least 0")
    if retry_rounds < 0:
        raise ValueError("retry_rounds must be at least 0")
    if max_stored_bytes < 1:
        raise ValueError("max_stored_bytes must be at least 1")
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / RAW_FILENAME
    receipt_path = output_dir / RECEIPT_FILENAME
    _repair_trailing_jsonl_line(raw_path)
    _repair_trailing_jsonl_line(receipt_path)
    completed_record_ids, raw_record_ids, retryable_failure_counts, synthesized_receipt_count = (
        _existing_output_record_ids(raw_path, receipt_path, retry_rounds=retry_rounds)
        if resume
        else (set(), set(), Counter(), 0)
    )
    selected: list[tuple[int, dict[str, Any]]] = []
    summary: Counter[str] = Counter()
    if synthesized_receipt_count:
        summary["synthesized_missing_receipts"] = synthesized_receipt_count
    for source_index, record in enumerate(records):
        if source_index < start_index:
            continue
        record_id = record.get("record_id")
        if isinstance(record_id, str):
            if record_id in completed_record_ids:
                summary["skipped_existing_receipt"] += 1
                continue
            if record_id in raw_record_ids:
                summary["skipped_existing_raw"] += 1
                continue
        selected.append((source_index, record))
        if limit is not None and len(selected) >= limit:
            break

    queued = deque(selected)
    max_attempts = retry_rounds + 1
    run_stored_bytes = 0
    stop_for_cap = False
    with (
        raw_path.open("a", encoding="utf-8") as raw_handle,
        receipt_path.open("a", encoding="utf-8") as receipt_handle,
        ThreadPoolExecutor(max_workers=workers) as executor,
    ):
        futures: dict[Future[FetchOutcome], tuple[int, dict[str, Any], int]] = {}
        while queued or futures:
            while queued and len(futures) < workers and not stop_for_cap:
                source_index, record = queued.popleft()
                record_id = record.get("record_id")
                prior_failures = (
                    retryable_failure_counts[record_id] if isinstance(record_id, str) else 0
                )
                attempt_number = prior_failures + 1
                future = executor.submit(
                    fetch_candidate,
                    record,
                    source_index,
                    config,
                    attempt_number=attempt_number,
                    max_attempts=max_attempts,
                )
                futures[future] = (source_index, record, attempt_number)

            if not futures:
                break

            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                source_index, record, attempt_number = futures.pop(future)
                record_id = record.get("record_id")
                try:
                    outcome = future.result()
                except Exception as error:  # pragma: no cover - defensive worker boundary
                    requested_url = record.get("representative_website")
                    outcome = FetchOutcome(
                        raw_row=None,
                        receipt={
                            "record_id": (
                                record_id
                                if isinstance(record_id, str) and record_id
                                else f"source-index-{source_index}"
                            ),
                            "source_index": source_index,
                            "requested_url": (
                                requested_url if isinstance(requested_url, str) else None
                            ),
                            "capture_status": "worker_error",
                            "captured_at": _captured_at(),
                            "error_kind": type(error).__name__,
                            "raw_data_file": None,
                            "attempt_number": attempt_number,
                            "max_attempts": max_attempts,
                        },
                    )

                receipt = dict(outcome.receipt)
                status = str(receipt["capture_status"])
                if outcome.raw_row is not None:
                    raw_line_bytes = len(f"{_json_line(outcome.raw_row)}\n".encode())
                    if run_stored_bytes + raw_line_bytes > max_stored_bytes:
                        receipt["capture_status"] = "storage_cap_reached"
                        receipt["raw_data_file"] = None
                        receipt["storage_cap_bytes"] = max_stored_bytes
                        receipt["run_stored_jsonl_bytes_before_response"] = run_stored_bytes
                        receipt["raw_jsonl_line_bytes"] = raw_line_bytes
                        status = "storage_cap_reached"
                        stop_for_cap = True
                    else:
                        _append_json_line(raw_handle, outcome.raw_row)
                        run_stored_bytes += raw_line_bytes

                _append_json_line(receipt_handle, receipt)
                summary[status] += 1
                summary["attempted"] += 1

                if isinstance(record_id, str) and _receipt_is_retryable_failure(receipt):
                    retryable_failure_counts[record_id] += 1
                    if retryable_failure_counts[record_id] <= retry_rounds:
                        queued.append((source_index, record))
                        summary["retry_enqueued"] += 1
                    else:
                        summary[f"{status}_retry_exhausted"] += 1
                elif isinstance(record_id, str):
                    completed_record_ids.add(record_id)
                    retryable_failure_counts[record_id] = 0

    summary["pending_unfetched"] = len(queued)
    summary["run_stored_bytes"] = run_stored_bytes
    summary["max_stored_bytes"] = max_stored_bytes
    summary["storage_remaining_bytes"] = max(0, max_stored_bytes - run_stored_bytes)
    return summary


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Candidate inventory JSONL path.")
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="Ignored raw-output directory."
    )
    parser.add_argument(
        "--workers", type=int, default=12, help="Concurrent HTTP workers (default: 12)."
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0, help="Per-request timeout.")
    parser.add_argument(
        "--max-bytes", type=int, default=1_000_000, help="Maximum response bytes per page."
    )
    parser.add_argument(
        "--max-stored-bytes",
        type=int,
        default=DEFAULT_MAX_STORED_BYTES,
        help="Maximum raw JSONL bytes to append during this run (default: 80 GiB).",
    )
    parser.add_argument(
        "--retry-rounds",
        type=int,
        default=4,
        help="Retries for network errors and HTTP 429/5xx after the initial attempt.",
    )
    parser.add_argument("--limit", type=int, help="Maximum unprocessed candidates to collect.")
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Zero-based source index to start from before applying --limit.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Do not skip record IDs already present in the receipt JSONL.",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="HTTP User-Agent header.")
    parser.add_argument(
        "--allow-sandbox-egress-alias",
        action="store_true",
        help=(
            "Allow hostname DNS results in 198.18.0.0/15 for known sandbox egress "
            "aliasing. IP literals in this range are still rejected."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.max_bytes < 1:
        print("--max-bytes must be at least 1", file=sys.stderr)
        return 2
    if args.start_index < 0:
        print("--start-index must be at least 0", file=sys.stderr)
        return 2
    if args.retry_rounds < 0:
        print("--retry-rounds must be at least 0", file=sys.stderr)
        return 2
    if args.max_stored_bytes < 1:
        print("--max-stored-bytes must be at least 1", file=sys.stderr)
        return 2
    try:
        records = _load_jsonl(args.input)
        summary = collect_candidates(
            records,
            args.output_dir,
            config=CollectorConfig(
                timeout_seconds=args.timeout_seconds,
                max_bytes=args.max_bytes,
                user_agent=args.user_agent,
                allow_sandbox_egress_alias=args.allow_sandbox_egress_alias,
            ),
            workers=args.workers,
            limit=args.limit,
            resume=not args.no_resume,
            start_index=args.start_index,
            retry_rounds=args.retry_rounds,
            max_stored_bytes=args.max_stored_bytes,
        )
    except (OSError, ValueError) as error:
        print(f"collection failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(dict(sorted(summary.items())), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
