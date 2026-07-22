#!/usr/bin/env python3
"""Select high-quality recruitment crawl seeds from sitemap and ATS discoveries."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

OUTPUT_FILENAME = "prioritized_recruitment_seeds_2026-07-18.jsonl"
SELECTOR_VERSION = "prioritized_recruitment_seeds_v1"
DEFAULT_MAX_SITEMAP_SEEDS_PER_COMPANY = 25
FIRST_PARTY_RELATIONSHIPS = {"first_party", "known_ats"}
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
JOB_DETAIL_TERMS = {
    "jobs",
    "job",
    "careers",
    "career",
    "positions",
    "position",
    "openings",
    "opening",
    "roles",
    "role",
    "vacancies",
    "vacancy",
    "opportunities",
    "opportunity",
}
CAREER_ROOT_TERMS = {
    "careers",
    "career",
    "jobs",
    "join-us",
    "join",
    "work-with-us",
    "open-positions",
    "openings",
    "positions",
}
EXCLUDED_SEGMENTS = {
    "api",
    "apis",
    "docs",
    "doc",
    "documentation",
    "reference",
    "developers",
    "developer",
    "swagger",
    "openapi",
    "blog",
    "blogs",
    "article",
    "articles",
    "news",
    "press",
    "resources",
    "resource",
    "learn",
    "learning",
    "glossary",
    "dictionary",
    "case-study",
    "case-studies",
    "casestudy",
    "webinar",
    "webinars",
    "guide",
    "guides",
    "templates",
    "template",
    "product",
    "products",
    "solutions",
    "solution",
    "use-cases",
    "use-case",
    "customers",
    "customer",
    "pricing",
}
VENDOR_PRODUCT_PHRASES = (
    "recruiting-software",
    "recruitment-software",
    "recruiting-platform",
    "recruitment-platform",
    "candidate-screening",
    "talent-intelligence",
    "applicant-tracking",
    "ats-integration",
    "hire-at-scale",
    "hiring-platform",
)
JOB_ID_PATTERN = re.compile(r"(?:^|[-_/])(?:jobs?|positions?|req|requisition)[-_/]?\d{3,}")


@dataclass(frozen=True)
class CandidateSeed:
    canonical_url: str
    score: int
    quality_tier: str
    target_kind: str
    reason_codes: tuple[str, ...]
    source_kind: str
    source_line_number: int
    input_path: Path
    source_row: dict[str, Any]


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


def _path_segments(url: str) -> list[str]:
    return [segment.lower() for segment in urlsplit(url).path.split("/") if segment]


def _is_known_ats_url(url: str) -> bool:
    hostname = _hostname(url)
    return any(hostname == host or hostname.endswith(f".{host}") for host in ATS_HOST_PATTERNS)


def _relationship(row: dict[str, Any], canonical_url: str) -> str:
    relationship = row.get("source_relationship")
    if isinstance(relationship, str) and relationship:
        return relationship
    if _is_known_ats_url(canonical_url):
        return "known_ats"
    return ""


def _excluded_reason(canonical_url: str, source_kind: str) -> str | None:
    if source_kind == "ats_feed":
        return None
    segments = _path_segments(canonical_url)
    if any(segment in EXCLUDED_SEGMENTS for segment in segments):
        return "excluded_content_or_vendor_segment"
    searchable = "-".join(segments)
    if any(phrase in searchable for phrase in VENDOR_PRODUCT_PHRASES):
        return "excluded_vendor_product_phrase"
    if urlsplit(canonical_url).query:
        return "excluded_query_url"
    return None


def _score_sitemap_row(row: dict[str, Any], canonical_url: str) -> tuple[int, str, list[str]]:
    relationship = _relationship(row, canonical_url)
    if relationship not in FIRST_PARTY_RELATIONSHIPS:
        return 0, "excluded", ["excluded_non_first_party_or_unknown_ats"]
    segments = _path_segments(canonical_url)
    if not segments:
        return 0, "excluded", ["excluded_root_url"]

    reason_codes: list[str] = []
    score = 0
    target_kind = "career_or_jobs_page"
    if relationship == "known_ats" or _is_known_ats_url(canonical_url):
        score += 88
        target_kind = "known_ats_page"
        reason_codes.append("known_ats_host")
    else:
        score += 45
        reason_codes.append("first_party")

    last_segment = segments[-1]
    if last_segment in CAREER_ROOT_TERMS or "-".join(segments) in CAREER_ROOT_TERMS:
        score += 35
        target_kind = "career_portal"
        reason_codes.append("career_root_path")
    if any(segment in JOB_DETAIL_TERMS for segment in segments):
        score += 22
        reason_codes.append("job_or_career_segment")
    if len(segments) >= 2 and any(segment in JOB_DETAIL_TERMS for segment in segments[:-1]):
        score += 18
        target_kind = "likely_job_detail_page"
        reason_codes.append("nested_under_job_or_career_path")
    if JOB_ID_PATTERN.search(urlsplit(canonical_url).path.lower()):
        score += 20
        target_kind = "likely_job_detail_page"
        reason_codes.append("job_id_pattern")
    if any(term in last_segment for term in ("apply", "opening", "position", "role")):
        score += 10
        reason_codes.append("action_or_role_slug")

    if not any(code.endswith("path") or code.endswith("segment") for code in reason_codes):
        return 0, "excluded", ["excluded_no_career_or_job_path_signal"]
    return min(score, 100), target_kind, reason_codes


def _score_ats_row(row: dict[str, Any], canonical_url: str) -> tuple[int, str, list[str]]:
    reason_codes = ["public_ats_feed", "known_ats_job_feed"]
    if isinstance(row.get("source_job_id"), str) and row["source_job_id"]:
        reason_codes.append("source_job_id")
    if isinstance(row.get("source_job_title"), str) and row["source_job_title"]:
        reason_codes.append("source_job_title")
    return 100, "ats_job_posting", reason_codes


def _quality_tier(score: int) -> str:
    if score >= 95:
        return "high"
    if score >= 80:
        return "medium"
    return "low"


def score_row(
    row: dict[str, Any],
    *,
    source_kind: str,
    source_line_number: int,
    input_path: Path,
) -> CandidateSeed | None:
    raw_url = row.get("canonical_url") or row.get("url")
    if not isinstance(raw_url, str):
        return None
    canonical_url = canonicalize_url(raw_url)
    if not canonical_url:
        return None
    excluded_reason = _excluded_reason(canonical_url, source_kind)
    if excluded_reason is not None:
        return None
    if source_kind == "ats_feed":
        score, target_kind, reason_codes = _score_ats_row(row, canonical_url)
    else:
        score, target_kind, reason_codes = _score_sitemap_row(row, canonical_url)
    if score < 70:
        return None
    return CandidateSeed(
        canonical_url=canonical_url,
        score=score,
        quality_tier=_quality_tier(score),
        target_kind=target_kind,
        reason_codes=tuple(sorted(set(reason_codes))),
        source_kind=source_kind,
        source_line_number=source_line_number,
        input_path=input_path,
        source_row=row,
    )


def _load_candidates(path: Path, *, source_kind: str) -> list[CandidateSeed]:
    candidates: list[CandidateSeed] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSONL: {error.msg}") from error
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            candidate = score_row(
                row,
                source_kind=source_kind,
                source_line_number=line_number,
                input_path=path,
            )
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def _candidate_sort_key(candidate: CandidateSeed) -> tuple[int, int, str, str]:
    source_priority = 1 if candidate.source_kind == "ats_feed" else 0
    return (
        candidate.score,
        source_priority,
        str(candidate.source_row.get("source_record_id") or ""),
        candidate.canonical_url,
    )


def _source_company_key(candidate: CandidateSeed) -> str:
    row = candidate.source_row
    source_record_id = row.get("source_record_id")
    if isinstance(source_record_id, str) and source_record_id:
        return f"record:{source_record_id}"
    domain = row.get("registrable_domain")
    if isinstance(domain, str) and domain:
        return f"domain:{domain.lower()}"
    return f"url-host:{_hostname(candidate.canonical_url)}"


def _cap_sitemap_candidates_per_company(
    candidates: Iterable[CandidateSeed], *, max_per_company: int
) -> list[CandidateSeed]:
    if max_per_company < 1:
        raise ValueError("max_sitemap_per_company must be at least 1")
    sitemap_by_company: dict[str, list[CandidateSeed]] = {}
    output: list[CandidateSeed] = []
    for candidate in candidates:
        if candidate.source_kind != "sitemap":
            output.append(candidate)
            continue
        sitemap_by_company.setdefault(_source_company_key(candidate), []).append(candidate)
    for grouped_candidates in sitemap_by_company.values():
        grouped_candidates.sort(key=_candidate_sort_key, reverse=True)
        output.extend(grouped_candidates[:max_per_company])
    return output


def _select_best_by_url(candidates: Iterable[CandidateSeed]) -> list[CandidateSeed]:
    best_by_url: dict[str, CandidateSeed] = {}
    for candidate in candidates:
        current = best_by_url.get(candidate.canonical_url)
        if current is None or _candidate_sort_key(candidate) > _candidate_sort_key(current):
            best_by_url[candidate.canonical_url] = candidate
    output = list(best_by_url.values())
    output.sort(
        key=lambda candidate: (
            -candidate.score,
            candidate.source_kind != "ats_feed",
            str(candidate.source_row.get("source_record_id") or ""),
            candidate.canonical_url,
        )
    )
    return output


def _source_provenance(candidate: CandidateSeed) -> dict[str, Any]:
    row = candidate.source_row
    keys = (
        "source_record_id",
        "source_index",
        "registrable_domain",
        "root_url",
        "source_relationship",
        "source_assessment",
        "source_sitemap_url",
        "source_feed_ats",
        "source_feed_token",
        "source_feed_url",
        "source_job_id",
        "source_job_title",
        "source_job_location",
        "discovered_at",
        "discovery_method",
    )
    provenance = {key: row[key] for key in keys if key in row}
    provenance["input_path"] = str(candidate.input_path)
    provenance["input_line_number"] = candidate.source_line_number
    return provenance


def _output_row(candidate: CandidateSeed, rank: int) -> dict[str, Any]:
    row = candidate.source_row
    relationship = _relationship(row, candidate.canonical_url)
    original_url = row.get("url") if isinstance(row.get("url"), str) else candidate.canonical_url
    source_assessment = row.get("source_assessment")
    if not isinstance(source_assessment, str) or not source_assessment:
        source_assessment = (
            "external_ats_needs_verification"
            if relationship == "known_ats"
            else "first_party_career_entry"
        )
    source_index = row.get("source_index")
    if not isinstance(source_index, int):
        source_index = candidate.source_line_number - 1
    return {
        "canonical_url": candidate.canonical_url,
        "original_url": original_url,
        "priority_rank": rank,
        "quality_tier": candidate.quality_tier,
        "registrable_domain": row.get("registrable_domain"),
        "score": candidate.score,
        "selector_version": SELECTOR_VERSION,
        "source_assessment": source_assessment,
        "source_kind": candidate.source_kind,
        "source_index": source_index,
        "source_provenance": _source_provenance(candidate),
        "source_record_id": row.get("source_record_id"),
        "source_relationship": relationship,
        "source_sitemap_url": row.get("source_sitemap_url"),
        "target_kind": candidate.target_kind,
        "url": candidate.canonical_url,
        "validation": {
            "excluded_forbidden_access_claim": False,
            "known_ats_url": _is_known_ats_url(candidate.canonical_url),
            "reason_codes": list(candidate.reason_codes),
            "selected_without_fetching": True,
        },
    }


def select_prioritized_seeds(
    *,
    sitemap_inputs: Sequence[Path],
    ats_feed_inputs: Sequence[Path],
    max_sitemap_per_company: int = DEFAULT_MAX_SITEMAP_SEEDS_PER_COMPANY,
) -> list[dict[str, Any]]:
    candidates: list[CandidateSeed] = []
    for path in sitemap_inputs:
        candidates.extend(_load_candidates(path, source_kind="sitemap"))
    for path in ats_feed_inputs:
        candidates.extend(_load_candidates(path, source_kind="ats_feed"))
    capped_candidates = _cap_sitemap_candidates_per_company(
        candidates, max_per_company=max_sitemap_per_company
    )
    selected = _select_best_by_url(capped_candidates)
    return [_output_row(candidate, rank) for rank, candidate in enumerate(selected, start=1)]


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
    sitemap_inputs: Sequence[Path],
    ats_feed_inputs: Sequence[Path],
    output_path: Path,
    max_sitemap_per_company: int = DEFAULT_MAX_SITEMAP_SEEDS_PER_COMPANY,
) -> dict[str, Any]:
    rows = select_prioritized_seeds(
        sitemap_inputs=sitemap_inputs,
        ats_feed_inputs=ats_feed_inputs,
        max_sitemap_per_company=max_sitemap_per_company,
    )
    _write_jsonl_atomic(output_path, rows)
    counts = Counter(str(row["source_kind"]) for row in rows)
    tiers = Counter(str(row["quality_tier"]) for row in rows)
    target_kinds = Counter(str(row["target_kind"]) for row in rows)
    return {
        "ats_feed_inputs": [str(path) for path in ats_feed_inputs],
        "output_path": str(output_path),
        "quality_tier_counts": dict(sorted(tiers.items())),
        "row_count": len(rows),
        "selector_version": SELECTOR_VERSION,
        "sitemap_inputs": [str(path) for path in sitemap_inputs],
        "max_sitemap_per_company": max_sitemap_per_company,
        "source_kind_counts": dict(sorted(counts.items())),
        "target_kind_counts": dict(sorted(target_kinds.items())),
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sitemap-input", type=Path, action="append", default=[])
    parser.add_argument("--ats-feed-input", type=Path, action="append", default=[])
    parser.add_argument(
        "--max-sitemap-per-company",
        type=int,
        default=DEFAULT_MAX_SITEMAP_SEEDS_PER_COMPANY,
        help=(
            "Maximum selected sitemap seeds per source company/domain; ATS feed rows are uncapped."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.sitemap_input and not args.ats_feed_input:
        print("at least one sitemap or ATS feed input is required", file=sys.stderr)
        return 2
    try:
        summary = run_selector(
            sitemap_inputs=args.sitemap_input,
            ats_feed_inputs=args.ats_feed_input,
            output_path=args.output,
            max_sitemap_per_company=args.max_sitemap_per_company,
        )
    except (OSError, ValueError) as error:
        print(f"prioritized seed selection failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
