#!/usr/bin/env python3
"""Discover recruitment URL seeds from public robots.txt and XML sitemaps.

This stage expands the recruitment crawler's queue without fetching full page
content. It only reads public robots/sitemap resources, keeps URLs whose
path/query look recruitment-related, and writes append-only JSONL artifacts for
resumable downstream crawls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
import zlib
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, build_opener

from collect_recruitment_pages import (
    DEFAULT_USER_AGENT,
    ELIGIBLE_ASSESSMENTS,
    ELIGIBLE_RELATIONSHIPS,
    TEXT_CONTENT_TYPE_PREFIXES,
    TEXT_CONTENT_TYPES,
    UnsafeTargetError,
    _ensure_public_http_url,
    _hostname,
    _json_line,
    _PublicRedirectHandler,
    canonicalize_url,
    is_eligible_crawl_url,
    link_relationship,
)

DISCOVERED_FILENAME = "discovered_recruitment_sitemap_urls.jsonl"
RECEIPT_FILENAME = "recruitment_sitemap_discovery_receipts.jsonl"
SUMMARY_FILENAME = "recruitment_sitemap_discovery_summary.json"
MANIFEST_FILENAME = "recruitment_sitemap_discovery_manifest.json"
FAILURE_STREAKS_FILENAME = "recruitment_sitemap_failure_streaks.json"

DEFAULT_MAX_SITEMAP_BYTES = 5_000_000
DEFAULT_RETRY_ROUNDS = 4
DEFAULT_CONCURRENCY = 12
GZIP_DECOMPRESS_CHUNK_BYTES = 64 * 1024
SITEMAP_CANDIDATE_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap/sitemap.xml",
    "/sitemaps.xml",
)
XML_CONTENT_TYPES = {
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
    "text/xml",
}
RETRYABLE_STATUSES = {"network_error", "retryable_http_error"}


@dataclass(frozen=True)
class SitemapRoot:
    root_url: str
    canonical_source_url: str
    source_record_id: str | None
    source_index: int | None
    source_assessment: str | None
    source_relationship: str
    registrable_domain: str
    ats_path_prefix: str | None = None


@dataclass(frozen=True)
class FetchTarget:
    canonical_url: str
    url: str
    kind: str
    root: SitemapRoot
    discovered_from: str | None = None


@dataclass(frozen=True)
class FetchConfig:
    timeout_seconds: float = 20.0
    max_sitemap_bytes: int = DEFAULT_MAX_SITEMAP_BYTES
    user_agent: str = DEFAULT_USER_AGENT
    allow_sandbox_egress_alias: bool = False


@dataclass(frozen=True)
class FetchResult:
    target: FetchTarget
    status: str
    body_text: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    body_bytes: int = 0
    body_truncated: bool = False
    body_sha256: str | None = None
    error_kind: str | None = None


class DecompressedSizeLimitError(ValueError):
    """Raised when a compressed sitemap inflates beyond the configured cap."""


Fetcher = Callable[[FetchTarget, FetchConfig], FetchResult]


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


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _path_prefix_for_known_ats(url: str, relationship: str) -> str | None:
    if relationship != "known_ats":
        return None
    path_parts = [part for part in urlsplit(url).path.split("/") if part]
    return f"/{path_parts[0]}" if path_parts else None


def select_sitemap_roots(rows: Iterable[dict[str, Any]]) -> list[SitemapRoot]:
    roots: list[SitemapRoot] = []
    seen: set[tuple[str, str | None]] = set()
    for source_index, row in enumerate(rows):
        assessment = row.get("employer_hiring_assessment")
        if assessment not in ELIGIBLE_ASSESSMENTS:
            continue
        registrable_domain = str(row.get("registrable_domain") or "").strip().lower()
        links = row.get("links")
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, dict):
                continue
            href = link.get("href")
            stated_relationship = link.get("relationship")
            if not isinstance(href, str) or stated_relationship not in ELIGIBLE_RELATIONSHIPS:
                continue
            canonical_source_url = canonicalize_url(href)
            if not canonical_source_url:
                continue
            actual_relationship = link_relationship(canonical_source_url, registrable_domain)
            if actual_relationship not in ELIGIBLE_RELATIONSHIPS:
                continue
            if not is_eligible_crawl_url(canonical_source_url, registrable_domain, ""):
                continue
            root_url = _origin(canonical_source_url)
            ats_path_prefix = _path_prefix_for_known_ats(canonical_source_url, actual_relationship)
            key = (root_url, ats_path_prefix)
            if key in seen:
                continue
            seen.add(key)
            roots.append(
                SitemapRoot(
                    root_url=root_url,
                    canonical_source_url=canonical_source_url,
                    source_record_id=(
                        row.get("record_id") if isinstance(row.get("record_id"), str) else None
                    ),
                    source_index=source_index,
                    source_assessment=str(assessment),
                    source_relationship=actual_relationship,
                    registrable_domain=registrable_domain,
                    ats_path_prefix=ats_path_prefix,
                )
            )
    return roots


def sitemap_targets_for_root(root: SitemapRoot) -> list[FetchTarget]:
    targets = [
        FetchTarget(
            canonical_url=canonicalize_url(f"{root.root_url}/robots.txt"),
            url=f"{root.root_url}/robots.txt",
            kind="robots",
            root=root,
        )
    ]
    for path in SITEMAP_CANDIDATE_PATHS:
        url = f"{root.root_url}{path}"
        targets.append(
            FetchTarget(
                canonical_url=canonicalize_url(url),
                url=url,
                kind="sitemap",
                root=root,
            )
        )
    return [target for target in targets if target.canonical_url]


def _is_supported_content_type(content_type: str) -> bool:
    return (
        content_type.startswith(TEXT_CONTENT_TYPE_PREFIXES)
        or content_type in TEXT_CONTENT_TYPES
        or content_type in XML_CONTENT_TYPES
        or content_type == "application/x-gzip"
    )


def _bounded_gzip_decompress(raw_body: bytes, max_output_bytes: int) -> tuple[bytes, bool]:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    chunks: list[bytes] = []
    output_bytes = 0
    for offset in range(0, len(raw_body), GZIP_DECOMPRESS_CHUNK_BYTES):
        chunk = raw_body[offset : offset + GZIP_DECOMPRESS_CHUNK_BYTES]
        remaining = max_output_bytes - output_bytes
        if remaining < 0:
            raise DecompressedSizeLimitError("gzip sitemap expanded beyond byte cap")
        decoded = decompressor.decompress(chunk, remaining + 1)
        output_bytes += len(decoded)
        if output_bytes > max_output_bytes:
            raise DecompressedSizeLimitError("gzip sitemap expanded beyond byte cap")
        chunks.append(decoded)
    remaining = max_output_bytes - output_bytes
    decoded = decompressor.flush(remaining + 1)
    output_bytes += len(decoded)
    if output_bytes > max_output_bytes:
        raise DecompressedSizeLimitError("gzip sitemap expanded beyond byte cap")
    chunks.append(decoded)
    return b"".join(chunks), bool(decompressor.unused_data)


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _is_retryable_status(status: str) -> bool:
    return status in RETRYABLE_STATUSES


def fetch_sitemap_target(target: FetchTarget, config: FetchConfig) -> FetchResult:
    try:
        _ensure_public_http_url(
            target.url,
            allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
        )
    except UnsafeTargetError as error:
        return FetchResult(target=target, status="invalid_url", error_kind=type(error).__name__)
    except OSError as error:
        return FetchResult(target=target, status="network_error", error_kind=type(error).__name__)

    request = Request(
        target.url,
        headers={
            "Accept": "application/xml,text/xml,text/plain;q=0.9,*/*;q=0.1",
            "User-Agent": config.user_agent,
        },
        method="GET",
    )
    try:
        opener = build_opener(
            _PublicRedirectHandler(
                allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
            )
        )
        with opener.open(request, timeout=config.timeout_seconds) as response:
            raw_body = response.read(config.max_sitemap_bytes + 1)
            body_truncated = len(raw_body) > config.max_sitemap_bytes
            if body_truncated:
                raw_body = raw_body[: config.max_sitemap_bytes]
            content_type = response.headers.get_content_type().lower()
            final_url = response.geturl()
            status_code = response.getcode()
            if not _is_supported_content_type(content_type):
                return FetchResult(
                    target=target,
                    status="unsupported_content_type",
                    final_url=final_url,
                    http_status=status_code,
                    content_type=content_type,
                    body_bytes=len(raw_body),
                    body_truncated=body_truncated,
                )
            charset = response.headers.get_content_charset() or "utf-8"
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

    body_sha256 = hashlib.sha256(raw_body).hexdigest()
    if target.url.endswith(".gz") or content_type == "application/x-gzip":
        try:
            raw_body, decompressed_truncated = _bounded_gzip_decompress(
                raw_body, config.max_sitemap_bytes
            )
            body_truncated = body_truncated or decompressed_truncated
        except DecompressedSizeLimitError as error:
            return FetchResult(
                target=target,
                status="decompressed_size_cap_reached",
                final_url=final_url,
                http_status=status_code,
                content_type=content_type,
                body_bytes=config.max_sitemap_bytes,
                body_truncated=True,
                body_sha256=body_sha256,
                error_kind=type(error).__name__,
            )
        except zlib.error as error:
            return FetchResult(
                target=target,
                status="parse_error",
                final_url=final_url,
                http_status=status_code,
                content_type=content_type,
                body_bytes=len(raw_body),
                body_truncated=body_truncated,
                body_sha256=body_sha256,
                error_kind=type(error).__name__,
            )
    text = raw_body.decode(charset, errors="replace")
    if not text.strip():
        return FetchResult(
            target=target,
            status="empty_text",
            final_url=final_url,
            http_status=status_code,
            content_type=content_type,
            body_bytes=len(raw_body),
            body_truncated=body_truncated,
            body_sha256=body_sha256,
        )
    return FetchResult(
        target=target,
        status="fetched",
        body_text=text,
        final_url=final_url,
        http_status=status_code,
        content_type=content_type,
        body_bytes=len(raw_body),
        body_truncated=body_truncated,
        body_sha256=body_sha256,
    )


def parse_robots_sitemaps(robots_text: str, base_url: str) -> tuple[str, ...]:
    urls: list[str] = []
    seen: set[str] = set()
    for line in robots_text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped or ":" not in stripped:
            continue
        field, value = stripped.split(":", 1)
        if field.strip().lower() != "sitemap":
            continue
        canonical = canonicalize_url(urljoin(base_url, value.strip()))
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        urls.append(canonical)
    return tuple(urls)


def parse_sitemap_xml(xml_text: str) -> tuple[str, ...]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ()
    urls: list[str] = []
    seen: set[str] = set()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() != "loc" or not element.text:
            continue
        canonical = canonicalize_url(element.text.strip())
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        urls.append(canonical)
    return tuple(urls)


def _within_ats_path_scope(url: str, root: SitemapRoot) -> bool:
    if not root.ats_path_prefix:
        return True
    source_host = _hostname(root.canonical_source_url)
    target_host = _hostname(url)
    if source_host != target_host:
        return False
    target_path = urlsplit(url).path or "/"
    return target_path == root.ats_path_prefix or target_path.startswith(f"{root.ats_path_prefix}/")


def is_allowed_sitemap_target_url(url: str, root: SitemapRoot) -> bool:
    canonical = canonicalize_url(url)
    if not canonical:
        return False
    relationship = link_relationship(canonical, root.registrable_domain)
    return relationship in ELIGIBLE_RELATIONSHIPS and _within_ats_path_scope(canonical, root)


def is_retained_recruitment_url(url: str, root: SitemapRoot) -> bool:
    canonical = canonicalize_url(url)
    if not canonical:
        return False
    relationship = link_relationship(canonical, root.registrable_domain)
    return (
        relationship in ELIGIBLE_RELATIONSHIPS
        and is_eligible_crawl_url(canonical, root.registrable_domain, "")
        and _within_ats_path_scope(canonical, root)
    )


def _receipt_row(result: FetchResult) -> dict[str, Any]:
    target = result.target
    row = {
        "canonical_url": target.canonical_url,
        "requested_url": target.url,
        "target_kind": target.kind,
        "root_url": target.root.root_url,
        "source_record_id": target.root.source_record_id,
        "source_index": target.root.source_index,
        "source_assessment": target.root.source_assessment,
        "source_relationship": target.root.source_relationship,
        "registrable_domain": target.root.registrable_domain,
        "ats_path_prefix": target.root.ats_path_prefix,
        "discovered_from": target.discovered_from,
        "capture_status": result.status,
        "captured_at": _captured_at(),
        "final_url": result.final_url,
        "http_status": result.http_status,
        "content_type": result.content_type,
        "body_bytes": result.body_bytes,
        "body_truncated": result.body_truncated,
        "body_sha256": result.body_sha256,
        "error_kind": result.error_kind,
    }
    return row


def _discovered_row(url: str, source: FetchTarget) -> dict[str, Any]:
    canonical = canonicalize_url(url)
    return {
        "canonical_url": canonical,
        "url": canonical,
        "source_sitemap_url": source.canonical_url,
        "source_target_kind": source.kind,
        "root_url": source.root.root_url,
        "source_record_id": source.root.source_record_id,
        "source_index": source.root.source_index,
        "source_assessment": source.root.source_assessment,
        "source_relationship": link_relationship(canonical, source.root.registrable_domain),
        "registrable_domain": source.root.registrable_domain,
        "ats_path_prefix": source.root.ats_path_prefix,
        "discovered_at": _captured_at(),
        "discovery_method": "robots.txt and XML sitemap parsing; no browser, login, forms, or JS",
    }


def _resume_state(
    receipt_path: Path, discovered_path: Path, *, retry_rounds: int
) -> tuple[set[str], set[str], Counter[str]]:
    completed_targets: set[str] = set()
    discovered_urls: set[str] = set()
    failure_streaks: Counter[str] = Counter()
    for row in _load_output_jsonl(discovered_path):
        canonical = row.get("canonical_url")
        if isinstance(canonical, str) and canonical:
            discovered_urls.add(canonical)
    for row in _load_output_jsonl(receipt_path):
        canonical = row.get("canonical_url")
        if not isinstance(canonical, str) or not canonical:
            continue
        status = row.get("capture_status")
        if isinstance(status, str) and _is_retryable_status(status):
            completed_targets.discard(canonical)
            failure_streaks[canonical] += 1
            if failure_streaks[canonical] > retry_rounds:
                completed_targets.add(canonical)
        else:
            completed_targets.add(canonical)
            failure_streaks[canonical] = 0
    return completed_targets, discovered_urls, failure_streaks


def _host_failure_streaks(failure_streaks: Counter[str]) -> Counter[str]:
    by_host: Counter[str] = Counter()
    for url, count in failure_streaks.items():
        by_host[_hostname(url)] += count
    return by_host


def discover_recruitment_sitemap_urls(
    triage_rows: Iterable[dict[str, Any]],
    output_dir: Path,
    *,
    config: FetchConfig,
    retry_rounds: int = DEFAULT_RETRY_ROUNDS,
    concurrency: int = DEFAULT_CONCURRENCY,
    resume: bool = True,
    fetcher: Fetcher = fetch_sitemap_target,
) -> Counter[str]:
    if retry_rounds < 0:
        raise ValueError("retry_rounds must be at least 0")
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if config.max_sitemap_bytes < 1:
        raise ValueError("max_sitemap_bytes must be at least 1")

    output_dir.mkdir(parents=True, exist_ok=True)
    discovered_path = output_dir / DISCOVERED_FILENAME
    receipt_path = output_dir / RECEIPT_FILENAME
    summary_path = output_dir / SUMMARY_FILENAME
    manifest_path = output_dir / MANIFEST_FILENAME
    failure_streaks_path = output_dir / FAILURE_STREAKS_FILENAME

    if resume:
        completed_targets, discovered_urls, failure_streaks = _resume_state(
            receipt_path, discovered_path, retry_rounds=retry_rounds
        )
    else:
        completed_targets = set()
        discovered_urls = set()
        failure_streaks = Counter()

    roots = select_sitemap_roots(triage_rows)
    queue: deque[FetchTarget] = deque()
    queued_targets: set[str] = set()
    for root in roots:
        for target in sitemap_targets_for_root(root):
            if resume and target.canonical_url in completed_targets:
                continue
            if target.canonical_url in queued_targets:
                continue
            queued_targets.add(target.canonical_url)
            queue.append(target)

    summary: Counter[str] = Counter()
    summary["root_count"] = len(roots)
    summary["resume_completed_target_count"] = len(completed_targets)
    summary["resume_discovered_url_count"] = len(discovered_urls)

    with (
        discovered_path.open("a", encoding="utf-8") as discovered_handle,
        receipt_path.open("a", encoding="utf-8") as receipt_handle,
        ThreadPoolExecutor(max_workers=concurrency) as executor,
    ):
        futures: dict[Future[FetchResult], FetchTarget] = {}

        while queue or futures:
            while queue and len(futures) < concurrency:
                target = queue.popleft()
                futures[executor.submit(fetcher, target, config)] = target

            if not futures:
                break

            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
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
                _append_json_line(receipt_handle, _receipt_row(result))
                summary[result.status] += 1
                summary["attempted"] += 1

                if _is_retryable_status(result.status):
                    failure_streaks[target.canonical_url] += 1
                    if failure_streaks[target.canonical_url] <= retry_rounds:
                        queue.append(target)
                        summary["retry_enqueued"] += 1
                    else:
                        completed_targets.add(target.canonical_url)
                        summary["network_error_retry_exhausted"] += 1
                    continue

                completed_targets.add(target.canonical_url)
                failure_streaks[target.canonical_url] = 0
                if result.status != "fetched" or not result.body_text:
                    continue

                if target.kind == "robots":
                    for sitemap_url in parse_robots_sitemaps(result.body_text, target.url):
                        canonical = canonicalize_url(sitemap_url)
                        if (
                            not canonical
                            or canonical in completed_targets
                            or canonical in queued_targets
                        ):
                            continue
                        if not is_allowed_sitemap_target_url(canonical, target.root):
                            continue
                        queued_targets.add(canonical)
                        queue.append(
                            FetchTarget(
                                canonical_url=canonical,
                                url=canonical,
                                kind="sitemap",
                                root=target.root,
                                discovered_from=target.canonical_url,
                            )
                        )
                        summary["sitemap_enqueued_from_robots"] += 1
                    continue

                for loc in parse_sitemap_xml(result.body_text):
                    if loc.endswith(".xml") or loc.endswith(".xml.gz"):
                        canonical = canonicalize_url(loc)
                        if (
                            not canonical
                            or canonical in completed_targets
                            or canonical in queued_targets
                        ):
                            continue
                        if not is_allowed_sitemap_target_url(canonical, target.root):
                            continue
                        queued_targets.add(canonical)
                        queue.append(
                            FetchTarget(
                                canonical_url=canonical,
                                url=canonical,
                                kind="sitemap",
                                root=target.root,
                                discovered_from=target.canonical_url,
                            )
                        )
                        summary["sitemap_index_child_enqueued"] += 1
                        continue
                    if not is_retained_recruitment_url(loc, target.root):
                        continue
                    canonical = canonicalize_url(loc)
                    if canonical in discovered_urls:
                        summary["discovered_duplicate_skipped"] += 1
                        continue
                    discovered_urls.add(canonical)
                    _append_json_line(discovered_handle, _discovered_row(canonical, target))
                    summary["discovered_url_written"] += 1

    _write_json(summary_path, dict(sorted(summary.items())))
    _write_json(
        manifest_path,
        {
            "created_at": _captured_at(),
            "discovered_file": DISCOVERED_FILENAME,
            "receipt_file": RECEIPT_FILENAME,
            "summary_file": SUMMARY_FILENAME,
            "failure_streaks_file": FAILURE_STREAKS_FILENAME,
            "max_sitemap_bytes": config.max_sitemap_bytes,
            "timeout_seconds": config.timeout_seconds,
            "concurrency": concurrency,
            "retry_rounds": retry_rounds,
            "allow_sandbox_egress_alias": config.allow_sandbox_egress_alias,
            "resume": resume,
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
        "--output-dir", type=Path, required=True, help="Discovery output directory."
    )
    parser.add_argument(
        "--max-sitemap-bytes",
        type=int,
        default=DEFAULT_MAX_SITEMAP_BYTES,
        help="Maximum bytes read per robots/sitemap response.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0, help="Per-request timeout.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="Concurrent robots/sitemap HTTP workers.",
    )
    parser.add_argument(
        "--retry-rounds",
        type=int,
        default=DEFAULT_RETRY_ROUNDS,
        help="Retries for network errors after the initial attempt (default: 4; total: 5).",
    )
    parser.add_argument("--no-resume", action="store_true", help="Do not skip completed targets.")
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
        summary = discover_recruitment_sitemap_urls(
            triage_rows,
            args.output_dir,
            config=FetchConfig(
                timeout_seconds=args.timeout_seconds,
                max_sitemap_bytes=args.max_sitemap_bytes,
                user_agent=args.user_agent,
                allow_sandbox_egress_alias=args.allow_sandbox_egress_alias,
            ),
            retry_rounds=args.retry_rounds,
            concurrency=args.concurrency,
            resume=not args.no_resume,
        )
    except (OSError, ValueError) as error:
        print(f"recruitment sitemap discovery failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(dict(sorted(summary.items())), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
