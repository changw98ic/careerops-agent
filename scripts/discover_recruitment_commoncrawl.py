#!/usr/bin/env python3
"""Discover recruitment URL seeds from the public Common Crawl CDX index.

This stage expands the recruitment crawler queue without downloading archived
page bodies. It queries Common Crawl's public CDX API for first-party company
domains from recruitment triage JSONL, keeps recruitment-looking URLs, and
writes append-only JSONL artifacts compatible with
``collect_recruitment_pages.py --additional-seeds``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from collect_recruitment_pages import (
    DEFAULT_USER_AGENT,
    ELIGIBLE_ASSESSMENTS,
    UnsafeTargetError,
    _ensure_public_http_url,
    _host_matches_domain,
    _hostname,
    _json_line,
    _PublicRedirectHandler,
    canonicalize_url,
    is_eligible_crawl_url,
    link_relationship,
)

DISCOVERED_FILENAME = "discovered_recruitment_commoncrawl_urls.jsonl"
RECEIPT_FILENAME = "recruitment_commoncrawl_discovery_receipts.jsonl"
SUMMARY_FILENAME = "recruitment_commoncrawl_discovery_summary.json"
MANIFEST_FILENAME = "recruitment_commoncrawl_discovery_manifest.json"
FAILURE_STREAKS_FILENAME = "recruitment_commoncrawl_failure_streaks.json"

DEFAULT_INDEX_CATALOG_URL = "https://index.commoncrawl.org/collinfo.json"
DEFAULT_RETRY_ROUNDS = 4
DEFAULT_CONCURRENCY = 3
DEFAULT_MAX_INDEXES = 2
DEFAULT_MAX_RESULTS_PER_QUERY = 100
DEFAULT_MAX_DISCOVERED_URLS = 50_000
DEFAULT_PROVIDER_CIRCUIT_BREAKER_FAILURES = 5
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5
DEFAULT_MAX_RETRY_BACKOFF_SECONDS = 8.0
COMMONCRAWL_DISCOVERY_METHOD = (
    "Common Crawl CDX index query; URL metadata only; no archive body fetch, browser, login, "
    "forms, or JS"
)
QUERY_PATTERNS = (
    "/careers*",
    "/career*",
    "/jobs*",
    "/job*",
    "/join-us*",
    "/join_our_team*",
    "/join-our-team*",
    "/open-positions*",
    "/openings*",
    "/positions*",
    "/roles*",
    "/vacancies*",
    "/hiring*",
    "/recruiting*",
    "/recruitment*",
)
RETRYABLE_STATUSES = {"network_error", "retryable_http_error"}


@dataclass(frozen=True)
class CompanyDomain:
    registrable_domain: str
    source_record_id: str | None
    source_index: int | None
    source_assessment: str | None
    source_relationship: str = "first_party"


@dataclass(frozen=True)
class IndexInfo:
    index_id: str
    cdx_api_url: str


@dataclass(frozen=True)
class QueryTarget:
    canonical_url: str
    url: str
    index_id: str
    pattern: str
    company: CompanyDomain


@dataclass(frozen=True)
class FetchConfig:
    timeout_seconds: float = 20.0
    user_agent: str = DEFAULT_USER_AGENT
    allow_sandbox_egress_alias: bool = False
    max_results_per_query: int = DEFAULT_MAX_RESULTS_PER_QUERY
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS
    max_retry_backoff_seconds: float = DEFAULT_MAX_RETRY_BACKOFF_SECONDS


@dataclass(frozen=True)
class FetchResult:
    target: QueryTarget
    status: str
    rows: tuple[dict[str, Any], ...] = ()
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    body_bytes: int = 0
    error_kind: str | None = None


Indexer = Callable[[FetchConfig], tuple[IndexInfo, ...]]
Fetcher = Callable[[QueryTarget, FetchConfig], FetchResult]


def _captured_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _append_json_line(handle: Any, row: dict[str, Any]) -> None:
    handle.write(f"{_json_line(row)}\n")
    handle.flush()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
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


def _load_output_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return _load_jsonl(path)


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _is_retryable_status(status: str) -> bool:
    return status in RETRYABLE_STATUSES


def _response_text(url: str, config: FetchConfig, *, accept: str) -> tuple[str, str, int, str, int]:
    _ensure_public_http_url(
        url,
        allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
    )
    request = Request(
        url,
        headers={"Accept": accept, "User-Agent": config.user_agent},
        method="GET",
    )
    opener = build_opener(
        _PublicRedirectHandler(
            allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
        )
    )
    with opener.open(request, timeout=config.timeout_seconds) as response:
        raw_body = response.read()
        content_type = response.headers.get_content_type().lower()
        charset = response.headers.get_content_charset() or "utf-8"
        return (
            raw_body.decode(charset, errors="replace"),
            response.geturl(),
            response.getcode(),
            content_type,
            len(raw_body),
        )


def load_commoncrawl_indexes(
    config: FetchConfig,
    *,
    catalog_url: str = DEFAULT_INDEX_CATALOG_URL,
    max_indexes: int = DEFAULT_MAX_INDEXES,
) -> tuple[IndexInfo, ...]:
    if max_indexes < 1:
        raise ValueError("max_indexes must be at least 1")
    try:
        text, _final_url, _status, _content_type, _body_bytes = _response_text(
            catalog_url, config, accept="application/json"
        )
    except HTTPError as error:
        raise ValueError(f"failed to load Common Crawl index catalog: HTTP {error.code}") from error
    except (UnsafeTargetError, OSError, TimeoutError, URLError) as error:
        raise ValueError(f"failed to load Common Crawl index catalog: {error}") from error
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("Common Crawl index catalog was not valid JSON") from error
    if not isinstance(decoded, list):
        raise ValueError("Common Crawl index catalog must be a JSON array")
    indexes: list[IndexInfo] = []
    for item in decoded:
        if not isinstance(item, dict):
            continue
        index_id = item.get("id")
        api_url = item.get("cdx-api")
        if not isinstance(index_id, str) or not isinstance(api_url, str):
            continue
        parsed = urlsplit(api_url)
        if parsed.scheme != "https" or parsed.hostname != "index.commoncrawl.org":
            continue
        indexes.append(IndexInfo(index_id=index_id, cdx_api_url=api_url))
    if not indexes:
        raise ValueError("Common Crawl index catalog did not contain HTTPS CDX API URLs")
    return tuple(indexes[:max_indexes])


def _has_first_party_evidence(row: dict[str, Any], registrable_domain: str) -> bool:
    for field_name in ("final_url", "requested_url"):
        value = row.get(field_name)
        if isinstance(value, str) and _host_matches_domain(_hostname(value), registrable_domain):
            return True
    links = row.get("links")
    if not isinstance(links, list):
        return False
    for link in links:
        if not isinstance(link, dict) or link.get("relationship") != "first_party":
            continue
        href = link.get("href")
        if isinstance(href, str) and _host_matches_domain(_hostname(href), registrable_domain):
            return True
    return False


def select_company_domains(rows: Iterable[dict[str, Any]]) -> list[CompanyDomain]:
    domains: list[CompanyDomain] = []
    seen: set[str] = set()
    for source_index, row in enumerate(rows):
        assessment = row.get("employer_hiring_assessment")
        if assessment not in ELIGIBLE_ASSESSMENTS:
            continue
        registrable_domain = str(row.get("registrable_domain") or "").strip().lower()
        if not registrable_domain or registrable_domain in seen:
            continue
        if not _has_first_party_evidence(row, registrable_domain):
            continue
        seen.add(registrable_domain)
        domains.append(
            CompanyDomain(
                registrable_domain=registrable_domain,
                source_record_id=(
                    row.get("record_id") if isinstance(row.get("record_id"), str) else None
                ),
                source_index=source_index,
                source_assessment=str(assessment),
            )
        )
    return domains


def query_url(index: IndexInfo, company: CompanyDomain, pattern: str, limit: int) -> str:
    params = {
        "url": f"*.{company.registrable_domain}{pattern}",
        "output": "json",
        "fl": "url,status,mime,timestamp,digest",
        "filter": ("status:200",),
        "collapse": "urlkey",
        "limit": str(limit),
    }
    return f"{index.cdx_api_url}?{urlencode(params, doseq=True)}"


def build_query_targets(
    indexes: Sequence[IndexInfo],
    companies: Sequence[CompanyDomain],
    *,
    max_results_per_query: int = DEFAULT_MAX_RESULTS_PER_QUERY,
) -> list[QueryTarget]:
    targets: list[QueryTarget] = []
    seen: set[str] = set()
    for index in indexes:
        for company in companies:
            for pattern in QUERY_PATTERNS:
                url = query_url(index, company, pattern, max_results_per_query)
                canonical = canonicalize_url(url)
                if not canonical or canonical in seen:
                    continue
                seen.add(canonical)
                targets.append(
                    QueryTarget(
                        canonical_url=canonical,
                        url=url,
                        index_id=index.index_id,
                        pattern=pattern,
                        company=company,
                    )
                )
    return targets


def fetch_cdx_query(target: QueryTarget, config: FetchConfig) -> FetchResult:
    try:
        text, final_url, status_code, content_type, body_bytes = _response_text(
            target.url, config, accept="application/json,text/plain;q=0.9,*/*;q=0.1"
        )
    except HTTPError as error:
        status = "retryable_http_error" if _is_retryable_http_status(error.code) else "http_error"
        return FetchResult(
            target=target,
            status=status,
            final_url=error.geturl(),
            http_status=error.code,
            error_kind=type(error).__name__,
        )
    except UnsafeTargetError as error:
        return FetchResult(target=target, status="invalid_url", error_kind=type(error).__name__)
    except (OSError, TimeoutError, URLError) as error:
        return FetchResult(target=target, status="network_error", error_kind=type(error).__name__)

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return FetchResult(
                target=target,
                status="parse_error",
                final_url=final_url,
                http_status=status_code,
                content_type=content_type,
                body_bytes=body_bytes,
                error_kind="JSONDecodeError",
            )
        if isinstance(decoded, dict):
            rows.append(decoded)
    return FetchResult(
        target=target,
        status="fetched",
        rows=tuple(rows),
        final_url=final_url,
        http_status=status_code,
        content_type=content_type,
        body_bytes=body_bytes,
    )


def _receipt_row(
    result: FetchResult,
    *,
    retained_url_count: int = 0,
    materialized_url_count: int = 0,
    duplicate_url_count: int = 0,
) -> dict[str, Any]:
    target = result.target
    return {
        "canonical_url": target.canonical_url,
        "requested_url": target.url,
        "commoncrawl_index_id": target.index_id,
        "query_pattern": target.pattern,
        "registrable_domain": target.company.registrable_domain,
        "source_record_id": target.company.source_record_id,
        "source_index": target.company.source_index,
        "source_assessment": target.company.source_assessment,
        "source_relationship": target.company.source_relationship,
        "capture_status": result.status,
        "captured_at": _captured_at(),
        "final_url": result.final_url,
        "http_status": result.http_status,
        "content_type": result.content_type,
        "body_bytes": result.body_bytes,
        "result_count": len(result.rows),
        "retained_url_count": retained_url_count,
        "materialized_url_count": materialized_url_count,
        "duplicate_url_count": duplicate_url_count,
        "error_kind": result.error_kind,
    }


def _discovered_row(url: str, target: QueryTarget, cdx_row: dict[str, Any]) -> dict[str, Any]:
    canonical = canonicalize_url(url)
    return {
        "canonical_url": canonical,
        "url": canonical,
        "source_sitemap_url": None,
        "source_commoncrawl_query_url": target.canonical_url,
        "source_commoncrawl_index_id": target.index_id,
        "source_commoncrawl_timestamp": cdx_row.get("timestamp"),
        "source_commoncrawl_digest": cdx_row.get("digest"),
        "source_commoncrawl_mime": cdx_row.get("mime"),
        "source_record_id": target.company.source_record_id,
        "source_index": target.company.source_index,
        "source_assessment": target.company.source_assessment,
        "source_relationship": link_relationship(canonical, target.company.registrable_domain),
        "registrable_domain": target.company.registrable_domain,
        "discovered_at": _captured_at(),
        "discovery_method": COMMONCRAWL_DISCOVERY_METHOD,
    }


def retained_cdx_url(cdx_row: dict[str, Any], company: CompanyDomain) -> str:
    url = cdx_row.get("url")
    if not isinstance(url, str):
        return ""
    canonical = canonicalize_url(url)
    if not canonical:
        return ""
    if link_relationship(canonical, company.registrable_domain) != "first_party":
        return ""
    if not is_eligible_crawl_url(canonical, company.registrable_domain, ""):
        return ""
    return canonical


def _retained_discovered_rows(result: FetchResult) -> tuple[dict[str, Any], ...]:
    rows_by_url: dict[str, dict[str, Any]] = {}
    for cdx_row in result.rows:
        canonical = retained_cdx_url(cdx_row, result.target.company)
        if not canonical or canonical in rows_by_url:
            continue
        rows_by_url[canonical] = _discovered_row(canonical, result.target, cdx_row)
    return tuple(rows_by_url[canonical] for canonical in sorted(rows_by_url))


def _int_field(row: dict[str, Any], field_name: str) -> int | None:
    value = row.get(field_name)
    return value if isinstance(value, int) and value >= 0 else None


def _resume_state(
    receipt_path: Path, discovered_path: Path, *, retry_rounds: int
) -> tuple[set[str], set[str], Counter[str]]:
    completed_queries: set[str] = set()
    discovered_urls: set[str] = set()
    materialized_by_query: Counter[str] = Counter()
    failure_streaks: Counter[str] = Counter()
    for row in _load_output_jsonl(discovered_path):
        canonical = row.get("canonical_url")
        if isinstance(canonical, str) and canonical:
            discovered_urls.add(canonical)
        source_query = row.get("source_commoncrawl_query_url")
        if isinstance(source_query, str) and source_query:
            materialized_by_query[source_query] += 1
    for row in _load_output_jsonl(receipt_path):
        canonical = row.get("canonical_url")
        if not isinstance(canonical, str) or not canonical:
            continue
        status = row.get("capture_status")
        if isinstance(status, str) and _is_retryable_status(status):
            completed_queries.discard(canonical)
            failure_streaks[canonical] += 1
            if failure_streaks[canonical] > retry_rounds:
                completed_queries.add(canonical)
        elif status == "fetched":
            result_count = _int_field(row, "result_count")
            retained_url_count = _int_field(row, "retained_url_count")
            materialized_url_count = _int_field(row, "materialized_url_count")
            duplicate_url_count = _int_field(row, "duplicate_url_count") or 0
            is_materialized_receipt_complete = (
                materialized_url_count is not None
                and retained_url_count is not None
                and materialized_url_count + duplicate_url_count >= retained_url_count
            )
            has_legacy_materialization_evidence = (
                materialized_url_count is None and materialized_by_query[canonical] > 0
            )
            if (
                is_materialized_receipt_complete
                or result_count == 0
                or has_legacy_materialization_evidence
            ):
                completed_queries.add(canonical)
                failure_streaks[canonical] = 0
            else:
                completed_queries.discard(canonical)
                failure_streaks[canonical] = 0
        else:
            completed_queries.add(canonical)
            failure_streaks[canonical] = 0
    return completed_queries, discovered_urls, failure_streaks


def _host_failure_streaks(failure_streaks: Counter[str]) -> Counter[str]:
    by_host: Counter[str] = Counter()
    for url, count in failure_streaks.items():
        by_host[_hostname(url)] += count
    return by_host


def _retry_ready_at(config: FetchConfig, attempt_number: int) -> float:
    base_delay = max(0.0, config.retry_backoff_seconds)
    max_delay = max(base_delay, config.max_retry_backoff_seconds)
    if base_delay == 0:
        return time.monotonic()
    delay = min(max_delay, base_delay * (2 ** max(0, attempt_number - 1)))
    return time.monotonic() + delay


def discover_recruitment_commoncrawl_urls(
    triage_rows: Iterable[dict[str, Any]],
    output_dir: Path,
    *,
    config: FetchConfig,
    indexes: Sequence[IndexInfo] | None = None,
    indexer: Indexer | None = None,
    max_indexes: int = DEFAULT_MAX_INDEXES,
    retry_rounds: int = DEFAULT_RETRY_ROUNDS,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_discovered_urls: int = DEFAULT_MAX_DISCOVERED_URLS,
    provider_circuit_breaker_failures: int = DEFAULT_PROVIDER_CIRCUIT_BREAKER_FAILURES,
    resume: bool = True,
    fetcher: Fetcher = fetch_cdx_query,
) -> Counter[str]:
    if retry_rounds < 0:
        raise ValueError("retry_rounds must be at least 0")
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if config.max_results_per_query < 1:
        raise ValueError("max_results_per_query must be at least 1")
    if config.retry_backoff_seconds < 0:
        raise ValueError("retry_backoff_seconds must be at least 0")
    if config.max_retry_backoff_seconds < 0:
        raise ValueError("max_retry_backoff_seconds must be at least 0")
    if max_indexes < 1:
        raise ValueError("max_indexes must be at least 1")
    if max_discovered_urls < 1:
        raise ValueError("max_discovered_urls must be at least 1")
    if provider_circuit_breaker_failures < 0:
        raise ValueError("provider_circuit_breaker_failures must be at least 0")

    output_dir.mkdir(parents=True, exist_ok=True)
    discovered_path = output_dir / DISCOVERED_FILENAME
    receipt_path = output_dir / RECEIPT_FILENAME
    summary_path = output_dir / SUMMARY_FILENAME
    manifest_path = output_dir / MANIFEST_FILENAME
    failure_streaks_path = output_dir / FAILURE_STREAKS_FILENAME

    if resume:
        completed_queries, discovered_urls, failure_streaks = _resume_state(
            receipt_path, discovered_path, retry_rounds=retry_rounds
        )
    else:
        completed_queries = set()
        discovered_urls = set()
        failure_streaks = Counter()

    selected_indexes = (
        tuple(indexes) if indexes is not None else (indexer or load_commoncrawl_indexes)(config)
    )
    selected_indexes = selected_indexes[:max_indexes]
    companies = select_company_domains(triage_rows)
    targets = build_query_targets(
        selected_indexes,
        companies,
        max_results_per_query=config.max_results_per_query,
    )

    queue: deque[tuple[QueryTarget, float]] = deque()
    queued_queries: set[str] = set()
    summary: Counter[str] = Counter()
    for target in targets:
        if resume and target.canonical_url in completed_queries:
            summary["skipped_existing_receipt"] += 1
            continue
        if target.canonical_url in queued_queries:
            continue
        queued_queries.add(target.canonical_url)
        queue.append((target, 0.0))
    summary["index_count"] = len(selected_indexes)
    summary["company_domain_count"] = len(companies)
    summary["query_count"] = len(targets)
    summary["resume_completed_query_count"] = len(completed_queries)
    summary["resume_discovered_url_count"] = len(discovered_urls)

    stop_for_cap = len(discovered_urls) >= max_discovered_urls
    stop_for_provider_breaker = False
    consecutive_retryable_provider_failures = 0
    provider_breaker_pending_futures = 0
    provider_breaker_cancelled_futures = 0
    with (
        discovered_path.open("a", encoding="utf-8") as discovered_handle,
        receipt_path.open("a", encoding="utf-8") as receipt_handle,
        ThreadPoolExecutor(max_workers=concurrency) as executor,
    ):
        futures: dict[Future[FetchResult], QueryTarget] = {}
        while queue or futures:
            now = time.monotonic()
            while (
                queue
                and len(futures) < concurrency
                and not stop_for_cap
                and not stop_for_provider_breaker
            ):
                target, ready_at = queue[0]
                if ready_at > now:
                    break
                queue.popleft()
                futures[executor.submit(fetcher, target, config)] = target

            if not futures:
                if queue and not stop_for_cap and not stop_for_provider_breaker:
                    sleep_seconds = max(0.0, queue[0][1] - time.monotonic())
                    time.sleep(min(sleep_seconds, 1.0))
                    continue
                break

            timeout = None
            if queue and not stop_for_cap and not stop_for_provider_breaker:
                timeout = max(0.0, min(1.0, queue[0][1] - time.monotonic()))
            done, _pending = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                target = futures.pop(future)
                try:
                    result = future.result()
                except Exception as error:  # pragma: no cover - defensive worker boundary
                    result = FetchResult(
                        target=target,
                        status="worker_error",
                        error_kind=type(error).__name__,
                    )
                summary[result.status] += 1
                summary["attempted"] += 1

                if _is_retryable_status(result.status):
                    consecutive_retryable_provider_failures += 1
                    failure_streaks[target.canonical_url] += 1
                    if failure_streaks[target.canonical_url] <= retry_rounds:
                        ready_at = _retry_ready_at(config, failure_streaks[target.canonical_url])
                        queue.append((target, ready_at))
                        summary["retry_enqueued"] += 1
                        _append_json_line(receipt_handle, _receipt_row(result))
                    else:
                        completed_queries.add(target.canonical_url)
                        summary["retry_exhausted"] += 1
                        _append_json_line(receipt_handle, _receipt_row(result))
                    if (
                        provider_circuit_breaker_failures
                        and consecutive_retryable_provider_failures
                        >= provider_circuit_breaker_failures
                    ):
                        stop_for_provider_breaker = True
                        summary["provider_circuit_breaker_tripped"] = 1
                        provider_breaker_pending_futures = len(futures)
                        summary["provider_circuit_breaker_pending_futures"] = (
                            provider_breaker_pending_futures
                        )
                        provider_breaker_cancelled_futures = sum(
                            1 for pending_future in futures if pending_future.cancel()
                        )
                        summary["provider_circuit_breaker_cancelled_futures"] = (
                            provider_breaker_cancelled_futures
                        )
                        futures.clear()
                        break
                    continue

                consecutive_retryable_provider_failures = 0
                completed_queries.add(target.canonical_url)
                failure_streaks[target.canonical_url] = 0
                if result.status != "fetched":
                    _append_json_line(receipt_handle, _receipt_row(result))
                    continue

                retained_rows = _retained_discovered_rows(result)
                materialized_url_count = 0
                duplicate_url_count = 0
                summary["cdx_row_filtered"] += len(result.rows) - len(retained_rows)
                for discovered_row in retained_rows:
                    canonical = discovered_row["canonical_url"]
                    if canonical in discovered_urls:
                        duplicate_url_count += 1
                        summary["discovered_duplicate_skipped"] += 1
                        continue
                    if len(discovered_urls) >= max_discovered_urls:
                        stop_for_cap = True
                        summary["discovery_cap_reached"] += 1
                        break
                    _append_json_line(discovered_handle, discovered_row)
                    discovered_urls.add(canonical)
                    materialized_url_count += 1
                    summary["discovered_url_written"] += 1
                _append_json_line(
                    receipt_handle,
                    _receipt_row(
                        result,
                        retained_url_count=len(retained_rows),
                        materialized_url_count=materialized_url_count,
                        duplicate_url_count=duplicate_url_count,
                    ),
                )
            if stop_for_provider_breaker:
                break

    summary["provider_circuit_breaker_failures"] = provider_circuit_breaker_failures
    summary["consecutive_retryable_provider_failures"] = consecutive_retryable_provider_failures
    summary["pending_unfetched"] = len(queue) + provider_breaker_pending_futures
    _write_json(summary_path, dict(sorted(summary.items())))
    _write_json(
        manifest_path,
        {
            "created_at": _captured_at(),
            "commoncrawl_catalog_url": DEFAULT_INDEX_CATALOG_URL,
            "discovered_file": DISCOVERED_FILENAME,
            "receipt_file": RECEIPT_FILENAME,
            "summary_file": SUMMARY_FILENAME,
            "failure_streaks_file": FAILURE_STREAKS_FILENAME,
            "max_indexes": max_indexes,
            "max_results_per_query": config.max_results_per_query,
            "max_discovered_urls": max_discovered_urls,
            "provider_circuit_breaker_failures": provider_circuit_breaker_failures,
            "retry_backoff_seconds": config.retry_backoff_seconds,
            "max_retry_backoff_seconds": config.max_retry_backoff_seconds,
            "timeout_seconds": config.timeout_seconds,
            "concurrency": concurrency,
            "retry_rounds": retry_rounds,
            "allow_sandbox_egress_alias": config.allow_sandbox_egress_alias,
            "resume": resume,
            "indexes": [index.index_id for index in selected_indexes],
            "summary": dict(sorted(summary.items())),
        },
    )
    _write_json(
        failure_streaks_path,
        {
            "by_url": dict(sorted(failure_streaks.items())),
            "by_host": dict(sorted(_host_failure_streaks(failure_streaks).items())),
        },
    )
    return summary


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--triage-input", type=Path, required=True, help="Recruitment triage JSONL."
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="Common Crawl discovery output directory."
    )
    parser.add_argument(
        "--max-indexes",
        type=int,
        default=DEFAULT_MAX_INDEXES,
        help="Newest Common Crawl CDX indexes to query.",
    )
    parser.add_argument(
        "--max-results-per-query",
        type=int,
        default=DEFAULT_MAX_RESULTS_PER_QUERY,
        help="Common Crawl CDX API limit per domain/pattern query.",
    )
    parser.add_argument(
        "--max-discovered-urls",
        type=int,
        default=DEFAULT_MAX_DISCOVERED_URLS,
        help="Hard cap for new additional-seed rows written.",
    )
    parser.add_argument(
        "--provider-circuit-breaker-failures",
        type=int,
        default=DEFAULT_PROVIDER_CIRCUIT_BREAKER_FAILURES,
        help=(
            "Stop cleanly after this many consecutive retryable Common Crawl provider "
            "failures across queries; use 0 to disable."
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0, help="Per-request timeout.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=(
            "Concurrent Common Crawl index query workers. Default is provider-safe; "
            "higher values may trigger Common Crawl rate limits."
        ),
    )
    parser.add_argument(
        "--retry-rounds",
        type=int,
        default=DEFAULT_RETRY_ROUNDS,
        help="Retries for network/429/5xx errors after the initial attempt (default: 4; total: 5).",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=DEFAULT_RETRY_BACKOFF_SECONDS,
        help="Initial retry backoff for network/429/5xx errors.",
    )
    parser.add_argument(
        "--max-retry-backoff-seconds",
        type=float,
        default=DEFAULT_MAX_RETRY_BACKOFF_SECONDS,
        help="Maximum retry backoff for network/429/5xx errors.",
    )
    parser.add_argument("--no-resume", action="store_true", help="Do not skip completed queries.")
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
    try:
        triage_rows = _load_jsonl(args.triage_input)
        summary = discover_recruitment_commoncrawl_urls(
            triage_rows,
            args.output_dir,
            config=FetchConfig(
                timeout_seconds=args.timeout_seconds,
                user_agent=args.user_agent,
                allow_sandbox_egress_alias=args.allow_sandbox_egress_alias,
                max_results_per_query=args.max_results_per_query,
                retry_backoff_seconds=args.retry_backoff_seconds,
                max_retry_backoff_seconds=args.max_retry_backoff_seconds,
            ),
            max_indexes=args.max_indexes,
            retry_rounds=args.retry_rounds,
            concurrency=args.concurrency,
            max_discovered_urls=args.max_discovered_urls,
            provider_circuit_breaker_failures=args.provider_circuit_breaker_failures,
            resume=not args.no_resume,
        )
    except (OSError, ValueError) as error:
        print(f"recruitment Common Crawl discovery failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(dict(sorted(summary.items())), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
