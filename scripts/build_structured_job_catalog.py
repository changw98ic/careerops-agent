#!/usr/bin/env python3
"""Build a safe, job-level catalog from captured JSON-LD JobPosting pages.

The existing recruitment-page signal catalog intentionally excludes page bodies.
This local-only derivation uses the signal provenance only to seek directly to
the corresponding raw response, then exports a bounded allowlist of fields from
JSON-LD ``JobPosting`` objects.  It never exports raw HTML, response text,
source paths, offsets, hashes, or URL query strings.  Public contact emails are
preserved only when they appear in a structured application field, a direct
job-description application instruction, or a company-domain mailto link that
is itself labelled as an application action.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote, urlsplit, urlunsplit

DEFAULT_INPUT = Path(
    "datasets/private/recruitment-processing/2026-07-19/"
    "global_raw_52_v1/merged/recruitment_page_signals.jsonl"
)
DEFAULT_OUTPUT = Path(
    "datasets/private/recruitment-processing/2026-07-19/"
    "final_global_closeout_v1/job-catalog/structured-jobs.json"
)
CATALOG_SCHEMA_VERSION = "structured_job_catalog_v2"
DEFAULT_MAX_BODY_BYTES = 1_000_000
MAX_DESCRIPTION_CHARS = 3_200
MAX_SECTION_CHARS = 1_600
MAX_LIST_ITEMS = 8
EMPLOYMENT_TYPE_ALIASES = {
    "FULL_TIME": "FULL_TIME",
    "FULLTIME": "FULL_TIME",
    "PART_TIME": "PART_TIME",
    "PARTTIME": "PART_TIME",
    "CONTRACT": "CONTRACT",
    "CONTRACTOR": "CONTRACT",
    "TEMPORARY": "TEMPORARY",
    "INTERN": "INTERN",
    "INTERNSHIP": "INTERN",
}

FORBIDDEN_OUTPUT_KEYS = {
    "body",
    "body_bytes",
    "body_sha256",
    "body_text",
    "html",
    "input_path",
    "output_path",
    "raw",
    "raw_byte_offset",
    "raw_file",
    "raw_html",
    "raw_line",
    "raw_line_number",
    "response",
    "response_body",
    "response_text",
    "signal_key",
    "source_path",
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
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
NON_CONTACT_EMAIL_CONTEXT_PATTERN = re.compile(
    r"\b(?:privacy|legal|support|sales|marketing|press|media|unsubscribe|cookie|gdpr|"
    r"data protection|accommodation|disability|question(?:s)?|feedback|help|customer service|"
    r"reference(?:s)?|sender(?:s)?|approved sender(?:s)?|spam|notification(?:s)?|alert(?:s)?|"
    r"accessibility|alternative method|technical issue(?:s)?|technical support|unsolicited "
    r"(?:application|applications|document|documents))\b|隐私|法务|客服|营销|媒体|无障碍|问题|反馈|通知",
    re.IGNORECASE,
)
DIRECT_APPLICATION_EMAIL_PATTERN = re.compile(
    r"(?:"
    r"\b(?:send|submit|email|e-mail|forward)\b.{0,140}?"
    r"\b(?:resume|résumé|curriculum vitae|cv|cover letter|applications?(?: materials)?|"
    r"candidate profile)\b.{0,60}?\b(?:to|at|via|through)\b\s*(?:e-?mail\s*)?\[\[EMAIL\]\]"
    r"|\bapply(?:\s+now)?\s*(?:"
    r"(?:to|at|via|through|by)\s*(?:e-?mail\s*)?"
    r"|[,:;—-]?\s*(?:email|e-mail|contact)\s*"
    r")\[\[EMAIL\]\]"
    r"|\b(?:email|e-mail|contact)\b\s*\[\[EMAIL\]\].{0,80}?"
    r"\b(?:to\s+)?(?:apply|submit|send)\b"
    r"|(?:投递|发送|提交).{0,80}?(?:简历|履历|申请材料).{0,80}?"
    r"(?:至|到|给|邮箱)\s*\[\[EMAIL\]\]"
    r"|(?:应聘|申请).{0,80}?(?:至|到|通过|邮箱)\s*\[\[EMAIL\]\]"
    r"|(?:邮件|联系)\s*\[\[EMAIL\]\].{0,60}?(?:投递|申请|应聘)"
    r")",
    re.IGNORECASE,
)
WIDGET_ARTIFACT_PATTERN = re.compile(r"\bdata-widget_type\s*=", re.IGNORECASE)
LOCATION_DETAIL_PATTERN = re.compile(
    r"\b(?:suite|floor|tower|building|office|ifc|street|st\.?|road|rd\.?|"
    r"avenue|ave\.?|boulevard|blvd\.?|level|plaza)\b",
    re.IGNORECASE,
)

GENERIC_TITLES = {
    "career",
    "careers",
    "job",
    "jobs",
    "join us",
    "job openings",
    "open positions",
    "open roles",
    "work with us",
}
REQUIREMENTS_HEADING = re.compile(
    r"\b(requirements?|qualifications?|what\s+(?:you(?:'|\u2019)?ll\s+need|we\s+look\s+for)|"
    r"who\s+you\s+are|skills?|experience)\b|任职要求|职位要求|岗位要求",
    re.IGNORECASE,
)
RESPONSIBILITIES_HEADING = re.compile(
    r"\b(responsibilities|what\s+you(?:'|\u2019)?ll\s+do|your\s+(?:role|mission)|"
    r"the\s+role|duties)\b|岗位职责|工作职责",
    re.IGNORECASE,
)
EXPLICIT_HYBRID_PATTERN = re.compile(
    r"\bhybrid\s+(?:work|working|role|position|schedule|arrangement|model)\b|"
    r"\bsplit\s+(?:their|your)\s+time\s+between\s+(?:the\s+)?office\s+and\s+home\b",
    re.IGNORECASE,
)
EXPLICIT_REMOTE_PATTERN = re.compile(
    r"\b(?:fully|100%)\s+remote\b|\bremote\s+(?:work|role|position|job|opportunity|location)\b|"
    r"\bwork\s+from\s+home\b",
    re.IGNORECASE,
)
EXPLICIT_ONSITE_PATTERN = re.compile(
    r"\b(?:on[-\s]?site|in[-\s]?office)\s+(?:work|role|position|job)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RawReference:
    public_url: str
    raw_path: Path
    raw_offset: int
    company_domain: str
    captured_at: str
    body_truncated: bool


class _JsonLdParser(HTMLParser):
    """Collect application/ld+json script contents without rendering HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._script_chunks: list[str] | None = None
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = {name.lower(): value or "" for name, value in attrs}
        type_value = attributes.get("type", "").lower()
        if "ld+json" in type_value:
            self._script_chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or self._script_chunks is None:
            return
        value = "".join(self._script_chunks).strip()
        if value:
            self.scripts.append(value)
        self._script_chunks = None

    def handle_data(self, data: str) -> None:
        if self._script_chunks is not None:
            self._script_chunks.append(data)


class _MailtoAnchorParser(HTMLParser):
    """Collect contextual public mailto links without exporting page HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._recent_text = ""
        self._active_href: str | None = None
        self._active_before = ""
        self._active_text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self._active_href is not None:
            return
        attributes = {name.lower(): value or "" for name, value in attrs}
        href = attributes.get("href", "")
        if href.casefold().startswith("mailto:"):
            self._active_href = href
            self._active_before = self._recent_text
            self._active_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._active_href is None:
            return
        context = f"{self._active_before} {' '.join(self._active_text)}"
        self.links.append((self._active_href, context))
        self._active_href = None
        self._active_before = ""
        self._active_text = []

    def handle_data(self, data: str) -> None:
        if self._active_href is not None:
            self._active_text.append(data)
        self._recent_text = (self._recent_text + data)[-240:]


class _TextParser(HTMLParser):
    """Turn an HTML fragment into compact readable text."""

    _SKIPPED: ClassVar[set[str]] = {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
        "head",
    }
    _BREAKS: ClassVar[set[str]] = {
        "p",
        "br",
        "li",
        "div",
        "section",
        "article",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_stack: list[str] = []
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in self._SKIPPED:
            self._skip_stack.append(lowered)
        elif not self._skip_stack and lowered in self._BREAKS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if self._skip_stack:
            if lowered == self._skip_stack[-1]:
                self._skip_stack.pop()
        elif lowered in self._BREAKS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_stack:
            self.chunks.append(data)


class _SectionParser(HTMLParser):
    """Extract text after explicit headings in a job description fragment."""

    _SKIPPED: ClassVar[set[str]] = {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
        "head",
    }
    _BREAKS: ClassVar[set[str]] = {"p", "br", "li", "div", "section", "article"}
    _HEADINGS: ClassVar[set[str]] = {"h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_stack: list[str] = []
        self._heading_tag: str | None = None
        self._heading_chunks: list[str] = []
        self._active_heading: str | None = None
        self._active_chunks: list[str] = []
        self.sections: list[tuple[str, str]] = []

    def _flush_section(self) -> None:
        if not self._active_heading:
            return
        content = _compact_text("".join(self._active_chunks))
        if content:
            self.sections.append((self._active_heading, content))
        self._active_heading = None
        self._active_chunks = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in self._SKIPPED:
            self._skip_stack.append(lowered)
            return
        if self._skip_stack:
            return
        if lowered in self._HEADINGS and self._heading_tag is None:
            self._flush_section()
            self._heading_tag = lowered
            self._heading_chunks = []
        elif lowered in self._BREAKS and self._active_heading is not None:
            self._active_chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if self._skip_stack:
            if lowered == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if self._heading_tag == lowered:
            heading = _compact_text("".join(self._heading_chunks))
            self._heading_tag = None
            self._heading_chunks = []
            if heading:
                self._active_heading = heading
                self._active_chunks = []
        elif lowered in self._BREAKS and self._active_heading is not None:
            self._active_chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_stack:
            return
        if self._heading_tag is not None:
            self._heading_chunks.append(data)
        elif self._active_heading is not None:
            self._active_chunks.append(data)

    def close(self) -> None:
        super().close()
        self._flush_section()


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSONL: {error.msg}") from error
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected an object")
            yield value


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _clip(value: str, maximum: int) -> str:
    compact = _compact_text(value)
    compact = EMAIL_PATTERN.sub("[公开联系邮箱见卡片]", compact)
    if len(compact) > maximum:
        return compact[: maximum - 1].rstrip() + "…"
    return compact


def _plain_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    # JSON encoders sometimes preserve escaped closing HTML tags (for example
    # ``<\\/path>``) inside a JSON-LD description.  Normalize that escape
    # before parsing so the tag is not exposed as user-facing text.
    extracted = value
    for _ in range(4):
        extracted = re.sub(r"\\+/", "/", extracted)
        parser = _TextParser()
        parser.feed(extracted)
        parser.close()
        next_value = html.unescape("".join(parser.chunks))
        if next_value == extracted:
            break
        extracted = next_value
    plain_text = _clip(extracted, maximum)
    # A widget attribute means the structured field was polluted by page
    # chrome rather than a standalone job description.  Do not expose a
    # misleading mixture of navigation labels and technical markup.
    return "" if WIDGET_ARTIFACT_PATTERN.search(plain_text) else plain_text


def _safe_public_url(*values: Any) -> str:
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            continue
        hostname = parsed.hostname.lower()
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = parsed.port
        except ValueError:
            continue
        netloc = f"{hostname}:{port}" if port is not None else hostname
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))
    return ""


def _has_schema_evidence(row: dict[str, Any]) -> bool:
    evidence = row.get("evidence")
    if not isinstance(evidence, list):
        return False
    return any(
        isinstance(item, dict) and item.get("rule") == "schema_org_jobposting" for item in evidence
    )


def select_references(
    rows: Iterable[dict[str, Any]], *, max_body_bytes: int
) -> tuple[list[RawReference], Counter[str]]:
    """Select one complete high-confidence raw response for each public URL."""

    stats: Counter[str] = Counter()
    selected: dict[str, RawReference] = {}
    for row in rows:
        stats["input_signals"] += 1
        if row.get("classification") != "actual_job_posting" or not _has_schema_evidence(row):
            continue
        stats["schema_candidates"] += 1
        provenance = row.get("provenance")
        if not isinstance(provenance, dict):
            stats["skipped_missing_provenance"] += 1
            continue
        if provenance.get("body_truncated") is True:
            stats["skipped_truncated"] += 1
            continue
        body_bytes = provenance.get("body_bytes")
        if isinstance(body_bytes, int) and body_bytes > max_body_bytes:
            stats["skipped_large_body"] += 1
            continue
        public_url = _safe_public_url(
            provenance.get("canonical_url"),
            provenance.get("final_url"),
            provenance.get("requested_url"),
        )
        raw_file = provenance.get("raw_file")
        raw_offset = provenance.get("raw_byte_offset")
        if (
            not public_url
            or not isinstance(raw_file, str)
            or not isinstance(raw_offset, int)
            or raw_offset < 0
        ):
            stats["skipped_unreadable_reference"] += 1
            continue
        company_domain = str(provenance.get("registrable_domain") or "").strip().lower()
        captured_at = str(provenance.get("captured_at") or "").strip()
        reference = RawReference(
            public_url=public_url,
            raw_path=Path(raw_file),
            raw_offset=raw_offset,
            company_domain=company_domain,
            captured_at=captured_at,
            body_truncated=False,
        )
        existing = selected.get(public_url)
        if existing is None or (reference.raw_path.as_posix(), reference.raw_offset) < (
            existing.raw_path.as_posix(),
            existing.raw_offset,
        ):
            selected[public_url] = reference
    stats["unique_references"] = len(selected)
    return (
        sorted(selected.values(), key=lambda item: (item.raw_path.as_posix(), item.raw_offset)),
        stats,
    )


def _parse_jsonld_scripts(response: str) -> tuple[list[Any], int, int]:
    parser = _JsonLdParser()
    parser.feed(response)
    parser.close()
    values: list[Any] = []
    errors = 0
    for script in parser.scripts:
        cleaned = script.strip().removeprefix("<!--").removesuffix("-->").strip()
        try:
            values.append(json.loads(cleaned))
        except json.JSONDecodeError:
            errors += 1
    return values, len(parser.scripts), errors


def _types(value: Any) -> set[str]:
    raw = value.get("@type") if isinstance(value, dict) else None
    values = raw if isinstance(raw, list) else [raw]
    return {
        item.lower().rsplit("/", 1)[-1] for item in values if isinstance(item, str) and item.strip()
    }


def _job_postings(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if "jobposting" in _types(value):
            yield value
        for child in value.values():
            yield from _job_postings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _job_postings(child)


def _string_items(value: Any, *, maximum: int = MAX_SECTION_CHARS) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, list):
                return _string_items(decoded, maximum=maximum)
        cleaned = _plain_text(value, maximum)
        return [cleaned] if cleaned else []
    if isinstance(value, list):
        items: list[str] = []
        for child in value:
            items.extend(_string_items(child, maximum=maximum))
        return items
    if isinstance(value, dict):
        items: list[str] = []
        for key in ("description", "name", "value"):
            if key in value:
                items.extend(_string_items(value[key], maximum=maximum))
        return items
    return []


def _unique_items(values: Iterable[str], *, limit: int = MAX_LIST_ITEMS) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clip(value, MAX_SECTION_CHARS)
        fingerprint = cleaned.casefold()
        if not cleaned or fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(cleaned)
        if len(result) == limit:
            break
    return result


def _sections_from_description(value: Any) -> list[tuple[str, str]]:
    if not isinstance(value, str) or not value.strip():
        return []
    parser = _SectionParser()
    parser.feed(value)
    parser.close()
    return parser.sections


def _matching_sections(sections: Iterable[tuple[str, str]], pattern: re.Pattern[str]) -> list[str]:
    return _unique_items(
        _clip(content, MAX_SECTION_CHARS)
        for heading, content in sections
        if pattern.search(heading)
    )


def _location_label(value: str) -> str:
    cleaned = _clip(value, 240)
    cleaned = re.split(r"\s+-\s+(?=\d)", cleaned, maxsplit=1)[0].strip()
    return "" if LOCATION_DETAIL_PATTERN.search(cleaned) else cleaned


def _location_part(value: Any) -> str:
    if isinstance(value, str):
        return _location_label(value)
    if not isinstance(value, dict):
        return ""
    name = value.get("name")
    if isinstance(name, str) and name.strip():
        return _location_label(name)
    address = value.get("address")
    if isinstance(address, str):
        return _location_label(address)
    if not isinstance(address, dict):
        address = value
    parts: list[str] = []
    for key in ("addressLocality", "addressRegion", "addressCountry"):
        part = address.get(key)
        if not isinstance(part, str):
            continue
        label = _location_label(part.strip())
        if label:
            parts.append(label)
    return _clip(", ".join(parts), 240)


def _location(value: Any) -> str:
    values = value if isinstance(value, list) else [value]
    return "; ".join(_unique_items((_location_part(item) for item in values), limit=6))


def _remote_scope(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return _unique_items((_location_part(item) for item in values), limit=6)


def _field_values(value: Any) -> list[str]:
    return [item.casefold() for item in _string_items(value, maximum=200)]


def _remote_fields(job: dict[str, Any], description: str) -> tuple[str, str, list[str]]:
    values: list[str] = []
    for key in ("jobLocationType", "workplaceType", "workplace_type", "workMode", "work_mode"):
        values.extend(_field_values(job.get(key)))
    boolean_remote = job.get("isRemote")
    if boolean_remote is True:
        values.append("remote")
    if any("telecommute" in value or value == "remote" for value in values):
        return "remote", "structured", ["结构化工作地点类型"]
    if any("hybrid" in value for value in values):
        return "hybrid", "structured", ["结构化工作方式"]
    if any("on-site" in value or "onsite" in value or "in office" in value for value in values):
        return "onsite", "structured", ["结构化工作方式"]
    if EXPLICIT_HYBRID_PATTERN.search(description):
        return "hybrid", "description_explicit", ["职位简介明确提及混合办公"]
    if EXPLICIT_REMOTE_PATTERN.search(description):
        return "remote", "description_explicit", ["职位简介明确提及远程工作"]
    if EXPLICIT_ONSITE_PATTERN.search(description):
        return "onsite", "description_explicit", ["职位简介明确提及现场办公"]
    return "unknown", "unknown", []


def _organization_name(job: dict[str, Any], fallback: str) -> str:
    value = job.get("hiringOrganization")
    if isinstance(value, dict):
        name = _plain_text(value.get("name"), 220)
        if any(character.isalnum() for character in name):
            return name
    return fallback


def _title(job: dict[str, Any]) -> str:
    title = _plain_text(job.get("title"), 240)
    return "" if title.casefold() in GENERIC_TITLES else title


def _employment_types(value: Any) -> list[str]:
    normalized: list[str] = []
    for item in _string_items(value, maximum=180):
        alias_key = re.sub(r"[^A-Z0-9]+", "_", item.upper()).strip("_")
        normalized.append(EMPLOYMENT_TYPE_ALIASES.get(alias_key, item))
    return _unique_items(normalized, limit=8)


def _emails_from_text(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    candidates = EMAIL_PATTERN.findall(html.unescape(value))
    results: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        email = _normalize_email(candidate)
        if not email or email in seen:
            continue
        seen.add(email)
        results.append(email)
    return results


def _normalize_email(value: str) -> str:
    email = html.unescape(value).strip().casefold()
    if email.startswith("mailto:"):
        email = unquote(email.removeprefix("mailto:").split("?", maxsplit=1)[0])
    email = email.strip('.,;:)]}>"')
    return email if EMAIL_PATTERN.fullmatch(email) else ""


def _contextual_description_emails(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    raw = html.unescape(value)
    results: list[str] = []
    seen: set[str] = set()
    for match in EMAIL_PATTERN.finditer(raw):
        context = (
            raw[max(0, match.start() - 360) : match.start()]
            + "[[EMAIL]]"
            + raw[match.end() : match.end() + 180]
        )
        if not _has_direct_application_email_context(context):
            continue
        email = _normalize_email(match.group(0))
        if email and email not in seen:
            seen.add(email)
            results.append(email)
    return results


def _email_matches_company_domain(email: str, company_domain: str) -> bool:
    if not company_domain or "@" not in email:
        return False
    email_domain = email.rsplit("@", maxsplit=1)[1]
    return email_domain == company_domain or email_domain.endswith(f".{company_domain}")


def _has_direct_application_email_context(value: str) -> bool:
    compact = _compact_text(re.sub(r"<[^>]+>", " ", value))
    if NON_CONTACT_EMAIL_CONTEXT_PATTERN.search(compact):
        return False
    compact = EMAIL_PATTERN.sub("[[EMAIL]]", compact)
    if "[[EMAIL]]" not in compact:
        compact = f"{compact} [[EMAIL]]"
    return DIRECT_APPLICATION_EMAIL_PATTERN.search(compact) is not None


def _mailto_contact_emails(response: str, company_domain: str) -> list[str]:
    parser = _MailtoAnchorParser()
    parser.feed(response)
    parser.close()
    results: list[str] = []
    seen: set[str] = set()
    for href, context in parser.links:
        email = _normalize_email(href)
        compact_context = _compact_text(context)
        if not email or email in seen:
            continue
        if not _email_matches_company_domain(email, company_domain):
            continue
        if not _has_direct_application_email_context(f"{compact_context} [[EMAIL]]"):
            continue
        seen.add(email)
        results.append(email)
        if len(results) == MAX_LIST_ITEMS:
            break
    return results


def _emails_from_contact_value(value: Any) -> list[str]:
    if isinstance(value, str):
        return _emails_from_text(value)
    if isinstance(value, list):
        return [email for item in value for email in _emails_from_contact_value(item)]
    if not isinstance(value, dict):
        return []
    results: list[str] = []
    for key, child in value.items():
        normalized_key = key.casefold().replace("_", "").replace("-", "")
        if normalized_key in {"email", "emailaddress", "emailid", "contactemail"}:
            results.extend(_emails_from_contact_value(child))
    return results


def _public_contact_emails(
    job: dict[str, Any], description_source: Any, page_contact_emails: Iterable[str]
) -> list[dict[str, str]]:
    contacts: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(values: Iterable[str], source: str) -> None:
        for email in values:
            if email in seen:
                continue
            seen.add(email)
            contacts.append({"email": email, "source": source})
            if len(contacts) == MAX_LIST_ITEMS:
                return

    for key in ("applicationContact", "applicationEmail"):
        add(_emails_from_contact_value(job.get(key)), "结构化职位联系人")
        if len(contacts) == MAX_LIST_ITEMS:
            return contacts
    add(_contextual_description_emails(description_source), "职位简介中的投递说明")
    if len(contacts) == MAX_LIST_ITEMS:
        return contacts
    add(page_contact_emails, "公开职位页投递链接")
    return contacts


def _job_record(
    job: dict[str, Any], reference: RawReference, page_contact_emails: Iterable[str] = ()
) -> dict[str, Any] | None:
    title = _title(job)
    if not title:
        return None
    description_source = job.get("description")
    description = _plain_text(description_source, MAX_DESCRIPTION_CHARS)
    sections = _sections_from_description(description_source) if description else []
    requirements = _unique_items(
        [
            *_string_items(job.get("qualifications")),
            *_string_items(job.get("skills")),
            *_string_items(job.get("experienceRequirements")),
            *_string_items(job.get("educationRequirements")),
            *_matching_sections(sections, REQUIREMENTS_HEADING),
        ]
    )
    responsibilities = _unique_items(
        [
            *_string_items(job.get("responsibilities")),
            *_matching_sections(sections, RESPONSIBILITIES_HEADING),
        ]
    )
    remote_status, remote_confidence, remote_evidence = _remote_fields(job, description)
    employment_types = _employment_types(job.get("employmentType"))
    public_contact_emails = _public_contact_emails(job, description_source, page_contact_emails)
    source_url = _safe_public_url(job.get("url"), reference.public_url) or reference.public_url
    raw_identifier = f"{source_url}\n{title.casefold()}"
    return {
        "body_truncated": reference.body_truncated,
        "captured_at": reference.captured_at,
        "company": _organization_name(job, reference.company_domain),
        "company_domain": reference.company_domain,
        "description": description,
        "employment_types": employment_types,
        "extraction_confidence": "structured",
        "extraction_method": "jsonld_jobposting",
        "id": hashlib.sha256(raw_identifier.encode("utf-8")).hexdigest()[:24],
        "location": _location(job.get("jobLocation")),
        "posted_at": _plain_text(job.get("datePosted"), 80),
        "public_contact_emails": public_contact_emails,
        "public_url": source_url,
        "remote_confidence": remote_confidence,
        "remote_evidence": remote_evidence,
        "remote_scope": _remote_scope(job.get("applicantLocationRequirements")),
        "remote_status": remote_status,
        "requirements": requirements,
        "responsibilities": responsibilities,
        "title": title,
        "valid_through": _plain_text(job.get("validThrough"), 80),
    }


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


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_catalog(rows: Iterable[dict[str, Any]], *, max_body_bytes: int) -> dict[str, Any]:
    references, stats = select_references(rows, max_body_bytes=max_body_bytes)
    jobs: dict[tuple[str, str], dict[str, Any]] = {}
    references_by_path: dict[Path, list[RawReference]] = {}
    for reference in references:
        references_by_path.setdefault(reference.raw_path, []).append(reference)

    for raw_path, path_references in references_by_path.items():
        if not raw_path.is_file():
            stats["skipped_missing_raw_file"] += len(path_references)
            continue
        try:
            handle = raw_path.open("rb")
        except OSError:
            stats["skipped_raw_read_error"] += len(path_references)
            continue
        with handle:
            for reference in path_references:
                try:
                    handle.seek(reference.raw_offset)
                    line = handle.readline()
                    raw = json.loads(line.decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    stats["skipped_raw_read_error"] += 1
                    continue
                if not isinstance(raw, dict):
                    stats["skipped_invalid_raw_row"] += 1
                    continue
                row_url = _safe_public_url(
                    raw.get("canonical_url"), raw.get("final_url"), raw.get("requested_url")
                )
                if row_url and row_url != reference.public_url:
                    stats["skipped_reference_mismatch"] += 1
                    continue
                response = raw.get("response_text")
                if not isinstance(response, str) or not response.strip():
                    stats["skipped_missing_content"] += 1
                    continue
                stats["raw_rows_loaded"] += 1
                values, script_count, parse_errors = _parse_jsonld_scripts(response)
                page_contact_emails = _mailto_contact_emails(response, reference.company_domain)
                stats["jsonld_scripts"] += script_count
                stats["jsonld_parse_errors"] += parse_errors
                for value in values:
                    for posting in _job_postings(value):
                        stats["jobposting_objects"] += 1
                        record = _job_record(posting, reference, page_contact_emails)
                        if record is None:
                            stats["skipped_generic_title"] += 1
                            continue
                        errors = _validation_errors(record)
                        if errors:
                            stats["skipped_unsafe"] += 1
                            continue
                        key = (str(record["public_url"]), str(record["title"]).casefold())
                        if key in jobs:
                            stats["duplicate_jobs"] += 1
                            continue
                        jobs[key] = record

    records = sorted(
        jobs.values(),
        key=lambda job: (
            str(job.get("company") or "").casefold(),
            str(job.get("title") or "").casefold(),
        ),
    )
    remote_counts = Counter(str(job["remote_status"]) for job in records)
    catalog = {
        "jobs": records,
        "metadata": {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "source": "complete, schema.org JobPosting recruitment-page captures",
            "notes": "Descriptions and sections are bounded extracted fields, not raw page bodies.",
        },
        "stats": {
            **dict(sorted(stats.items())),
            "exported_jobs": len(records),
            "jobs_with_description": sum(1 for job in records if job["description"]),
            "jobs_with_public_contact_emails": sum(
                1 for job in records if job["public_contact_emails"]
            ),
            "jobs_with_requirements": sum(1 for job in records if job["requirements"]),
            "public_contact_email_count": sum(len(job["public_contact_emails"]) for job in records),
            "remote_status_counts": dict(sorted(remote_counts.items())),
        },
    }
    errors = _validation_errors(catalog)
    if errors:
        preview = "; ".join(errors[:5])
        raise ValueError(f"structured job catalog safety validation failed: {preview}")
    return catalog


def run_export(input_path: Path, output_path: Path, *, max_body_bytes: int) -> dict[str, Any]:
    catalog = build_catalog(_iter_jsonl(input_path), max_body_bytes=max_body_bytes)
    _write_json_atomic(output_path, catalog)
    return {
        "exported_jobs": catalog["stats"]["exported_jobs"],
        "output_file": output_path.name,
        "schema_version": CATALOG_SCHEMA_VERSION,
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-body-bytes", type=int, default=DEFAULT_MAX_BODY_BYTES)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        summary = run_export(args.input, args.output, max_body_bytes=args.max_body_bytes)
    except (OSError, ValueError) as error:
        print(f"structured job catalog export failed: {error}")
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
