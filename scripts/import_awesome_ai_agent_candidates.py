#!/usr/bin/env python3
"""Import provisional AI-agent candidates from a pinned public Awesome README.

The importer reads the Apache-2.0 `jim-schwoebel/awesome_ai_agents` README at a
pinned commit, extracts external HTTP(S) product/company-looking Markdown links,
dedupes against an existing candidate inventory, and writes metadata-only JSONL
candidate rows. It does not fetch any linked product/company websites.
"""

from __future__ import annotations

import argparse
import hashlib
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

SOURCE_REPOSITORY = "jim-schwoebel/awesome_ai_agents"
SOURCE_COMMIT = "51b1a7ebc7ad9bfa91632ef77a315faca0bb8356"
SOURCE_LICENSE = "Apache-2.0"
SOURCE_README_URL = (
    f"https://raw.githubusercontent.com/{SOURCE_REPOSITORY}/{SOURCE_COMMIT}/README.md"
)
SOURCE_ID = "github-jim-schwoebel-awesome-ai-agents-readme"
DEFAULT_OUTPUT_FILENAME = "awesome_ai_agent_candidates.jsonl"
DEFAULT_MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_RETRY_ROUNDS = 4
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5

MARKDOWN_LINK_PATTERN = re.compile(
    r"(?<!!)\[([^\]\n]{1,240})\]\((https?://[^)\s]+)(?:\s+\"[^\"]*\")?\)",
    re.IGNORECASE,
)
AUTOLINK_PATTERN = re.compile(r"<(https?://[^>\s]+)>", re.IGNORECASE)
BARE_URL_PATTERN = re.compile(r"(?<![\](<])\bhttps?://[^\s)>\]]+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ".,;:!?)]}'\""
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
    "github.com",
    "raw.githubusercontent.com",
    "gist.github.com",
    "gitlab.com",
    "bitbucket.org",
    "npmjs.com",
    "pypi.org",
    "crates.io",
    "docker.com",
    "hub.docker.com",
    "huggingface.co",
    "kaggle.com",
    "arxiv.org",
    "paperswithcode.com",
    "wikipedia.org",
    "wikidata.org",
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "discord.gg",
    "discord.com",
    "slack.com",
    "medium.com",
    "substack.com",
    "notion.site",
    "notion.so",
    "docs.google.com",
    "drive.google.com",
    "cloudflare.com",
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
class ReadmeFetch:
    text: str
    url: str
    final_url: str
    http_status: int
    content_type: str
    body_bytes: int
    body_truncated: bool
    body_sha256: str
    attempts: int


@dataclass(frozen=True)
class LinkCandidate:
    label: str
    url: str
    canonical_url: str
    hostname: str
    registrable_domain: str
    source_line: int


class ResponseTooLargeError(ValueError):
    """Raised when the pinned README exceeds the configured response cap."""


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


def _clean_label(label: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", label)).strip()


def _clean_url(url: str) -> str:
    return url.strip().rstrip(TRAILING_URL_PUNCTUATION)


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(_clean_url(url))
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


def is_company_link_candidate(canonical_url: str) -> bool:
    hostname = hostname_for_url(canonical_url)
    if not hostname or _host_is_excluded(hostname):
        return False
    registrable_domain = registrable_domain_for_hostname(hostname)
    if not registrable_domain or "." not in registrable_domain:
        return False
    return not _path_is_excluded(canonical_url)


def extract_markdown_links(markdown_text: str) -> list[LinkCandidate]:
    candidates: list[LinkCandidate] = []
    seen_pairs: set[tuple[str, str]] = set()
    for line_number, line in enumerate(markdown_text.splitlines(), start=1):
        matches: list[tuple[str, str]] = []
        matches.extend(
            (match.group(1), match.group(2)) for match in MARKDOWN_LINK_PATTERN.finditer(line)
        )
        matches.extend(
            (match.group(1), match.group(1)) for match in AUTOLINK_PATTERN.finditer(line)
        )
        matches.extend(
            (match.group(0), match.group(0)) for match in BARE_URL_PATTERN.finditer(line)
        )
        for label, raw_url in matches:
            canonical = canonicalize_url(raw_url)
            if not canonical:
                continue
            hostname = hostname_for_url(canonical)
            registrable_domain = registrable_domain_for_hostname(hostname)
            key = (_clean_label(label), canonical)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            candidates.append(
                LinkCandidate(
                    label=_clean_label(label),
                    url=_clean_url(raw_url),
                    canonical_url=canonical,
                    hostname=hostname,
                    registrable_domain=registrable_domain,
                    source_line=line_number,
                )
            )
    return candidates


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


def candidate_row(candidate: LinkCandidate, fetch: ReadmeFetch) -> dict[str, Any]:
    record_id = record_id_for_domain(candidate.registrable_domain)
    label = candidate.label or candidate.registrable_domain
    return {
        "record_id": record_id,
        "candidate_entity_name": label,
        "normalized_name": label.lower(),
        "registrable_domain": candidate.registrable_domain,
        "representative_website": candidate.canonical_url,
        "candidate_type": "provisional_public_awesome_ai_agent_link_candidate",
        "candidate_state": "unverified",
        "website_fetch_status": "not_fetched",
        "company_terms_status": "unknown",
        "source_usage_status": "public_apache_2_0_readme_link_pending_human_provenance_review",
        "source_evidence": [
            {
                "source_id": SOURCE_ID,
                "repository": SOURCE_REPOSITORY,
                "repository_url": f"https://github.com/{SOURCE_REPOSITORY}",
                "source_license": SOURCE_LICENSE,
                "source_commit": SOURCE_COMMIT,
                "source_file": "README.md",
                "source_url": fetch.url,
                "source_final_url": fetch.final_url,
                "source_body_sha256": fetch.body_sha256,
                "source_line": candidate.source_line,
                "source_link_text": candidate.label,
                "source_link_url": candidate.url,
                "source_link_canonical_url": candidate.canonical_url,
            }
        ],
        "personal_data_included": False,
        "collection_boundary": (
            "Metadata-only provisional candidate seed from a public README link. "
            "No linked product/company site was fetched or crawled."
        ),
        "captured_at": _captured_at(),
    }


def select_new_candidates(
    links: Iterable[LinkCandidate],
    fetch: ReadmeFetch,
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
    for link in links:
        summary["link_seen"] += 1
        if not is_company_link_candidate(link.canonical_url):
            summary["filtered_non_company"] += 1
            continue
        record_id = record_id_for_domain(link.registrable_domain)
        if (
            record_id in seen_record_ids
            or link.hostname in seen_hostnames
            or link.registrable_domain in seen_domains
        ):
            summary["deduped_existing"] += 1
            continue
        seen_record_ids.add(record_id)
        seen_hostnames.add(link.hostname)
        seen_domains.add(link.registrable_domain)
        rows.append(candidate_row(link, fetch))
        summary["candidate_written"] += 1
    return rows, summary


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def fetch_readme(url: str, config: ImportConfig) -> ReadmeFetch:
    if config.max_response_bytes < 1:
        raise ValueError("max_response_bytes must be at least 1")
    if config.retry_rounds < 0:
        raise ValueError("retry_rounds must be at least 0")
    if config.retry_backoff_seconds < 0:
        raise ValueError("retry_backoff_seconds must be at least 0")
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
                    "Accept": "text/markdown,text/plain;q=0.9,*/*;q=0.1",
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
                raw_body = response.read(config.max_response_bytes + 1)
                if len(raw_body) > config.max_response_bytes:
                    raise ResponseTooLargeError("README response exceeded byte cap")
                content_type = response.headers.get_content_type().lower()
                charset = response.headers.get_content_charset() or "utf-8"
                final_url = response.geturl()
                status_code = response.getcode()
            return ReadmeFetch(
                text=raw_body.decode(charset, errors="replace"),
                url=url,
                final_url=final_url,
                http_status=status_code,
                content_type=content_type,
                body_bytes=len(raw_body),
                body_truncated=False,
                body_sha256=hashlib.sha256(raw_body).hexdigest(),
                attempts=attempt,
            )
        except HTTPError as error:
            last_error = error
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


def import_awesome_candidates(
    readme_text: str,
    fetch: ReadmeFetch,
    output_path: Path,
    *,
    existing_record_ids: set[str],
    existing_hostnames: set[str],
    existing_domains: set[str],
) -> Counter[str]:
    links = extract_markdown_links(readme_text)
    rows, summary = select_new_candidates(
        links,
        fetch,
        existing_record_ids=existing_record_ids,
        existing_hostnames=existing_hostnames,
        existing_domains=existing_domains,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            _append_json_line(handle, row)
    summary["readme_body_bytes"] = fetch.body_bytes
    summary["readme_attempts"] = fetch.attempts
    summary["output_rows"] = len(rows)
    return summary


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
    parser.add_argument(
        "--readme-url",
        default=SOURCE_README_URL,
        help="Pinned raw README URL. Defaults to the approved commit.",
    )
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=DEFAULT_MAX_RESPONSE_BYTES,
        help="Maximum bytes read from the pinned README.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--retry-rounds",
        type=int,
        default=DEFAULT_RETRY_ROUNDS,
        help="Retries after the initial attempt (default: 4; total attempts: 5).",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=DEFAULT_RETRY_BACKOFF_SECONDS,
        help="Initial exponential retry backoff.",
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
    try:
        existing_record_ids, existing_hostnames, existing_domains = load_existing_identity(
            args.existing_candidates
        )
        fetch = fetch_readme(
            args.readme_url,
            ImportConfig(
                timeout_seconds=args.timeout_seconds,
                max_response_bytes=args.max_response_bytes,
                retry_rounds=args.retry_rounds,
                retry_backoff_seconds=args.retry_backoff_seconds,
                user_agent=args.user_agent,
                allow_sandbox_egress_alias=args.allow_sandbox_egress_alias,
            ),
        )
        summary = import_awesome_candidates(
            fetch.text,
            fetch,
            args.output,
            existing_record_ids=existing_record_ids,
            existing_hostnames=existing_hostnames,
            existing_domains=existing_domains,
        )
    except (OSError, ValueError, HTTPError, URLError) as error:
        print(f"awesome AI agent candidate import failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(dict(sorted(summary.items())), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
