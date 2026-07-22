#!/usr/bin/env python3
"""First-pass deterministic recruitment signal filter for captured homepages."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

DEFAULT_INDEX_PATH = Path(
    "datasets/raw/agent_company_homepages/2026-07-18/http_concurrent/"
    "http_captured_index_after_retry_run4.jsonl"
)
DEFAULT_OUTPUT_PATH = Path(
    "datasets/private/agent-company-seeds/recruitment_homepage_triage_2026-07-18.jsonl"
)
DEFAULT_SUMMARY_PATH = Path(
    "datasets/private/agent-company-seeds/recruitment_homepage_triage_2026-07-18.summary.json"
)

TAXONOMY = (
    "open_roles",
    "career_portal",
    "recruitment_mention",
    "no_recruitment_evidence",
)
ASSESSMENTS = (
    "likely_self_hiring",
    "first_party_career_entry",
    "external_ats_needs_verification",
    "self_hiring_text_only_needs_verification",
    "third_party_or_ambiguous_signal",
    "no_recruitment_evidence",
)
SCORE_SCOPE = "homepage_recruitment_signal_strength_only"
MAX_EXCERPTS = 5
MAX_LINKS = 8
MAX_EXCERPT_CHARS = 260

SKIPPED_TAGS = {"script", "style", "noscript", "template", "svg", "canvas"}
ATS_HOST_PATTERNS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workdayjobs.com",
    "myworkdayjobs.com",
    "workable.com",
    "smartrecruiters.com",
    "jobvite.com",
    "icims.com",
    "bamboohr.com",
    "breezy.hr",
    "recruitee.com",
    "personio.de",
    "comeet.com",
    "teamtailor.com",
    "jobs.ashbyhq.com",
    "boards.greenhouse.io",
)

FALSE_POSITIVE_PATTERNS = (
    re.compile(
        r"\b("
        r"cron\s+job|job\s+queue|queue\s+job|background\s+job|scheduled\s+job|"
        r"job\s+runner|job\s+scheduler|job\s+id|jobs?\s+to\s+be\s+done"
        r")\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b("
        r"recruiting\s+agency|recruitment\s+agency|staffing\s+agency|staffing\s+firm|"
        r"talent\s+agency|executive\s+search\s+firm|headhunt(?:er|ing)"
        r")\b",
        re.IGNORECASE,
    ),
)

STAFFING_VENDOR_PATTERN = re.compile(
    r"\b(recruiting|recruitment|staffing|talent)\s+(agency|firm|vendor|platform|service)s?\b",
    re.IGNORECASE,
)

CHINESE_OPEN_ROLE_PATTERN = (
    "\u70ed\u62db\u804c\u4f4d|\u5728\u62db\u804c\u4f4d|\u804c\u4f4d\u7a7a\u7f3a|"
    "\u5f00\u653e\u804c\u4f4d|\u67e5\u770b\u804c\u4f4d|\u7533\u8bf7\u804c\u4f4d|"
    "\u62db\u8058\u5c97\u4f4d|\u793e\u4f1a\u62db\u8058|\u6821\u56ed\u62db\u8058"
)
CHINESE_CAREER_LABEL_PATTERN = (
    "\u52a0\u5165\u6211\u4eec|\u62db\u8d24\u7eb3\u58eb|\u4eba\u624d\u62db\u8058"
)
CHINESE_RECRUITMENT_PATTERN = (
    "\u62db\u8058|\u52a0\u5165\u6211\u4eec|\u62db\u8d24\u7eb3\u58eb|\u4eba\u624d\u62db\u8058"
)

OPEN_ROLE_RULES = (
    (
        "open_positions",
        re.compile(
            r"\b(open|current|available)\s+"
            r"(roles?|positions?|jobs?|vacanc(?:y|ies)|opportunities)\b",
            re.IGNORECASE,
        ),
    ),
    ("job_openings", re.compile(r"\b(job|career)\s+openings\b", re.IGNORECASE)),
    (
        "apply_jobs",
        re.compile(
            r"\b(apply|view|search|browse)\s+(now\s+)?(all\s+)?(open\s+)?"
            r"(jobs?|roles?|positions?|vacanc(?:y|ies))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "hiring_now",
        re.compile(r"\b(we(?:'|\u2019)re|we\s+are)\s+hiring\b|\bhiring\s+now\b", re.IGNORECASE),
    ),
    (
        "english_cta",
        re.compile(
            r"\b("
            r"apply\s+(now\s+)?(for\s+)?(jobs?|roles?|positions?|openings?)|"
            r"see\s+open\s+(roles?|positions?)|view\s+(job\s+)?openings|"
            r"search\s+jobs"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    ("chinese_open_roles", re.compile(f"({CHINESE_OPEN_ROLE_PATTERN})")),
)

CAREER_PORTAL_TEXT_RULES = (
    (
        "careers_label",
        re.compile(
            r"^\s*(careers?|jobs?|join\s+us|join\s+our\s+team|work\s+with\s+us|"
            f"{CHINESE_CAREER_LABEL_PATTERN})"
            r"\s*$",
            re.IGNORECASE,
        ),
    ),
)

CAREER_ROUTE_PATTERN = re.compile(
    r"(^|/)(careers?|jobs?|join-us|join_?us|join-our-team|work-with-us|"
    "\u4eba\u624d\u62db\u8058|\u62db\u8058)(/|$)",
    re.IGNORECASE,
)

RECRUITMENT_MENTION_RULES = (
    ("career_word", re.compile(r"\bcareers?\b", re.IGNORECASE)),
    ("hiring_word", re.compile(r"\bhiring\b", re.IGNORECASE)),
    ("recruitment_word", re.compile(r"\brecruit(?:ing|ment)\b", re.IGNORECASE)),
    ("talent_acquisition", re.compile(r"\btalent\s+acquisition\b", re.IGNORECASE)),
    ("join_team", re.compile(r"\bjoin\s+our\s+team\b", re.IGNORECASE)),
    ("chinese_recruitment", re.compile(f"({CHINESE_RECRUITMENT_PATTERN})")),
)

SELF_HIRING_RULES = (
    ("we_are_hiring", re.compile(r"\b(we(?:'|\u2019)re|we\s+are)\s+hiring\b", re.IGNORECASE)),
    ("join_our_team", re.compile(r"\bjoin\s+our\s+team\b", re.IGNORECASE)),
    (
        "chinese_self_hiring",
        re.compile(r"(\u6211\u4eec\u6b63\u5728\u62db\u8058|\u52a0\u5165\u6211\u4eec)"),
    ),
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
class Match:
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
        href = attrs_by_name.get("href", "").strip()
        self._anchor_stack.append({"href": href, "chunks": []})

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if self._skip_stack:
            if lowered == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if lowered == "a" and self._anchor_stack:
            anchor = self._anchor_stack.pop()
            text = _normalize_space(" ".join(anchor["chunks"]))
            href = str(anchor["href"]).strip()
            if text or href:
                self.anchors.append(Anchor(text=text, href=_normalize_href(href, self._base_url)))

    def handle_data(self, data: str) -> None:
        if self._skip_stack or _looks_like_json_blob(data):
            return
        text = _normalize_space(data)
        if not text:
            return
        self._text_chunks.append(text)
        if self._anchor_stack:
            self._anchor_stack[-1]["chunks"].append(text)

    def page_text(self) -> PageText:
        return PageText(
            visible_text=_normalize_space(" ".join(self._text_chunks)),
            anchors=tuple(self.anchors),
        )


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _looks_like_json_blob(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < 120 or stripped[:1] not in {"{", "["}:
        return False
    punctuation = sum(1 for char in stripped[:500] if char in '{}[]":,')
    return punctuation >= 40


def _normalize_href(href: str, base_url: str) -> str:
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return href
    return urljoin(base_url, href)


def extract_page_text(html: str, base_url: str = "") -> PageText:
    parser = VisibleTextParser(base_url)
    parser.feed(html)
    parser.close()
    return parser.page_text()


def _bounded_excerpt(text: str, start: int, end: int, limit: int = MAX_EXCERPT_CHARS) -> str:
    window = max(0, (limit - (end - start)) // 2)
    excerpt_start = max(0, start - window)
    excerpt_end = min(len(text), end + window)
    if excerpt_start:
        space = text.find(" ", excerpt_start)
        if 0 <= space < start:
            excerpt_start = space + 1
    if excerpt_end < len(text):
        space = text.rfind(" ", end, excerpt_end)
        if space > end:
            excerpt_end = space
    prefix = "..." if excerpt_start > 0 else ""
    suffix = "..." if excerpt_end < len(text) else ""
    return f"{prefix}{text[excerpt_start:excerpt_end].strip()}{suffix}"


def _is_false_positive_context(text: str) -> bool:
    return any(pattern.search(text) for pattern in FALSE_POSITIVE_PATTERNS)


def _append_regex_matches(
    matches: list[Match],
    *,
    category: str,
    rules: tuple[tuple[str, re.Pattern[str]], ...],
    text: str,
    location: str,
    href: str | None = None,
) -> None:
    if not text or _is_false_positive_context(text):
        return
    for rule_name, pattern in rules:
        for found in pattern.finditer(text):
            matches.append(
                Match(
                    category=category,
                    rule=rule_name,
                    matched_term=found.group(0),
                    location=location,
                    excerpt=_bounded_excerpt(text, found.start(), found.end()),
                    href=href,
                )
            )


def _href_has_ats_host(href: str) -> bool:
    hostname = (urlsplit(href).hostname or "").lower()
    return any(hostname == host or hostname.endswith(f".{host}") for host in ATS_HOST_PATTERNS)


def _href_has_career_route(href: str) -> bool:
    parsed = urlsplit(href)
    route = parsed.path.strip("/")
    return bool(CAREER_ROUTE_PATTERN.search(route))


def _hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _candidate_domain_is_known_ats(registrable_domain: str) -> bool:
    domain = registrable_domain.lower().strip(".")
    return any(host == domain or host.endswith(f".{domain}") for host in ATS_HOST_PATTERNS)


def _host_matches_candidate_domain(hostname: str, registrable_domain: str) -> bool:
    host = hostname.lower().strip(".")
    domain = registrable_domain.lower().strip(".")
    if not host or not domain:
        return False
    if _candidate_domain_is_known_ats(domain):
        return host == domain or host == f"www.{domain}"
    return host == domain or host.endswith(f".{domain}")


def _link_relationship(href: str, registrable_domain: str) -> str:
    hostname = _hostname(href)
    if _host_matches_candidate_domain(hostname, registrable_domain):
        return "first_party"
    if _href_has_ats_host(href):
        return "known_ats"
    return "external"


def _has_self_hiring_phrase(text: str) -> bool:
    if _is_false_positive_context(text):
        return False
    return any(pattern.search(text) for _name, pattern in SELF_HIRING_RULES)


def classify_page(page: PageText) -> tuple[str, list[Match], int]:
    matches: list[Match] = []
    visible_text = page.visible_text

    staffing_vendor_page = bool(STAFFING_VENDOR_PATTERN.search(visible_text))
    if not staffing_vendor_page:
        _append_regex_matches(
            matches,
            category="open_roles",
            rules=OPEN_ROLE_RULES,
            text=visible_text,
            location="visible_text",
        )
        _append_regex_matches(
            matches,
            category="recruitment_mention",
            rules=RECRUITMENT_MENTION_RULES,
            text=visible_text,
            location="visible_text",
        )

    for anchor in page.anchors:
        anchor_text = anchor.text
        href = anchor.href
        link_context = _normalize_space(f"{anchor_text} {href}")
        if (
            staffing_vendor_page
            and not _href_has_career_route(href)
            and not _href_has_ats_host(href)
        ):
            continue
        _append_regex_matches(
            matches,
            category="open_roles",
            rules=OPEN_ROLE_RULES,
            text=link_context,
            location="anchor",
            href=href or None,
        )
        if _href_has_ats_host(href):
            matches.append(
                Match(
                    category="career_portal",
                    rule="known_ats_host",
                    matched_term=urlsplit(href).hostname or href,
                    location="href",
                    excerpt=anchor_text or href,
                    href=href,
                )
            )
        if _href_has_career_route(href):
            matches.append(
                Match(
                    category="career_portal",
                    rule="career_route",
                    matched_term=urlsplit(href).path or href,
                    location="href",
                    excerpt=anchor_text or href,
                    href=href,
                )
            )
        _append_regex_matches(
            matches,
            category="career_portal",
            rules=CAREER_PORTAL_TEXT_RULES,
            text=anchor_text,
            location="anchor_text",
            href=href or None,
        )
        _append_regex_matches(
            matches,
            category="recruitment_mention",
            rules=RECRUITMENT_MENTION_RULES,
            text=anchor_text,
            location="anchor_text",
            href=href or None,
        )

    categories = {match.category for match in matches}
    if "open_roles" in categories:
        label = "open_roles"
    elif "career_portal" in categories:
        label = "career_portal"
    elif "recruitment_mention" in categories:
        label = "recruitment_mention"
    else:
        label = "no_recruitment_evidence"

    score = _score(label, matches)
    return label, _dedupe_matches(matches), score


def _score(label: str, matches: list[Match]) -> int:
    category_weights = {
        "open_roles": 100,
        "career_portal": 70,
        "recruitment_mention": 35,
        "no_recruitment_evidence": 0,
    }
    return min(100, category_weights[label] + min(20, max(0, len(matches) - 1) * 5))


def _dedupe_matches(matches: list[Match]) -> list[Match]:
    seen: set[tuple[str, str, str, str | None]] = set()
    deduped: list[Match] = []
    for match in matches:
        key = (match.category, match.rule, match.matched_term.lower(), match.href)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(match)
    return deduped


def _match_to_json(match: Match) -> dict[str, Any]:
    row: dict[str, Any] = {
        "category": match.category,
        "rule": match.rule,
        "matched_term": match.matched_term,
        "location": match.location,
        "excerpt": match.excerpt[:MAX_EXCERPT_CHARS],
    }
    if match.href:
        row["href"] = match.href
    return row


def _is_valid_output_href(href: str) -> bool:
    parsed = urlsplit(href)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _evidence_links(
    matches: list[Match],
    anchors: tuple[Anchor, ...],
    registrable_domain: str,
) -> list[dict[str, str]]:
    anchor_text_by_href: dict[str, str] = {}
    for anchor in anchors:
        if anchor.href and anchor.href not in anchor_text_by_href:
            anchor_text_by_href[anchor.href] = anchor.text[:120]

    links: list[dict[str, str]] = []
    seen_hrefs: set[str] = set()
    for match in matches:
        if not match.href or match.href in seen_hrefs or not _is_valid_output_href(match.href):
            continue
        seen_hrefs.add(match.href)
        links.append(
            {
                "text": anchor_text_by_href.get(match.href, "")[:120],
                "href": match.href,
                "relationship": _link_relationship(match.href, registrable_domain),
            }
        )
        if len(links) >= MAX_LINKS:
            break
    return links


def _evidence_hrefs(matches: list[Match]) -> tuple[str, ...]:
    seen_hrefs: set[str] = set()
    hrefs: list[str] = []
    for match in matches:
        href = match.href
        if not href or href in seen_hrefs or not _is_valid_output_href(href):
            continue
        seen_hrefs.add(href)
        hrefs.append(href)
    return tuple(hrefs)


def _employer_hiring_assessment(
    *,
    classification: str,
    page: PageText,
    matches: list[Match],
    final_url: str,
    registrable_domain: str,
) -> tuple[str, list[str]]:
    if classification == "no_recruitment_evidence":
        return "no_recruitment_evidence", ["no homepage recruitment signal"]

    final_url_is_candidate_domain = _host_matches_candidate_domain(
        _hostname(final_url), registrable_domain
    )
    has_self_hiring_phrase = _has_self_hiring_phrase(page.visible_text)
    evidence_hrefs = _evidence_hrefs(matches)
    has_first_party_career_link = any(
        _link_relationship(href, registrable_domain) == "first_party"
        and _href_has_career_route(href)
        for href in evidence_hrefs
    )
    has_known_ats_link = any(
        _link_relationship(href, registrable_domain) == "known_ats" for href in evidence_hrefs
    )

    if final_url_is_candidate_domain and has_self_hiring_phrase and has_first_party_career_link:
        return (
            "likely_self_hiring",
            [
                "final_url belongs to candidate registrable_domain",
                "explicit self-hiring phrase present",
                "first-party career/jobs link present",
            ],
        )
    if has_first_party_career_link:
        return "first_party_career_entry", ["first-party career/jobs link present"]
    if has_known_ats_link:
        return "external_ats_needs_verification", ["known ATS link present"]
    if has_self_hiring_phrase:
        return (
            "self_hiring_text_only_needs_verification",
            ["explicit self-hiring phrase present without first-party career/jobs entry"],
        )
    return "third_party_or_ambiguous_signal", ["recruitment signal is third-party or ambiguous"]


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


def _key_from_row(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("record_id"),
        row.get("body_sha256"),
        row.get("requested_url"),
        row.get("final_url"),
    )


def _raw_indexes(
    index_rows: list[dict[str, Any]],
    repo_root: Path,
) -> dict[str, dict[tuple[Any, ...], dict[str, Any]]]:
    raw_files = sorted({str(row["raw_file"]) for row in index_rows if row.get("raw_file")})
    indexes: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
    for raw_file in raw_files:
        raw_path = repo_root / raw_file
        rows_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
        for raw_row in _load_jsonl(raw_path):
            rows_by_key[_key_from_row(raw_row)] = raw_row
        indexes[raw_file] = rows_by_key
    return indexes


def _resolve_raw_row(
    index_row: dict[str, Any],
    raw_by_file: dict[str, dict[tuple[Any, ...], dict[str, Any]]],
) -> dict[str, Any] | None:
    raw_file = str(index_row.get("raw_file", ""))
    return raw_by_file.get(raw_file, {}).get(_key_from_row(index_row))


def _triage_row(index_row: dict[str, Any], raw_row: dict[str, Any] | None) -> dict[str, Any]:
    base = {
        "record_id": index_row.get("record_id"),
        "candidate_entity_name": index_row.get("candidate_entity_name"),
        "registrable_domain": index_row.get("registrable_domain"),
        "selected_run": index_row.get("selected_run"),
        "raw_file": index_row.get("raw_file"),
        "requested_url": index_row.get("requested_url"),
        "final_url": index_row.get("final_url"),
        "http_status": index_row.get("http_status"),
        "content_type": index_row.get("content_type"),
        "body_sha256": index_row.get("body_sha256"),
    }
    if raw_row is None:
        return {
            **base,
            "classification": "no_recruitment_evidence",
            "employer_hiring_assessment": "no_recruitment_evidence",
            "assessment_reasons": ["raw capture was not found"],
            "score": 0,
            "score_scope": SCORE_SCOPE,
            "evidence": [],
            "links": [],
            "warnings": ["raw_row_not_found"],
        }

    page = extract_page_text(
        str(raw_row.get("response_text") or ""),
        base_url=str(index_row.get("final_url") or index_row.get("requested_url") or ""),
    )
    label, matches, score = classify_page(page)
    registrable_domain = str(index_row.get("registrable_domain") or "")
    links = _evidence_links(matches, page.anchors, registrable_domain)
    assessment, assessment_reasons = _employer_hiring_assessment(
        classification=label,
        page=page,
        matches=matches,
        final_url=str(index_row.get("final_url") or ""),
        registrable_domain=registrable_domain,
    )
    evidence = [_match_to_json(match) for match in matches[:MAX_EXCERPTS]]
    return {
        **base,
        "classification": label,
        "employer_hiring_assessment": assessment,
        "assessment_reasons": assessment_reasons,
        "score": score,
        "score_scope": SCORE_SCOPE,
        "evidence": evidence,
        "links": links,
        "warnings": [],
    }


def run_filter(
    index_path: Path,
    output_path: Path,
    summary_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    index_rows = _load_jsonl(index_path)
    raw_by_file = _raw_indexes(index_rows, repo_root)
    summary_counter: Counter[str] = Counter()
    assessment_counter: Counter[str] = Counter()
    warning_counter: Counter[str] = Counter()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        for index_row in index_rows:
            raw_row = _resolve_raw_row(index_row, raw_by_file)
            row = _triage_row(index_row, raw_row)
            summary_counter[str(row["classification"])] += 1
            assessment_counter[str(row["employer_hiring_assessment"])] += 1
            for warning in row["warnings"]:
                warning_counter[str(warning)] += 1
            output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    summary = {
        "input_index": str(index_path),
        "output_jsonl": str(output_path),
        "total_records": sum(summary_counter.values()),
        "classification_definition": (
            "homepage recruitment signal; not a claim that the candidate employer is hiring"
        ),
        "classification_counts": {label: summary_counter[label] for label in TAXONOMY},
        "assessment_definition": (
            "conservative employer-hiring attribution based on candidate-domain, ATS, "
            "and self-hiring evidence"
        ),
        "assessment_counts": {label: assessment_counter[label] for label in ASSESSMENTS},
        "warning_counts": dict(sorted(warning_counter.items())),
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    summary = run_filter(
        index_path=args.index,
        output_path=args.output,
        summary_path=args.summary,
        repo_root=args.repo_root,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
