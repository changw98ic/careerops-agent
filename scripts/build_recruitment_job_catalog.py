#!/usr/bin/env python3
"""Build a compact safe JSON catalog from merged recruitment page signals."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_INPUT = Path("global_raw_52_v1/merged/recruitment_page_signals.jsonl")
DEFAULT_OUTPUT = Path("global_raw_52_v1/merged/recruitment_job_catalog.json")
CATALOG_SCHEMA_VERSION = "recruitment_job_catalog_v1"
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
FORBIDDEN_OUTPUT_KEYS = {
    "body",
    "body_sha256",
    "body_text",
    "html",
    "input_path",
    "output_path",
    "raw_byte_offset",
    "raw_file",
    "raw_html",
    "raw_line",
    "raw_line_number",
    "response_body",
    "response_text",
    "signal_key",
    "text",
}
CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bsk(?:-proj)?-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b(?:pk|sk)_live_[A-Za-z0-9]{16,}\b"),
)


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
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


def _write_json_atomic(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _provenance(row: dict[str, Any]) -> dict[str, Any]:
    provenance = row.get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def _text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _safe_url(*values: Any) -> str | None:
    value = _text(*values)
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    hostname = parsed.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = f"{hostname}:{port}" if port is not None else hostname
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def _safe_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _safe_title(*values: Any) -> str:
    value = _text(*values)
    if value is None:
        return ""
    title = re.sub(r"\s+", " ", value).strip()
    title = title.replace("<", "").replace(">", "")
    if len(title) > 120:
        title = title[:117].rstrip() + "..."
    return title


def _evidence_summary(row: dict[str, Any]) -> dict[str, Any]:
    category_counts: Counter[str] = Counter()
    rules: set[str] = set()
    evidence = row.get("evidence")
    if not isinstance(evidence, list):
        return {"category_counts": {}, "rules": []}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        if isinstance(category, str) and category.strip():
            category_counts[category.strip()] += 1
        rule = item.get("rule")
        if isinstance(rule, str) and rule.strip():
            rules.add(rule.strip())
    return {
        "category_counts": dict(sorted(category_counts.items())),
        "rules": sorted(rules),
    }


def _company_key(row: dict[str, Any], provenance: dict[str, Any]) -> str | None:
    source_record_id = _text(provenance.get("source_record_id"), row.get("source_record_id"))
    registrable_domain = _text(provenance.get("registrable_domain"), row.get("registrable_domain"))
    if source_record_id is not None:
        return f"record:{source_record_id}"
    if registrable_domain is not None:
        return f"domain:{registrable_domain.lower()}"
    return None


def _catalog_id(row: dict[str, Any], provenance: dict[str, Any], public_url: str) -> str:
    source = _company_key(row, provenance) or "unknown-source"
    return f"{source}:{public_url}".lower()


def _catalog_item(row: dict[str, Any]) -> dict[str, Any] | None:
    provenance = _provenance(row)
    public_url = _safe_url(
        provenance.get("canonical_url"),
        provenance.get("final_url"),
        provenance.get("requested_url"),
        row.get("canonical_url"),
        row.get("final_url"),
        row.get("requested_url"),
    )
    if public_url is None:
        return None

    classification = _text(row.get("classification")) or "unknown"
    item: dict[str, Any] = {
        "body_truncated": _safe_bool(provenance.get("body_truncated")),
        "captured_at": _text(provenance.get("captured_at")),
        "classification": classification,
        "company_key": _company_key(row, provenance),
        "depth": _safe_int(provenance.get("depth")),
        "evidence": _evidence_summary(row),
        "http_status": _safe_int(provenance.get("http_status")),
        "id": _catalog_id(row, provenance, public_url),
        "public_url": public_url,
        "registrable_domain": _text(
            provenance.get("registrable_domain"),
            row.get("registrable_domain"),
        ),
        "score": _safe_int(row.get("signal_score")),
        "source_record_id": _text(
            provenance.get("source_record_id"),
            row.get("source_record_id"),
        ),
        "source_assessment": _text(provenance.get("source_assessment")),
        "source_relationship": _text(provenance.get("source_relationship")),
        "title": _safe_title(
            row.get("title"),
            row.get("page_title"),
            provenance.get("title"),
            provenance.get("page_title"),
        ),
    }
    return {key: value for key, value in item.items() if value is not None}


def _sort_key(item: dict[str, Any]) -> tuple[int, int, str, str]:
    classification = item.get("classification")
    score = item.get("score")
    return (
        -CLASSIFICATION_RANK.get(classification, 0) if isinstance(classification, str) else 0,
        -(score if isinstance(score, int) else -1),
        str(item.get("registrable_domain") or ""),
        str(item.get("public_url") or ""),
    )


def _validation_errors(value: Any, path: str = "$") -> list[str]:
    if isinstance(value, dict):
        errors: list[str] = []
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in FORBIDDEN_OUTPUT_KEYS:
                errors.append(f"forbidden output key {key!r} at {child_path}")
            errors.extend(_validation_errors(child, child_path))
        return errors
    if isinstance(value, list):
        return [
            error
            for index, child in enumerate(value)
            for error in _validation_errors(child, f"{path}[{index}]")
        ]
    if isinstance(value, str):
        return [
            f"credential-like pattern at {path}"
            for pattern in CREDENTIAL_PATTERNS
            if pattern.search(value) is not None
        ]
    return []


def _require_safe_output(value: dict[str, Any]) -> None:
    errors = _validation_errors(value)
    if errors:
        preview = "; ".join(errors[:5])
        suffix = "" if len(errors) <= 5 else f"; plus {len(errors) - 5} more"
        raise ValueError(f"catalog safety validation failed: {preview}{suffix}")


def build_catalog(rows: Iterable[dict[str, Any]], *, input_path: Path) -> dict[str, Any]:
    input_rows = 0
    skipped_unsafe = 0
    skipped_without_url = 0
    classification_counts: Counter[str] = Counter()
    jobs: list[dict[str, Any]] = []

    for row in rows:
        input_rows += 1
        classification = row.get("classification")
        if isinstance(classification, str) and classification:
            classification_counts[classification] += 1
        item = _catalog_item(row)
        if item is None:
            skipped_without_url += 1
            continue
        if _validation_errors(item):
            skipped_unsafe += 1
            continue
        jobs.append(item)

    jobs.sort(key=_sort_key)
    catalog = {
        "jobs": jobs,
        "metadata": {
            "schema": {
                "fields": {
                    "body_truncated": (
                        "whether the captured response body was truncated when available"
                    ),
                    "captured_at": "ISO-8601 capture timestamp when available",
                    "classification": "signal classification",
                    "company_key": "deterministic company/source key when available",
                    "depth": "crawl depth when available",
                    "evidence": "counts and rule names only; no excerpts or page text",
                    "http_status": "HTTP status from capture provenance when available",
                    "id": "deterministic source/url identifier",
                    "public_url": "http(s) URL without query string or fragment",
                    "registrable_domain": "company domain when available",
                    "score": "integer signal score when available",
                    "source_assessment": "source assessment label when available",
                    "source_record_id": "source dataset record id when available",
                    "source_relationship": "relationship between source record and page",
                    "title": "safe short title when available, otherwise empty string",
                },
                "version": CATALOG_SCHEMA_VERSION,
            },
            "source_label": "merged_recruitment_page_signals",
        },
        "stats": {
            "classification_counts": dict(sorted(classification_counts.items())),
            "exported_jobs": len(jobs),
            "input_rows": input_rows,
            "skipped_unsafe": skipped_unsafe,
            "skipped_without_url": skipped_without_url,
        },
    }
    _require_safe_output(catalog)
    return catalog


def run_export(input_path: Path, output_path: Path) -> dict[str, Any]:
    catalog = build_catalog(_iter_jsonl(input_path), input_path=input_path)
    _write_json_atomic(output_path, catalog)
    return {
        "exported_jobs": catalog["stats"]["exported_jobs"],
        "output_file": output_path.name,
        "schema_version": CATALOG_SCHEMA_VERSION,
        "source_label": "merged_recruitment_page_signals",
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = run_export(args.input, args.output)
    except (OSError, ValueError) as error:
        print(f"job catalog export failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
