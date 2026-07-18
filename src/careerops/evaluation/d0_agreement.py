from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from os import PathLike
from pathlib import Path
from typing import Any, cast

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EVIDENCE_REF_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?=.*\S)[^:\\\x00]+$")
_MAX_ROWS = 10_000
_MAX_JSON_NODES = 100_000
_MAX_JSON_DEPTH = 64
_MAX_STRING_CHARS = 1024 * 1024
_MAX_TOTAL_STRING_CHARS = 64 * 1024 * 1024
_MAX_PARSE_BYTES = 64 * 1024 * 1024
_MAX_PARSE_CHARS = 64 * 1024 * 1024


class D0LabelReviewError(ValueError):
    """Raised when D0 label-review evidence is malformed or unverifiable."""


class AdjudicationStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    ADJUDICATED = "adjudicated"


@dataclass(frozen=True, slots=True)
class LabelEvidence:
    sample_id: str
    primary_annotator_id: str
    primary_label: str
    primary_value: JsonValue
    secondary_annotator_id: str | None
    secondary_label: str | None
    secondary_value: JsonValue | None
    adjudication_status: AdjudicationStatus
    adjudicator_id: str | None
    final_label: str | None
    final_value: JsonValue | None
    reason: str | None
    evidence_ref: str | None
    evidence_sha256: str | None

    def __post_init__(self) -> None:
        if not self.sample_id.strip():
            raise D0LabelReviewError("sample_id must be a non-empty string")
        if not self.primary_annotator_id.strip():
            raise D0LabelReviewError("primary annotator_id must be a non-empty string")
        if not self.primary_label.strip():
            raise D0LabelReviewError("primary_label must be a non-empty string")
        _assert_json_value(self.primary_value, context="primary_value")

        if (self.secondary_annotator_id is None) != (self.secondary_label is None) or (
            self.secondary_annotator_id is None and self.secondary_value is not None
        ):
            raise D0LabelReviewError("partial secondary evidence")
        if self.secondary_label is not None and not self.secondary_label.strip():
            raise D0LabelReviewError("secondary_label must be a non-empty string")
        if self.secondary_annotator_id is not None:
            if not self.secondary_annotator_id.strip():
                raise D0LabelReviewError("secondary annotator_id must be a non-empty string")
            if self.secondary_annotator_id.strip() == self.primary_annotator_id.strip():
                raise D0LabelReviewError(
                    "secondary annotator_id must differ from primary annotator_id"
                )
            _assert_json_value(self.secondary_value, context="secondary_value")

        adjudication_fields = (
            self.adjudicator_id,
            self.final_label,
            self.reason,
            self.evidence_ref,
            self.evidence_sha256,
        )
        has_adjudication = self.final_value is not None or any(
            value is not None for value in adjudication_fields
        )
        complete_adjudication = all(
            isinstance(value, str) and bool(value.strip()) for value in adjudication_fields
        )
        if self.adjudication_status is AdjudicationStatus.ADJUDICATED:
            if not self.is_disagreement:
                raise D0LabelReviewError(
                    "adjudicated status requires a double-labeled disagreement"
                )
            if not complete_adjudication:
                raise D0LabelReviewError("adjudicated disagreement requires complete evidence")
            _assert_json_value(self.final_value, context="final_value")
            if _EVIDENCE_REF_RE.fullmatch(cast(str, self.evidence_ref)) is None:
                raise D0LabelReviewError(
                    "adjudication evidence_ref must be a safe repository-relative path"
                )
            if _SHA256_RE.fullmatch(cast(str, self.evidence_sha256)) is None:
                raise D0LabelReviewError(
                    "adjudication evidence_sha256 must be lowercase SHA-256 hex"
                )
        elif has_adjudication:
            raise D0LabelReviewError("non-adjudicated row must not contain adjudication evidence")

        if self.is_disagreement and self.adjudication_status is AdjudicationStatus.NOT_REQUIRED:
            raise D0LabelReviewError("disagreement cannot use not_required status")

    @property
    def is_double_labeled(self) -> bool:
        return self.secondary_annotator_id is not None

    @property
    def is_disagreement(self) -> bool:
        if not self.is_double_labeled:
            return False
        return self.primary_label != self.secondary_label or _canonical_json(
            self.primary_value
        ) != _canonical_json(self.secondary_value)


@dataclass(frozen=True, slots=True)
class AgreementMetrics:
    sample_count: int
    double_labeled_count: int
    categorical_kappa: float | None
    structured_exact_agreement: float | None
    disagreement_count: int
    all_disagreements_adjudicated: bool


def load_label_review_evidence(path: Path) -> tuple[LabelEvidence, ...]:
    """Load strict JSON, JSON array, or JSONL D0 label-review evidence."""

    return parse_label_review_evidence(_read_bounded_utf8(path))


def parse_label_review_evidence(
    text: str,
    *,
    source: str = "label review evidence",
) -> tuple[LabelEvidence, ...]:
    """Parse label-review evidence from one already-bound text snapshot."""

    if len(text) > _MAX_PARSE_CHARS:
        raise D0LabelReviewError(f"{source}: evidence text exceeds maximum size")
    try:
        rows = _parse_rows(text, source=source)
    except D0LabelReviewError:
        raise
    except RecursionError as exc:
        raise D0LabelReviewError(f"{source}: JSON nesting exceeds maximum depth") from exc
    except UnicodeError as exc:
        raise D0LabelReviewError(f"{source}: invalid text encoding") from exc
    return _rows_to_evidence(rows)


def calculate_agreement(rows: tuple[LabelEvidence, ...]) -> AgreementMetrics:
    seen_ids: set[str] = set()
    for index, row in enumerate(rows):
        if row.sample_id in seen_ids:
            raise D0LabelReviewError(f"duplicate sample_id at row {index}")
        seen_ids.add(row.sample_id)

    double_labeled = tuple(row for row in rows if row.is_double_labeled)
    disagreement_count = 0
    adjudicated_disagreements = 0

    for row in double_labeled:
        if row.secondary_label is None:
            raise D0LabelReviewError("partial secondary evidence")
        if row.is_disagreement:
            disagreement_count += 1
            if row.adjudication_status is AdjudicationStatus.ADJUDICATED:
                adjudicated_disagreements += 1

    return AgreementMetrics(
        sample_count=len(rows),
        double_labeled_count=len(double_labeled),
        categorical_kappa=_cohens_kappa(double_labeled),
        structured_exact_agreement=_structured_exact_agreement(double_labeled),
        disagreement_count=disagreement_count,
        all_disagreements_adjudicated=disagreement_count == adjudicated_disagreements,
    )


def _parse_rows(text: str, *, source: str) -> list[dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        raise D0LabelReviewError(f"{source} is empty")

    if stripped[0] in "[{":
        parsed = _loads_strict_or_jsonl(stripped, source)
        if isinstance(parsed, _JsonlRows):
            return parsed.rows
        if isinstance(parsed, list):
            return _coerce_row_list(cast(list[Any], parsed), source)
        if isinstance(parsed, dict):
            parsed_dict = cast(dict[str, Any], parsed)
            rows: object | None = parsed_dict.get("rows")
            if rows is None:
                return _coerce_row_list([parsed_dict], source)
            unexpected_keys = set(parsed_dict) - {"rows", "metrics", "declared_metrics"}
            if unexpected_keys:
                raise D0LabelReviewError(f"{source}: unexpected top-level keys")
            if not isinstance(rows, list):
                raise D0LabelReviewError(f"{source}: rows must be an array")
            return _coerce_row_list(cast(list[Any], rows), source)
        raise D0LabelReviewError(f"{source}: top-level JSON must be an object or array")

    return _parse_jsonl(text, source)


@dataclass(frozen=True, slots=True)
class _JsonlRows:
    rows: list[dict[str, Any]]


def _loads_strict_or_jsonl(text: str, source: str) -> Any | _JsonlRows:
    try:
        return _loads_strict(text, source)
    except D0LabelReviewError as exc:
        if "\n" not in text or "invalid JSON: Extra data" not in str(exc):
            raise
        return _JsonlRows(_parse_jsonl(text, source))


def _parse_jsonl(text: str, source: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total_nodes = 0
    total_string_chars = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(rows) >= _MAX_ROWS:
            raise D0LabelReviewError(f"{source}: row count exceeds maximum")
        parsed = _loads_strict(line, f"{source}:{line_number}")
        row_nodes, row_string_chars = _json_value_size(parsed, context=f"{source}:{line_number}")
        total_nodes += row_nodes
        total_string_chars += row_string_chars
        if total_nodes > _MAX_JSON_NODES:
            raise D0LabelReviewError(f"{source} exceeds maximum JSON node count")
        if total_string_chars > _MAX_TOTAL_STRING_CHARS:
            raise D0LabelReviewError(f"{source} exceeds maximum total string length")
        if not isinstance(parsed, dict):
            raise D0LabelReviewError(f"{source}:{line_number}: JSONL row must be an object")
        rows.append(cast(dict[str, Any], parsed))
    return rows


def _loads_strict(text: str, source: str) -> Any:
    if len(text) > _MAX_PARSE_CHARS:
        raise D0LabelReviewError(f"{source}: evidence text exceeds maximum size")
    try:
        parsed = json.loads(
            text,
            parse_constant=lambda value: _reject_json_constant(value, source),
            object_pairs_hook=lambda pairs: _object_without_duplicate_keys(pairs, source),
        )
        _assert_json_value(parsed, context=source)
        return parsed
    except json.JSONDecodeError as exc:
        raise D0LabelReviewError(f"{source}: invalid JSON: {exc.msg}") from exc
    except RecursionError as exc:
        raise D0LabelReviewError(f"{source}: JSON nesting exceeds maximum depth") from exc


def _reject_json_constant(value: str, source: str) -> None:
    raise D0LabelReviewError(f"{source}: non-standard JSON numeric constant rejected: {value}")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]], source: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if len(key) > _MAX_STRING_CHARS:
            raise D0LabelReviewError(f"{source}: JSON string exceeds maximum length")
        if key in parsed:
            raise D0LabelReviewError(f"{source}: duplicate JSON object key")
        parsed[key] = value
    return parsed


def _coerce_row_list(rows: list[Any], source: str) -> list[dict[str, Any]]:
    if len(rows) > _MAX_ROWS:
        raise D0LabelReviewError(f"{source}: row count exceeds maximum")
    coerced: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise D0LabelReviewError(f"{source}: row {index} must be an object")
        coerced.append(cast(dict[str, Any], row))
    return coerced


def _rows_to_evidence(rows: list[dict[str, Any]]) -> tuple[LabelEvidence, ...]:
    evidence = tuple(_row_to_evidence(row, index) for index, row in enumerate(rows))
    sample_ids: set[str] = set()
    for index, row in enumerate(evidence):
        if row.sample_id in sample_ids:
            raise D0LabelReviewError(f"duplicate sample_id at row {index}")
        sample_ids.add(row.sample_id)
    return evidence


def _row_to_evidence(row: dict[str, Any], index: int) -> LabelEvidence:
    allowed_keys = {
        "sample_id",
        "primary",
        "secondary",
        "adjudication_status",
        "adjudication",
    }
    unexpected_keys = set(row) - allowed_keys
    if unexpected_keys:
        raise D0LabelReviewError(f"row {index}: unexpected keys")

    sample_id = _required_string(row, "sample_id", f"row {index}")
    primary_annotator_id, primary_label, primary_value = _label_value(
        row.get("primary"), f"row {index}.primary"
    )
    secondary = row.get("secondary")
    if secondary is None:
        secondary_annotator_id = None
        secondary_label = None
        secondary_value = None
    else:
        secondary_annotator_id, secondary_label, secondary_value = _label_value(
            secondary, f"row {index}.secondary"
        )

    status_value = _required_string(row, "adjudication_status", f"row {index}")
    try:
        status = AdjudicationStatus(status_value)
    except ValueError as exc:
        raise D0LabelReviewError(f"row {index}: unrecognized adjudication_status") from exc

    if status is AdjudicationStatus.ADJUDICATED:
        if "adjudication" not in row:
            raise D0LabelReviewError(
                f"row {index}: adjudicated disagreement requires adjudication evidence"
            )
        (
            adjudicator_id,
            final_label,
            final_value,
            reason,
            evidence_ref,
            evidence_sha256,
        ) = _adjudication_value(row.get("adjudication"), f"row {index}.adjudication")
    else:
        if "adjudication" in row:
            raise D0LabelReviewError(
                f"row {index}: non-adjudicated row must not contain adjudication evidence"
            )
        adjudicator_id = None
        final_label = None
        final_value = None
        reason = None
        evidence_ref = None
        evidence_sha256 = None

    return LabelEvidence(
        sample_id=sample_id,
        primary_annotator_id=primary_annotator_id,
        primary_label=primary_label,
        primary_value=primary_value,
        secondary_annotator_id=secondary_annotator_id,
        secondary_label=secondary_label,
        secondary_value=secondary_value,
        adjudication_status=status,
        adjudicator_id=adjudicator_id,
        final_label=final_label,
        final_value=final_value,
        reason=reason,
        evidence_ref=evidence_ref,
        evidence_sha256=evidence_sha256,
    )


def _required_string(row: dict[str, Any], key: str, context: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise D0LabelReviewError(f"{context}: {key} must be a non-empty string")
    return value


def _label_value(raw: Any, context: str) -> tuple[str, str, JsonValue]:
    if not isinstance(raw, dict):
        raise D0LabelReviewError(f"{context} must be an object")
    raw = cast(dict[str, Any], raw)
    allowed_keys = {"annotator_id", "label", "value"}
    if set(raw) != allowed_keys:
        raise D0LabelReviewError(f"{context} must contain exactly annotator_id, label, and value")
    annotator_id = raw.get("annotator_id")
    if not isinstance(annotator_id, str) or not annotator_id.strip():
        raise D0LabelReviewError(f"{context}.annotator_id must be a non-empty string")
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        raise D0LabelReviewError(f"{context}.label must be a non-empty string")
    value = raw.get("value")
    _assert_json_value(value, context=f"{context}.value")
    return annotator_id, label, value


def _adjudication_value(raw: Any, context: str) -> tuple[str, str, JsonValue, str, str, str]:
    if not isinstance(raw, dict):
        raise D0LabelReviewError(f"{context} must be an object")
    raw = cast(dict[str, Any], raw)
    allowed_keys = {
        "adjudicator_id",
        "final_label",
        "final_value",
        "reason",
        "evidence_ref",
        "evidence_sha256",
    }
    if set(raw) != allowed_keys:
        raise D0LabelReviewError(
            f"{context} must contain exactly adjudicator_id, final_label, final_value, "
            "reason, evidence_ref, and evidence_sha256"
        )
    adjudicator_id = _required_string(raw, "adjudicator_id", context)
    final_label = _required_string(raw, "final_label", context)
    final_value = raw.get("final_value")
    _assert_json_value(final_value, context=f"{context}.final_value")
    reason = _required_string(raw, "reason", context)
    evidence_ref = _required_string(raw, "evidence_ref", context)
    evidence_sha256 = _required_string(raw, "evidence_sha256", context)
    return (
        adjudicator_id,
        final_label,
        final_value,
        reason,
        evidence_ref,
        evidence_sha256,
    )


def _read_bounded_utf8(path: Path | PathLike[str] | str) -> str:
    try:
        candidate = Path(path)
        stat = candidate.stat()
        if stat.st_size > _MAX_PARSE_BYTES:
            raise D0LabelReviewError("label review evidence file exceeds maximum size")
        with candidate.open("rb") as handle:
            payload = handle.read(_MAX_PARSE_BYTES + 1)
    except D0LabelReviewError:
        raise
    except OSError as exc:
        raise D0LabelReviewError("unable to read label review evidence") from exc

    if len(payload) > _MAX_PARSE_BYTES:
        raise D0LabelReviewError("label review evidence file exceeds maximum size")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise D0LabelReviewError("label review evidence must be UTF-8") from exc


def _assert_json_value(value: Any, *, context: str) -> None:
    _json_value_size(value, context=context)


def _json_value_size(value: Any, *, context: str) -> tuple[int, int]:
    stack: list[tuple[Any, int, tuple[int, ...]]] = [(value, 0, ())]
    nodes = 0
    total_string_chars = 0

    while stack:
        item, depth, ancestors = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise D0LabelReviewError(f"{context} exceeds maximum JSON node count")
        if depth > _MAX_JSON_DEPTH:
            raise D0LabelReviewError(f"{context} exceeds maximum JSON depth")

        if item is None or isinstance(item, bool | int):
            continue
        if isinstance(item, str):
            string_length = len(item)
            if string_length > _MAX_STRING_CHARS:
                raise D0LabelReviewError(f"{context} contains a string exceeding maximum length")
            total_string_chars += string_length
            if total_string_chars > _MAX_TOTAL_STRING_CHARS:
                raise D0LabelReviewError(f"{context} exceeds maximum total string length")
            continue
        if isinstance(item, float):
            if math.isfinite(item):
                continue
            raise D0LabelReviewError(f"{context} contains a non-finite number")
        if isinstance(item, list):
            item_list = cast(list[Any], item)
            container_id = id(item_list)
            if container_id in ancestors:
                raise D0LabelReviewError(f"{context} contains a cyclic JSON container")
            child_ancestors = (*ancestors, container_id)
            stack.extend((child, depth + 1, child_ancestors) for child in item_list)
            continue
        if isinstance(item, dict):
            item_dict = cast(dict[Any, Any], item)
            container_id = id(item_dict)
            if container_id in ancestors:
                raise D0LabelReviewError(f"{context} contains a cyclic JSON container")
            child_ancestors = (*ancestors, container_id)
            for key, child in item_dict.items():
                if not isinstance(key, str):
                    raise D0LabelReviewError(f"{context} object keys must be strings")
                key_length = len(key)
                if key_length > _MAX_STRING_CHARS:
                    raise D0LabelReviewError(
                        f"{context} contains an object key exceeding maximum length"
                    )
                total_string_chars += key_length
                if total_string_chars > _MAX_TOTAL_STRING_CHARS:
                    raise D0LabelReviewError(f"{context} exceeds maximum total string length")
                stack.append((child, depth + 1, child_ancestors))
            continue
        raise D0LabelReviewError(f"{context} is not a JSON value")
    return nodes, total_string_chars


def _cohens_kappa(rows: tuple[LabelEvidence, ...]) -> float | None:
    if not rows:
        return None

    observed_matches = sum(1 for row in rows if row.primary_label == row.secondary_label)
    observed_agreement = observed_matches / len(rows)
    primary_counts = Counter(row.primary_label for row in rows)
    secondary_counts = Counter(_secondary_label(row) for row in rows)
    labels = set(primary_counts) | set(secondary_counts)
    expected_agreement = sum(
        (primary_counts[label] / len(rows)) * (secondary_counts[label] / len(rows))
        for label in labels
    )
    denominator = 1 - expected_agreement
    if denominator == 0:
        return None
    return (observed_agreement - expected_agreement) / denominator


def _secondary_label(row: LabelEvidence) -> str:
    if row.secondary_label is None:
        raise D0LabelReviewError("partial secondary evidence")
    return row.secondary_label


def _structured_exact_agreement(rows: tuple[LabelEvidence, ...]) -> float | None:
    if not rows:
        return None
    matches = sum(
        1
        for row in rows
        if _canonical_json(row.primary_value) == _canonical_json(row.secondary_value)
    )
    return matches / len(rows)


def _canonical_json(value: JsonValue) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
