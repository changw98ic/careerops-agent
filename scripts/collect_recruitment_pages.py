#!/usr/bin/env python3
"""Collect public recruitment-page candidates from recruitment triage JSONL.

This is a resumable, stdlib-only crawler for first-party career pages and known
ATS URLs already identified by the deterministic recruitment triage pass. It
does not use a browser, log in, submit forms, or execute JavaScript.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import re
import socket
import stat
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

RAW_FILENAME = "recruitment_pages_raw.jsonl"
RECEIPT_FILENAME = "recruitment_page_receipts.jsonl"
MANIFEST_FILENAME = "recruitment_pages_manifest.json"
SUMMARY_FILENAME = "recruitment_pages_summary.json"
FAILURE_STREAKS_FILENAME = "recruitment_page_failure_streaks.json"
DEFAULT_USER_AGENT = "CareerOpsRecruitmentPageCollector/0.1"
DEFAULT_MAX_STORED_BYTES = 80 * 1024 * 1024 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_MAX_CONCURRENCY_PER_HOST = 8
DEFAULT_HOST_FAILURE_COOLDOWN_THRESHOLD = 3
DEFAULT_HOST_FAILURE_COOLDOWN_SECONDS = 30.0
ELIGIBLE_ASSESSMENTS = {
    "likely_self_hiring",
    "first_party_career_entry",
    "external_ats_needs_verification",
    "self_hiring_text_only_needs_verification",
}
ELIGIBLE_RELATIONSHIPS = {"first_party", "known_ats"}
TEXT_CONTENT_TYPE_PREFIXES = ("text/",)
TEXT_CONTENT_TYPES = {
    "application/json",
    "application/xhtml+xml",
    "application/xml",
}
LOCAL_HOSTNAMES = {"localhost"}
SANDBOX_EGRESS_ALIAS_NETWORK = ipaddress.ip_network("198.18.0.0/15")
ATS_HOST_PATTERNS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workdayjobs.com",
    "myworkdayjobs.com",
    "workable.com",
    "smartrecruiters.com",
    "jobvite.com",
    "icims.com",
    "bamboohr.com",
    "breezy.hr",
    "recruitee.com",
    "personio.de",
    "comeet.com",
    "teamtailor.com",
    "jobs.ashbyhq.com",
    "boards.greenhouse.io",
)
CAREER_LINK_PATTERN = re.compile(
    r"("
    r"careers?|jobs?|openings?|positions?|roles?|vacanc(?:y|ies)|"
    r"join(?:[-_\s]+us|[-_\s]+our[-_\s]+team)?|work[-_\s]+with[-_\s]+us|"
    r"apply|hiring|recruit(?:ing|ment)|"
    r"\u52a0\u5165\u6211\u4eec|\u62db\u8058|\u804c\u4f4d"
    r")",
    re.IGNORECASE,
)
SKIPPED_TAGS = {"script", "style", "noscript", "template", "svg", "canvas"}


@dataclass(frozen=True)
class CrawlRequest:
    canonical_url: str
    url: str
    source_record_id: str | None
    source_index: int | None
    source_assessment: str | None
    source_relationship: str
    registrable_domain: str
    depth: int
    discovered_from: str | None = None
    seed_source: str = "triage_link"
    source_seed_url: str | None = None


@dataclass(frozen=True)
class CollectorConfig:
    timeout_seconds: float = 20.0
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    user_agent: str = DEFAULT_USER_AGENT
    allow_sandbox_egress_alias: bool = False


@dataclass(frozen=True)
class FetchOutcome:
    raw_row: dict[str, Any] | None
    receipt: dict[str, Any]
    extracted_links: tuple[str, ...] = ()


@dataclass(frozen=True)
class FetchContext:
    opener: Any


@dataclass(frozen=True)
class PendingRequest:
    request: CrawlRequest
    host: str


class UnsafeTargetError(ValueError):
    """Raised when a URL points at a local or non-public network target."""


_FETCH_CONTEXT = threading.local()


class _PublicRedirectHandler(HTTPRedirectHandler):
    def __init__(self, *, allow_sandbox_egress_alias: bool) -> None:
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
            allow_sandbox_egress_alias=self._allow_sandbox_egress_alias,
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class LinkExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._skip_stack: list[str] = []
        self._anchor_stack: list[dict[str, Any]] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in SKIPPED_TAGS:
            self._skip_stack.append(lowered)
            return
        if self._skip_stack or lowered != "a":
            return
        attrs_by_name = {name.lower(): value for name, value in attrs if value is not None}
        href = attrs_by_name.get("href", "").strip()
        self._anchor_stack.append({"href": href, "chunks": []})

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if self._skip_stack:
            if lowered == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if lowered == "a" and self._anchor_stack:
            anchor = self._anchor_stack.pop()
            href = _normalize_href(str(anchor["href"]), self._base_url)
            text = _normalize_space(" ".join(anchor["chunks"]))
            if href:
                self.links.append((html.unescape(text), href))

    def handle_data(self, data: str) -> None:
        if self._skip_stack or not self._anchor_stack:
            return
        text = _normalize_space(data)
        if text:
            self._anchor_stack[-1]["chunks"].append(text)


def _captured_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_line(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _append_json_line(handle: Any, row: dict[str, Any]) -> None:
    handle.write(f"{_json_line(row)}\n")
    handle.flush()


def _build_fetch_context(config: CollectorConfig) -> FetchContext:
    return FetchContext(
        opener=build_opener(
            _PublicRedirectHandler(
                allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
            )
        ),
    )


def _set_thread_fetch_context(config: CollectorConfig) -> None:
    _FETCH_CONTEXT.value = _build_fetch_context(config)


def _thread_fetch_context() -> FetchContext | None:
    value = getattr(_FETCH_CONTEXT, "value", None)
    return value if isinstance(value, FetchContext) else None


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalize_href(href: str, base_url: str) -> str:
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return ""
    return urljoin(base_url, href)


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


def _iter_output_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
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
            yield decoded


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
    path = re.sub(r"/{2,}", "/", path)
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def _hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").rstrip(".").lower()


def _request_host(request: CrawlRequest) -> str:
    return _hostname(request.canonical_url) or _hostname(request.url)


def _host_matches_domain(hostname: str, domain: str) -> bool:
    host = hostname.rstrip(".").lower()
    normalized_domain = domain.rstrip(".").lower()
    return bool(host and normalized_domain) and (
        host == normalized_domain or host.endswith(f".{normalized_domain}")
    )


def _href_has_ats_host(url: str) -> bool:
    hostname = _hostname(url)
    return any(hostname == host or hostname.endswith(f".{host}") for host in ATS_HOST_PATTERNS)


def _is_candidate_link(url: str, text: str = "") -> bool:
    parsed = urlsplit(url)
    searchable = _normalize_space(f"{text} {parsed.path} {parsed.query}")
    return bool(CAREER_LINK_PATTERN.search(searchable))


def link_relationship(url: str, registrable_domain: str) -> str:
    if _host_matches_domain(_hostname(url), registrable_domain):
        return "first_party"
    if _href_has_ats_host(url):
        return "known_ats"
    return "external"


def is_eligible_crawl_url(url: str, registrable_domain: str, text: str = "") -> bool:
    canonical = canonicalize_url(url)
    if not canonical:
        return False
    relationship = link_relationship(canonical, registrable_domain)
    return relationship in ELIGIBLE_RELATIONSHIPS and _is_candidate_link(canonical, text)


def select_seed_urls(
    rows: Iterable[dict[str, Any]], seen: set[str] | None = None
) -> list[CrawlRequest]:
    seeds: list[CrawlRequest] = []
    if seen is None:
        seen = set()
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
            relationship = link.get("relationship")
            text = str(link.get("text") or "")
            if not isinstance(href, str) or relationship not in ELIGIBLE_RELATIONSHIPS:
                continue
            canonical = canonicalize_url(href)
            if not canonical or canonical in seen:
                continue
            if link_relationship(canonical, registrable_domain) not in ELIGIBLE_RELATIONSHIPS:
                continue
            if not _is_candidate_link(canonical, text):
                continue
            seen.add(canonical)
            seeds.append(
                CrawlRequest(
                    canonical_url=canonical,
                    url=href,
                    source_record_id=(
                        row.get("record_id") if isinstance(row.get("record_id"), str) else None
                    ),
                    source_index=source_index,
                    source_assessment=str(assessment),
                    source_relationship=str(relationship),
                    registrable_domain=registrable_domain,
                    depth=0,
                    seed_source="triage_link",
                    source_seed_url=canonical,
                )
            )
    return seeds


def select_additional_seed_urls(
    rows: Iterable[dict[str, Any]], seen: set[str] | None = None
) -> list[CrawlRequest]:
    seeds: list[CrawlRequest] = []
    if seen is None:
        seen = set()
    for source_index, row in enumerate(rows):
        assessment = row.get("source_assessment")
        if assessment not in ELIGIBLE_ASSESSMENTS:
            continue
        registrable_domain = str(row.get("registrable_domain") or "").strip().lower()
        href = row.get("url") or row.get("canonical_url")
        if not isinstance(href, str):
            continue
        canonical = canonicalize_url(href)
        if not canonical or canonical in seen:
            continue
        relationship = link_relationship(canonical, registrable_domain)
        if relationship not in ELIGIBLE_RELATIONSHIPS:
            continue
        if not _is_candidate_link(canonical, ""):
            continue
        source_index_value = row.get("source_index")
        source_sitemap_url = row.get("source_sitemap_url")
        seen.add(canonical)
        seeds.append(
            CrawlRequest(
                canonical_url=canonical,
                url=canonical,
                source_record_id=(
                    row.get("source_record_id")
                    if isinstance(row.get("source_record_id"), str)
                    else None
                ),
                source_index=(
                    source_index_value if isinstance(source_index_value, int) else source_index
                ),
                source_assessment=str(assessment),
                source_relationship=relationship,
                registrable_domain=registrable_domain,
                depth=0,
                discovered_from=source_sitemap_url if isinstance(source_sitemap_url, str) else None,
                seed_source="additional_seed_jsonl",
                source_seed_url=canonical,
            )
        )
    return seeds


def extract_recruitment_links(
    html_text: str, base_url: str, registrable_domain: str
) -> tuple[str, ...]:
    parser = LinkExtractor(base_url)
    parser.feed(html_text)
    parser.close()
    links: list[str] = []
    seen: set[str] = set()
    for text, href in parser.links:
        canonical = canonicalize_url(href)
        if not canonical or canonical in seen:
            continue
        if not is_eligible_crawl_url(canonical, registrable_domain, text):
            continue
        seen.add(canonical)
        links.append(canonical)
    return tuple(links)


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


def _ensure_public_http_url(
    url: str,
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


def _base_receipt(request: CrawlRequest, status: str) -> dict[str, Any]:
    return {
        "canonical_url": request.canonical_url,
        "requested_url": request.url,
        "source_record_id": request.source_record_id,
        "source_index": request.source_index,
        "source_assessment": request.source_assessment,
        "source_relationship": request.source_relationship,
        "registrable_domain": request.registrable_domain,
        "depth": request.depth,
        "discovered_from": request.discovered_from,
        "seed_source": request.seed_source,
        "source_seed_url": request.source_seed_url,
        "capture_status": status,
        "captured_at": _captured_at(),
        "raw_data_file": RAW_FILENAME if status == "captured" else None,
    }


def fetch_page(request: CrawlRequest, config: CollectorConfig) -> FetchOutcome:
    context = _thread_fetch_context()
    try:
        _ensure_public_http_url(
            request.url,
            allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
        )
    except UnsafeTargetError as error:
        receipt = _base_receipt(request, "invalid_url")
        receipt.update({"error_kind": type(error).__name__, "raw_data_file": None})
        return FetchOutcome(raw_row=None, receipt=receipt)
    except OSError as error:
        receipt = _base_receipt(request, "network_error")
        receipt.update({"error_kind": type(error).__name__, "raw_data_file": None})
        return FetchOutcome(raw_row=None, receipt=receipt)

    http_request = Request(
        request.url,
        headers={
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.1",
            "User-Agent": config.user_agent,
        },
        method="GET",
    )
    try:
        opener = (
            context.opener
            if context is not None
            else build_opener(
                _PublicRedirectHandler(
                    allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
                )
            )
        )
        with opener.open(http_request, timeout=config.timeout_seconds) as response:
            raw_body = response.read(config.max_response_bytes + 1)
            body_truncated = len(raw_body) > config.max_response_bytes
            if body_truncated:
                raw_body = raw_body[: config.max_response_bytes]
            content_type = response.headers.get_content_type().lower()
            final_url = response.geturl()
            status_code = response.getcode()
            if not _is_supported_text_content_type(content_type):
                receipt = _base_receipt(request, "unsupported_content_type")
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
        receipt = _base_receipt(request, "http_error")
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
        receipt = _base_receipt(request, "invalid_url")
        receipt.update({"error_kind": type(error).__name__, "raw_data_file": None})
        return FetchOutcome(raw_row=None, receipt=receipt)
    except (OSError, TimeoutError, URLError) as error:
        receipt = _base_receipt(request, "network_error")
        receipt.update({"error_kind": type(error).__name__, "raw_data_file": None})
        return FetchOutcome(raw_row=None, receipt=receipt)

    response_text = raw_body.decode(charset, errors="replace")
    body_sha256 = hashlib.sha256(raw_body).hexdigest()
    if not response_text.strip():
        receipt = _base_receipt(request, "empty_text")
        receipt.update(
            {
                "final_url": final_url,
                "http_status": status_code,
                "content_type": content_type,
                "body_bytes": len(raw_body),
                "body_truncated": body_truncated,
                "body_sha256": body_sha256,
                "raw_data_file": None,
            }
        )
        return FetchOutcome(raw_row=None, receipt=receipt)

    extracted_links = extract_recruitment_links(
        response_text, final_url, request.registrable_domain
    )
    raw_row = {
        "canonical_url": request.canonical_url,
        "requested_url": request.url,
        "final_url": final_url,
        "source_record_id": request.source_record_id,
        "source_index": request.source_index,
        "source_assessment": request.source_assessment,
        "source_relationship": request.source_relationship,
        "registrable_domain": request.registrable_domain,
        "depth": request.depth,
        "discovered_from": request.discovered_from,
        "http_status": status_code,
        "content_type": content_type,
        "body_bytes": len(raw_body),
        "body_truncated": body_truncated,
        "body_sha256": body_sha256,
        "response_text": response_text,
        "extracted_links": list(extracted_links),
        "capture_method": (
            "concurrent HTTP GET; response text only; no browser, login, forms, or JS"
        ),
    }
    receipt = _base_receipt(request, "captured")
    receipt.update(
        {
            "final_url": final_url,
            "http_status": status_code,
            "content_type": content_type,
            "body_bytes": len(raw_body),
            "body_truncated": body_truncated,
            "body_sha256": body_sha256,
            "extracted_link_count": len(extracted_links),
        }
    )
    raw_row["captured_at"] = receipt["captured_at"]
    return FetchOutcome(raw_row=raw_row, receipt=receipt, extracted_links=extracted_links)


def _resume_state(
    receipt_path: Path, *, retry_rounds: int
) -> tuple[set[str], Counter[str], Counter[str]]:
    completed: set[str] = set()
    url_failure_streaks: Counter[str] = Counter()
    host_failure_streaks: Counter[str] = Counter()
    for row in _load_output_jsonl(receipt_path):
        canonical = row.get("canonical_url")
        if isinstance(canonical, str) and canonical:
            hostname = _hostname(canonical)
            if _receipt_is_retryable_failure(row):
                completed.discard(canonical)
                url_failure_streaks[canonical] += 1
                host_failure_streaks[hostname] += 1
                if url_failure_streaks[canonical] > retry_rounds:
                    completed.add(canonical)
            else:
                completed.add(canonical)
                url_failure_streaks[canonical] = 0
                host_failure_streaks[hostname] = 0
    return completed, url_failure_streaks, host_failure_streaks


def _stored_bytes(raw_path: Path) -> int:
    return raw_path.stat().st_size if raw_path.exists() else 0


def _regular_file_size(path: Path, seen_files: set[tuple[int, int]]) -> int:
    try:
        file_stat = path.stat()
    except OSError:
        return 0
    if not stat.S_ISREG(file_stat.st_mode):
        return 0
    file_key = (file_stat.st_dev, file_stat.st_ino)
    if file_key in seen_files:
        return 0
    seen_files.add(file_key)
    return file_stat.st_size


def total_regular_file_bytes(paths: Iterable[Path]) -> int:
    total = 0
    seen_files: set[tuple[int, int]] = set()
    for root in paths:
        if root.is_symlink():
            continue
        if root.is_file():
            total += _regular_file_size(root, seen_files)
            continue
        if not root.is_dir():
            continue
        for candidate in root.rglob("*"):
            if candidate.is_symlink():
                continue
            if candidate.is_file():
                total += _regular_file_size(candidate, seen_files)
    return total


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _is_retryable_http_status(status_code: object) -> bool:
    return isinstance(status_code, int) and (status_code == 429 or 500 <= status_code <= 599)


def _receipt_is_retryable_failure(receipt: dict[str, Any]) -> bool:
    status = receipt.get("capture_status")
    return status == "network_error" or (
        status == "http_error" and _is_retryable_http_status(receipt.get("http_status"))
    )


def _request_from_discovered(parent: CrawlRequest, url: str) -> CrawlRequest:
    relationship = link_relationship(url, parent.registrable_domain)
    return CrawlRequest(
        canonical_url=canonicalize_url(url),
        url=url,
        source_record_id=parent.source_record_id,
        source_index=parent.source_index,
        source_assessment=parent.source_assessment,
        source_relationship=relationship,
        registrable_domain=parent.registrable_domain,
        depth=parent.depth + 1,
        discovered_from=parent.canonical_url,
        seed_source=parent.seed_source,
        source_seed_url=parent.source_seed_url,
    )


class HostAwarePendingQueue:
    """FIFO queue with optional host round-robin and per-host in-flight caps."""

    def __init__(
        self,
        requests: Iterable[CrawlRequest] = (),
        *,
        max_concurrency_per_host: int,
        global_concurrency: int,
    ) -> None:
        self._fifo: deque[CrawlRequest] | None = (
            deque() if max_concurrency_per_host >= global_concurrency else None
        )
        self._by_host: dict[str, deque[CrawlRequest]] = {}
        self._host_order: deque[str] = deque()
        self._size = 0
        self._max_concurrency_per_host = max_concurrency_per_host
        for request in requests:
            self.append(request)

    def __len__(self) -> int:
        return self._size

    def __bool__(self) -> bool:
        return self._size > 0

    def append(self, request: CrawlRequest) -> None:
        if self._fifo is not None:
            self._fifo.append(request)
            self._size += 1
            return

        host = _request_host(request)
        if host not in self._by_host:
            self._by_host[host] = deque()
            self._host_order.append(host)
        self._by_host[host].append(request)
        self._size += 1

    def popleft_eligible(self, in_flight_by_host: Counter[str]) -> CrawlRequest | None:
        return self.popleft_ready(in_flight_by_host, deferred_until_by_host={}, now=0.0)

    def popleft_ready(
        self,
        in_flight_by_host: Counter[str],
        deferred_until_by_host: dict[str, float],
        *,
        now: float,
    ) -> CrawlRequest | None:
        if self._fifo is not None:
            for _ in range(len(self._fifo)):
                request = self._fifo[0]
                deferred_until = deferred_until_by_host.get(_request_host(request), 0.0)
                if deferred_until > now:
                    self._fifo.rotate(-1)
                    continue
                self._size -= 1
                return self._fifo.popleft()
            return None

        for host in list(deferred_until_by_host):
            if deferred_until_by_host[host] <= now:
                del deferred_until_by_host[host]

        for _ in range(len(self._host_order)):
            host = self._host_order.popleft()
            host_queue = self._by_host.get(host)
            if not host_queue:
                self._by_host.pop(host, None)
                continue
            self._host_order.append(host)
            if deferred_until_by_host.get(host, 0.0) > now:
                continue
            if in_flight_by_host[host] >= self._max_concurrency_per_host:
                continue
            request = host_queue.popleft()
            if not host_queue:
                self._by_host.pop(host, None)
                with suppress(ValueError):
                    self._host_order.remove(host)
            self._size -= 1
            return request
        return None

    def next_ready_at(
        self,
        deferred_until_by_host: dict[str, float],
        *,
        now: float,
    ) -> float | None:
        if not self:
            return None
        if self._fifo is not None:
            ready_at_values = [
                deferred_until_by_host.get(_request_host(request), 0.0) for request in self._fifo
            ]
        else:
            ready_at_values = [
                deferred_until_by_host.get(host, 0.0)
                for host, requests in self._by_host.items()
                if requests
            ]
        future_ready_at = [ready_at for ready_at in ready_at_values if ready_at > now]
        if len(future_ready_at) == len(ready_at_values):
            return min(future_ready_at)
        return now


def _raw_row_parent_request(row: dict[str, Any]) -> CrawlRequest | None:
    canonical_url = row.get("canonical_url")
    if not isinstance(canonical_url, str):
        return None
    canonical = canonicalize_url(canonical_url)
    if not canonical:
        return None
    depth = row.get("depth")
    if not isinstance(depth, int):
        return None
    registrable_domain = row.get("registrable_domain")
    if not isinstance(registrable_domain, str) or not registrable_domain:
        return None
    requested_url = row.get("requested_url")
    source_relationship = row.get("source_relationship")
    seed_source = row.get("seed_source")
    return CrawlRequest(
        canonical_url=canonical,
        url=requested_url if isinstance(requested_url, str) else canonical,
        source_record_id=(
            row.get("source_record_id") if isinstance(row.get("source_record_id"), str) else None
        ),
        source_index=row.get("source_index") if isinstance(row.get("source_index"), int) else None,
        source_assessment=(
            row.get("source_assessment") if isinstance(row.get("source_assessment"), str) else None
        ),
        source_relationship=(
            source_relationship
            if isinstance(source_relationship, str)
            else link_relationship(canonical, registrable_domain)
        ),
        registrable_domain=registrable_domain,
        depth=depth,
        discovered_from=(
            row.get("discovered_from") if isinstance(row.get("discovered_from"), str) else None
        ),
        seed_source=seed_source if isinstance(seed_source, str) else "raw_resume",
        source_seed_url=(
            row.get("source_seed_url") if isinstance(row.get("source_seed_url"), str) else canonical
        ),
    )


def reconstruct_resume_discovered_requests(
    raw_path: Path,
    *,
    completed: set[str],
    seen: set[str],
    depth: int,
) -> list[CrawlRequest]:
    reconstructed: list[CrawlRequest] = []
    for row in _iter_output_jsonl(raw_path):
        parent = _raw_row_parent_request(row)
        if parent is None or parent.depth >= depth:
            continue
        extracted_links = row.get("extracted_links")
        if not isinstance(extracted_links, list):
            continue
        for discovered_url in extracted_links:
            if not isinstance(discovered_url, str):
                continue
            discovered_canonical = canonicalize_url(discovered_url)
            if (
                not discovered_canonical
                or discovered_canonical in seen
                or discovered_canonical in completed
            ):
                continue
            if not is_eligible_crawl_url(discovered_canonical, parent.registrable_domain):
                continue
            discovered_request = _request_from_discovered(parent, discovered_canonical)
            seen.add(discovered_canonical)
            reconstructed.append(discovered_request)
    return reconstructed


def collect_recruitment_pages(
    triage_rows: Iterable[dict[str, Any]],
    output_dir: Path,
    *,
    config: CollectorConfig,
    concurrency: int,
    retry_rounds: int,
    depth: int,
    additional_seed_rows: Iterable[dict[str, Any]] = (),
    max_stored_bytes: int = DEFAULT_MAX_STORED_BYTES,
    aggregate_data_dirs: Iterable[Path] = (),
    max_total_data_bytes: int | None = None,
    max_concurrency_per_host: int = DEFAULT_MAX_CONCURRENCY_PER_HOST,
    host_failure_cooldown_threshold: int = DEFAULT_HOST_FAILURE_COOLDOWN_THRESHOLD,
    host_failure_cooldown_seconds: float = DEFAULT_HOST_FAILURE_COOLDOWN_SECONDS,
    skip_triage_seeds: bool = False,
    resume: bool = True,
) -> Counter[str]:
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if retry_rounds < 0:
        raise ValueError("retry_rounds must be at least 0")
    if depth < 0:
        raise ValueError("depth must be at least 0")
    if max_stored_bytes < 1:
        raise ValueError("max_stored_bytes must be at least 1")
    if max_total_data_bytes is not None and max_total_data_bytes < 1:
        raise ValueError("max_total_data_bytes must be at least 1 when provided")
    if max_concurrency_per_host < 1:
        raise ValueError("max_concurrency_per_host must be at least 1")
    if host_failure_cooldown_threshold < 1:
        raise ValueError("host_failure_cooldown_threshold must be at least 1")
    if host_failure_cooldown_seconds < 0:
        raise ValueError("host_failure_cooldown_seconds must be at least 0")

    output_dir.mkdir(parents=True, exist_ok=True)
    aggregate_data_dir_list = list(aggregate_data_dirs)
    raw_path = output_dir / RAW_FILENAME
    receipt_path = output_dir / RECEIPT_FILENAME
    manifest_path = output_dir / MANIFEST_FILENAME
    summary_path = output_dir / SUMMARY_FILENAME
    failure_streaks_path = output_dir / FAILURE_STREAKS_FILENAME

    if resume:
        completed, url_failure_streaks, host_failure_streaks = _resume_state(
            receipt_path, retry_rounds=retry_rounds
        )
    else:
        completed = set()
        url_failure_streaks = Counter()
        host_failure_streaks = Counter()
    stored_bytes = _stored_bytes(raw_path) if resume else 0
    triage_seen: set[str] = set()
    triage_seed_candidates = select_seed_urls(triage_rows, triage_seen)
    triage_seeds = [] if skip_triage_seeds else triage_seed_candidates
    seed_seen: set[str] = set() if skip_triage_seeds else triage_seen
    additional_seeds = select_additional_seed_urls(additional_seed_rows, seed_seen)
    seeds = [*triage_seeds, *additional_seeds]
    queueable_seeds: list[CrawlRequest] = []
    summary: Counter[str] = Counter()
    for seed in seeds:
        if resume and seed.canonical_url in completed:
            summary["skipped_existing_receipt"] += 1
            continue
        queueable_seeds.append(seed)
    seen = {request.canonical_url for request in seeds} | completed
    reconstructed_requests = (
        reconstruct_resume_discovered_requests(
            raw_path,
            completed=completed,
            seen=seen,
            depth=depth,
        )
        if resume
        else []
    )
    queued = HostAwarePendingQueue(
        [*queueable_seeds, *reconstructed_requests],
        max_concurrency_per_host=max_concurrency_per_host,
        global_concurrency=concurrency,
    )
    attempts: Counter[str] = Counter()
    summary["triage_seed_count"] = len(triage_seeds)
    summary["triage_seed_candidate_count"] = len(triage_seed_candidates)
    if skip_triage_seeds:
        summary["triage_seed_skipped"] = len(triage_seed_candidates)
    summary["additional_seed_count"] = len(additional_seeds)
    summary["seed_count"] = len(seeds)
    summary["resume_reconstructed_discovered_count"] = len(reconstructed_requests)
    summary["resume_completed_count"] = len(completed)
    summary["resume_stored_bytes"] = stored_bytes
    aggregate_data_bytes = (
        total_regular_file_bytes(aggregate_data_dir_list) if max_total_data_bytes is not None else 0
    )
    stop_for_aggregate_cap = (
        max_total_data_bytes is not None and aggregate_data_bytes >= max_total_data_bytes
    )

    with (
        raw_path.open("a", encoding="utf-8") as raw_handle,
        receipt_path.open("a", encoding="utf-8") as receipt_handle,
        ThreadPoolExecutor(
            max_workers=concurrency,
            initializer=_set_thread_fetch_context,
            initargs=(config,),
        ) as executor,
    ):
        futures: dict[Future[FetchOutcome], tuple[CrawlRequest, int]] = {}
        in_flight_by_host: Counter[str] = Counter()
        deferred_until_by_host: dict[str, float] = {}
        stop_for_cap = stored_bytes >= max_stored_bytes
        reserved_bytes = 0

        while queued or futures:
            while (
                queued
                and len(futures) < concurrency
                and not stop_for_cap
                and not stop_for_aggregate_cap
            ):
                if max_total_data_bytes is not None:
                    aggregate_data_bytes = total_regular_file_bytes(aggregate_data_dir_list)
                    aggregate_remaining_capacity = max_total_data_bytes - aggregate_data_bytes
                    if aggregate_remaining_capacity <= 0:
                        if not futures:
                            stop_for_aggregate_cap = True
                        break
                else:
                    aggregate_remaining_capacity = max_stored_bytes
                remaining_capacity = max_stored_bytes - stored_bytes
                reservable_capacity = (
                    min(remaining_capacity, aggregate_remaining_capacity) - reserved_bytes
                )
                if reservable_capacity <= 0:
                    if not futures:
                        stop_for_cap = True
                    break
                now = time.monotonic()
                request = queued.popleft_ready(
                    in_flight_by_host,
                    deferred_until_by_host,
                    now=now,
                )
                if request is None:
                    break
                attempts[request.canonical_url] += 1
                response_byte_budget = min(config.max_response_bytes, reservable_capacity)
                reserved_bytes += response_byte_budget
                request_config = CollectorConfig(
                    timeout_seconds=config.timeout_seconds,
                    max_response_bytes=response_byte_budget,
                    user_agent=config.user_agent,
                    allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
                )
                futures[executor.submit(fetch_page, request, request_config)] = (
                    request,
                    response_byte_budget,
                )
                in_flight_by_host[_request_host(request)] += 1

            if not futures:
                next_ready_at = queued.next_ready_at(
                    deferred_until_by_host,
                    now=time.monotonic(),
                )
                if next_ready_at is not None and next_ready_at > time.monotonic():
                    sleep_seconds = max(0.0, next_ready_at - time.monotonic())
                    summary["host_cooldown_waits"] += 1
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                    continue
                break

            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                request, response_byte_budget = futures.pop(future)
                in_flight_by_host[_request_host(request)] -= 1
                reserved_bytes -= response_byte_budget
                try:
                    outcome = future.result()
                except Exception as error:  # pragma: no cover - defensive worker boundary
                    outcome = FetchOutcome(
                        raw_row=None,
                        receipt={
                            **_base_receipt(request, "worker_error"),
                            "error_kind": type(error).__name__,
                            "raw_data_file": None,
                        },
                    )

                receipt = dict(outcome.receipt)
                status = str(receipt["capture_status"])
                hostname = _hostname(request.canonical_url)

                raw_line_bytes = (
                    len(f"{_json_line(outcome.raw_row)}\n".encode())
                    if outcome.raw_row is not None
                    else 0
                )
                if (
                    outcome.raw_row is not None
                    and max_total_data_bytes is not None
                    and total_regular_file_bytes(aggregate_data_dir_list) + raw_line_bytes
                    > max_total_data_bytes
                ):
                    aggregate_data_bytes = total_regular_file_bytes(aggregate_data_dir_list)
                    receipt["capture_status"] = "aggregate_storage_cap_reached"
                    receipt["raw_data_file"] = None
                    receipt["aggregate_storage_cap_bytes"] = max_total_data_bytes
                    receipt["aggregate_stored_bytes_before_response"] = aggregate_data_bytes
                    receipt["raw_jsonl_line_bytes"] = raw_line_bytes
                    status = "aggregate_storage_cap_reached"
                    stop_for_aggregate_cap = True
                elif (
                    outcome.raw_row is not None and stored_bytes + raw_line_bytes > max_stored_bytes
                ):
                    receipt["capture_status"] = "storage_cap_reached"
                    receipt["raw_data_file"] = None
                    receipt["storage_cap_bytes"] = max_stored_bytes
                    receipt["stored_jsonl_bytes_before_response"] = stored_bytes
                    receipt["raw_jsonl_line_bytes"] = raw_line_bytes
                    status = "storage_cap_reached"
                    stop_for_cap = True
                elif outcome.raw_row is not None:
                    _append_json_line(raw_handle, outcome.raw_row)
                    stored_bytes += raw_line_bytes

                _append_json_line(receipt_handle, receipt)
                summary[status] += 1
                summary["attempted"] += 1

                if _receipt_is_retryable_failure(receipt):
                    url_failure_streaks[request.canonical_url] += 1
                    host_failure_streaks[hostname] += 1
                    if (
                        host_failure_cooldown_seconds > 0
                        and host_failure_streaks[hostname] >= host_failure_cooldown_threshold
                    ):
                        deferred_until_by_host[hostname] = (
                            time.monotonic() + host_failure_cooldown_seconds
                        )
                        summary["host_cooldown_enqueued"] += 1
                    if url_failure_streaks[request.canonical_url] <= retry_rounds:
                        queued.append(request)
                        summary["retry_enqueued"] += 1
                        continue
                    else:
                        completed.add(request.canonical_url)
                        summary[f"{status}_retry_exhausted"] += 1
                else:
                    completed.add(request.canonical_url)
                    url_failure_streaks[request.canonical_url] = 0
                    host_failure_streaks[hostname] = 0

                if status != "captured" or request.depth >= depth or stop_for_cap:
                    continue
                for discovered_url in outcome.extracted_links:
                    discovered_canonical = canonicalize_url(discovered_url)
                    if not discovered_canonical or discovered_canonical in seen:
                        continue
                    discovered_request = _request_from_discovered(request, discovered_canonical)
                    seen.add(discovered_canonical)
                    queued.append(discovered_request)
                    summary["discovered_enqueued"] += 1

    summary["stored_bytes"] = stored_bytes
    summary["storage_cap_bytes"] = max_stored_bytes
    summary["storage_remaining_bytes"] = max(0, max_stored_bytes - stored_bytes)
    if max_total_data_bytes is not None:
        aggregate_data_bytes = total_regular_file_bytes(aggregate_data_dir_list)
        summary["aggregate_data_bytes"] = aggregate_data_bytes
        summary["aggregate_storage_cap_bytes"] = max_total_data_bytes
        summary["aggregate_storage_remaining_bytes"] = max(
            0, max_total_data_bytes - aggregate_data_bytes
        )
    summary["pending_unfetched"] = len(queued)
    _write_json(
        manifest_path,
        {
            "created_at": _captured_at(),
            "raw_file": RAW_FILENAME,
            "receipt_file": RECEIPT_FILENAME,
            "summary_file": SUMMARY_FILENAME,
            "failure_streaks_file": FAILURE_STREAKS_FILENAME,
            "concurrency": concurrency,
            "max_concurrency_per_host": max_concurrency_per_host,
            "host_failure_cooldown_threshold": host_failure_cooldown_threshold,
            "host_failure_cooldown_seconds": host_failure_cooldown_seconds,
            "retry_rounds": retry_rounds,
            "depth": depth,
            "max_response_bytes": config.max_response_bytes,
            "max_stored_bytes": max_stored_bytes,
            "aggregate_data_dirs": [str(path) for path in aggregate_data_dir_list],
            "max_total_data_bytes": max_total_data_bytes,
            "allow_sandbox_egress_alias": config.allow_sandbox_egress_alias,
            "skip_triage_seeds": skip_triage_seeds,
            "resume": resume,
            "summary": dict(sorted(summary.items())),
        },
    )
    _write_json(summary_path, dict(sorted(summary.items())))
    _write_json(
        failure_streaks_path,
        {
            "by_url": dict(sorted(url_failure_streaks.items())),
            "by_host": dict(sorted(host_failure_streaks.items())),
        },
    )
    return summary


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--triage-input", type=Path, required=True, help="Recruitment triage JSONL."
    )
    parser.add_argument(
        "--additional-seeds",
        type=Path,
        action="append",
        default=[],
        help=(
            "Additional recruitment seed JSONL from sitemap discovery. "
            "May be provided multiple times."
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="Crawler output directory.")
    parser.add_argument(
        "--max-stored-bytes",
        type=int,
        default=DEFAULT_MAX_STORED_BYTES,
        help="Hard cap for stored raw response bytes (default: 80 GiB).",
    )
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=DEFAULT_MAX_RESPONSE_BYTES,
        help="Maximum bytes kept per response before truncation.",
    )
    parser.add_argument(
        "--aggregate-data-dir",
        type=Path,
        action="append",
        default=[],
        help=(
            "Directory or file whose regular-file bytes count toward --max-total-data-bytes. "
            "May be provided multiple times."
        ),
    )
    parser.add_argument(
        "--max-total-data-bytes",
        type=int,
        default=None,
        help="Optional hard cap for regular-file bytes across --aggregate-data-dir paths.",
    )
    parser.add_argument("--concurrency", type=int, default=8, help="Concurrent HTTP workers.")
    parser.add_argument(
        "--max-concurrency-per-host",
        type=int,
        default=DEFAULT_MAX_CONCURRENCY_PER_HOST,
        help=(
            "Maximum in-flight requests per hostname before scheduling another queued host "
            f"(default: {DEFAULT_MAX_CONCURRENCY_PER_HOST}). Set at or above --concurrency "
            "to preserve FIFO submission."
        ),
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0, help="Per-request timeout.")
    parser.add_argument(
        "--host-failure-cooldown-threshold",
        type=int,
        default=DEFAULT_HOST_FAILURE_COOLDOWN_THRESHOLD,
        help=(
            "Consecutive retryable failures on a hostname before temporarily deferring that "
            f"host (default: {DEFAULT_HOST_FAILURE_COOLDOWN_THRESHOLD})."
        ),
    )
    parser.add_argument(
        "--host-failure-cooldown-seconds",
        type=float,
        default=DEFAULT_HOST_FAILURE_COOLDOWN_SECONDS,
        help=(
            "Seconds to defer a host after the failure threshold is reached "
            f"(default: {DEFAULT_HOST_FAILURE_COOLDOWN_SECONDS})."
        ),
    )
    parser.add_argument(
        "--retry-rounds",
        type=int,
        default=4,
        help="Retries for network errors after the initial attempt (default: 4; total: 5).",
    )
    parser.add_argument("--depth", type=int, default=1, help="Eligible link crawl depth.")
    parser.add_argument("--no-resume", action="store_true", help="Do not skip completed URLs.")
    parser.add_argument(
        "--skip-triage-seeds",
        action="store_true",
        help=(
            "Do not schedule seeds selected from --triage-input. The triage file is still "
            "loaded and validated; use with --additional-seeds for dedicated additional-seed "
            "crawls."
        ),
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
    try:
        triage_rows = _load_jsonl(args.triage_input)
        additional_seed_rows: list[dict[str, Any]] = []
        for additional_seed_path in args.additional_seeds:
            additional_seed_rows.extend(_load_jsonl(additional_seed_path))
        summary = collect_recruitment_pages(
            triage_rows,
            args.output_dir,
            config=CollectorConfig(
                timeout_seconds=args.timeout_seconds,
                max_response_bytes=args.max_response_bytes,
                user_agent=args.user_agent,
                allow_sandbox_egress_alias=args.allow_sandbox_egress_alias,
            ),
            concurrency=args.concurrency,
            retry_rounds=args.retry_rounds,
            depth=args.depth,
            additional_seed_rows=additional_seed_rows,
            max_stored_bytes=args.max_stored_bytes,
            aggregate_data_dirs=args.aggregate_data_dir,
            max_total_data_bytes=args.max_total_data_bytes,
            max_concurrency_per_host=args.max_concurrency_per_host,
            host_failure_cooldown_threshold=args.host_failure_cooldown_threshold,
            host_failure_cooldown_seconds=args.host_failure_cooldown_seconds,
            skip_triage_seeds=args.skip_triage_seeds,
            resume=not args.no_resume,
        )
    except (OSError, ValueError) as error:
        print(f"recruitment page collection failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(dict(sorted(summary.items())), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
