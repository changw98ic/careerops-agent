#!/usr/bin/env python3
"""Import metadata-only AI-agent company candidates from Hugging Face.

The importer resolves the public Hugging Face dataset API for
`DeepNLP/AI-Agent-Index-2025-March`, reads the public JSONL data file with a
bounded byte cap and retry policy, extracts the publisher website field, dedupes
against existing seed inventories, and writes provisional candidate rows. It
does not fetch or crawl any linked publisher websites.
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
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener, getproxies

from collect_agent_homepages import (
    DEFAULT_USER_AGENT,
    _ensure_public_http_url,
    _PublicRedirectHandler,
)

SOURCE_REPOSITORY = "DeepNLP/AI-Agent-Index-2025-March"
SOURCE_ID = "deepnlp-ai-agent-index-2025-march"
SOURCE_DATASET_URL = f"https://huggingface.co/datasets/{SOURCE_REPOSITORY}"
SOURCE_API_URL = f"https://huggingface.co/api/datasets/{SOURCE_REPOSITORY}"
SOURCE_FILE = "data_agent_202503.json"
SOURCE_LICENSE = "mit"
DEFAULT_OUTPUT_FILENAME = "hf_ai_agent_candidates.jsonl"
DEFAULT_MAX_METADATA_BYTES = 1_000_000
DEFAULT_MAX_DATA_BYTES = 20_000_000
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRY_ROUNDS = 4
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5
DEFAULT_CHUNK_BYTES = 64 * 1024

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
    "huggingface.co",
    "kaggle.com",
    "arxiv.org",
    "paperswithcode.com",
    "wikipedia.org",
    "wikidata.org",
    "youtube.com",
    "youtu.be",
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
    ".framer.website",
    ".gitbook.io",
    ".readthedocs.io",
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
    max_metadata_bytes: int = DEFAULT_MAX_METADATA_BYTES
    max_data_bytes: int = DEFAULT_MAX_DATA_BYTES
    retry_rounds: int = DEFAULT_RETRY_ROUNDS
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS
    user_agent: str = DEFAULT_USER_AGENT
    allow_sandbox_egress_alias: bool = False


@dataclass(frozen=True)
class DatasetMetadata:
    api_url: str
    dataset_url: str
    revision: str
    license: str
    source_file: str
    raw_file_url: str
    body_bytes: int
    body_sha256: str
    attempts: int


@dataclass(frozen=True)
class DatasetFetch:
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
class SourceRecord:
    row_number: int
    content_name: str
    publisher_id: str
    website: str
    canonical_url: str
    hostname: str
    registrable_domain: str
    field: str
    subfield: str
    month: str
    statistic: dict[str, Any]


class ResponseTooLargeError(ValueError):
    """Raised when a Hugging Face response exceeds the configured byte cap."""


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


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    return name or "unknown"


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


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _read_response_limited(response: Any, max_bytes: int) -> bytes:
    if max_bytes < 1:
        raise ValueError("response byte cap must be at least 1")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(DEFAULT_CHUNK_BYTES, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError("Hugging Face response exceeded byte cap")
    return b"".join(chunks)


def _fetch_public_bytes(
    url: str,
    config: ImportConfig,
    *,
    max_bytes: int,
    accept: str,
) -> tuple[bytes, str, int, str, int]:
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
                headers={"Accept": accept, "User-Agent": config.user_agent},
                method="GET",
            )
            opener = build_opener(
                _PublicRedirectHandler(
                    proxies,
                    allow_sandbox_egress_alias=config.allow_sandbox_egress_alias,
                )
            )
            with opener.open(request, timeout=config.timeout_seconds) as response:
                raw_body = _read_response_limited(response, max_bytes)
                final_url = response.geturl()
                status_code = response.getcode()
                content_type = response.headers.get_content_type().lower()
            return raw_body, final_url, status_code, content_type, attempt
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


def raw_dataset_file_url(repository: str, revision: str, source_file: str) -> str:
    quoted_file = "/".join(quote(part, safe="") for part in source_file.split("/"))
    return f"https://huggingface.co/datasets/{repository}/raw/{revision}/{quoted_file}"


def resolve_dataset_metadata(
    api_url: str,
    config: ImportConfig,
    *,
    source_file: str = SOURCE_FILE,
) -> DatasetMetadata:
    raw_body, _final_url, _status, _content_type, attempts = _fetch_public_bytes(
        api_url,
        config,
        max_bytes=config.max_metadata_bytes,
        accept="application/json",
    )
    try:
        metadata = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid Hugging Face dataset API JSON: {error.msg}") from error
    if not isinstance(metadata, dict):
        raise ValueError("Hugging Face dataset API response must be a JSON object")
    revision = metadata.get("sha")
    if not isinstance(revision, str) or not revision:
        raise ValueError("Hugging Face dataset API response did not include a revision sha")
    card_data = metadata.get("cardData")
    license_name = ""
    if isinstance(card_data, dict):
        license_value = card_data.get("license")
        if isinstance(license_value, str):
            license_name = license_value.lower()
    siblings = metadata.get("siblings")
    if not isinstance(siblings, list):
        raise ValueError("Hugging Face dataset API response did not include sibling files")
    sibling_names = {
        sibling.get("rfilename")
        for sibling in siblings
        if isinstance(sibling, dict) and isinstance(sibling.get("rfilename"), str)
    }
    if source_file not in sibling_names:
        raise ValueError(f"Hugging Face dataset does not expose {source_file!r}")
    return DatasetMetadata(
        api_url=api_url,
        dataset_url=SOURCE_DATASET_URL,
        revision=revision,
        license=license_name or "unknown",
        source_file=source_file,
        raw_file_url=raw_dataset_file_url(SOURCE_REPOSITORY, revision, source_file),
        body_bytes=len(raw_body),
        body_sha256=hashlib.sha256(raw_body).hexdigest(),
        attempts=attempts,
    )


def fetch_dataset_file(metadata: DatasetMetadata, config: ImportConfig) -> DatasetFetch:
    raw_body, final_url, status, content_type, attempts = _fetch_public_bytes(
        metadata.raw_file_url,
        config,
        max_bytes=config.max_data_bytes,
        accept="application/json,text/plain;q=0.9,*/*;q=0.1",
    )
    charset = "utf-8"
    return DatasetFetch(
        text=raw_body.decode(charset, errors="replace"),
        url=metadata.raw_file_url,
        final_url=final_url,
        http_status=status,
        content_type=content_type,
        body_bytes=len(raw_body),
        body_truncated=False,
        body_sha256=hashlib.sha256(raw_body).hexdigest(),
        attempts=attempts,
    )


def parse_source_records(dataset_text: str) -> tuple[list[SourceRecord], Counter[str]]:
    records: list[SourceRecord] = []
    summary: Counter[str] = Counter()
    for line_number, line in enumerate(dataset_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        summary["source_rows"] += 1
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise ValueError(f"dataset line {line_number}: invalid JSON: {error.msg}") from error
        if not isinstance(row, dict):
            raise ValueError(f"dataset line {line_number}: expected a JSON object")
        website = row.get("website")
        if not isinstance(website, str) or not website.strip():
            summary["missing_website"] += 1
            continue
        canonical = canonicalize_url(website)
        if not canonical:
            summary["invalid_website"] += 1
            continue
        if not is_company_website_candidate(canonical):
            summary["filtered_non_company"] += 1
            continue
        hostname = hostname_for_url(canonical)
        statistic = row.get("statistic")
        records.append(
            SourceRecord(
                row_number=line_number,
                content_name=_clean_name(str(row.get("content_name") or "")),
                publisher_id=str(row.get("publisher_id") or ""),
                website=website.strip(),
                canonical_url=canonical,
                hostname=hostname,
                registrable_domain=registrable_domain_for_hostname(hostname),
                field=str(row.get("field") or ""),
                subfield=str(row.get("subfield") or ""),
                month=str(row.get("month") or ""),
                statistic=statistic if isinstance(statistic, dict) else {},
            )
        )
    return records, summary


def candidate_row(
    record: SourceRecord,
    metadata: DatasetMetadata,
    fetch: DatasetFetch,
) -> dict[str, Any]:
    return {
        "record_id": record_id_for_domain(record.registrable_domain),
        "candidate_entity_name": record.content_name,
        "normalized_name": record.content_name.lower(),
        "registrable_domain": record.registrable_domain,
        "representative_website": record.canonical_url,
        "candidate_type": "agent_ecosystem_company_candidate",
        "candidate_state": "unverified",
        "website_fetch_status": "not_fetched",
        "company_terms_status": "unknown",
        "source_usage_status": (
            "publisher_declared_mit_metadata_seed_pending_human_provenance_review"
        ),
        "source_evidence": [
            {
                "source_id": SOURCE_ID,
                "dataset_url": metadata.dataset_url,
                "dataset_api_url": metadata.api_url,
                "dataset_revision": metadata.revision,
                "dataset_license": metadata.license,
                "source_file": metadata.source_file,
                "source_url": fetch.url,
                "source_final_url": fetch.final_url,
                "source_body_sha256": fetch.body_sha256,
                "source_row": record.row_number,
                "source_record_id": record.publisher_id,
                "agent_field": record.field,
                "agent_subfield": record.subfield,
                "source_entity_name": record.content_name,
                "source_website": record.website,
                "source_statistic": record.statistic,
                "source_month": record.month,
            }
        ],
        "personal_data_included": False,
        "collection_boundary": (
            "Metadata-only candidate seed from a public Hugging Face dataset row. "
            "No linked product/company site was fetched or crawled."
        ),
        "captured_at": _captured_at(),
    }


def select_new_candidates(
    records: Iterable[SourceRecord],
    metadata: DatasetMetadata,
    fetch: DatasetFetch,
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
        rows.append(candidate_row(record, metadata, fetch))
        summary["candidate_written"] += 1
    return rows, summary


def import_hf_candidates(
    dataset_text: str,
    metadata: DatasetMetadata,
    fetch: DatasetFetch,
    output_path: Path,
    *,
    existing_record_ids: set[str],
    existing_hostnames: set[str],
    existing_domains: set[str],
) -> Counter[str]:
    records, parse_summary = parse_source_records(dataset_text)
    rows, summary = select_new_candidates(
        records,
        metadata,
        fetch,
        existing_record_ids=existing_record_ids,
        existing_hostnames=existing_hostnames,
        existing_domains=existing_domains,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            _append_json_line(handle, row)
    summary.update(parse_summary)
    summary["dataset_metadata_bytes"] = metadata.body_bytes
    summary["dataset_metadata_attempts"] = metadata.attempts
    summary["dataset_body_bytes"] = fetch.body_bytes
    summary["dataset_attempts"] = fetch.attempts
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
    parser.add_argument("--dataset-api-url", default=SOURCE_API_URL)
    parser.add_argument("--source-file", default=SOURCE_FILE)
    parser.add_argument("--max-metadata-bytes", type=int, default=DEFAULT_MAX_METADATA_BYTES)
    parser.add_argument("--max-data-bytes", type=int, default=DEFAULT_MAX_DATA_BYTES)
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
    config = ImportConfig(
        timeout_seconds=args.timeout_seconds,
        max_metadata_bytes=args.max_metadata_bytes,
        max_data_bytes=args.max_data_bytes,
        retry_rounds=args.retry_rounds,
        retry_backoff_seconds=args.retry_backoff_seconds,
        user_agent=args.user_agent,
        allow_sandbox_egress_alias=args.allow_sandbox_egress_alias,
    )
    try:
        existing_record_ids, existing_hostnames, existing_domains = load_existing_identity(
            args.existing_candidates
        )
        metadata = resolve_dataset_metadata(
            args.dataset_api_url,
            config,
            source_file=args.source_file,
        )
        if metadata.license != SOURCE_LICENSE:
            raise ValueError(
                f"expected source license metadata {SOURCE_LICENSE!r}, got {metadata.license!r}"
            )
        fetch = fetch_dataset_file(metadata, config)
        summary = import_hf_candidates(
            fetch.text,
            metadata,
            fetch,
            args.output,
            existing_record_ids=existing_record_ids,
            existing_hostnames=existing_hostnames,
            existing_domains=existing_domains,
        )
    except (OSError, ValueError, HTTPError, URLError) as error:
        print(f"Hugging Face AI agent candidate import failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(dict(sorted(summary.items())), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
