from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type PathPart = tuple[str, str | int]

REPORT_VERSION = 2
SCAN_SCOPE = "technical_patterns_only"
TOOL_NAME = "careerops-d0-scan"
TOOL_VERSION = "2.1.0"
SCANNER_IMPLEMENTATION_VERSION = "opaque-locator-artifact-bound-v3"
RULE_SET_VERSION = "d0-technical-scan-rules-v3"
ALLOWED_SUPPRESSION_REASONS = frozenset(
    {
        "documented_false_positive",
        "public_recruiting_contact",
        "synthetic_test_fixture",
    }
)

MAX_SCAN_ROWS = 10_000
MAX_SCAN_NODES = 100_000
MAX_SCAN_DEPTH = 64
MAX_STRING_CHARS = 1_000_000
MAX_TOTAL_STRING_CHARS = 64 * 1024 * 1024
MAX_SCAN_FINDINGS = 50_000

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_PUBLIC_CONTACT_RULES = frozenset(
    {
        "d0.pii.email_like.v2",
        "d0.pii.phone_like.v2",
    }
)


class D0TechnicalScanError(ValueError):
    """Raised when D0 technical scan input or suppression evidence is invalid."""


@dataclass(frozen=True, slots=True)
class D0ScanFinding:
    finding_id: str
    rule_id: str
    target_ref: str


@dataclass(frozen=True, slots=True)
class D0SuppressionInput:
    finding_id: str
    rule_id: str
    target_ref: str
    reason_code: str
    reviewer_id: str
    reviewed_at: str
    evidence_ref: str
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class D0ReviewedSuppression:
    finding_id: str
    rule_id: str
    target_ref: str
    reason_code: str
    reviewer_id: str
    reviewed_at: str
    evidence_ref: str
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class D0ScanReport:
    version: Literal[2]
    scan_scope: Literal["technical_patterns_only"]
    rule_set_version: str
    rule_set_sha256: str
    findings: tuple[D0ScanFinding, ...]
    reviewed_suppressions: tuple[D0ReviewedSuppression, ...]

    @property
    def findings_count(self) -> int:
        return len(self.findings)


@dataclass(frozen=True, slots=True)
class _ScanRule:
    rule_id: str
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class _FindingContext:
    row_index: int


_RULES = (
    _ScanRule(
        "d0.credential.private_key.v2",
        re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    ),
    _ScanRule("d0.credential.aws_access_key.v2", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    _ScanRule("d0.credential.github_token.v2", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    _ScanRule(
        "d0.credential.github_fine_grained_token.v2",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"),
    ),
    _ScanRule("d0.credential.google_api_key.v2", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    _ScanRule("d0.credential.slack_token.v2", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{20,}\b")),
    _ScanRule(
        "d0.pii.email_like.v2",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    _ScanRule(
        "d0.pii.phone_like.v2",
        re.compile(r"(?<!\w)(?:\+?\d[\d .()/-]{7,}\d)(?!\w)"),
    ),
)

RULE_SET_SHA256 = hashlib.sha256(
    json.dumps(
        {
            "implementation": SCANNER_IMPLEMENTATION_VERSION,
            "version": RULE_SET_VERSION,
            "rules": tuple(
                {
                    "rule_id": rule.rule_id,
                    "pattern": rule.pattern.pattern,
                    "flags": rule.pattern.flags,
                }
                for rule in _RULES
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
).hexdigest()


def scan_rows(
    rows: object,
    *,
    suppressions: Sequence[D0SuppressionInput] = (),
    artifact_sha256: str | None = None,
) -> D0ScanReport:
    """Scan strict JSON rows without persisting matched text or caller-controlled locators."""

    scan_input = _coerce_rows(rows)
    suppression_items = _coerce_suppressions(suppressions)
    artifact_binding = _artifact_binding(artifact_sha256, scan_input)
    findings_with_context: list[tuple[D0ScanFinding, _FindingContext]] = []
    for row_index, row in enumerate(scan_input):
        for finding in _scan_value(row, row_index, artifact_binding):
            findings_with_context.append((finding, _FindingContext(row_index=row_index)))
            if len(findings_with_context) > MAX_SCAN_FINDINGS:
                raise D0TechnicalScanError("technical scan exceeds the finding budget")
    findings = tuple(
        sorted(
            (finding for finding, _context in findings_with_context),
            key=lambda finding: (finding.rule_id, finding.target_ref, finding.finding_id),
        )
    )
    if len({finding.finding_id for finding in findings}) != len(findings):
        raise D0TechnicalScanError("scanner produced a duplicate finding identifier")

    contexts = {finding.finding_id: context for finding, context in findings_with_context}
    reviewed_suppressions = _review_suppressions(
        suppression_items,
        findings,
        contexts,
        scan_input,
    )
    suppressed_ids = {suppression.finding_id for suppression in reviewed_suppressions}
    return D0ScanReport(
        version=REPORT_VERSION,
        scan_scope=SCAN_SCOPE,
        rule_set_version=RULE_SET_VERSION,
        rule_set_sha256=RULE_SET_SHA256,
        findings=tuple(finding for finding in findings if finding.finding_id not in suppressed_ids),
        reviewed_suppressions=reviewed_suppressions,
    )


def _artifact_binding(
    value: object,
    rows: tuple[dict[str, JsonValue], ...],
) -> str:
    if value is None:
        return hashlib.sha256(
            json.dumps(
                rows,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise D0TechnicalScanError("artifact_sha256 must be lowercase SHA-256 hex")
    return value


def _coerce_suppressions(value: object) -> tuple[D0SuppressionInput, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise D0TechnicalScanError("suppressions must be a sequence")
    items = tuple(cast(Sequence[object], value))
    if any(not isinstance(item, D0SuppressionInput) for item in items):
        raise D0TechnicalScanError("suppression entries must be D0SuppressionInput values")
    return cast(tuple[D0SuppressionInput, ...], items)


def _scan_value(
    value: JsonValue,
    row_index: int,
    artifact_binding: str,
) -> tuple[D0ScanFinding, ...]:
    findings: list[D0ScanFinding] = []
    for text, path in _iter_text_values(value):
        target_ref = _opaque_target_ref(row_index, path)
        for rule in _RULES:
            for ordinal, _match in enumerate(rule.pattern.finditer(text)):
                findings.append(
                    _finding(
                        rule_id=rule.rule_id,
                        target_ref=target_ref,
                        match_ordinal=ordinal,
                        artifact_binding=artifact_binding,
                    )
                )
    return tuple(findings)


def _iter_text_values(value: JsonValue) -> Iterable[tuple[str, tuple[PathPart, ...]]]:
    stack: list[tuple[JsonValue, tuple[PathPart, ...], int]] = [(value, (), 0)]
    visited = 0
    while stack:
        current, path, depth = stack.pop()
        visited += 1
        if visited > MAX_SCAN_NODES:
            raise D0TechnicalScanError("scan input exceeds the node budget")
        if depth > MAX_SCAN_DEPTH:
            raise D0TechnicalScanError("scan input exceeds the depth budget")
        if isinstance(current, str):
            yield current, path
            continue
        if isinstance(current, list):
            for index in range(len(current) - 1, -1, -1):
                stack.append((current[index], (*path, ("index", index)), depth + 1))
            continue
        if isinstance(current, dict):
            sorted_keys = sorted(current)
            for key_index in range(len(sorted_keys) - 1, -1, -1):
                key = sorted_keys[key_index]
                stack.append(
                    (
                        current[key],
                        (*path, ("member", key)),
                        depth + 1,
                    )
                )
                stack.append(
                    (
                        key,
                        (*path, ("member_name", key_index)),
                        depth + 1,
                    )
                )


def _opaque_target_ref(row_index: int, path: tuple[PathPart, ...]) -> str:
    locator = hashlib.sha256(
        json.dumps(path, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"row:{row_index}:locator:{locator}"


def _finding(
    *,
    rule_id: str,
    target_ref: str,
    match_ordinal: int,
    artifact_binding: str,
) -> D0ScanFinding:
    finding_id = hashlib.sha256(
        json.dumps(
            {
                "artifact_sha256": artifact_binding,
                "match_ordinal": match_ordinal,
                "rule_id": rule_id,
                "target_ref": target_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return D0ScanFinding(finding_id=finding_id, rule_id=rule_id, target_ref=target_ref)


def _review_suppressions(
    suppressions: tuple[D0SuppressionInput, ...],
    findings: tuple[D0ScanFinding, ...],
    contexts: dict[str, _FindingContext],
    rows: tuple[dict[str, JsonValue], ...],
) -> tuple[D0ReviewedSuppression, ...]:
    findings_by_id = {finding.finding_id: finding for finding in findings}
    reviewed: list[D0ReviewedSuppression] = []
    seen: set[str] = set()

    for suppression in suppressions:
        if suppression.finding_id in seen:
            raise D0TechnicalScanError("duplicate suppression finding identifier")
        seen.add(suppression.finding_id)

        finding = findings_by_id.get(suppression.finding_id)
        if finding is None:
            raise D0TechnicalScanError("orphan suppression finding identifier")
        if suppression.rule_id != finding.rule_id or suppression.target_ref != finding.target_ref:
            raise D0TechnicalScanError("suppression does not bind its finding")
        if suppression.reason_code not in ALLOWED_SUPPRESSION_REASONS:
            raise D0TechnicalScanError("unknown suppression reason_code")
        _validate_reason_policy(suppression, contexts[finding.finding_id], rows)
        for field_name in ("reviewer_id", "reviewed_at", "evidence_ref"):
            value = getattr(suppression, field_name)
            if not isinstance(value, str) or not value.strip():
                raise D0TechnicalScanError(f"suppression {field_name} must be non-empty")
        _parse_review_time(suppression.reviewed_at)
        if _SHA256_RE.fullmatch(suppression.evidence_sha256) is None:
            raise D0TechnicalScanError("suppression evidence_sha256 must be lowercase SHA-256 hex")

        reviewed.append(
            D0ReviewedSuppression(
                finding_id=suppression.finding_id,
                rule_id=suppression.rule_id,
                target_ref=suppression.target_ref,
                reason_code=suppression.reason_code,
                reviewer_id=suppression.reviewer_id,
                reviewed_at=suppression.reviewed_at,
                evidence_ref=suppression.evidence_ref,
                evidence_sha256=suppression.evidence_sha256,
            )
        )

    return tuple(sorted(reviewed, key=lambda suppression: suppression.finding_id))


def _validate_reason_policy(
    suppression: D0SuppressionInput,
    context: _FindingContext,
    rows: tuple[dict[str, JsonValue], ...],
) -> None:
    if (
        suppression.reason_code == "public_recruiting_contact"
        and suppression.rule_id not in _PUBLIC_CONTACT_RULES
    ):
        raise D0TechnicalScanError("public_recruiting_contact cannot suppress this rule family")
    if suppression.reason_code == "synthetic_test_fixture":
        row = rows[context.row_index]
        if row.get("synthetic") is not True or row.get("split") != "development":
            raise D0TechnicalScanError(
                "synthetic fixture suppression requires a development fixture"
            )


def _parse_review_time(value: str) -> None:
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise D0TechnicalScanError("suppression reviewed_at must be offset-aware ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise D0TechnicalScanError("suppression reviewed_at must be offset-aware ISO-8601")


def _coerce_rows(rows: object) -> tuple[dict[str, JsonValue], ...]:
    if not isinstance(rows, list | tuple):
        raise D0TechnicalScanError("rows must be a list or tuple")
    sequence = cast(Sequence[object], rows)
    if len(sequence) > MAX_SCAN_ROWS:
        raise D0TechnicalScanError("scan input exceeds the row budget")
    coerced: list[dict[str, JsonValue]] = []
    total_nodes = 0
    total_string_chars = 0
    for index, row in enumerate(sequence):
        if not isinstance(row, dict):
            raise D0TechnicalScanError(f"row {index} must be an object")
        row_nodes, row_string_chars = _assert_json_value(cast(dict[Any, Any], row), row_index=index)
        total_nodes += row_nodes
        total_string_chars += row_string_chars
        if total_nodes > MAX_SCAN_NODES:
            raise D0TechnicalScanError("scan input exceeds the total node budget")
        if total_string_chars > MAX_TOTAL_STRING_CHARS:
            raise D0TechnicalScanError("scan input exceeds the total string budget")
        coerced.append(cast(dict[str, JsonValue], row))
    return tuple(coerced)


def _assert_json_value(value: Any, *, row_index: int) -> tuple[int, int]:
    stack: list[tuple[Any, int]] = [(value, 0)]
    visited = 0
    string_chars = 0
    while stack:
        current, depth = stack.pop()
        visited += 1
        if visited > MAX_SCAN_NODES:
            raise D0TechnicalScanError(f"row {row_index} exceeds the node budget")
        if depth > MAX_SCAN_DEPTH:
            raise D0TechnicalScanError(f"row {row_index} exceeds the depth budget")
        if current is None or isinstance(current, bool | int):
            continue
        if isinstance(current, str):
            if len(current) > MAX_STRING_CHARS:
                raise D0TechnicalScanError(f"row {row_index} contains an oversized string")
            string_chars += len(current)
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise D0TechnicalScanError(f"row {row_index} contains a non-finite number")
            continue
        if isinstance(current, list):
            stack.extend((item, depth + 1) for item in cast(list[Any], current))
            continue
        if isinstance(current, dict):
            for key, item in cast(dict[Any, Any], current).items():
                if not isinstance(key, str):
                    raise D0TechnicalScanError(f"row {row_index} object keys must be strings")
                if len(key) > MAX_STRING_CHARS:
                    raise D0TechnicalScanError(f"row {row_index} contains an oversized object key")
                string_chars += len(key)
                stack.append((item, depth + 1))
            continue
        raise D0TechnicalScanError(f"row {row_index} contains a non-JSON value")
    return visited, string_chars
