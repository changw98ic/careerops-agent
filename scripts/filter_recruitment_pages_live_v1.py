#!/usr/bin/env python3
"""Streaming live-filter for captured recruitment pages.

This writes compact, provenance-preserving derived indexes without modifying raw
captures. The output JSONL is append-safe: existing signal keys are loaded first,
and reruns append only newly discovered positive records.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

DEFAULT_RAW_PATH = Path(
    "datasets/raw/recruitment_pages/2026-07-18/depth3_v2/recruitment_pages_raw.jsonl"
)
DEFAULT_OUTPUT_DIR = Path("datasets/derived/recruitment_pages/2026-07-18/live_filter_v1")

SIGNALS_FILENAME = "recruitment_page_signals.jsonl"
SUMMARY_FILENAME = "summary.json"
REPRESENTATIVE_FILENAME = "representative_signals.json"
MANIFEST_FILENAME = "manifest.json"

MAX_EXCERPTS = 6
MAX_EXCERPT_CHARS = 280
MAX_LINKS = 12
REPRESENTATIVE_PER_CLASS = 8

SKIPPED_TAGS = {"script", "style", "noscript", "template", "svg", "canvas"}
ATS_HOST_PATTERNS = (
    "ashbyhq.com",
    "bamboohr.com",
    "boards.greenhouse.io",
    "breezy.hr",
    "comeet.com",
    "greenhouse.io",
    "icims.com",
    "jobvite.com",
    "jobs.ashbyhq.com",
    "lever.co",
    "myworkdayjobs.com",
    "personio.de",
    "recruitee.com",
    "smartrecruiters.com",
    "teamtailor.com",
    "workable.com",
    "workdayjobs.com",
)

JOB_SCHEMA_PATTERN = re.compile(r'"@type"\s*:\s*"JobPosting"', re.IGNORECASE)
CAREER_ROUTE_PATTERN = re.compile(
    r"(^|/)(careers?|jobs?|job|positions?|openings?|join-us|join_?us|work-with-us)(/|$)",
    re.IGNORECASE,
)
JOB_URL_PATTERN = re.compile(
    r"/(job|jobs|careers|positions|openings)/[^/?#]{3,}",
    re.IGNORECASE,
)

FALSE_POSITIVE_PATTERN = re.compile(
    r"\b("
    r"cron\s+job|job\s+queue|queue\s+job|background\s+job|scheduled\s+job|"
    r"job\s+runner|job\s+scheduler|job\s+id|jobs?\s+to\s+be\s+done|"
    r"recruiting\s+agency|recruitment\s+agency|staffing\s+agency|staffing\s+firm"
    r")\b",
    re.IGNORECASE,
)

JOB_POSTING_RULES = (
    ("job_description", re.compile(r"\bjob\s+description\b", re.IGNORECASE)),
    (
        "responsibilities",
        re.compile(r"\b(responsibilities|what\s+you(?:'|\u2019)ll\s+do)\b", re.IGNORECASE),
    ),
    (
        "qualifications",
        re.compile(
            r"\b(qualifications|requirements|what\s+you(?:'|\u2019)ll\s+need)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "apply_for_job",
        re.compile(r"\bapply\s+(for\s+)?(this\s+)?(job|role|position)\b", re.IGNORECASE),
    ),
    (
        "employment_type",
        re.compile(r"\b(full[-\s]?time|part[-\s]?time|contract|internship)\b", re.IGNORECASE),
    ),
    (
        "department_location",
        re.compile(r"\b(department|location|work\s+location)\b", re.IGNORECASE),
    ),
)
LISTING_RULES = (
    (
        "open_roles",
        re.compile(
            r"\b(open|current|available)\s+(roles?|positions?|jobs?|openings?)\b",
            re.IGNORECASE,
        ),
    ),
    ("job_openings", re.compile(r"\b(job|career)\s+openings\b", re.IGNORECASE)),
    (
        "search_jobs",
        re.compile(
            r"\b(search|browse|view)\s+(all\s+)?(open\s+)?(jobs?|roles?|positions?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "we_are_hiring",
        re.compile(r"\b(we(?:'|\u2019)re|we\s+are)\s+hiring\b|\bhiring\s+now\b", re.IGNORECASE),
    ),
)
PORTAL_RULES = (
    (
        "career_label",
        re.compile(
            r"^\s*(careers?|jobs?|join\s+us|join\s+our\s+team|work\s+with\s+us)\s*$",
            re.IGNORECASE,
        ),
    ),
    ("career_route", CAREER_ROUTE_PATTERN),
)
MENTION_RULES = (
    ("career_word", re.compile(r"\bcareers?\b", re.IGNORECASE)),
    ("hiring_word", re.compile(r"\bhiring\b", re.IGNORECASE)),
    ("recruitment_word", re.compile(r"\brecruit(?:ing|ment)\b", re.IGNORECASE)),
    ("talent_acquisition", re.compile(r"\btalent\s+acquisition\b", re.IGNORECASE)),
    ("join_team", re.compile(r"\bjoin\s+our\s+team\b", re.IGNORECASE)),
)


@dataclass(frozen=True)
class Anchor:
    text: str
    href: str


@dataclass(frozen=True)
class PageText:
    visible_text: str
    anchors: tuple[Anchor, ...]


@dataclass(frozen=True)
class Evidence:
    category: str
    rule: str
    matched_term: str
    location: str
    excerpt: str
    href: str | None = None


class VisibleTextParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._skip_stack: list[str] = []
        self._text_chunks: list[str] = []
        self._anchor_stack: list[dict[str, Any]] = []
        self.anchors: list[Anchor] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in SKIPPED_TAGS:
            self._skip_stack.append(lowered)
            return
        if self._skip_stack or lowered != "a":
            return
        attrs_by_name = {name.lower(): value for name, value in attrs if value is not None}
        self._anchor_stack.append({"href": attrs_by_name.get("href", "").strip(), "chunks": []})

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if self._skip_stack:
            if lowered == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if lowered == "a" and self._anchor_stack:
            anchor = self._anchor_stack.pop()
            text = normalize_space(" ".join(anchor["chunks"]))
            href = normalize_href(str(anchor["href"]).strip(), self._base_url)
            if text or href:
                self.anchors.append(Anchor(text=text, href=href))

    def handle_data(self, data: str) -> None:
        if self._skip_stack:
            return
        text = normalize_space(data)
        if not text:
            return
        self._text_chunks.append(text)
        if self._anchor_stack:
            self._anchor_stack[-1]["chunks"].append(text)

    def page_text(self) -> PageText:
        return PageText(normalize_space(" ".join(self._text_chunks)), tuple(self.anchors))


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_href(href: str, base_url: str) -> str:
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return href
    return urljoin(base_url, href)


def extract_page_text(response_text: str, base_url: str = "", content_type: str = "") -> PageText:
    lowered_type = content_type.lower()
    stripped = response_text.lstrip()
    if "json" in lowered_type or stripped[:1] in {"{", "["}:
        flattened = " ".join(_flatten_json_strings(response_text))
        return PageText(normalize_space(flattened), ())

    parser = VisibleTextParser(base_url)
    parser.feed(response_text)
    parser.close()
    return parser.page_text()


def _flatten_json_strings(response_text: str) -> Iterable[str]:
    try:
        decoded = json.loads(response_text)
    except json.JSONDecodeError:
        yield response_text
        return
    yield from _walk_json_strings(decoded)


def _walk_json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        if len(value) <= 5000:
            yield value
        return
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_json_strings(child)
        return
    if isinstance(value, list):
        for child in value:
            yield from _walk_json_strings(child)


def classify_record(row: dict[str, Any]) -> tuple[str, list[Evidence], int, list[dict[str, str]]]:
    response_text = str(row.get("response_text") or "")
    final_url = str(row.get("final_url") or row.get("requested_url") or "")
    content_type = str(row.get("content_type") or "")
    page = extract_page_text(response_text, final_url, content_type)
    scan_text = page.visible_text
    url_context = final_url
    evidence: list[Evidence] = []

    if JOB_SCHEMA_PATTERN.search(response_text):
        evidence.append(
            Evidence(
                category="actual_job_posting",
                rule="schema_org_jobposting",
                matched_term="JobPosting",
                location="response_text",
                excerpt="schema.org JobPosting structured data present",
            )
        )

    if not FALSE_POSITIVE_PATTERN.search(scan_text):
        _append_matches(
            evidence,
            "actual_job_posting",
            JOB_POSTING_RULES,
            scan_text,
            "visible_text",
        )
        _append_matches(evidence, "job_listing", LISTING_RULES, scan_text, "visible_text")
        _append_matches(evidence, "recruitment_mention", MENTION_RULES, scan_text, "visible_text")

    for anchor in page.anchors:
        link_context = normalize_space(f"{anchor.text} {anchor.href}")
        if FALSE_POSITIVE_PATTERN.search(link_context):
            continue
        _append_matches(evidence, "job_listing", LISTING_RULES, link_context, "anchor", anchor.href)
        _append_matches(
            evidence,
            "career_portal",
            PORTAL_RULES,
            link_context,
            "anchor",
            anchor.href,
        )
        _append_matches(
            evidence,
            "recruitment_mention",
            MENTION_RULES,
            anchor.text,
            "anchor_text",
            anchor.href,
        )
        if href_has_ats_host(anchor.href):
            evidence.append(
                Evidence(
                    category="career_portal",
                    rule="known_ats_host",
                    matched_term=urlsplit(anchor.href).hostname or anchor.href,
                    location="href",
                    excerpt=anchor.text or anchor.href,
                    href=anchor.href,
                )
            )

    if JOB_URL_PATTERN.search(url_context) and _job_detail_signal_count(evidence) >= 2:
        evidence.append(
            Evidence(
                category="actual_job_posting",
                rule="job_url_with_detail_signals",
                matched_term=urlsplit(url_context).path,
                location="final_url",
                excerpt=url_context,
            )
        )

    evidence = dedupe_evidence(evidence)
    categories = {item.category for item in evidence}
    if "actual_job_posting" in categories:
        classification = "actual_job_posting"
    elif "job_listing" in categories:
        classification = "job_listing"
    elif "career_portal" in categories:
        classification = "career_portal"
    elif "recruitment_mention" in categories:
        classification = "recruitment_mention"
    else:
        classification = "no_signal"

    score = score_classification(classification, evidence)
    registrable_domain = str(row.get("registrable_domain") or "")
    links = evidence_links(evidence, page.anchors, registrable_domain)
    return classification, evidence, score, links


def _append_matches(
    evidence: list[Evidence],
    category: str,
    rules: tuple[tuple[str, re.Pattern[str]], ...],
    text: str,
    location: str,
    href: str | None = None,
) -> None:
    for rule_name, pattern in rules:
        for found in pattern.finditer(text):
            evidence.append(
                Evidence(
                    category=category,
                    rule=rule_name,
                    matched_term=found.group(0),
                    location=location,
                    excerpt=bounded_excerpt(text, found.start(), found.end()),
                    href=href,
                )
            )


def bounded_excerpt(text: str, start: int, end: int, limit: int = MAX_EXCERPT_CHARS) -> str:
    window = max(0, (limit - (end - start)) // 2)
    excerpt_start = max(0, start - window)
    excerpt_end = min(len(text), end + window)
    prefix = "..." if excerpt_start > 0 else ""
    suffix = "..." if excerpt_end < len(text) else ""
    return f"{prefix}{text[excerpt_start:excerpt_end].strip()}{suffix}"


def _job_detail_signal_count(evidence: list[Evidence]) -> int:
    return len({item.rule for item in evidence if item.category == "actual_job_posting"})


def dedupe_evidence(evidence: list[Evidence]) -> list[Evidence]:
    seen: set[tuple[str, str, str, str | None]] = set()
    deduped: list[Evidence] = []
    for item in evidence:
        key = (item.category, item.rule, item.matched_term.lower(), item.href)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= MAX_EXCERPTS:
            break
    return deduped


def score_classification(classification: str, evidence: list[Evidence]) -> int:
    base = {
        "actual_job_posting": 100,
        "job_listing": 85,
        "career_portal": 70,
        "recruitment_mention": 35,
        "no_signal": 0,
    }[classification]
    return min(100, base + max(0, len(evidence) - 1) * 3)


def href_has_ats_host(href: str) -> bool:
    hostname = (urlsplit(href).hostname or "").lower()
    return any(hostname == host or hostname.endswith(f".{host}") for host in ATS_HOST_PATTERNS)


def link_relationship(href: str, registrable_domain: str) -> str:
    hostname = (urlsplit(href).hostname or "").lower().strip(".")
    domain = registrable_domain.lower().strip(".")
    if domain and (hostname == domain or hostname.endswith(f".{domain}")):
        return "first_party"
    if href_has_ats_host(href):
        return "known_ats"
    return "external"


def evidence_links(
    evidence: list[Evidence],
    anchors: tuple[Anchor, ...],
    registrable_domain: str,
) -> list[dict[str, str]]:
    text_by_href: dict[str, str] = {}
    for anchor in anchors:
        if anchor.href and anchor.href not in text_by_href:
            text_by_href[anchor.href] = anchor.text[:120]
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in evidence:
        if not item.href or item.href in seen:
            continue
        parsed = urlsplit(item.href)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        seen.add(item.href)
        links.append(
            {
                "text": text_by_href.get(item.href, "")[:120],
                "href": item.href,
                "relationship": link_relationship(item.href, registrable_domain),
            }
        )
        if len(links) >= MAX_LINKS:
            break
    return links


def signal_key(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("source_record_id") or ""),
        str(row.get("requested_url") or ""),
        str(row.get("final_url") or ""),
        str(row.get("body_sha256") or ""),
        str(row.get("captured_at") or ""),
    ]
    return "\x1f".join(parts)


def derived_row(
    row: dict[str, Any],
    *,
    raw_path: Path,
    raw_line_number: int,
    raw_byte_offset: int,
    classification: str,
    evidence: list[Evidence],
    score: int,
    links: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "filter_version": "live_filter_v1",
        "signal_key": signal_key(row),
        "classification": classification,
        "signal_score": score,
        "evidence": [evidence_to_json(item) for item in evidence],
        "links": links,
        "provenance": {
            "raw_file": str(raw_path),
            "raw_line_number": raw_line_number,
            "raw_byte_offset": raw_byte_offset,
            "source_record_id": row.get("source_record_id"),
            "source_index": row.get("source_index"),
            "source_assessment": row.get("source_assessment"),
            "source_relationship": row.get("source_relationship"),
            "registrable_domain": row.get("registrable_domain"),
            "requested_url": row.get("requested_url"),
            "final_url": row.get("final_url"),
            "canonical_url": row.get("canonical_url"),
            "captured_at": row.get("captured_at"),
            "http_status": row.get("http_status"),
            "content_type": row.get("content_type"),
            "body_sha256": row.get("body_sha256"),
            "body_bytes": row.get("body_bytes"),
            "body_truncated": row.get("body_truncated"),
            "depth": row.get("depth"),
            "discovered_from": row.get("discovered_from"),
        },
    }


def evidence_to_json(item: Evidence) -> dict[str, Any]:
    data: dict[str, Any] = {
        "category": item.category,
        "rule": item.rule,
        "matched_term": item.matched_term,
        "location": item.location,
        "excerpt": item.excerpt[:MAX_EXCERPT_CHARS],
    }
    if item.href:
        data["href"] = item.href
    return data


def load_existing_signal_keys(signals_path: Path) -> set[str]:
    keys: set[str] = set()
    if not signals_path.exists():
        return keys
    for _line_number, row in iter_jsonl(signals_path):
        key = row.get("signal_key")
        if isinstance(key, str):
            keys.add(key)
    return keys


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            decoded = json.loads(stripped)
            if not isinstance(decoded, dict):
                raise ValueError(f"{path}:{line_number}: expected JSON object")
            yield line_number, decoded


def run_filter(raw_path: Path, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals_path = output_dir / SIGNALS_FILENAME
    summary_path = output_dir / SUMMARY_FILENAME
    representative_path = output_dir / REPRESENTATIVE_FILENAME
    manifest_path = output_dir / MANIFEST_FILENAME

    existing_keys = load_existing_signal_keys(signals_path)
    seen_keys = set(existing_keys)
    scanned = 0
    invalid_json_lines = 0
    appended = 0
    duplicate_positive_records = 0
    classification_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    http_status_counts: Counter[str] = Counter()
    source_assessment_counts: Counter[str] = Counter()
    representative: dict[str, list[dict[str, Any]]] = defaultdict(list)

    with raw_path.open("rb") as raw_handle, signals_path.open("a", encoding="utf-8") as out:
        while True:
            raw_byte_offset = raw_handle.tell()
            line = raw_handle.readline()
            if not line:
                break
            scanned += 1
            stripped = line.strip()
            if not stripped:
                status_counts["blank_line"] += 1
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                invalid_json_lines += 1
                status_counts["invalid_json"] += 1
                continue
            if not isinstance(row, dict):
                invalid_json_lines += 1
                status_counts["non_object_json"] += 1
                continue

            classification, evidence, score, links = classify_record(row)
            classification_counts[classification] += 1
            http_status_counts[str(row.get("http_status"))] += 1
            source_assessment_counts[str(row.get("source_assessment"))] += 1

            if classification == "no_signal":
                status_counts["no_signal"] += 1
                continue

            status_counts["positive_signal"] += 1
            derived = derived_row(
                row,
                raw_path=raw_path,
                raw_line_number=scanned,
                raw_byte_offset=raw_byte_offset,
                classification=classification,
                evidence=evidence,
                score=score,
                links=links,
            )
            key = derived["signal_key"]
            if key in seen_keys:
                duplicate_positive_records += 1
                continue
            out.write(json.dumps(derived, ensure_ascii=False, sort_keys=True) + "\n")
            seen_keys.add(key)
            appended += 1

            bucket = representative[classification]
            if len(bucket) < REPRESENTATIVE_PER_CLASS:
                bucket.append(_representative_row(derived))

    summary = {
        "filter_version": "live_filter_v1",
        "raw_file": str(raw_path),
        "output_dir": str(output_dir),
        "signals_file": str(signals_path),
        "generated_at": datetime.now(UTC).isoformat(),
        "total_raw_records_scanned": scanned,
        "invalid_json_lines": invalid_json_lines,
        "existing_signal_records_before_run": len(existing_keys),
        "new_signal_records_appended": appended,
        "positive_signal_records_seen_this_run": status_counts["positive_signal"],
        "duplicate_positive_records_skipped_this_run": duplicate_positive_records,
        "total_signal_records_after_run": len(seen_keys),
        "status_counts": dict(sorted(status_counts.items())),
        "classification_counts": dict(sorted(classification_counts.items())),
        "http_status_counts": dict(sorted(http_status_counts.items())),
        "source_assessment_counts": dict(sorted(source_assessment_counts.items())),
        "notes": [
            (
                "Derived JSONL stores compact evidence and raw provenance only; "
                "raw response_text remains in the raw file."
            ),
            (
                "Reruns append only unseen positive signal_key rows while rescanning raw "
                "for current run counts."
            ),
        ],
    }
    write_json_atomic(summary_path, summary)
    write_json_atomic(representative_path, dict(sorted(representative.items())))
    write_json_atomic(
        manifest_path,
        {
            "filter_version": "live_filter_v1",
            "created_at": summary["generated_at"],
            "raw_file": str(raw_path),
            "outputs": {
                "signals": str(signals_path),
                "summary": str(summary_path),
                "representative_signals": str(representative_path),
            },
            "idempotency_key": "signal_key",
        },
    )
    return summary


def _representative_row(derived: dict[str, Any]) -> dict[str, Any]:
    provenance = derived["provenance"]
    return {
        "classification": derived["classification"],
        "signal_score": derived["signal_score"],
        "registrable_domain": provenance.get("registrable_domain"),
        "final_url": provenance.get("final_url"),
        "source_record_id": provenance.get("source_record_id"),
        "raw_line_number": provenance.get("raw_line_number"),
        "evidence": derived["evidence"][:3],
        "links": derived["links"][:3],
    }


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temp_path.replace(path)


def validate_outputs(output_dir: Path) -> dict[str, Any]:
    signals_path = output_dir / SIGNALS_FILENAME
    summary_path = output_dir / SUMMARY_FILENAME
    representative_path = output_dir / REPRESENTATIVE_FILENAME
    manifest_path = output_dir / MANIFEST_FILENAME
    required = [signals_path, summary_path, representative_path, manifest_path]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"missing output files: {missing}")

    keys: set[str] = set()
    rows = 0
    classification_counts: Counter[str] = Counter()
    for line_number, row in iter_jsonl(signals_path):
        rows += 1
        key = row.get("signal_key")
        if not isinstance(key, str) or not key:
            raise ValueError(f"{signals_path}:{line_number}: missing signal_key")
        if key in keys:
            raise ValueError(f"{signals_path}:{line_number}: duplicate signal_key")
        keys.add(key)
        if "response_text" in row:
            raise ValueError(f"{signals_path}:{line_number}: response_text must not be duplicated")
        provenance = row.get("provenance")
        if not isinstance(provenance, dict) or not provenance.get("raw_file"):
            raise ValueError(f"{signals_path}:{line_number}: missing raw provenance")
        classification_counts[str(row.get("classification"))] += 1

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    json.loads(representative_path.read_text(encoding="utf-8"))
    json.loads(manifest_path.read_text(encoding="utf-8"))
    if summary.get("total_signal_records_after_run") != rows:
        raise ValueError(
            "summary total_signal_records_after_run does not match signal JSONL row count"
        )
    return {
        "valid": True,
        "signal_rows": rows,
        "classification_counts": dict(sorted(classification_counts.items())),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.validate_only:
        result = validate_outputs(args.output_dir)
    else:
        result = run_filter(args.raw, args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
