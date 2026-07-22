#!/usr/bin/env python3
"""Import metadata-only company candidates from YC's public AI directory.

The importer fetches bounded public YC directory pages, parses the embedded
`data-page` JSON, extracts official company website fields from company-like
objects, dedupes against existing candidate inventories, and writes provisional
JSONL rows. It does not fetch or crawl any linked company websites.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener, getproxies

from collect_agent_homepages import (
    DEFAULT_USER_AGENT,
    _ensure_public_http_url,
    _PublicRedirectHandler,
)

SOURCE_ID = "yc-ai-directory"
SOURCE_NAME = "Y Combinator AI Directory"
SOURCE_URL = "https://www.ycombinator.com/companies/industry/ai"
DEFAULT_OUTPUT_FILENAME = "yc_ai_directory_candidates.jsonl"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 3_000_000
DEFAULT_RETRY_ROUNDS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
DEFAULT_START_PAGE = 1
DEFAULT_END_PAGE = 7
DEFAULT_MAX_PAGES = 30
DEFAULT_CONCURRENCY = 2
DEFAULT_CHUNK_BYTES = 64 * 1024

DATA_PAGE_PATTERN = re.compile(
    r"""data-page\s*=\s*(?P<quote>["'])(?P<value>.*?)(?P=quote)""",
    re.IGNORECASE | re.DOTALL,
)
OFFICIAL_WEBSITE_KEYS = {
    "website",
    "website_url",
    "homepage",
    "homepage_url",
    "company_url",
}
NAME_KEYS = ("name", "company_name", "title")
COMMON_SECOND_LEVEL_SUFFIXES = {
    "ac",
    "co",
    "com",
    "edu",
    "gov",
    "net",
    "org",
}
NON_COMPANY_HOSTS = {
    "ycombinator.com",
    "www.ycombinator.com",
    "bookface.ycombinator.com",
    "news.ycombinator.com",
    "startupschool.org",
    "github.com",
    "raw.githubusercontent.com",
    "huggingface.co",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "medium.com",
    "substack.com",
    "notion.site",
    "notion.so",
    "docs.google.com",
    "drive.google.com",
    "cloudfront.net",
    "amazonaws.com",
    "googleusercontent.com",
    "githubusercontent.com",
    "jsdelivr.net",
    "unpkg.com",
}
NON_COMPANY_HOST_SUFFIXES = (
    ".github.io",
    ".githubusercontent.com",
    ".cloudfront.net",
    ".amazonaws.com",
    ".googleusercontent.com",
    ".jsdelivr.net",
    ".vercel.app",
    ".netlify.app",
)
NON_COMPANY_PATH_PARTS = {
    "docs",
    "documentation",
    "blog",
    "papers",
    "paper",
    "tutorial",
    "tutorials",
    "research",
    "wiki",
    "careers",
    "jobs",
}


@dataclass(frozen=True)
class ImportConfig:
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    retry_rounds: int = DEFAULT_RETRY_ROUNDS
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS
    user_agent: str = DEFAULT_USER_AGENT
    allow_sandbox_egress_alias: bool = False


@dataclass(frozen=True)
class PageFetch:
    page_number: int
    url: str
    final_url: str
    http_status: int
    content_type: str
    body_bytes: int
    body_sha256: str
    attempts: int
    text: str


@dataclass(frozen=True)
class SourceRecord:
    page_number: int
    source_index: int
    company_name: str
    yc_company_id: str
    yc_slug: str
    yc_url: str
    website: str
    canonical_url: str
    hostname: str
    registrable_domain: str
    batch: str
    status: str
    location: str
    team_size: int | None
    one_liner: str
    tags: list[str]


class ResponseTooLargeError(ValueError):
    """Raised when a YC page exceeds the configured response byte cap."""


class TerminalHttpStatusError(RuntimeError):
    """Raised for HTTP statuses where collection must stop."""

    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"terminal HTTP {status_code} for {url}")
        self.status_code = status_code
        self.url = url


def _captured_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_line(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _append_json_line(handle: Any, row: dict[str, Any]) -> None:
    handle.write(f"{_json_line(row)}\n")
    handle.flush()


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


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _clean_name(value: Any, fallback: str) -> str:
    return _clean_text(value) or fallback


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.username or parsed.password:
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
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    query = urlencode(sorted(query_pairs), doseq=True)
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def hostname_for_url(url: str) -> str:
    return (urlsplit(url).hostname or "").rstrip(".").lower()


def registrable_domain_for_hostname(hostname: str) -> str:
    parts = [part for part in hostname.rstrip(".").lower().split(".") if part]
    if len(parts) <= 2:
        return ".".join(parts)
    if len(parts[-1]) == 2 and parts[-2] in COMMON_SECOND_LEVEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def record_id_for_domain(registrable_domain: str) -> str:
    return f"agent-ecosystem-domain--{registrable_domain}"


def _host_is_excluded(hostname: str) -> bool:
    if hostname.startswith(("docs.", "blog.", "cdn.", "static.", "assets.")):
        return True
    if hostname in NON_COMPANY_HOSTS:
        return True
    if any(hostname.endswith(suffix) for suffix in NON_COMPANY_HOST_SUFFIXES):
        return True
    return any(hostname.endswith(f".{host}") for host in NON_COMPANY_HOSTS)


def _path_is_excluded(canonical_url: str) -> bool:
    parts = [part.lower() for part in urlsplit(canonical_url).path.split("/") if part]
    return bool(parts and parts[0] in NON_COMPANY_PATH_PARTS)


def is_company_website_candidate(canonical_url: str) -> bool:
    hostname = hostname_for_url(canonical_url)
    if not hostname or _host_is_excluded(hostname):
        return False
    registrable_domain = registrable_domain_for_hostname(hostname)
    if not registrable_domain or "." not in registrable_domain:
        return False
    return not _path_is_excluded(canonical_url)


def _read_existing_identity(rows: Iterable[dict[str, Any]]) -> tuple[set[str], set[str], set[str]]:
    record_ids: set[str] = set()
    hostnames: set[str] = set()
    domains: set[str] = set()
    for row in rows:
        record_id = row.get("record_id")
        if isinstance(record_id, str) and record_id:
            record_ids.add(record_id)
        domain = row.get("registrable_domain")
        if isinstance(domain, str) and domain:
            domains.add(domain.strip().lower())
        for field_name in ("representative_website", "website", "url"):
            url = row.get(field_name)
            if not isinstance(url, str):
                continue
            canonical = canonicalize_url(url)
            hostname = hostname_for_url(canonical)
            if hostname:
                hostnames.add(hostname)
                domains.add(registrable_domain_for_hostname(hostname))
    return record_ids, hostnames, domains


def load_existing_identity(paths: Sequence[Path]) -> tuple[set[str], set[str], set[str]]:
    record_ids: set[str] = set()
    hostnames: set[str] = set()
    domains: set[str] = set()
    for path in paths:
        rows = _load_jsonl(path)
        path_record_ids, path_hostnames, path_domains = _read_existing_identity(rows)
        record_ids.update(path_record_ids)
        hostnames.update(path_hostnames)
        domains.update(path_domains)
    return record_ids, hostnames, domains


def directory_page_url(page_number: int) -> str:
    if page_number <= 1:
        return SOURCE_URL
    return f"{SOURCE_URL}?page={page_number}"


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code == 408 or status_code == 425 or 500 <= status_code <= 599


def _read_response_limited(response: Any, max_bytes: int) -> bytes:
    if max_bytes < 1:
        raise ValueError("max_response_bytes must be at least 1")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(DEFAULT_CHUNK_BYTES, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError("YC page response exceeded byte cap")
    return b"".join(chunks)


def fetch_page(page_number: int, config: ImportConfig) -> PageFetch:
    if config.retry_rounds < 0:
        raise ValueError("retry_rounds must be at least 0")
    if config.retry_backoff_seconds < 0:
        raise ValueError("retry_backoff_seconds must be at least 0")
    url = directory_page_url(page_number)
    attempts = config.retry_rounds + 1
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            proxies = getproxies()
            _ensure_public_http_url(
                url,
                proxies,
                allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
            )
            request = Request(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                    "User-Agent": config.user_agent,
                },
                method="GET",
            )
            opener = build_opener(
                _PublicRedirectHandler(
                    proxies,
                    allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
                )
            )
            with opener.open(request, timeout=config.timeout_seconds) as response:
                raw_body = _read_response_limited(response, config.max_response_bytes)
                final_url = response.geturl()
                status_code = response.getcode()
                content_type = response.headers.get_content_type().lower()
                charset = response.headers.get_content_charset() or "utf-8"
            return PageFetch(
                page_number=page_number,
                url=url,
                final_url=final_url,
                http_status=status_code,
                content_type=content_type,
                body_bytes=len(raw_body),
                body_sha256=hashlib.sha256(raw_body).hexdigest(),
                attempts=attempt,
                text=raw_body.decode(charset, errors="replace"),
            )
        except HTTPError as error:
            last_error = error
            if error.code in {403, 429}:
                raise TerminalHttpStatusError(error.code, url) from error
            if not _is_retryable_http_status(error.code) or attempt == attempts:
                raise
        except (OSError, TimeoutError, URLError) as error:
            last_error = error
            if attempt == attempts:
                raise
        if config.retry_backoff_seconds:
            time.sleep(config.retry_backoff_seconds * (2 ** (attempt - 1)))
    assert last_error is not None
    raise last_error


def fetch_pages(
    page_numbers: Sequence[int],
    config: ImportConfig,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> list[PageFetch]:
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if not page_numbers:
        return []
    ordered_pages = sorted(page_numbers)
    fetches: list[PageFetch] = []
    worker_count = min(concurrency, len(ordered_pages))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        future_to_page = {pool.submit(fetch_page, page, config): page for page in ordered_pages}
        try:
            for future in concurrent.futures.as_completed(future_to_page):
                fetches.append(future.result())
        except TerminalHttpStatusError:
            for pending in future_to_page:
                pending.cancel()
            raise
    return sorted(fetches, key=lambda fetch: fetch.page_number)


def parse_data_page_payloads(html_text: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for match in DATA_PAGE_PATTERN.finditer(html_text):
        raw_value = html.unescape(match.group("value"))
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid data-page JSON: {error.msg}") from error
        if not isinstance(decoded, dict):
            raise ValueError("data-page JSON must decode to an object")
        payloads.append(decoded)
    return payloads


def _iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_dicts(item)


def _list_texts(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_clean_text(item) for item in value if _clean_text(item)]
    return []


def _yc_profile_url(company: dict[str, Any]) -> str:
    url = _clean_text(company.get("url") or company.get("ycdc_company_url"))
    if url.startswith("/companies/"):
        return f"https://www.ycombinator.com{url}"
    if url.startswith("https://www.ycombinator.com/companies/"):
        return url
    slug = _clean_text(company.get("slug"))
    if slug:
        return f"https://www.ycombinator.com/companies/{slug}"
    return ""


def _company_name(company: dict[str, Any], registrable_domain: str) -> str:
    for key in NAME_KEYS:
        name = _clean_text(company.get(key))
        if name:
            return name
    return registrable_domain


def _source_record_from_company(
    company: dict[str, Any],
    *,
    page_number: int,
    source_index: int,
) -> SourceRecord | None:
    website = ""
    for key in OFFICIAL_WEBSITE_KEYS:
        candidate = _clean_text(company.get(key))
        if candidate:
            website = candidate
            break
    if not website:
        return None
    canonical = canonicalize_url(website)
    if not canonical or not is_company_website_candidate(canonical):
        return None
    hostname = hostname_for_url(canonical)
    registrable_domain = registrable_domain_for_hostname(hostname)
    team_size = company.get("team_size")
    if not isinstance(team_size, int):
        team_size = None
    tags = _list_texts(company.get("tags")) or _list_texts(company.get("industries"))
    return SourceRecord(
        page_number=page_number,
        source_index=source_index,
        company_name=_company_name(company, registrable_domain),
        yc_company_id=str(company.get("id") or ""),
        yc_slug=_clean_text(company.get("slug")),
        yc_url=_yc_profile_url(company),
        website=website,
        canonical_url=canonical,
        hostname=hostname,
        registrable_domain=registrable_domain,
        batch=_clean_text(company.get("batch") or company.get("batch_name")),
        status=_clean_text(company.get("status") or company.get("ycdc_status")),
        location=_clean_text(company.get("location")),
        team_size=team_size,
        one_liner=_clean_text(company.get("one_liner") or company.get("description")),
        tags=tags,
    )


def parse_source_records(fetches: Iterable[PageFetch]) -> tuple[list[SourceRecord], Counter[str]]:
    records: list[SourceRecord] = []
    summary: Counter[str] = Counter()
    seen_page_domain_pairs: set[tuple[int, str]] = set()
    for fetch in fetches:
        summary["pages_parsed"] += 1
        payloads = parse_data_page_payloads(fetch.text)
        summary["data_page_payloads"] += len(payloads)
        for payload in payloads:
            for source_index, company in enumerate(_iter_dicts(payload), start=1):
                if not any(key in company for key in OFFICIAL_WEBSITE_KEYS):
                    continue
                summary["company_like_objects"] += 1
                record = _source_record_from_company(
                    company,
                    page_number=fetch.page_number,
                    source_index=source_index,
                )
                if record is None:
                    summary["filtered_without_company_website"] += 1
                    continue
                page_domain_key = (record.page_number, record.registrable_domain)
                if page_domain_key in seen_page_domain_pairs:
                    summary["deduped_same_page_domain"] += 1
                    continue
                seen_page_domain_pairs.add(page_domain_key)
                records.append(record)
    return records, summary


def candidate_row(record: SourceRecord, fetch: PageFetch) -> dict[str, Any]:
    return {
        "record_id": record_id_for_domain(record.registrable_domain),
        "candidate_entity_name": record.company_name,
        "normalized_name": record.company_name.lower(),
        "registrable_domain": record.registrable_domain,
        "representative_website": record.canonical_url,
        "candidate_type": "provisional_public_yc_ai_directory_company_candidate",
        "candidate_state": "unverified",
        "website_fetch_status": "not_fetched",
        "company_terms_status": "unknown",
        "source_usage_status": "public_yc_directory_metadata_seed_pending_human_review",
        "source_evidence": [
            {
                "source_id": SOURCE_ID,
                "source_name": SOURCE_NAME,
                "source_url": fetch.url,
                "source_final_url": fetch.final_url,
                "source_body_sha256": fetch.body_sha256,
                "source_page": record.page_number,
                "source_index": record.source_index,
                "source_entity_name": record.company_name,
                "source_company_id": record.yc_company_id,
                "source_slug": record.yc_slug,
                "source_yc_url": record.yc_url,
                "source_batch": record.batch,
                "source_status": record.status,
                "source_location": record.location,
                "source_team_size": record.team_size,
                "source_one_liner": record.one_liner,
                "source_tags": record.tags,
                "source_website": record.website,
            }
        ],
        "personal_data_included": False,
        "collection_boundary": (
            "Metadata-only provisional candidate seed from public YC directory JSON. "
            "No linked company site was fetched or crawled."
        ),
        "captured_at": _captured_at(),
    }


def select_new_candidates(
    records: Iterable[SourceRecord],
    fetch_by_page: dict[int, PageFetch],
    *,
    existing_record_ids: set[str],
    existing_hostnames: set[str],
    existing_domains: set[str],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    summary: Counter[str] = Counter()
    seen_domains = set(existing_domains)
    seen_hostnames = set(existing_hostnames)
    seen_record_ids = set(existing_record_ids)
    for record in records:
        summary["valid_website_records"] += 1
        record_id = record_id_for_domain(record.registrable_domain)
        if (
            record_id in seen_record_ids
            or record.hostname in seen_hostnames
            or record.registrable_domain in seen_domains
        ):
            summary["deduped_existing"] += 1
            continue
        seen_record_ids.add(record_id)
        seen_hostnames.add(record.hostname)
        seen_domains.add(record.registrable_domain)
        rows.append(candidate_row(record, fetch_by_page[record.page_number]))
        summary["candidate_written"] += 1
    return rows, summary


def import_yc_candidates(
    fetches: Sequence[PageFetch],
    output_path: Path,
    *,
    existing_record_ids: set[str],
    existing_hostnames: set[str],
    existing_domains: set[str],
) -> dict[str, Any]:
    records, parse_summary = parse_source_records(fetches)
    rows, summary = select_new_candidates(
        records,
        {fetch.page_number: fetch for fetch in fetches},
        existing_record_ids=existing_record_ids,
        existing_hostnames=existing_hostnames,
        existing_domains=existing_domains,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            _append_json_line(handle, row)
    summary_dict: dict[str, Any] = dict(summary)
    summary_dict.update(parse_summary)
    summary_dict["pages_fetched"] = len(fetches)
    summary_dict["page_numbers_fetched"] = [fetch.page_number for fetch in fetches]
    summary_dict["page_body_bytes"] = sum(fetch.body_bytes for fetch in fetches)
    summary_dict["page_fetch_attempts"] = sum(fetch.attempts for fetch in fetches)
    summary_dict["output_rows"] = len(rows)
    summary_dict["unique_output_domains"] = len({row["registrable_domain"] for row in rows})
    return summary_dict


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--existing-candidates",
        type=Path,
        action="append",
        default=[],
        help="Existing candidate inventory JSONL for record/domain/host dedupe.",
    )
    parser.add_argument("--output", type=Path, required=True, help="Output candidate JSONL path.")
    parser.add_argument("--start-page", type=int, default=DEFAULT_START_PAGE)
    parser.add_argument("--end-page", type=int, default=DEFAULT_END_PAGE)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-response-bytes", type=int, default=DEFAULT_MAX_RESPONSE_BYTES)
    parser.add_argument("--retry-rounds", type=int, default=DEFAULT_RETRY_ROUNDS)
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=DEFAULT_RETRY_BACKOFF_SECONDS,
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
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
    if args.start_page < 1 or args.end_page < args.start_page:
        print("invalid page range", file=sys.stderr)
        return 2
    if args.end_page > args.max_pages:
        print("end page exceeds max-pages safety cap", file=sys.stderr)
        return 2
    config = ImportConfig(
        timeout_seconds=args.timeout_seconds,
        max_response_bytes=args.max_response_bytes,
        retry_rounds=args.retry_rounds,
        retry_backoff_seconds=args.retry_backoff_seconds,
        user_agent=args.user_agent,
        allow_sandbox_egress_alias=args.allow_sandbox_egress_alias,
    )
    try:
        existing_record_ids, existing_hostnames, existing_domains = load_existing_identity(
            args.existing_candidates
        )
        fetches = fetch_pages(
            list(range(args.start_page, args.end_page + 1)),
            config,
            concurrency=args.concurrency,
        )
        summary = import_yc_candidates(
            fetches,
            args.output,
            existing_record_ids=existing_record_ids,
            existing_hostnames=existing_hostnames,
            existing_domains=existing_domains,
        )
    except TerminalHttpStatusError as error:
        print(f"YC AI directory import stopped: {error}", file=sys.stderr)
        return 3
    except (OSError, RuntimeError, ValueError, HTTPError, URLError) as error:
        print(f"YC AI directory import failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(dict(sorted(summary.items())), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
