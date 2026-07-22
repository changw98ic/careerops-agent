#!/usr/bin/env python3
"""Discover public ATS job URLs from known ATS recruitment links.

This stage expands the recruitment crawler with unauthenticated public job-feed
APIs. It does not use a browser, log in, submit forms, or execute JavaScript.
Supported feeds:
- Greenhouse: boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true
- Ashby: api.ashbyhq.com/posting-api/job-board/{board}
- Lever: api.lever.co/v0/postings/{site}?mode=json
- SmartRecruiters: api.smartrecruiters.com/v1/companies/{company}/postings
- Recruitee: {company}.recruitee.com/api/offers/
- Workable: www.workable.com/api/accounts/{subdomain}?details=true
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener

from collect_recruitment_pages import (
    DEFAULT_USER_AGENT,
    ELIGIBLE_ASSESSMENTS,
    ELIGIBLE_RELATIONSHIPS,
    UnsafeTargetError,
    _ensure_public_http_url,
    _hostname,
    _json_line,
    _PublicRedirectHandler,
    canonicalize_url,
    link_relationship,
)

DISCOVERED_FILENAME = "discovered_public_ats_jobs.jsonl"
RECEIPT_FILENAME = "public_ats_job_feed_receipts.jsonl"
SUMMARY_FILENAME = "public_ats_job_feed_summary.json"
MANIFEST_FILENAME = "public_ats_job_feed_manifest.json"
FAILURE_STREAKS_FILENAME = "public_ats_job_feed_failure_streaks.json"

DEFAULT_MAX_FEED_BYTES = 10_000_000
DEFAULT_RETRY_ROUNDS = 4
DEFAULT_CONCURRENCY = 12
ARTIFACT_HASH_CHUNK_BYTES = 65_536
SMARTRECRUITERS_MAX_CONCURRENCY = 8
SMARTRECRUITERS_PAGE_SIZE = 100
RETRYABLE_STATUSES = {"network_error", "retryable_http_error"}
PUBLIC_ATS_JOB_ROW_SCHEMA_VERSION = 2
MAX_JOB_DESCRIPTION_CHARS = 48_000
MAX_JOB_KEYWORDS = 100
MAX_JOB_KEYWORD_CHARS = 160
_SPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?%\)\]\}])")
_KEYWORD_SEPARATOR = re.compile(r"[,;|\n]")


@dataclass(frozen=True)
class AtsFeedTarget:
    canonical_url: str
    url: str
    ats: str
    token: str
    source_url: str
    source_record_id: str | None
    source_index: int | None
    source_assessment: str | None
    registrable_domain: str


@dataclass(frozen=True)
class FetchConfig:
    timeout_seconds: float = 20.0
    max_feed_bytes: int = DEFAULT_MAX_FEED_BYTES
    user_agent: str = DEFAULT_USER_AGENT
    allow_sandbox_egress_alias: bool = False


@dataclass(frozen=True)
class FeedResult:
    target: AtsFeedTarget
    status: str
    payload: Any = None
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    body_bytes: int = 0
    body_truncated: bool = False
    body_sha256: str | None = None
    error_kind: str | None = None


Fetcher = Callable[[AtsFeedTarget, FetchConfig], FeedResult]


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


def _artifact_metadata(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(ARTIFACT_HASH_CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
    return {
        "path": str(path),
        "bytes": size,
        "artifact_sha256": digest.hexdigest(),
    }


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


def _first_path_segment(url: str) -> str:
    return next((part for part in urlsplit(url).path.split("/") if part), "")


def _path_segments(url: str) -> list[str]:
    return [part for part in urlsplit(url).path.split("/") if part]


def _greenhouse_feed_url(token: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def _ashby_feed_url(token: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{token}"


def _lever_feed_url(token: str) -> str:
    return f"https://api.lever.co/v0/postings/{token}?mode=json"


def _smartrecruiters_feed_url(token: str) -> str:
    return f"https://api.smartrecruiters.com/v1/companies/{token}/postings"


def _recruitee_feed_url(token: str) -> str:
    return f"https://{token}.recruitee.com/api/offers/"


def _workable_feed_url(token: str) -> str:
    return f"https://www.workable.com/api/accounts/{token}?details=true"


def _smartrecruiters_token(url: str) -> str | None:
    hostname = _hostname(url)
    segments = _path_segments(url)
    if hostname == "api.smartrecruiters.com":
        if len(segments) >= 4 and segments[0] == "v1" and segments[1] == "companies":
            return segments[2] or None
        return None
    if hostname not in {
        "jobs.smartrecruiters.com",
        "careers.smartrecruiters.com",
    } and not hostname.endswith(".smartrecruiters.com"):
        return None
    return _first_path_segment(url) or None


def _recruitee_token(url: str) -> str | None:
    hostname = _hostname(url)
    if not hostname.endswith(".recruitee.com"):
        return None
    token = hostname.removesuffix(".recruitee.com")
    if not token or token == "www":
        return None
    return token


def _workable_token(url: str) -> str | None:
    hostname = _hostname(url)
    segments = _path_segments(url)
    if hostname == "www.workable.com":
        if len(segments) >= 3 and segments[0] == "api" and segments[1] == "accounts":
            return segments[2] or None
        return None
    if hostname == "apply.workable.com":
        return segments[0] if segments else None
    if hostname.endswith(".workable.com"):
        if hostname in {"www.workable.com", "apply.workable.com"}:
            return None
        return segments[0] if segments else None
    return None


def _smartrecruiters_page_url(token: str, offset: int) -> str:
    return f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit={SMARTRECRUITERS_PAGE_SIZE}&offset={offset}"


def _next_page_url(payload: Any, base_url: str | None = None) -> str | None:
    if not isinstance(payload, dict):
        return None
    for candidate in (
        _nested_string(payload, "paging", "next"),
        _nested_string(payload, "links", "next"),
        payload.get("next") if isinstance(payload.get("next"), str) else None,
    ):
        if isinstance(candidate, str) and candidate.strip():
            next_url = candidate.strip()
            if base_url is not None:
                next_url = urljoin(base_url, next_url)
            return next_url
    return None


def feed_target_from_known_ats_url(
    url: str,
    *,
    source_record_id: str | None,
    source_index: int | None,
    source_assessment: str | None,
    registrable_domain: str,
) -> AtsFeedTarget | None:
    canonical_source = canonicalize_url(url)
    if not canonical_source:
        return None
    hostname = _hostname(canonical_source)
    ats: str
    feed_url: str
    if hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        token = _first_path_segment(canonical_source)
        if not token:
            return None
        ats = "greenhouse"
        feed_url = _greenhouse_feed_url(token)
    elif hostname == "jobs.ashbyhq.com":
        token = _first_path_segment(canonical_source)
        if not token:
            return None
        ats = "ashby"
        feed_url = _ashby_feed_url(token)
    elif hostname == "jobs.lever.co":
        token = _first_path_segment(canonical_source)
        if not token:
            return None
        ats = "lever"
        feed_url = _lever_feed_url(token)
    elif hostname in {
        "jobs.smartrecruiters.com",
        "careers.smartrecruiters.com",
    } or hostname.endswith(".smartrecruiters.com"):
        token = _smartrecruiters_token(canonical_source)
        if not token:
            return None
        ats = "smartrecruiters"
        feed_url = _smartrecruiters_feed_url(token)
    elif hostname.endswith(".recruitee.com"):
        token = _recruitee_token(canonical_source)
        if not token:
            return None
        ats = "recruitee"
        feed_url = _recruitee_feed_url(token)
    elif hostname.endswith(".workable.com") or hostname == "www.workable.com":
        token = _workable_token(canonical_source)
        if not token:
            return None
        ats = "workable"
        feed_url = _workable_feed_url(token)
    else:
        return None
    canonical_feed = canonicalize_url(feed_url)
    if not canonical_feed:
        return None
    return AtsFeedTarget(
        canonical_url=canonical_feed,
        url=feed_url,
        ats=ats,
        token=token,
        source_url=canonical_source,
        source_record_id=source_record_id,
        source_index=source_index,
        source_assessment=source_assessment,
        registrable_domain=registrable_domain,
    )


def select_feed_targets(rows: Iterable[dict[str, Any]]) -> list[AtsFeedTarget]:
    targets: list[AtsFeedTarget] = []
    seen: set[str] = set()
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
            if not isinstance(href, str) or relationship != "known_ats":
                continue
            canonical = canonicalize_url(href)
            if not canonical:
                continue
            if link_relationship(canonical, registrable_domain) not in ELIGIBLE_RELATIONSHIPS:
                continue
            target = feed_target_from_known_ats_url(
                canonical,
                source_record_id=(
                    row.get("record_id") if isinstance(row.get("record_id"), str) else None
                ),
                source_index=source_index,
                source_assessment=str(assessment),
                registrable_domain=registrable_domain,
            )
            if target is None or target.canonical_url in seen:
                continue
            seen.add(target.canonical_url)
            targets.append(target)
    return targets


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _is_retryable_status(status: str) -> bool:
    return status in RETRYABLE_STATUSES


def _fetch_json_page(url: str, target: AtsFeedTarget, config: FetchConfig) -> FeedResult:
    try:
        _ensure_public_http_url(
            url,
            allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
        )
    except UnsafeTargetError as error:
        return FeedResult(target=target, status="invalid_url", error_kind=type(error).__name__)
    except OSError as error:
        return FeedResult(target=target, status="network_error", error_kind=type(error).__name__)

    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": config.user_agent},
        method="GET",
    )
    try:
        opener = build_opener(
            _PublicRedirectHandler(
                allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
            )
        )
        with opener.open(request, timeout=config.timeout_seconds) as response:
            raw_body = response.read(config.max_feed_bytes + 1)
            body_truncated = len(raw_body) > config.max_feed_bytes
            if body_truncated:
                raw_body = raw_body[: config.max_feed_bytes]
            content_type = response.headers.get_content_type().lower()
            charset = response.headers.get_content_charset() or "utf-8"
            final_url = response.geturl()
            status_code = response.getcode()
    except HTTPError as error:
        status = "retryable_http_error" if _is_retryable_http_status(error.code) else "http_error"
        return FeedResult(
            target=target,
            status=status,
            final_url=error.geturl(),
            http_status=error.code,
            error_kind=type(error).__name__,
        )
    except UnsafeTargetError as error:
        return FeedResult(target=target, status="invalid_url", error_kind=type(error).__name__)
    except (OSError, TimeoutError, URLError) as error:
        return FeedResult(target=target, status="network_error", error_kind=type(error).__name__)

    body_sha256 = hashlib.sha256(raw_body).hexdigest()
    if body_truncated:
        return FeedResult(
            target=target,
            status="body_size_cap_reached",
            final_url=final_url,
            http_status=status_code,
            content_type=content_type,
            body_bytes=len(raw_body),
            body_truncated=True,
            body_sha256=body_sha256,
        )
    if content_type != "application/json":
        return FeedResult(
            target=target,
            status="unsupported_content_type",
            final_url=final_url,
            http_status=status_code,
            content_type=content_type,
            body_bytes=len(raw_body),
            body_sha256=body_sha256,
        )
    try:
        payload = json.loads(raw_body.decode(charset, errors="replace"))
    except json.JSONDecodeError as error:
        return FeedResult(
            target=target,
            status="parse_error",
            final_url=final_url,
            http_status=status_code,
            content_type=content_type,
            body_bytes=len(raw_body),
            body_sha256=body_sha256,
            error_kind=type(error).__name__,
        )
    return FeedResult(
        target=target,
        status="fetched",
        payload=payload,
        final_url=final_url,
        http_status=status_code,
        content_type=content_type,
        body_bytes=len(raw_body),
        body_sha256=body_sha256,
    )


def _combine_body_sha256(digests: list[str | None]) -> str | None:
    filtered = [digest for digest in digests if digest]
    if not filtered:
        return None
    joined = "\n".join(filtered).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


def _fetch_smartrecruiters_feed(target: AtsFeedTarget, config: FetchConfig) -> FeedResult:
    offset = 0
    jobs: list[dict[str, Any]] = []
    total_found: int | None = None
    first_page: FeedResult | None = None
    body_bytes = 0
    body_digests: list[str | None] = []
    while True:
        page = _fetch_json_page(_smartrecruiters_page_url(target.token, offset), target, config)
        if page.status != "fetched":
            return page
        if first_page is None:
            first_page = page
        payload = page.payload
        if not isinstance(payload, dict):
            return FeedResult(
                target=target,
                status="parse_error",
                final_url=page.final_url,
                http_status=page.http_status,
                content_type=page.content_type,
                body_bytes=page.body_bytes,
                body_sha256=page.body_sha256,
                error_kind="UnexpectedPayloadShape",
            )
        page_jobs = payload.get("content")
        if not isinstance(page_jobs, list):
            page_jobs = []
        jobs.extend([job for job in page_jobs if isinstance(job, dict)])
        page_total_found = payload.get("totalFound")
        if isinstance(page_total_found, int) and page_total_found >= 0:
            total_found = page_total_found
        body_bytes += page.body_bytes
        body_digests.append(page.body_sha256)
        page_count = len(page_jobs)
        if page_count == 0:
            break
        if total_found is not None and offset + page_count >= total_found:
            break
        if page_count < SMARTRECRUITERS_PAGE_SIZE and total_found is None:
            break
        offset += SMARTRECRUITERS_PAGE_SIZE
    assert first_page is not None
    aggregated_payload = dict(first_page.payload) if isinstance(first_page.payload, dict) else {}
    aggregated_payload["content"] = jobs
    if total_found is None:
        total_found = len(jobs)
    aggregated_payload["limit"] = SMARTRECRUITERS_PAGE_SIZE
    aggregated_payload["offset"] = 0
    aggregated_payload["totalFound"] = total_found
    return FeedResult(
        target=target,
        status="fetched",
        payload=aggregated_payload,
        final_url=first_page.final_url,
        http_status=first_page.http_status,
        content_type=first_page.content_type,
        body_bytes=body_bytes,
        body_sha256=_combine_body_sha256(body_digests),
    )


def _fetch_workable_feed(target: AtsFeedTarget, config: FetchConfig) -> FeedResult:
    first_page = _fetch_json_page(target.url, target, config)
    if first_page.status != "fetched":
        return first_page
    if not isinstance(first_page.payload, dict):
        return FeedResult(
            target=target,
            status="parse_error",
            final_url=first_page.final_url,
            http_status=first_page.http_status,
            content_type=first_page.content_type,
            body_bytes=first_page.body_bytes,
            body_sha256=first_page.body_sha256,
            error_kind="UnexpectedPayloadShape",
        )
    jobs: list[dict[str, Any]] = []
    page = first_page
    body_bytes = 0
    body_digests: list[str | None] = []
    while True:
        payload = page.payload
        if not isinstance(payload, dict):
            break
        page_jobs = payload.get("jobs")
        if isinstance(page_jobs, list):
            jobs.extend([job for job in page_jobs if isinstance(job, dict)])
        body_bytes += page.body_bytes
        body_digests.append(page.body_sha256)
        next_url = _next_page_url(payload, page.final_url or page.target.url)
        if not next_url:
            break
        page = _fetch_json_page(next_url, target, config)
        if page.status != "fetched":
            return page
    aggregated_payload = dict(first_page.payload)
    aggregated_payload["jobs"] = jobs
    return FeedResult(
        target=target,
        status="fetched",
        payload=aggregated_payload,
        final_url=page.final_url,
        http_status=page.http_status,
        content_type=page.content_type,
        body_bytes=body_bytes,
        body_sha256=_combine_body_sha256(body_digests),
    )


def fetch_feed(target: AtsFeedTarget, config: FetchConfig) -> FeedResult:
    if target.ats == "smartrecruiters":
        return _fetch_smartrecruiters_feed(target, config)
    if target.ats == "workable":
        return _fetch_workable_feed(target, config)
    return _fetch_json_page(target.url, target, config)


def _nested_string(value: Any, *path: str) -> str | None:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, str) and current.strip() else None


class _PlainTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)

    def plain_text(self) -> str:
        compact = _SPACE.sub(" ", " ".join(self.parts)).strip()
        return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", compact)


def _first_string(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _description_text(value: str) -> str | None:
    parser = _PlainTextHTMLParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        # HTMLParser is deliberately best effort; untrusted markup remains data and
        # must never turn one malformed description into a failed feed.
        plain = _SPACE.sub(" ", value).strip()
    else:
        plain = parser.plain_text()
    if not plain:
        return None
    return plain[:MAX_JOB_DESCRIPTION_CHARS]


def _job_description(job: dict[str, Any], ats: str) -> str | None:
    plain_keys = {
        "greenhouse": ("descriptionPlain", "description_text"),
        "ashby": ("descriptionPlain", "descriptionText", "description_text"),
        "lever": ("descriptionPlain", "additionalPlain"),
        "smartrecruiters": ("descriptionText", "description_text"),
        "recruitee": ("description_plain", "descriptionPlain"),
        "workable": ("description_plain", "descriptionPlain", "description_text"),
    }[ats]
    plain = _first_string(job, *plain_keys)
    if plain is not None:
        return _description_text(plain)

    nested = (
        _nested_string(job, "jobAd", "sections", "jobDescription", "text")
        or _nested_string(job, "sections", "jobDescription", "text")
        or _nested_string(job, "jobAd", "sections", "qualifications", "text")
    )
    if nested is not None:
        return _description_text(nested)

    html_keys = {
        "greenhouse": ("content", "description", "descriptionHtml"),
        "ashby": ("descriptionHtml", "description", "content"),
        "lever": ("description", "additional", "descriptionHtml"),
        "smartrecruiters": ("jobDescription", "description", "descriptionHtml"),
        "recruitee": ("description", "description_html", "requirements"),
        "workable": ("description", "description_html", "full_description"),
    }[ats]
    rich = _first_string(job, *html_keys)
    return _description_text(rich) if rich is not None else None


def _canonical_optional_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return canonicalize_url(value.strip())


def _job_apply_url(job: dict[str, Any], ats: str) -> str | None:
    keys = {
        "greenhouse": ("application_url", "apply_url", "applyUrl"),
        "ashby": ("applyUrl", "applicationUrl", "apply_url"),
        "lever": ("applyUrl", "apply_url"),
        "smartrecruiters": ("applyUrl", "applicationUrl", "apply_url"),
        "recruitee": ("careers_apply_url", "apply_url", "application_url"),
        "workable": ("application_url", "apply_url", "applyUrl"),
    }[ats]
    for key in keys:
        candidate = _canonical_optional_url(job.get(key))
        if candidate is not None:
            return candidate
    return None


def _job_employment_type(job: dict[str, Any], ats: str) -> str | None:
    if ats == "lever":
        commitment = _nested_string(job, "categories", "commitment")
        if commitment is not None:
            return commitment.strip()
    if ats == "smartrecruiters":
        employment = _nested_string(job, "typeOfEmployment", "label") or _nested_string(
            job, "employmentType", "label"
        )
        if employment is not None:
            return employment.strip()
    return _first_string(
        job,
        "employmentType",
        "employment_type",
        "employment_type_code",
        "contractType",
        "contract_type",
        "jobType",
        "job_type",
    )


def _job_remote_value(job: dict[str, Any]) -> bool | str | None:
    for key in ("isRemote", "is_remote", "remote", "remoteFriendly", "remote_friendly"):
        value = job.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _job_work_mode(job: dict[str, Any]) -> str | None:
    explicit = _first_string(
        job,
        "workplaceType",
        "workplace_type",
        "workMode",
        "work_mode",
        "remotePolicy",
        "remote_policy",
    )
    if explicit is not None:
        return explicit
    remote = _job_remote_value(job)
    if remote is True:
        return "remote"
    if remote is False:
        return "not_remote"
    return remote if isinstance(remote, str) else None


def _job_seniority(job: dict[str, Any]) -> str | None:
    return _first_string(
        job,
        "seniority",
        "seniorityLevel",
        "seniority_level",
        "experienceLevel",
        "experience_level",
        "jobLevel",
        "job_level",
        "level",
    )


def _json_field_bundle(job: dict[str, Any], keys: Sequence[str]) -> dict[str, Any] | None:
    result: dict[str, Any] = {}
    for key in keys:
        value = job.get(key)
        if value is None:
            continue
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            continue
        result[key] = value
    return result or None


def _job_salary(job: dict[str, Any]) -> dict[str, Any] | None:
    return _json_field_bundle(
        job,
        (
            "compensation",
            "compensationTier",
            "compensation_tier",
            "salary",
            "salaryRange",
            "salary_range",
            "payRange",
            "pay_range",
        ),
    )


def _job_authorization(job: dict[str, Any]) -> dict[str, Any] | None:
    return _json_field_bundle(
        job,
        (
            "authorization",
            "workAuthorization",
            "work_authorization",
            "visaSponsorship",
            "visa_sponsorship",
            "sponsorship",
            "requiresSponsorship",
            "requires_sponsorship",
        ),
    )


def _keyword_values(value: object) -> list[str]:
    candidates: list[str] = []
    if isinstance(value, str):
        candidates.extend(_KEYWORD_SEPARATOR.split(value))
    elif isinstance(value, list | tuple):
        for item in value:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                candidate = _first_string(item, "name", "label", "value", "title")
                if candidate is not None:
                    candidates.append(candidate)
    elif isinstance(value, dict):
        candidate = _first_string(value, "name", "label", "value", "title")
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _job_keywords(job: dict[str, Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for key in ("skills", "keywords", "tags", "technologies", "techStack", "tech_stack"):
        for candidate in _keyword_values(job.get(key)):
            normalized = _SPACE.sub(" ", candidate).strip()
            dedupe_key = normalized.casefold()
            if (
                not normalized
                or len(normalized) > MAX_JOB_KEYWORD_CHARS
                or dedupe_key in seen
            ):
                continue
            seen.add(dedupe_key)
            result.append(normalized)
            if len(result) >= MAX_JOB_KEYWORDS:
                return result
    return result


def _job_industries(job: dict[str, Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    values: list[object] = [
        job.get("industry"),
        job.get("industries"),
        job.get("industryName"),
        job.get("industry_name"),
        job.get("industryLabels"),
        job.get("industry_labels"),
    ]
    categories = job.get("categories")
    if isinstance(categories, dict):
        values.extend((categories.get("industry"), categories.get("industries")))
    for value in values:
        for candidate in _keyword_values(value):
            normalized = _SPACE.sub(" ", candidate).strip()
            dedupe_key = normalized.casefold()
            if (
                not normalized
                or len(normalized) > MAX_JOB_KEYWORD_CHARS
                or dedupe_key in seen
            ):
                continue
            seen.add(dedupe_key)
            result.append(normalized)
            if len(result) >= MAX_JOB_KEYWORDS:
                return result
    return result


def _job_url(job: dict[str, Any], ats: str) -> str | None:
    keys = {
        "greenhouse": ("absolute_url", "url", "job_url"),
        "ashby": ("jobUrl", "job_url", "applyUrl", "applicationUrl", "hostedUrl"),
        "lever": ("hostedUrl", "applyUrl", "url"),
        "smartrecruiters": ("postingUrl", "applyUrl", "referralUrl", "url", "jobUrl"),
        "recruitee": ("careers_url", "careers_apply_url", "url", "apply_url"),
        "workable": ("url", "shortlink", "application_url"),
    }[ats]
    for key in keys:
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _job_id(job: dict[str, Any], ats: str) -> str | None:
    keys = {
        "greenhouse": ("id", "internal_job_id", "requisition_id"),
        "ashby": ("id", "jobId", "postingId"),
        "lever": ("id", "postingId"),
        "smartrecruiters": ("id", "uuid", "jobAdId"),
        "recruitee": ("id", "guid", "slug"),
        "workable": ("shortcode", "id", "code"),
    }[ats]
    for key in keys:
        value = job.get(key)
        if isinstance(value, str | int):
            return str(value)
    return None


def _job_title(job: dict[str, Any], ats: str) -> str | None:
    keys = {
        "greenhouse": ("title",),
        "ashby": ("title",),
        "lever": ("text", "title"),
        "smartrecruiters": ("name", "title"),
        "recruitee": ("title",),
        "workable": ("title", "full_title"),
    }[ats]
    for key in keys:
        value = job.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _job_location(job: dict[str, Any], ats: str) -> str | None:
    if ats == "greenhouse":
        return _nested_string(job, "location", "name") or _first_string(
            job, "locationName", "location_text"
        )
    if ats == "ashby":
        return _nested_string(job, "location", "name") or _first_string(
            job,
            "location",
            "locationName",
            "location_text",
        )
    if ats == "smartrecruiters":
        location = job.get("location")
        if isinstance(location, dict):
            full_location = location.get("fullLocation")
            if isinstance(full_location, str) and full_location.strip():
                return full_location.strip()
            pieces = [
                location.get("city"),
                location.get("region"),
                location.get("country"),
            ]
            joined = ", ".join(
                str(piece).strip() for piece in pieces if isinstance(piece, str) and piece.strip()
            )
            return joined or None
        return None
    if ats == "recruitee":
        location = job.get("location")
        if isinstance(location, str) and location.strip():
            return location.strip()
        locations = job.get("locations")
        if isinstance(locations, list) and locations:
            first = locations[0]
            if isinstance(first, dict):
                for key in ("name", "city", "state_name", "country"):
                    value = first.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return None
    if ats == "workable":
        location = job.get("location")
        if isinstance(location, str) and location.strip():
            return location.strip()
        if isinstance(location, dict):
            location_str = location.get("location_str")
            if isinstance(location_str, str) and location_str.strip():
                return location_str.strip()
            pieces = [
                location.get("city"),
                location.get("region"),
                location.get("country"),
            ]
            joined = ", ".join(
                str(piece).strip() for piece in pieces if isinstance(piece, str) and piece.strip()
            )
            return joined or None
        return None
    categories = job.get("categories")
    if isinstance(categories, dict):
        location = categories.get("location")
        if isinstance(location, str) and location.strip():
            return location.strip()
    return None


def _location_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, dict):
        return None
    direct = _first_string(
        value,
        "name",
        "fullLocation",
        "locationName",
        "location_str",
        "location",
    )
    if direct is not None:
        return direct
    pieces = [value.get(key) for key in ("city", "region", "state", "country")]
    joined = ", ".join(
        str(piece).strip() for piece in pieces if isinstance(piece, str) and piece.strip()
    )
    return joined or None


def _job_locations(job: dict[str, Any], ats: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def append(value: object) -> None:
        text = _location_text(value)
        if text is None:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        result.append(text)

    append(_job_location(job, ats))
    for key in ("locations", "secondaryLocations", "secondary_locations", "offices"):
        values = job.get(key)
        if isinstance(values, list | tuple):
            for value in values:
                append(value)
        else:
            append(values)
    return result


def _job_department(job: dict[str, Any], ats: str) -> str | None:
    if ats == "greenhouse":
        departments = job.get("departments")
        if isinstance(departments, dict):
            return _first_string(departments, "name")
        if isinstance(departments, list | tuple):
            for department in departments:
                if isinstance(department, dict):
                    name = _first_string(department, "name")
                    if name is not None:
                        return name
                elif isinstance(department, str) and department.strip():
                    return department.strip()
        return _first_string(job, "department")
    if ats == "ashby":
        department = job.get("department")
        if isinstance(department, str) and department.strip():
            return department.strip()
        return _nested_string(job, "department", "name")
    if ats == "smartrecruiters":
        return _nested_string(job, "department", "label") or _nested_string(
            job, "department", "description"
        )
    if ats == "recruitee":
        department = job.get("department")
        if isinstance(department, str) and department.strip():
            return department.strip()
        return _nested_string(job, "department", "name")
    if ats == "workable":
        department = job.get("department")
        if isinstance(department, str) and department.strip():
            return department.strip()
        if isinstance(department, dict):
            name = department.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None
    categories = job.get("categories")
    if isinstance(categories, dict):
        team = categories.get("team")
        if isinstance(team, str) and team.strip():
            return team.strip()
    return None


def _jobs_from_payload(payload: Any, ats: str) -> list[dict[str, Any]]:
    if ats in {"greenhouse", "ashby", "workable"}:
        key = "jobs"
        jobs = payload.get(key) if isinstance(payload, dict) else None
    elif ats == "smartrecruiters":
        jobs = payload.get("content") if isinstance(payload, dict) else None
    elif ats == "recruitee":
        jobs = payload.get("offers") if isinstance(payload, dict) else None
    else:
        jobs = payload if isinstance(payload, list) else None
    if not isinstance(jobs, list):
        return []
    return [job for job in jobs if isinstance(job, dict)]


def _job_url_matches_scope(url: str, target: AtsFeedTarget) -> bool:
    canonical = canonicalize_url(url)
    if not canonical:
        return False
    hostname = _hostname(canonical)
    first_segment = _first_path_segment(canonical)
    if target.ats == "greenhouse":
        return hostname in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and (
            first_segment == target.token
        )
    if target.ats == "ashby":
        return hostname == "jobs.ashbyhq.com" and first_segment == target.token
    if target.ats == "lever":
        return hostname == "jobs.lever.co" and first_segment == target.token
    if target.ats == "smartrecruiters":
        return hostname == "jobs.smartrecruiters.com" and first_segment == target.token
    if target.ats == "recruitee":
        return (
            hostname.endswith(".recruitee.com")
            and hostname.removesuffix(".recruitee.com") == target.token
        )
    if target.ats == "workable":
        return hostname == "apply.workable.com" and first_segment == "j"
    return False


def discovered_rows_from_feed(result: FeedResult) -> list[dict[str, Any]]:
    if result.status != "fetched":
        return []
    jobs = _jobs_from_payload(result.payload, result.target.ats)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for job in jobs:
        url = _job_url(job, result.target.ats)
        if not url or not _job_url_matches_scope(url, result.target):
            continue
        canonical = canonicalize_url(url)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        locations = _job_locations(job, result.target.ats)
        industries = _job_industries(job)
        rows.append(
            {
                "schema_version": PUBLIC_ATS_JOB_ROW_SCHEMA_VERSION,
                "canonical_url": canonical,
                "url": canonical,
                "source_record_id": result.target.source_record_id,
                "source_index": result.target.source_index,
                "source_assessment": result.target.source_assessment,
                "source_relationship": "known_ats",
                "registrable_domain": result.target.registrable_domain,
                "source_feed_url": result.target.canonical_url,
                "source_feed_ats": result.target.ats,
                "source_feed_token": result.target.token,
                "source_job_id": _job_id(job, result.target.ats),
                "source_job_title": _job_title(job, result.target.ats),
                "source_job_location": locations[0] if locations else None,
                "source_job_locations": locations,
                "source_job_department": _job_department(job, result.target.ats),
                "source_job_description": _job_description(job, result.target.ats),
                "source_job_apply_url": _job_apply_url(job, result.target.ats),
                "source_job_keywords": _job_keywords(job),
                "source_job_industry": industries[0] if industries else None,
                "source_job_industries": industries,
                "source_job_employment_type": _job_employment_type(job, result.target.ats),
                "source_job_work_mode": _job_work_mode(job),
                "source_job_remote": _job_remote_value(job),
                "source_job_seniority": _job_seniority(job),
                "source_job_salary": _job_salary(job),
                "source_job_authorization": _job_authorization(job),
                "discovered_at": _captured_at(),
                "discovery_method": "public ATS JSON feed; no browser, login, forms, or JS",
                "seed_source": "public_ats_feed",
            }
        )
    return rows


def _fetch_with_provider_limits(
    fetcher: Fetcher,
    target: AtsFeedTarget,
    config: FetchConfig,
    provider_semaphores: dict[str, threading.Semaphore],
) -> FeedResult:
    semaphore = provider_semaphores.get(target.ats)
    if semaphore is None:
        return fetcher(target, config)
    with semaphore:
        return fetcher(target, config)


def _receipt_row(result: FeedResult, discovered_count: int) -> dict[str, Any]:
    return {
        "canonical_url": result.target.canonical_url,
        "requested_url": result.target.url,
        "source_ats_url": result.target.source_url,
        "source_record_id": result.target.source_record_id,
        "source_index": result.target.source_index,
        "source_assessment": result.target.source_assessment,
        "source_relationship": "known_ats",
        "registrable_domain": result.target.registrable_domain,
        "source_feed_ats": result.target.ats,
        "source_feed_token": result.target.token,
        "capture_status": result.status,
        "captured_at": _captured_at(),
        "final_url": result.final_url,
        "http_status": result.http_status,
        "content_type": result.content_type,
        "body_bytes": result.body_bytes,
        "body_truncated": result.body_truncated,
        "body_sha256": result.body_sha256,
        "error_kind": result.error_kind,
        "raw_data_file": DISCOVERED_FILENAME if result.status == "fetched" else None,
        "discovered_job_count": discovered_count,
    }


def _resume_state(
    receipt_path: Path, discovered_path: Path, *, retry_rounds: int
) -> tuple[set[str], set[str], Counter[str]]:
    completed_targets: set[str] = set()
    discovered_urls: set[str] = set()
    failure_streaks: Counter[str] = Counter()
    for row in _load_output_jsonl(discovered_path):
        canonical_url = row.get("canonical_url")
        if isinstance(canonical_url, str):
            discovered_urls.add(canonical_url)
    for row in _load_output_jsonl(receipt_path):
        canonical_url = row.get("canonical_url")
        if not isinstance(canonical_url, str):
            continue
        status = row.get("capture_status")
        if status in RETRYABLE_STATUSES:
            failure_streaks[canonical_url] += 1
            if failure_streaks[canonical_url] > retry_rounds:
                completed_targets.add(canonical_url)
        else:
            completed_targets.add(canonical_url)
            failure_streaks[canonical_url] = 0
    return completed_targets, discovered_urls, failure_streaks


def discover_public_ats_jobs(
    *,
    triage_input: Path,
    output_dir: Path,
    fetcher: Fetcher = fetch_feed,
    concurrency: int = DEFAULT_CONCURRENCY,
    retry_rounds: int = DEFAULT_RETRY_ROUNDS,
    config: FetchConfig | None = None,
) -> dict[str, Any]:
    config = config or FetchConfig()
    output_dir.mkdir(parents=True, exist_ok=True)
    discovered_path = output_dir / DISCOVERED_FILENAME
    receipt_path = output_dir / RECEIPT_FILENAME
    summary_path = output_dir / SUMMARY_FILENAME
    manifest_path = output_dir / MANIFEST_FILENAME
    failure_path = output_dir / FAILURE_STREAKS_FILENAME

    targets = select_feed_targets(_load_jsonl(triage_input))
    completed, discovered_urls, failure_streaks = _resume_state(
        receipt_path, discovered_path, retry_rounds=retry_rounds
    )
    queue = [target for target in targets if target.canonical_url not in completed]
    status_counts: Counter[str] = Counter()
    feed_counts: Counter[str] = Counter()
    discovered_count = 0
    attempted_count = 0
    provider_semaphores: dict[str, threading.Semaphore] = {
        "smartrecruiters": threading.Semaphore(SMARTRECRUITERS_MAX_CONCURRENCY)
    }

    with (
        discovered_path.open("a", encoding="utf-8") as discovered_handle,
        receipt_path.open("a", encoding="utf-8") as receipt_handle,
        ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor,
    ):
        futures: dict[Future[FeedResult], AtsFeedTarget] = {}

        def submit_ready() -> None:
            while queue and len(futures) < max(1, concurrency):
                target = queue.pop(0)
                futures[
                    executor.submit(
                        _fetch_with_provider_limits, fetcher, target, config, provider_semaphores
                    )
                ] = target

        submit_ready()
        while futures:
            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                target = futures.pop(future)
                attempted_count += 1
                try:
                    result = future.result()
                except Exception as error:
                    result = FeedResult(
                        target=target,
                        status="network_error",
                        error_kind=type(error).__name__,
                    )

                if _is_retryable_status(result.status):
                    failure_streaks[target.canonical_url] += 1
                    if failure_streaks[target.canonical_url] <= retry_rounds:
                        queue.append(target)
                        submit_ready()
                        continue
                else:
                    failure_streaks[target.canonical_url] = 0

                rows = [
                    row
                    for row in discovered_rows_from_feed(result)
                    if row["canonical_url"] not in discovered_urls
                ]
                for row in rows:
                    discovered_urls.add(str(row["canonical_url"]))
                    _append_json_line(discovered_handle, row)
                discovered_count += len(rows)
                status_counts[result.status] += 1
                feed_counts[result.target.ats] += len(rows)
                _append_json_line(receipt_handle, _receipt_row(result, len(rows)))
            submit_ready()

    summary = {
        "triage_input": str(triage_input),
        "output_dir": str(output_dir),
        "total_feed_targets": len(targets),
        "skipped_completed_feeds": len(
            [target for target in targets if target.canonical_url in completed]
        ),
        "attempted_feed_fetches": attempted_count,
        "discovered_job_urls": discovered_count,
        "status_counts": dict(sorted(status_counts.items())),
        "discovered_job_counts_by_ats": dict(sorted(feed_counts.items())),
        "retry_rounds": retry_rounds,
        "max_total_attempts_per_feed": retry_rounds + 1,
        "concurrency": concurrency,
        "max_feed_bytes": config.max_feed_bytes,
        "completed_at": _captured_at(),
    }
    _write_json(summary_path, summary)
    _write_json(
        failure_path,
        {
            "failure_streaks": dict(sorted(failure_streaks.items())),
            "written_at": _captured_at(),
        },
    )
    manifest = {
        **summary,
        "version": 1,
        "kind": "public_ats_job_feed_manifest",
        "artifact_files": {
            "discovered_jobs": _artifact_metadata(discovered_path),
            "receipts": _artifact_metadata(receipt_path),
            "summary": _artifact_metadata(summary_path),
            "failure_streaks": _artifact_metadata(failure_path),
            "manifest": str(manifest_path),
        },
        "collection_method": "Unauthenticated public ATS JSON GET feeds only.",
    }
    _write_json(manifest_path, manifest)
    return summary


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--triage-input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--concurrency", type=_positive_int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--retry-rounds", type=int, default=DEFAULT_RETRY_ROUNDS)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--max-feed-bytes", type=_positive_int, default=DEFAULT_MAX_FEED_BYTES)
    parser.add_argument(
        "--allow-sandbox-egress-alias",
        action="store_true",
        help="Allow 198.18.0.0/15 DNS targets used by sandbox egress proxies.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    summary = discover_public_ats_jobs(
        triage_input=args.triage_input,
        output_dir=args.output_dir,
        concurrency=args.concurrency,
        retry_rounds=args.retry_rounds,
        config=FetchConfig(
            timeout_seconds=args.timeout_seconds,
            max_feed_bytes=args.max_feed_bytes,
            allow_sandbox_egress_alias=args.allow_sandbox_egress_alias,
        ),
    )
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
