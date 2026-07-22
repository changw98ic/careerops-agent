#!/usr/bin/env python3
"""Aggregate recruitment page signals into company-level screening rollups."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OUTPUT_JSONL = "recruitment_company_rollup.jsonl"
SUMMARY_JSON = "recruitment_company_rollup_summary.json"
ROLLUP_VERSION = "company_rollup_v1"
CLASSIFICATION_PRECEDENCE = (
    "actual_job_posting",
    "job_listing",
    "career_portal",
    "recruitment_mention",
)
CLASSIFICATION_RANK = {
    classification: len(CLASSIFICATION_PRECEDENCE) - index
    for index, classification in enumerate(CLASSIFICATION_PRECEDENCE)
}


@dataclass
class CompanyRollup:
    company_key: str
    source_record_id: str | None
    registrable_domain: str
    source_assessment: str | None
    source_relationships: set[str] = field(default_factory=set)
    page_count: int = 0
    classification_counts: Counter[str] = field(default_factory=Counter)
    evidence_counts: Counter[str] = field(default_factory=Counter)
    signal_score_max: int | None = None
    best_pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw_provenance: list[dict[str, Any]] = field(default_factory=list)
    first_captured_at: str | None = None
    last_captured_at: str | None = None


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


def _best_status(classification_counts: Counter[str]) -> str:
    for classification in CLASSIFICATION_PRECEDENCE:
        if classification_counts[classification] > 0:
            return classification
    return "no_signal"


def _company_key(row: dict[str, Any]) -> tuple[str, str | None, str]:
    provenance = row.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    source_record_id = provenance.get("source_record_id")
    if not isinstance(source_record_id, str) or not source_record_id.strip():
        source_record_id = row.get("source_record_id")
    source_record_id = (
        source_record_id.strip()
        if isinstance(source_record_id, str) and source_record_id.strip()
        else None
    )

    registrable_domain = provenance.get("registrable_domain")
    if not isinstance(registrable_domain, str) or not registrable_domain.strip():
        registrable_domain = row.get("registrable_domain")
    domain = (
        registrable_domain.strip().lower()
        if isinstance(registrable_domain, str) and registrable_domain.strip()
        else ""
    )
    if source_record_id:
        return f"record:{source_record_id}", source_record_id, domain
    if domain:
        return f"domain:{domain}", None, domain
    signal_key = row.get("signal_key")
    if isinstance(signal_key, str) and signal_key.strip():
        return f"signal:{signal_key.strip()}", None, ""
    raise ValueError("signal row is missing source_record_id, registrable_domain, and signal_key")


def _page_url(row: dict[str, Any], provenance: dict[str, Any]) -> str:
    for key in ("final_url", "canonical_url", "requested_url"):
        value = provenance.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("final_url", "canonical_url", "requested_url"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _evidence_counts(row: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    evidence = row.get("evidence")
    if not isinstance(evidence, list):
        return counts
    for item in evidence:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        if isinstance(category, str) and category:
            counts[category] += 1
    return counts


def _raw_provenance(row: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "raw_file",
        "raw_line_number",
        "raw_byte_offset",
        "body_sha256",
        "body_bytes",
        "body_truncated",
        "captured_at",
        "canonical_url",
        "final_url",
        "requested_url",
        "depth",
        "discovered_from",
    )
    output = {key: provenance[key] for key in keys if key in provenance}
    signal_key = row.get("signal_key")
    if isinstance(signal_key, str):
        output["signal_key"] = signal_key
    return output


def _page_summary(row: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    evidence_counts = _evidence_counts(row)
    score = row.get("signal_score")
    return {
        "canonical_url": provenance.get("canonical_url"),
        "classification": row.get("classification"),
        "evidence_counts": dict(sorted(evidence_counts.items())),
        "final_url": provenance.get("final_url"),
        "raw_file": provenance.get("raw_file"),
        "raw_line_number": provenance.get("raw_line_number"),
        "requested_url": provenance.get("requested_url"),
        "signal_score": score if isinstance(score, int) else None,
    }


def _page_sort_key(page: dict[str, Any]) -> tuple[int, int, str]:
    classification = page.get("classification")
    score = page.get("signal_score")
    url = page.get("final_url") or page.get("canonical_url") or page.get("requested_url") or ""
    return (
        CLASSIFICATION_RANK.get(classification, 0) if isinstance(classification, str) else 0,
        score if isinstance(score, int) else -1,
        str(url),
    )


def aggregate_company_signals(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rollups: dict[str, CompanyRollup] = {}
    for row in rows:
        company_key, source_record_id, domain = _company_key(row)
        provenance = row.get("provenance")
        if not isinstance(provenance, dict):
            provenance = {}
        rollup = rollups.get(company_key)
        if rollup is None:
            rollup = CompanyRollup(
                company_key=company_key,
                source_record_id=source_record_id,
                registrable_domain=domain,
                source_assessment=(
                    provenance.get("source_assessment")
                    if isinstance(provenance.get("source_assessment"), str)
                    else None
                ),
            )
            rollups[company_key] = rollup
        elif not rollup.registrable_domain and domain:
            rollup.registrable_domain = domain

        relationship = provenance.get("source_relationship")
        if isinstance(relationship, str) and relationship:
            rollup.source_relationships.add(relationship)

        classification = row.get("classification")
        if isinstance(classification, str) and classification:
            rollup.classification_counts[classification] += 1
        rollup.evidence_counts.update(_evidence_counts(row))
        rollup.page_count += 1

        score = row.get("signal_score")
        if isinstance(score, int):
            rollup.signal_score_max = (
                score if rollup.signal_score_max is None else max(rollup.signal_score_max, score)
            )

        page = _page_summary(row, provenance)
        if isinstance(classification, str) and classification:
            current = rollup.best_pages.get(classification)
            if current is None or _page_sort_key(page) > _page_sort_key(current):
                rollup.best_pages[classification] = page

        raw_provenance = _raw_provenance(row, provenance)
        rollup.raw_provenance.append(raw_provenance)

        captured_at = provenance.get("captured_at")
        if isinstance(captured_at, str) and captured_at:
            if rollup.first_captured_at is None or captured_at < rollup.first_captured_at:
                rollup.first_captured_at = captured_at
            if rollup.last_captured_at is None or captured_at > rollup.last_captured_at:
                rollup.last_captured_at = captured_at

    output: list[dict[str, Any]] = []
    for rollup in rollups.values():
        status = _best_status(rollup.classification_counts)
        best_page_by_status = {
            classification: rollup.best_pages[classification]
            for classification in CLASSIFICATION_PRECEDENCE
            if classification in rollup.best_pages
        }
        best_urls = {
            classification: _page_url(page, page)
            for classification, page in best_page_by_status.items()
            if _page_url(page, page)
        }
        raw_provenance = sorted(
            rollup.raw_provenance,
            key=lambda item: (
                str(item.get("raw_file") or ""),
                int(item.get("raw_line_number") or 0),
                str(item.get("signal_key") or ""),
            ),
        )
        output.append(
            {
                "best_page_by_status": best_page_by_status,
                "best_urls": best_urls,
                "classification_counts": {
                    classification: rollup.classification_counts[classification]
                    for classification in CLASSIFICATION_PRECEDENCE
                },
                "company_key": rollup.company_key,
                "evidence_counts": dict(sorted(rollup.evidence_counts.items())),
                "first_captured_at": rollup.first_captured_at,
                "has_actual_job_posting": rollup.classification_counts["actual_job_posting"] > 0,
                "has_career_portal": rollup.classification_counts["career_portal"] > 0,
                "has_job_listing": rollup.classification_counts["job_listing"] > 0,
                "has_recruitment_mention": (
                    rollup.classification_counts["recruitment_mention"] > 0
                ),
                "last_captured_at": rollup.last_captured_at,
                "page_count": rollup.page_count,
                "raw_provenance": raw_provenance,
                "registrable_domain": rollup.registrable_domain,
                "rollup_version": ROLLUP_VERSION,
                "signal_score_max": rollup.signal_score_max,
                "source_assessment": rollup.source_assessment,
                "source_record_id": rollup.source_record_id,
                "source_relationships": sorted(rollup.source_relationships),
                "status": status,
            }
        )
    output.sort(
        key=lambda item: (
            -CLASSIFICATION_RANK.get(str(item["status"]), 0),
            str(item.get("registrable_domain") or ""),
            str(item["company_key"]),
        )
    )
    return output


def build_summary(rows: Sequence[dict[str, Any]], *, input_path: Path) -> dict[str, Any]:
    status_counts: Counter[str] = Counter(str(row["status"]) for row in rows)
    return {
        "company_count": len(rows),
        "input_path": str(input_path),
        "output_jsonl": OUTPUT_JSONL,
        "rollup_version": ROLLUP_VERSION,
        "status_counts": dict(sorted(status_counts.items())),
        "total_page_signals": sum(
            row["page_count"] for row in rows if isinstance(row.get("page_count"), int)
        ),
    }


def run_rollup(input_path: Path, output_dir: Path) -> dict[str, Any]:
    input_rows = _load_jsonl(input_path)
    rollup_rows = aggregate_company_signals(input_rows)
    _write_jsonl_atomic(output_dir / OUTPUT_JSONL, rollup_rows)
    summary = build_summary(rollup_rows, input_path=input_path)
    _write_json_atomic(output_dir / SUMMARY_JSON, summary)
    return summary


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Live page signals JSONL.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Rollup output directory.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = run_rollup(args.input, args.output_dir)
    except (OSError, ValueError) as error:
        print(f"company rollup failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
