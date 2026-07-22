#!/usr/bin/env python3
"""Build remaining first-party recruitment URL candidates for priority_sharded_v3.

No network is used. Inputs are run6 sitemap discoveries plus deterministic
parallel-expansion endpoint seeds. The output excludes URLs already present in
v1/v2 seeds, current parallel_expansion_priority_v1 seeds, and recruitment page
raw/receipt outputs.
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

import collect_recruitment_pages as collector
import prioritize_recruitment_seeds as prioritizer

DEFAULT_RUN6_SITEMAP = Path(
    "datasets/raw/recruitment_sitemaps/2026-07-18/run6/discovered_recruitment_sitemap_urls.jsonl"
)
DEFAULT_ENDPOINT_SEEDS = Path(
    "datasets/private/agent-company-seeds/"
    "parallel_expansion_recruitment_endpoint_seeds_2026-07-18.jsonl"
)
DEFAULT_OUTPUT = Path(
    "datasets/private/agent-company-seeds/priority_sharded_v3_candidates_2026-07-18.jsonl"
)
DEFAULT_SUMMARY = Path(
    "datasets/private/agent-company-seeds/priority_sharded_v3_candidates_2026-07-18.summary.json"
)
DEFAULT_MANIFEST = Path(
    "datasets/private/agent-company-seeds/priority_sharded_v3_candidates_2026-07-18.manifest.json"
)
DEFAULT_RECRUITMENT_RAW_ROOT = Path("datasets/raw/recruitment_pages/2026-07-18")

SELECTOR_VERSION = "priority_sharded_v3_candidates_v1"
DEFAULT_MAX_PER_COMPANY = 10
MIN_SCORE = 55

EXCLUDE_SEED_FILES = (
    Path(
        "datasets/raw/recruitment_pages/2026-07-18/"
        "priority_sharded_v1/seeds/priority_sharded_v1_pending_seeds.jsonl"
    ),
    Path(
        "datasets/raw/recruitment_pages/2026-07-18/"
        "priority_sharded_v1/seeds/priority_sharded_v1_pending_seeds.final.jsonl"
    ),
    Path("datasets/private/agent-company-seeds/priority_sharded_v2_candidates_2026-07-18.jsonl"),
    Path(
        "datasets/raw/recruitment_pages/2026-07-18/"
        "parallel_expansion_priority_v1/parallel_expansion_priority_v1_additional_seeds.jsonl"
    ),
)

URL_FIELDS = (
    "canonical_url",
    "url",
    "requested_url",
    "final_url",
    "original_url",
    "source_seed_url",
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
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
            yield line_number, decoded


def _canonicalize(value: str) -> str:
    return prioritizer.canonicalize_url(value)


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


def _load_url_exclusions(files: Iterable[Path]) -> tuple[set[str], dict[str, int], Counter[str]]:
    urls: set[str] = set()
    by_file: dict[str, int] = {}
    counts: Counter[str] = Counter()
    for path in files:
        before = len(urls)
        for _line_number, row in _iter_jsonl(path):
            counts["rows"] += 1
            for value in _extract_urls(row):
                canonical = _canonicalize(value)
                if canonical:
                    counts["url_values"] += 1
                    urls.add(canonical)
        by_file[str(path)] = len(urls) - before
    counts["unique_urls"] = len(urls)
    return urls, by_file, counts


def _raw_inventory_files(raw_root: Path) -> list[Path]:
    if not raw_root.exists():
        return []
    files = list(raw_root.rglob("recruitment_page_receipts.jsonl"))
    # Include small/owned raw snapshots too; receipts remain the primary raw-output inventory.
    files.extend(
        path
        for path in raw_root.rglob("*.jsonl")
        if "raw_snapshot" in path.name or path.name.endswith("_raw_snapshot.jsonl")
    )
    return sorted({str(path): path for path in files}.values())


def _relationship(row: dict[str, Any], canonical_url: str) -> str:
    relationship = row.get("source_relationship")
    if isinstance(relationship, str) and relationship:
        return relationship
    return collector.link_relationship(canonical_url, str(row.get("registrable_domain") or ""))


def _base_output_row(
    *,
    canonical_url: str,
    input_line_number: int,
    input_path: Path,
    quality_tier: str,
    reason_codes: list[str],
    score: int,
    source_kind: str,
    source_row: dict[str, Any],
    target_kind: str,
) -> dict[str, Any]:
    relationship = _relationship(source_row, canonical_url)
    source_assessment = source_row.get("source_assessment")
    if not isinstance(source_assessment, str) or not source_assessment:
        source_assessment = "first_party_career_entry"
    source_index = source_row.get("source_index")
    if not isinstance(source_index, int):
        source_index = input_line_number - 1
    original_url = (
        source_row.get("url") if isinstance(source_row.get("url"), str) else canonical_url
    )
    return {
        "canonical_url": canonical_url,
        "original_url": original_url,
        "pending_reason": "not_seen_in_v1_v2_raw_or_parallel_expansion_priority_v1",
        "quality_tier": quality_tier,
        "registrable_domain": source_row.get("registrable_domain"),
        "score": score,
        "selector_version": SELECTOR_VERSION,
        "source_assessment": source_assessment,
        "source_index": source_index,
        "source_kind": source_kind,
        "source_provenance": {
            "input_line_number": input_line_number,
            "input_path": str(input_path),
            "source_record_id": source_row.get("source_record_id"),
            "source_sitemap_url": source_row.get("source_sitemap_url"),
        },
        "source_record_id": source_row.get("source_record_id"),
        "source_relationship": relationship,
        "source_sitemap_url": source_row.get("source_sitemap_url"),
        "target_kind": target_kind,
        "url": canonical_url,
        "validation": {
            "excluded_generic_docs_blog_api": False,
            "known_ats_url": prioritizer._is_known_ats_url(canonical_url),
            "reason_codes": sorted(set(reason_codes)),
            "selected_without_fetching": True,
        },
    }


def _score_run6_sitemap(
    path: Path,
    exclusions: set[str],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for line_number, row in _iter_jsonl(path):
        counts["input_rows"] += 1
        raw_url = row.get("canonical_url") or row.get("url")
        if not isinstance(raw_url, str):
            counts["missing_url"] += 1
            continue
        canonical = _canonicalize(raw_url)
        if not canonical:
            counts["invalid_url"] += 1
            continue
        excluded_reason = prioritizer._excluded_reason(canonical, "sitemap")
        if excluded_reason is not None:
            counts[excluded_reason] += 1
            continue
        relationship = _relationship(row, canonical)
        if relationship != "first_party":
            counts["excluded_non_first_party"] += 1
            continue
        score, target_kind, reason_codes = prioritizer._score_sitemap_row(row, canonical)
        if score < MIN_SCORE:
            reason = reason_codes[0] if reason_codes else "excluded_low_score"
            counts[reason] += 1
            continue
        if canonical in exclusions:
            counts["excluded_existing"] += 1
            continue
        rows.append(
            _base_output_row(
                canonical_url=canonical,
                input_line_number=line_number,
                input_path=path,
                quality_tier=prioritizer._quality_tier(score),
                reason_codes=reason_codes,
                score=score,
                source_kind="run6_sitemap",
                source_row=row,
                target_kind=target_kind,
            )
        )
    counts["candidate_rows_before_dedupe"] = len(rows)
    return rows, counts


def _endpoint_rows(path: Path, exclusions: set[str]) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for line_number, row in _iter_jsonl(path):
        counts["input_rows"] += 1
        raw_url = row.get("canonical_url") or row.get("url")
        if not isinstance(raw_url, str):
            counts["missing_url"] += 1
            continue
        canonical = _canonicalize(raw_url)
        if not canonical:
            counts["invalid_url"] += 1
            continue
        excluded_reason = prioritizer._excluded_reason(canonical, "sitemap")
        if excluded_reason is not None:
            counts[excluded_reason] += 1
            continue
        if _relationship(row, canonical) != "first_party":
            counts["excluded_non_first_party"] += 1
            continue
        score = row.get("score")
        score = score if isinstance(score, int) else 55
        if score < MIN_SCORE:
            counts["excluded_low_score"] += 1
            continue
        if canonical in exclusions:
            counts["excluded_existing"] += 1
            continue
        inference = row.get("inference")
        reason_codes = ["inferred_official_endpoint"]
        if isinstance(inference, dict) and isinstance(inference.get("endpoint_kind"), str):
            reason_codes.append(inference["endpoint_kind"])
        rows.append(
            _base_output_row(
                canonical_url=canonical,
                input_line_number=line_number,
                input_path=path,
                quality_tier="medium" if score < 95 else "high",
                reason_codes=reason_codes,
                score=score,
                source_kind="parallel_expansion_endpoint_seed",
                source_row=row,
                target_kind=str(row.get("target_kind") or "inferred_first_party_recruitment_entry"),
            )
        )
    counts["candidate_rows_before_dedupe"] = len(rows)
    return rows, counts


def _company_key(row: dict[str, Any]) -> str:
    value = row.get("source_record_id")
    if isinstance(value, str) and value:
        return f"record:{value}"
    domain = row.get("registrable_domain")
    if isinstance(domain, str) and domain:
        return f"domain:{domain.lower()}"
    return f"url:{row['canonical_url']}"


def _sort_key(row: dict[str, Any]) -> tuple[int, int, str, str]:
    source_priority = 1 if row["source_kind"] == "run6_sitemap" else 0
    return (
        int(row["score"]),
        source_priority,
        str(row.get("registrable_domain") or ""),
        str(row["canonical_url"]),
    )


def _select(
    rows: Iterable[dict[str, Any]],
    max_per_company: int,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    best_by_url: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    for row in rows:
        canonical = str(row["canonical_url"])
        current = best_by_url.get(canonical)
        if current is None or _sort_key(row) > _sort_key(current):
            if current is not None:
                counts["duplicate_url_replaced"] += 1
            best_by_url[canonical] = row
        else:
            counts["duplicate_url_dropped"] += 1

    by_company: dict[str, list[dict[str, Any]]] = {}
    for row in best_by_url.values():
        by_company.setdefault(_company_key(row), []).append(row)

    selected: list[dict[str, Any]] = []
    for company_rows in by_company.values():
        company_rows.sort(key=_sort_key, reverse=True)
        selected.extend(company_rows[:max_per_company])
        counts["company_cap_excluded"] += max(0, len(company_rows) - max_per_company)
    selected.sort(
        key=lambda row: (
            -int(row["score"]),
            str(row["registrable_domain"]),
            str(row["canonical_url"]),
        )
    )
    for rank, row in enumerate(selected, start=1):
        row["priority_rank"] = rank
        row["priority_sharded_v3_rank"] = rank
        row["priority_sharded_v3_selected_at"] = _now()
    return selected, counts


def _distribution(rows: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(key)) for row in rows).items()))


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
    run6_sitemap: Path,
    endpoint_seeds: Path,
    output_path: Path,
    summary_path: Path,
    manifest_path: Path,
    recruitment_raw_root: Path,
    max_per_company: int,
) -> dict[str, Any]:
    if output_path.exists() or summary_path.exists() or manifest_path.exists():
        raise ValueError("v3 output, summary, or manifest already exists; refusing to overwrite")

    raw_files = _raw_inventory_files(recruitment_raw_root)
    raw_urls, raw_by_file, raw_counts = _load_url_exclusions(raw_files)
    seed_urls, seed_by_file, seed_counts = _load_url_exclusions(EXCLUDE_SEED_FILES)
    exclusions = raw_urls | seed_urls

    sitemap_rows, sitemap_counts = _score_run6_sitemap(run6_sitemap, exclusions)
    endpoint_rows, endpoint_counts = _endpoint_rows(endpoint_seeds, exclusions)
    selected, selection_counts = _select([*sitemap_rows, *endpoint_rows], max_per_company)
    compatibility = validate_collector_compatibility(selected)
    if not compatibility["collector_round_trip_matches_rows"]:
        raise ValueError("collector compatibility validation failed")

    _write_jsonl_atomic(output_path, selected)
    summary = {
        "body_crawl_started": False,
        "collector_compatibility": compatibility,
        "created_at": _now(),
        "distributions": {
            "quality_tier": _distribution(selected, "quality_tier"),
            "score": _distribution(selected, "score"),
            "source_kind": _distribution(selected, "source_kind"),
            "target_kind": _distribution(selected, "target_kind"),
        },
        "endpoint_input": str(endpoint_seeds),
        "endpoint_source_counts": dict(sorted(endpoint_counts.items())),
        "exclude_url_count": len(exclusions),
        "existing_exclusion_counts": {
            "raw_output_or_receipt_urls": len(raw_urls),
            "v1_v2_and_parallel_expansion_priority_v1_seed_urls": len(seed_urls),
        },
        "input_files": [str(run6_sitemap), str(endpoint_seeds)],
        "max_per_company": max_per_company,
        "network_used": False,
        "output_count": len(selected),
        "output_path": str(output_path),
        "priority_batch": "priority_sharded_v3_candidates_2026-07-18",
        "raw_exclusion": {
            "canonical_url_count": len(raw_urls),
            "file_count": len(raw_files),
            "files": raw_by_file,
            "scan_counts": dict(sorted(raw_counts.items())),
        },
        "run6_sitemap_input": str(run6_sitemap),
        "run6_source_counts": dict(sorted(sitemap_counts.items())),
        "seed_exclusion": {
            "canonical_url_count": len(seed_urls),
            "file_count": len(EXCLUDE_SEED_FILES),
            "files": seed_by_file,
            "scan_counts": dict(sorted(seed_counts.items())),
        },
        "selection_counts": dict(sorted(selection_counts.items())),
        "selector_version": SELECTOR_VERSION,
        "summary_path": str(summary_path),
    }
    manifest = {
        "artifact": str(output_path),
        "collector_compatible_fields": [
            "canonical_url",
            "url",
            "source_assessment",
            "source_relationship",
            "registrable_domain",
            "source_index",
        ],
        "crawl_started": False,
        "created_at": summary["created_at"],
        "dedupe_against": {
            "raw_or_receipt_canonical_urls": len(raw_urls),
            "seed_canonical_urls": len(seed_urls),
        },
        "format": "jsonl",
        "network_used": False,
        "record_count": len(selected),
        "summary": str(summary_path),
    }
    _write_json_atomic(summary_path, summary)
    _write_json_atomic(manifest_path, manifest)
    return summary


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run6-sitemap", type=Path, default=DEFAULT_RUN6_SITEMAP)
    parser.add_argument("--endpoint-seeds", type=Path, default=DEFAULT_ENDPOINT_SEEDS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--recruitment-raw-root", type=Path, default=DEFAULT_RECRUITMENT_RAW_ROOT)
    parser.add_argument("--max-per-company", type=int, default=DEFAULT_MAX_PER_COMPANY)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = run(
            run6_sitemap=args.run6_sitemap,
            endpoint_seeds=args.endpoint_seeds,
            output_path=args.output,
            summary_path=args.summary,
            manifest_path=args.manifest,
            recruitment_raw_root=args.recruitment_raw_root,
            max_per_company=args.max_per_company,
        )
    except (OSError, ValueError) as error:
        print(f"priority sharded v3 candidate build failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
