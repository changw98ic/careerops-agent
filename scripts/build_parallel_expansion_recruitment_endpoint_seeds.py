#!/usr/bin/env python3
"""Build inferred first-party recruitment endpoint seeds for new company domains.

The script is metadata-only: it does not fetch pages. It expands each new
official domain into a small deterministic set of common recruitment entry URLs,
then removes URLs already present in raw receipts or priority seed queues.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import collect_recruitment_pages as collector

DEFAULT_INPUT = Path(
    "datasets/private/agent-company-seeds/"
    "agent_company_candidates_parallel_expansion_2026-07-18.jsonl"
)
DEFAULT_OUTPUT = Path(
    "datasets/private/agent-company-seeds/"
    "parallel_expansion_recruitment_endpoint_seeds_2026-07-18.jsonl"
)
DEFAULT_SUMMARY = Path(
    "datasets/private/agent-company-seeds/"
    "parallel_expansion_recruitment_endpoint_seeds_2026-07-18.summary.json"
)
DEFAULT_RECRUITMENT_RAW_ROOT = Path("datasets/raw/recruitment_pages/2026-07-18")
DEFAULT_PRIVATE_SEED_ROOT = Path("datasets/private/agent-company-seeds")

SELECTOR_VERSION = "parallel_expansion_recruitment_endpoint_seeds_v1"

ENDPOINT_TEMPLATES: tuple[tuple[int, str, str], ...] = (
    (10, "/careers", "common_careers_root"),
    (20, "/jobs", "common_jobs_root"),
    (30, "/careers/jobs", "careers_jobs_nested"),
    (40, "/company/careers", "company_careers_nested"),
    (50, "/company/jobs", "company_jobs_nested"),
    (60, "/join-us", "join_us_root"),
    (70, "/join-our-team", "join_our_team_root"),
    (80, "/work-with-us", "work_with_us_root"),
    (90, "/open-positions", "open_positions_root"),
    (100, "/positions", "positions_root"),
)

URL_FIELDS = (
    "canonical_url",
    "url",
    "requested_url",
    "final_url",
    "original_url",
    "collector_requested_url",
    "source_seed_url",
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_line(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


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
                raise ValueError(f"{path}:{line_number}: expected JSON object")
            rows.append(decoded)
    return rows


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
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
                raise ValueError(f"{path}:{line_number}: expected JSON object")
            yield decoded


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


def _write_json_atomic(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _canonicalize(value: str) -> str:
    return collector.canonicalize_url(value)


def _host_matches_domain(hostname: str, registrable_domain: str) -> bool:
    host = hostname.rstrip(".").lower()
    domain = registrable_domain.rstrip(".").lower()
    return bool(host and domain) and (host == domain or host.endswith(f".{domain}"))


def _official_base_url(row: dict[str, Any]) -> str:
    registrable_domain = str(row.get("registrable_domain") or "").strip().lower()
    if not registrable_domain:
        return ""
    representative = row.get("representative_website")
    if isinstance(representative, str):
        parsed = urlsplit(representative.strip())
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme in {"http", "https"} and _host_matches_domain(
            hostname,
            registrable_domain,
        ):
            scheme = "https"
            netloc = hostname
            if parsed.port and parsed.port not in {80, 443}:
                netloc = f"{netloc}:{parsed.port}"
            return urlunsplit((scheme, netloc, "/", "", ""))
    return f"https://{registrable_domain}/"


def _endpoint_url(base_url: str, path: str) -> str:
    parsed = urlsplit(base_url)
    return _canonicalize(urlunsplit((parsed.scheme, parsed.netloc, path, "", "")))


def _existing_url_files(
    *,
    private_seed_root: Path,
    recruitment_raw_root: Path,
    output_path: Path,
) -> list[Path]:
    files: list[Path] = []
    if private_seed_root.exists():
        files.extend(private_seed_root.rglob("*seed*.jsonl"))
        files.extend(private_seed_root.rglob("*priority*.jsonl"))
    if recruitment_raw_root.exists():
        files.extend(recruitment_raw_root.rglob("recruitment_page_receipts.jsonl"))
        files.extend(recruitment_raw_root.rglob("*seed*.jsonl"))
        files.extend(recruitment_raw_root.rglob("*priority*.jsonl"))
    output_resolved = output_path.resolve()
    unique: dict[str, Path] = {}
    for path in files:
        if path.resolve() == output_resolved:
            continue
        unique[str(path)] = path
    return [unique[key] for key in sorted(unique)]


def _extract_urls(row: dict[str, Any]) -> Iterable[str]:
    for field in URL_FIELDS:
        value = row.get(field)
        if isinstance(value, str):
            yield value
    provenance = row.get("source_provenance")
    if isinstance(provenance, dict):
        for field in URL_FIELDS:
            value = provenance.get(field)
            if isinstance(value, str):
                yield value


def load_existing_urls(files: Iterable[Path]) -> tuple[set[str], dict[str, int]]:
    existing: set[str] = set()
    by_file: dict[str, int] = {}
    for path in files:
        count_before = len(existing)
        for row in _iter_jsonl(path):
            for value in _extract_urls(row):
                canonical = _canonicalize(value)
                if canonical:
                    existing.add(canonical)
        by_file[str(path)] = len(existing) - count_before
    return existing, by_file


def _source_primary_category(row: dict[str, Any]) -> str | None:
    evidence = row.get("source_evidence")
    if not isinstance(evidence, list):
        return None
    for item in evidence:
        if isinstance(item, dict) and isinstance(item.get("source_primary_category"), str):
            return item["source_primary_category"]
    return None


def build_endpoint_rows(
    *,
    input_rows: Sequence[dict[str, Any]],
    existing_urls: set[str],
    input_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    generated: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = Counter()
    per_domain_counts: Counter[str] = Counter()
    endpoint_counts: Counter[str] = Counter()

    sorted_rows = sorted(
        enumerate(input_rows, start=1),
        key=lambda item: (
            str(item[1].get("registrable_domain") or ""),
            str(item[1].get("record_id") or ""),
            item[0],
        ),
    )
    for input_line_number, source in sorted_rows:
        registrable_domain = str(source.get("registrable_domain") or "").strip().lower()
        record_id = source.get("record_id")
        if not registrable_domain or not isinstance(record_id, str):
            skipped["missing_domain_or_record_id"] += len(ENDPOINT_TEMPLATES)
            continue
        base_url = _official_base_url(source)
        if not base_url:
            skipped["missing_official_base_url"] += len(ENDPOINT_TEMPLATES)
            continue
        for inference_priority, endpoint_path, endpoint_kind in ENDPOINT_TEMPLATES:
            canonical = _endpoint_url(base_url, endpoint_path)
            if not canonical:
                skipped["invalid_generated_url"] += 1
                continue
            if canonical in existing_urls:
                skipped["existing_raw_or_priority_url"] += 1
                continue
            if canonical in seen:
                skipped["duplicate_generated_url"] += 1
                continue
            seen.add(canonical)
            endpoint_counts[endpoint_kind] += 1
            per_domain_counts[registrable_domain] += 1
            generated.append(
                {
                    "canonical_url": canonical,
                    "inference": {
                        "endpoint_kind": endpoint_kind,
                        "endpoint_path": endpoint_path,
                        "inference_priority": inference_priority,
                        "method": "deterministic_common_official_recruitment_endpoint",
                        "selected_without_fetching": True,
                    },
                    "inference_priority": inference_priority,
                    "original_url": canonical,
                    "quality_tier": "inferred",
                    "registrable_domain": registrable_domain,
                    "score": max(1, 80 - inference_priority // 2),
                    "seed_source": "parallel_expansion_inferred_official_endpoint",
                    "selector_version": SELECTOR_VERSION,
                    "source_assessment": "first_party_career_entry",
                    "source_index": input_line_number,
                    "source_kind": "inferred_official_recruitment_endpoint",
                    "source_provenance": {
                        "candidate_entity_name": source.get("candidate_entity_name"),
                        "candidate_type": source.get("candidate_type"),
                        "input_line_number": input_line_number,
                        "input_path": str(input_path),
                        "representative_website": source.get("representative_website"),
                        "source_primary_category": _source_primary_category(source),
                    },
                    "source_record_id": record_id,
                    "source_relationship": "first_party",
                    "source_seed_url": canonical,
                    "target_kind": "inferred_first_party_recruitment_entry",
                    "url": canonical,
                    "validation": {
                        "candidate_domain_matches_url_host": True,
                        "excluded_existing_raw_or_priority_url": False,
                        "selected_without_fetching": True,
                    },
                }
            )

    generated.sort(
        key=lambda row: (
            str(row["registrable_domain"]),
            int(row["inference_priority"]),
            str(row["canonical_url"]),
        )
    )
    for rank, row in enumerate(generated, start=1):
        row["priority_rank"] = rank

    stats = {
        "endpoint_counts": dict(sorted(endpoint_counts.items())),
        "max_urls_per_domain": max(per_domain_counts.values(), default=0),
        "min_urls_per_domain": min(per_domain_counts.values(), default=0),
        "per_domain_counts": dict(sorted(per_domain_counts.items())),
        "skipped": dict(sorted(skipped.items())),
    }
    return generated, stats


def validate_collector_compatibility(rows: list[dict[str, Any]]) -> dict[str, Any]:
    requests = collector.select_additional_seed_urls(rows, seen=set())
    request_urls = [request.canonical_url for request in requests]
    row_urls = [str(row["canonical_url"]) for row in rows]
    return {
        "collector_request_count": len(requests),
        "collector_round_trip_matches_rows": request_urls == row_urls,
        "input_row_count": len(rows),
        "unique_collector_request_count": len(set(request_urls)),
    }


def run(
    *,
    input_path: Path,
    output_path: Path,
    summary_path: Path,
    private_seed_root: Path,
    recruitment_raw_root: Path,
) -> dict[str, Any]:
    input_rows = _load_jsonl(input_path)
    existing_files = _existing_url_files(
        private_seed_root=private_seed_root,
        recruitment_raw_root=recruitment_raw_root,
        output_path=output_path,
    )
    existing_urls, existing_by_file = load_existing_urls(existing_files)
    rows, build_stats = build_endpoint_rows(
        input_rows=input_rows,
        existing_urls=existing_urls,
        input_path=input_path,
    )
    compatibility = validate_collector_compatibility(rows)
    if not compatibility["collector_round_trip_matches_rows"]:
        raise ValueError("collector compatibility validation failed")

    _write_jsonl_atomic(output_path, rows)
    domains = {str(row["registrable_domain"]) for row in rows}
    summary = {
        "candidate_domain_count": len({str(row.get("registrable_domain")) for row in input_rows}),
        "collector_compatibility": compatibility,
        "created_at": _now(),
        "existing_url_count": len(existing_urls),
        "existing_url_files": existing_by_file,
        "input_file": str(input_path),
        "input_row_count": len(input_rows),
        "notes": [
            (
                "No network requests were made; all URLs are deterministic first-party "
                "endpoint guesses."
            ),
            (
                "Existing raw receipts and priority/additional seed files were canonicalized "
                "for exclusion."
            ),
        ],
        "output_file": str(output_path),
        "selector_version": SELECTOR_VERSION,
        "summary_file": str(summary_path),
        "unique_output_domain_count": len(domains),
        "unique_output_url_count": len({str(row["canonical_url"]) for row in rows}),
        "url_count": len(rows),
        **build_stats,
    }
    _write_json_atomic(summary_path, summary)
    return summary


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--private-seed-root", type=Path, default=DEFAULT_PRIVATE_SEED_ROOT)
    parser.add_argument("--recruitment-raw-root", type=Path, default=DEFAULT_RECRUITMENT_RAW_ROOT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = run(
            input_path=args.input,
            output_path=args.output,
            summary_path=args.summary,
            private_seed_root=args.private_seed_root,
            recruitment_raw_root=args.recruitment_raw_root,
        )
    except (OSError, ValueError) as error:
        print(f"endpoint seed build failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
