#!/usr/bin/env python3
"""Verify CareerOps M-1 contracts and, by default, real pilot evidence."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from careerops.evaluation import (  # noqa: E402
    REPORT_VERSION,
    RULE_SET_SHA256,
    RULE_SET_VERSION,
    SCAN_SCOPE,
    TOOL_NAME,
    TOOL_VERSION,
    AdjudicationStatus,
    D0LabelReviewError,
    D0LeakageError,
    D0SuppressionInput,
    D0TechnicalScanError,
    analyze_d0_leakage,
    calculate_agreement,
    parse_label_review_evidence,
    scan_rows,
)

SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
JSON_SCHEMA_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)
URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")
BAD_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
SPLITS = ("development", "validation", "holdout")
D0_MANIFEST_SCHEMA_PATH = "datasets/schemas/d0_dataset_manifest.schema.json"
D0_SCAN_SCHEMA_PATH = "datasets/schemas/d0_scan_report.schema.json"
D0_LABEL_REVIEW_SCHEMA_PATH = "datasets/schemas/d0_label_review.schema.json"
MAX_JSON_FILE_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_FILE_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_ROWS = 10_000
MAX_REPO_PATH_CHARS = 512
MAX_SCAN_FINDINGS = 50_000
MAX_REVIEWED_SUPPRESSIONS = 1_000
MAX_SUPPRESSION_EVIDENCE_BYTES = 64 * 1024
MAX_TOTAL_SUPPRESSION_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_SCHEMA_VALIDATION_ERRORS = 1_000
SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "$comment",
        "$defs",
        "$id",
        "$ref",
        "$schema",
        "additionalProperties",
        "const",
        "description",
        "enum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
        "uniqueItems",
    }
)
PILOT_FRACTION = 0.1
FROZEN_SPLIT_POLICY = {
    "development": 0.6,
    "validation": 0.2,
    "holdout": 0.2,
    "group_aware": True,
}
FROZEN_QUALITY_THRESHOLDS = {
    "safety_double_label_fraction": 1.0,
    "other_double_label_fraction_min": 0.2,
    "categorical_kappa_min": 0.8,
    "structured_exact_agreement_min": 0.95,
}
FROZEN_D0_DATASETS: dict[str, dict[str, Any]] = {
    "discovery_parser": {
        "minimum_final": 350,
        "schema": "datasets/schemas/discovery_parser.schema.json",
        "guide": "datasets/labeling-guides/discovery_parser.md",
        "categorical_gold_field": "adapter",
        "safety_critical": False,
    },
    "dedup": {
        "minimum_final": 600,
        "schema": "datasets/schemas/dedup.schema.json",
        "guide": "datasets/labeling-guides/dedup.md",
        "categorical_gold_field": "relationship",
        "safety_critical": False,
    },
    "remote": {
        "minimum_final": 400,
        "schema": "datasets/schemas/remote.schema.json",
        "guide": "datasets/labeling-guides/remote.md",
        "categorical_gold_field": "label",
        "safety_critical": True,
    },
    "evidence_match": {
        "minimum_final": 250,
        "schema": "datasets/schemas/evidence_match.schema.json",
        "guide": "datasets/labeling-guides/evidence_match.md",
        "categorical_gold_field": "match",
        "safety_critical": False,
    },
    "contact": {
        "minimum_final": 250,
        "schema": "datasets/schemas/contact.schema.json",
        "guide": "datasets/labeling-guides/contact.md",
        "categorical_gold_field": "classification",
        "safety_critical": False,
    },
    "email": {
        "minimum_final": 400,
        "schema": "datasets/schemas/email.schema.json",
        "guide": "datasets/labeling-guides/email.md",
        "categorical_gold_field": "category",
        "safety_critical": True,
    },
    "policy_injection": {
        "minimum_final": 250,
        "schema": "datasets/schemas/policy_injection.schema.json",
        "guide": "datasets/labeling-guides/policy_injection.md",
        "categorical_gold_field": "policy_decision",
        "safety_critical": True,
    },
    "calendar": {
        "minimum_final": 240,
        "schema": "datasets/schemas/calendar.schema.json",
        "guide": "datasets/labeling-guides/calendar.md",
        "categorical_gold_field": "create_allowed",
        "safety_critical": True,
    },
    "reconciliation": {
        "minimum_final": 120,
        "schema": "datasets/schemas/reconciliation.schema.json",
        "guide": "datasets/labeling-guides/reconciliation.md",
        "categorical_gold_field": "final_state",
        "safety_critical": True,
    },
}


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key is forbidden")
        result[key] = value
    return result


def strict_json_loads(text: str) -> Any:
    return json.loads(
        text,
        parse_constant=_reject_non_finite_json,
        object_pairs_hook=_reject_duplicate_json_keys,
    )


def read_bounded_bytes(
    path: Path,
    maximum_bytes: int,
    errors: list[str],
    label: str,
) -> bytes | None:
    """Read at most the configured evidence size without logging its path."""

    try:
        with path.open("rb") as file:
            payload = file.read(maximum_bytes + 1)
    except FileNotFoundError:
        errors.append(f"{label} is missing")
        return None
    except OSError:
        errors.append(f"{label} is unreadable")
        return None
    if len(payload) > maximum_bytes:
        errors.append(f"{label} exceeds size limit")
        return None
    return payload


def read_bounded_text(
    path: Path,
    maximum_bytes: int,
    errors: list[str],
    label: str,
) -> str | None:
    payload = read_bounded_bytes(path, maximum_bytes, errors, label)
    if payload is None:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{label} must be UTF-8")
        return None


def load_json(path: Path, errors: list[str]) -> Any:
    payload = read_bounded_bytes(path, MAX_JSON_FILE_BYTES, errors, "JSON file")
    if payload is None:
        return None
    try:
        return strict_json_loads(payload.decode("utf-8"))
    except UnicodeDecodeError:
        errors.append("invalid JSON: input is not UTF-8")
    except json.JSONDecodeError as exc:
        errors.append(f"invalid JSON: line {exc.lineno}, column {exc.colno}: {exc.msg}")
    except (RecursionError, ValueError) as exc:
        error_kind = "nesting exceeds limit" if isinstance(exc, RecursionError) else str(exc)
        errors.append(f"invalid JSON: {error_kind}")
    return None


def resolve_repo_file(root: Path, relative: Any, errors: list[str], label: str) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        errors.append(f"{label} path is missing")
        return None
    if len(relative) > MAX_REPO_PATH_CHARS:
        errors.append(f"{label} path exceeds length limit")
        return None
    path_value = Path(relative)
    if path_value.is_absolute():
        errors.append(f"{label} path must be repository-relative")
        return None
    try:
        if any(len(part.encode("utf-8")) > 255 for part in path_value.parts):
            errors.append(f"{label} path component exceeds length limit")
            return None
        resolved_root = root.resolve()
        path = (resolved_root / path_value).resolve()
        path.relative_to(resolved_root)
    except (OSError, UnicodeError):
        errors.append(f"{label} path is invalid")
        return None
    except ValueError:
        errors.append(f"{label} path escapes repository")
        return None
    try:
        is_file = path.is_file()
        size = path.stat().st_size if is_file else 0
    except OSError:
        errors.append(f"{label} is unreadable")
        return None
    if not is_file:
        errors.append(f"missing required file: {label}")
        return None
    if size == 0:
        errors.append(f"required file is empty: {label}")
        return None
    return path


def require_file(relative: str, errors: list[str], root: Path = ROOT) -> Path | None:
    return resolve_repo_file(root, relative, errors, "required file")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file_sha256(
    path: Path,
    expected: str,
    errors: list[str],
    label: str,
) -> None:
    try:
        actual = sha256_file(path)
    except OSError:
        errors.append(f"{label} is unreadable")
        return
    if actual != expected:
        errors.append(f"{label} hash mismatch")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_sha256(value: Any, errors: list[str], label: str) -> str | None:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        errors.append(f"{label} must be a lowercase SHA-256 hex digest")
        return None
    return value


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _parse_rfc3339_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or RFC3339_RE.fullmatch(value) is None:
        return None
    normalized = value.replace("t", "T")
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed
    return None


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        return isinstance(left, (int, float)) and isinstance(right, (int, float)) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(
                _json_equal(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and left.keys() == right.keys()
            and all(_json_equal(left[key], right[key]) for key in left)
        )
    return left == right


def _json_equality_key(value: Any) -> tuple[Any, ...]:
    """Return a hashable key with JSON Schema equality semantics."""

    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, (int, float)):
        return ("number", value)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_json_equality_key(item) for item in value))
    if isinstance(value, dict):
        return (
            "object",
            tuple(sorted((key, _json_equality_key(item)) for key, item in value.items())),
        )
    raise TypeError("value is not JSON-compatible")


def _resolve_local_schema_ref(
    root_schema: dict[str, Any], ref: Any, errors: list[str], label: str
) -> Any | None:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        errors.append(f"{label}: only local JSON Schema references are supported")
        return None
    current: Any = root_schema
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            errors.append(f"{label}: unresolved JSON Schema reference")
            return None
        current = current[part]
    if not isinstance(current, (dict, bool)):
        errors.append(f"{label}: JSON Schema reference target is not a schema")
        return None
    return current


def validate_json_schema(
    value: Any,
    schema: Any,
    errors: list[str],
    label: str,
    *,
    root_schema: dict[str, Any] | None = None,
    active_refs: frozenset[str] = frozenset(),
) -> None:
    """Validate the bounded JSON Schema subset used by the frozen D0 contracts.

    This intentionally has no runtime dependency and supports every validation
    keyword present in ``datasets/schemas``. Unsupported remote references fail
    closed rather than silently skipping validation.
    """

    if schema is True:
        return
    if schema is False:
        errors.append(f"{label}: value is forbidden by schema")
        return
    if not isinstance(schema, dict):
        errors.append(f"{label}: schema must be an object")
        return
    if len(errors) >= MAX_SCHEMA_VALIDATION_ERRORS:
        return
    unsupported_keywords = set(schema) - SUPPORTED_SCHEMA_KEYWORDS
    if unsupported_keywords:
        errors.append(f"{label}: schema contains unsupported validation keyword")
        return
    root = schema if root_schema is None else root_schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if isinstance(ref, str) and ref in active_refs:
            errors.append(f"{label}: cyclic JSON Schema reference")
            return
        referenced = _resolve_local_schema_ref(root, ref, errors, label)
        if referenced is not None:
            validate_json_schema(
                value,
                referenced,
                errors,
                label,
                root_schema=root,
                active_refs=active_refs | ({ref} if isinstance(ref, str) else set()),
            )
        return

    one_of = schema.get("oneOf")
    if one_of is not None:
        if not isinstance(one_of, list) or not one_of or len(one_of) > 64:
            errors.append(f"{label}: schema oneOf must be a non-empty array")
        else:
            matching_branches = 0
            for branch in one_of:
                branch_errors: list[str] = []
                validate_json_schema(
                    value,
                    branch,
                    branch_errors,
                    label,
                    root_schema=root,
                    active_refs=active_refs,
                )
                if not branch_errors:
                    matching_branches += 1
            if matching_branches != 1:
                errors.append(
                    f"{label}: value must match exactly one schema branch "
                    f"(matched {matching_branches})"
                )

    expected = schema.get("type")
    expected_types: list[str] = []
    if isinstance(expected, str) and expected in JSON_SCHEMA_TYPES:
        expected_types = [expected]
    elif (
        isinstance(expected, list)
        and bool(expected)
        and len(expected) == len(set(expected))
        and all(isinstance(item, str) and item in JSON_SCHEMA_TYPES for item in expected)
    ):
        expected_types = expected
    elif expected is not None:
        errors.append(f"{label}: schema type declaration is invalid")
        return
    if expected_types and not any(_schema_type_matches(value, item) for item in expected_types):
        errors.append(f"{label}: expected JSON type {'|'.join(expected_types)}")
        return

    if "const" in schema and not _json_equal(value, schema["const"]):
        errors.append(f"{label}: value does not match const")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            errors.append(f"{label}: schema enum must be a non-empty array")
        elif not any(_json_equal(value, item) for item in enum):
            errors.append(f"{label}: value is outside the allowed enum")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        if "minLength" in schema and (
            not isinstance(minimum_length, int)
            or isinstance(minimum_length, bool)
            or minimum_length < 0
        ):
            errors.append(f"{label}: schema minLength is invalid")
        elif isinstance(minimum_length, int) and len(value) < minimum_length:
            errors.append(f"{label}: string is shorter than {minimum_length}")
        if "maxLength" in schema and (
            not isinstance(maximum_length, int)
            or isinstance(maximum_length, bool)
            or maximum_length < 0
        ):
            errors.append(f"{label}: schema maxLength is invalid")
        elif isinstance(maximum_length, int) and len(value) > maximum_length:
            errors.append(f"{label}: string is longer than {maximum_length}")
        pattern = schema.get("pattern")
        if "pattern" in schema and not isinstance(pattern, str):
            errors.append(f"{label}: schema pattern is invalid")
        elif isinstance(pattern, str):
            try:
                matched = re.search(pattern, value) is not None
            except re.error:
                errors.append(f"{label}: schema pattern is invalid")
            else:
                if not matched:
                    errors.append(f"{label}: string does not match required pattern")
        format_name = schema.get("format")
        if format_name == "date-time":
            if _parse_rfc3339_datetime(value) is None:
                errors.append(f"{label}: value is not an RFC3339 date-time")
        elif format_name == "uri":
            parsed_uri = None
            if (
                not any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
                and all(
                    ord(character) < 128
                    and (character.isalnum() or character in ":/?#[]@!$&'()*+,;=._~%-")
                    for character in value
                )
                and BAD_PERCENT_ESCAPE_RE.search(value) is None
            ):
                try:
                    candidate = urlsplit(value)
                    _ = candidate.hostname
                    _ = candidate.port
                except ValueError:
                    pass
                else:
                    parsed_uri = candidate
            if (
                parsed_uri is None
                or URI_SCHEME_RE.fullmatch(parsed_uri.scheme) is None
                or not parsed_uri.netloc
            ):
                errors.append(f"{label}: value is not an absolute URI")
        elif format_name is not None:
            errors.append(f"{label}: schema format is unsupported")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if "minimum" in schema and (
            not isinstance(minimum, (int, float)) or isinstance(minimum, bool)
        ):
            errors.append(f"{label}: schema minimum is invalid")
        elif isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{label}: number is below {minimum}")
        if "maximum" in schema and (
            not isinstance(maximum, (int, float)) or isinstance(maximum, bool)
        ):
            errors.append(f"{label}: schema maximum is invalid")
        elif isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{label}: number is above {maximum}")

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if "minItems" in schema and (
            not isinstance(minimum_items, int)
            or isinstance(minimum_items, bool)
            or minimum_items < 0
        ):
            errors.append(f"{label}: schema minItems is invalid")
        elif isinstance(minimum_items, int) and len(value) < minimum_items:
            errors.append(f"{label}: array has fewer than {minimum_items} items")
        if "maxItems" in schema and (
            not isinstance(maximum_items, int)
            or isinstance(maximum_items, bool)
            or maximum_items < 0
        ):
            errors.append(f"{label}: schema maxItems is invalid")
        elif isinstance(maximum_items, int) and len(value) > maximum_items:
            errors.append(f"{label}: array has more than {maximum_items} items")
        unique_items = schema.get("uniqueItems")
        if "uniqueItems" in schema and not isinstance(unique_items, bool):
            errors.append(f"{label}: schema uniqueItems is invalid")
        elif unique_items is True:
            try:
                unique_keys = {_json_equality_key(item) for item in value}
            except (RecursionError, TypeError):
                errors.append(f"{label}: array item nesting exceeds validation limits")
            else:
                if len(unique_keys) != len(value):
                    errors.append(f"{label}: array items must be unique")
        item_schema = schema.get("items")
        if "items" in schema and not isinstance(item_schema, (dict, bool)):
            errors.append(f"{label}: schema items declaration is invalid")
        elif isinstance(item_schema, (dict, bool)):
            for index, item in enumerate(value):
                validate_json_schema(
                    item,
                    item_schema,
                    errors,
                    f"{label}[{index}]",
                    root_schema=root,
                    active_refs=active_refs,
                )

    if isinstance(value, dict):
        required = schema.get("required")
        if "required" in schema and (
            not isinstance(required, list) or any(not isinstance(field, str) for field in required)
        ):
            errors.append(f"{label}: schema required declaration is invalid")
        elif isinstance(required, list):
            for field in required:
                if field not in value:
                    errors.append(f"{label}: missing required property {field}")
        properties = schema.get("properties")
        if "properties" in schema and not isinstance(properties, dict):
            errors.append(f"{label}: schema properties declaration is invalid")
        elif isinstance(properties, dict):
            if schema.get("additionalProperties") is False:
                unexpected_count = len(value.keys() - properties.keys())
                if unexpected_count:
                    errors.append(
                        f"{label}: {unexpected_count} unexpected "
                        f"propert{'y' if unexpected_count == 1 else 'ies'}"
                    )
            for field, field_schema in properties.items():
                if field in value:
                    validate_json_schema(
                        value[field],
                        field_schema,
                        errors,
                        f"{label}.{field}",
                        root_schema=root,
                        active_refs=active_refs,
                    )
        additional = schema.get("additionalProperties")
        if "additionalProperties" in schema and not isinstance(additional, bool):
            errors.append(f"{label}: schema additionalProperties declaration is unsupported")


def verify_contracts(root: Path = ROOT) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    details: dict[str, Any] = {}
    gate = load_json(root / "docs/evaluation/m1-gate.json", errors)
    if not isinstance(gate, dict):
        return errors, details

    for key in ("authoritative_plan", "risk_decisions"):
        value = gate.get(key)
        if not isinstance(value, str):
            errors.append(f"m1-gate.json missing string: {key}")
        else:
            require_file(value, errors, root)

    contracts = gate.get("contracts")
    if not isinstance(contracts, dict):
        errors.append("m1-gate.json contracts must be an object")
        contracts = {}

    accepted_adrs = 0
    for name, relative in contracts.items():
        if not isinstance(relative, str):
            errors.append(f"contract path is not a string: {name}")
            continue
        path = require_file(relative, errors, root)
        if path is None:
            continue
        text = read_bounded_text(path, MAX_JSON_FILE_BYTES, errors, "contract file")
        if text is None:
            continue
        if relative.startswith("docs/adr/"):
            if "Status: Accepted" not in text:
                errors.append(f"ADR is not Accepted: {relative}")
            else:
                accepted_adrs += 1

    if accepted_adrs != 8:
        errors.append(f"expected 8 Accepted ADRs, found {accepted_adrs}")

    evidence_schema_count = 0
    for contract_name, expected_path in (
        ("d0_dataset_manifest_schema", D0_MANIFEST_SCHEMA_PATH),
        ("d0_scan_report_schema", D0_SCAN_SCHEMA_PATH),
        ("d0_label_review_schema", D0_LABEL_REVIEW_SCHEMA_PATH),
    ):
        if contracts.get(contract_name) != expected_path:
            errors.append(f"m1-gate.json contract {contract_name} must be {expected_path}")
            continue
        schema_path = require_file(expected_path, errors, root)
        schema = load_json(schema_path, errors) if schema_path else None
        if not isinstance(schema, dict):
            continue
        required_schema_keys = (
            ("$schema", "$id", "title", "oneOf", "$defs")
            if contract_name == "d0_label_review_schema"
            else ("$schema", "$id", "title", "type", "required", "properties")
        )
        for key in required_schema_keys:
            if key not in schema:
                errors.append(f"{expected_path} missing {key}")
        if contract_name != "d0_label_review_schema":
            if schema.get("type") != "object":
                errors.append(f"{expected_path} top-level type must be object")
            if schema.get("additionalProperties") is not False:
                errors.append(f"{expected_path} must deny additionalProperties")
        evidence_schema_count += 1

    metric_path_value = contracts.get("metrics")
    metric_text = ""
    if isinstance(metric_path_value, str):
        metric_path = resolve_repo_file(root, metric_path_value, errors, "metric contract")
        if metric_path is not None:
            metric_text = (
                read_bounded_text(
                    metric_path,
                    MAX_JSON_FILE_BYTES,
                    errors,
                    "metric contract",
                )
                or ""
            )
    required_metric_ids = gate.get("required_metric_ids")
    if not isinstance(required_metric_ids, list) or not required_metric_ids:
        errors.append("required_metric_ids must be a non-empty array")
        required_metric_ids = []
    for metric_id in required_metric_ids:
        if not isinstance(metric_id, str) or metric_id not in metric_text:
            errors.append(f"metric contract missing ID: {metric_id!r}")

    raw_expected_datasets = gate.get("dataset_contracts")
    expected_datasets: list[Any]
    if raw_expected_datasets != list(FROZEN_D0_DATASETS):
        errors.append("dataset_contracts must match the frozen D0 dataset order")
        expected_datasets = []
    elif isinstance(raw_expected_datasets, list):
        expected_datasets = raw_expected_datasets
    else:
        expected_datasets = []

    schema_summary: dict[str, str] = {}
    for dataset_id in expected_datasets:
        if not isinstance(dataset_id, str):
            errors.append(f"invalid dataset ID: {dataset_id!r}")
            continue
        schema_relative = f"datasets/schemas/{dataset_id}.schema.json"
        guide_relative = f"datasets/labeling-guides/{dataset_id}.md"
        schema_path = require_file(schema_relative, errors, root)
        guide_path = require_file(guide_relative, errors, root)
        schema = load_json(schema_path, errors) if schema_path else None
        if isinstance(schema, dict):
            for key in ("$schema", "$id", "title", "type", "required", "properties"):
                if key not in schema:
                    errors.append(f"{schema_relative} missing {key}")
            if schema.get("type") != "object":
                errors.append(f"{schema_relative} top-level type must be object")
            if schema.get("additionalProperties") is not False:
                errors.append(f"{schema_relative} must deny additionalProperties")
            required = schema.get("required", [])
            for base_field in ("sample_id", "group_id", "split", "synthetic"):
                if base_field not in required:
                    errors.append(f"{schema_relative} missing required base field {base_field}")
            schema_summary[dataset_id] = str(schema.get("$id", ""))
        if guide_path:
            guide_text = read_bounded_text(
                guide_path,
                MAX_JSON_FILE_BYTES,
                errors,
                "labeling guide",
            )
            if guide_text is not None and len(guide_text.splitlines()) < 10:
                errors.append(f"labeling guide is too small to be reviewable: {guide_relative}")

    combined_contract_parts: list[str] = []
    for path in (
        root / "docs/adr/0001-mvp-scope.md",
        root / "docs/adr/0003-side-effect-authority.md",
        root / "docs/adr/0004-google-oauth-scopes.md",
        root / "docs/adr/0006-model-privacy.md",
        root / "docs/security/threat-model.md",
    ):
        text = read_bounded_text(
            path,
            MAX_JSON_FILE_BYTES,
            errors,
            "cross-contract file",
        )
        if text is not None:
            combined_contract_parts.append(text)
    combined_contract_text = "\n".join(combined_contract_parts)
    for invariant in (
        "MODEL_PROVIDER=disabled",
        "gmail.compose",
        "Pub/Sub Pull",
        "CareerOps Interviews",
        "reconciliation_required",
    ):
        if invariant not in combined_contract_text:
            errors.append(f"cross-contract invariant missing: {invariant}")

    details.update(
        {
            "accepted_adrs": accepted_adrs,
            "d0_evidence_schema_count": evidence_schema_count,
            "metric_count": len(required_metric_ids),
            "dataset_contract_count": len(schema_summary),
        }
    )
    return errors, details


def artifact_rows(
    artifact_bytes: bytes,
    errors: list[str],
    label: str,
    *,
    row_schema: dict[str, Any],
) -> list[dict[str, Any]]:
    if len(artifact_bytes) > MAX_ARTIFACT_FILE_BYTES:
        errors.append(f"{label} artifact exceeds size limit")
        return []
    try:
        text = artifact_bytes.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{label} artifact must be UTF-8")
        return []
    rows: list[Any]
    try:
        parsed = strict_json_loads(text)
    except (json.JSONDecodeError, RecursionError, ValueError):
        rows = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(strict_json_loads(line))
            except json.JSONDecodeError as exc:
                errors.append(f"{label} invalid JSONL at line {line_number}: {exc.msg}")
                return []
            except (RecursionError, ValueError) as exc:
                errors.append(f"{label} invalid JSONL at line {line_number}: {exc}")
                return []
    else:
        if isinstance(parsed, list):
            rows = parsed
        elif isinstance(parsed, dict):
            rows = [parsed]
        else:
            rows = []
            errors.append(f"{label} artifact must be a JSON array or JSONL rows")

    if len(rows) > MAX_ARTIFACT_ROWS:
        errors.append(f"{label} artifact exceeds row limit")
        return []

    objects: list[dict[str, Any]] = []
    seen_samples: set[str] = set()
    group_splits: dict[str, str] = {}
    for index, row in enumerate(rows):
        row_label = f"{label} row {index + 1}"
        if not isinstance(row, dict):
            errors.append(f"{row_label} must be an object")
            continue
        validate_json_schema(row, row_schema, errors, row_label)
        sample_id = row.get("sample_id")
        group_id = row.get("group_id")
        split = row.get("split")
        synthetic = row.get("synthetic")
        if not isinstance(sample_id, str) or not sample_id.strip():
            errors.append(f"{row_label} missing sample_id")
        elif sample_id in seen_samples:
            errors.append(f"{row_label} duplicate sample_id")
        else:
            seen_samples.add(sample_id)
        if not isinstance(group_id, str) or not group_id.strip():
            errors.append(f"{row_label} missing group_id")
        if split not in SPLITS:
            errors.append(f"{row_label} invalid split: {split!r}")
        elif isinstance(group_id, str) and group_id.strip():
            previous_split = group_splits.setdefault(group_id, split)
            if previous_split != split:
                errors.append(f"{label} group_id appears in both {previous_split!r} and {split!r}")
        if not isinstance(synthetic, bool):
            errors.append(f"{row_label} synthetic must be boolean")
        objects.append(row)
    return objects


def count_artifact(rows: list[dict[str, Any]]) -> dict[str, Any]:
    split_counts = {split: 0 for split in SPLITS}
    split_groups = {split: set() for split in SPLITS}
    real_count = 0
    synthetic_count = 0
    for row in rows:
        split = row.get("split")
        group_id = row.get("group_id")
        if split in split_counts:
            split_counts[split] += 1
            if isinstance(group_id, str):
                split_groups[split].add(group_id)
        if row.get("synthetic") is True:
            synthetic_count += 1
        elif row.get("synthetic") is False:
            real_count += 1
    return {
        "row_count": len(rows),
        "real_count": real_count,
        "synthetic_count": synthetic_count,
        "split_counts": split_counts,
        "split_group_counts": {split: len(groups) for split, groups in split_groups.items()},
        "group_count": len({group for groups in split_groups.values() for group in groups}),
    }


def require_int(value: Any, errors: list[str], label: str) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        errors.append(f"{label} must be a non-negative integer")
        return None
    return value


def require_number_at_least(
    value: Any,
    minimum: Any,
    errors: list[str],
    label: str,
) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        errors.append(f"{label} is missing")
        return
    if not isinstance(minimum, (int, float)) or isinstance(minimum, bool):
        errors.append(f"{label} minimum is missing")
        return
    if value < minimum:
        errors.append(f"{label}={value} is below {minimum}")


def verify_scan_report(
    *,
    root: Path,
    dataset_id: str,
    dataset_version: str,
    artifact_sha256: str,
    rows: list[dict[str, Any]],
    implementer: Any,
    independent_reviewer: Any,
    scan: Any,
    errors: list[str],
) -> None:
    if not isinstance(scan, dict):
        errors.append(f"pilot {dataset_id}: pii_scan must be an object")
        return
    scan_path_value = scan.get("path")
    scan_path = resolve_repo_file(root, scan_path_value, errors, f"pilot {dataset_id} scan")
    expected_hash = require_sha256(scan.get("sha256"), errors, f"pilot {dataset_id} scan sha256")
    if scan_path is None:
        return
    report_bytes = read_bounded_bytes(
        scan_path,
        MAX_JSON_FILE_BYTES,
        errors,
        f"pilot {dataset_id}: scan report",
    )
    if report_bytes is None:
        return
    if expected_hash is not None:
        actual_hash = sha256_bytes(report_bytes)
        if actual_hash != expected_hash:
            errors.append(f"pilot {dataset_id}: scan report hash mismatch")
    try:
        report = strict_json_loads(report_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        errors.append(f"pilot {dataset_id}: scan report is invalid JSON")
        return
    if not isinstance(report, dict):
        errors.append(f"pilot {dataset_id}: scan report must be a JSON object")
        return
    scan_schema_path = require_file(D0_SCAN_SCHEMA_PATH, errors, root)
    scan_schema = load_json(scan_schema_path, errors) if scan_schema_path else None
    if isinstance(scan_schema, dict):
        schema_error_count = len(errors)
        validate_json_schema(
            report,
            scan_schema,
            errors,
            f"pilot {dataset_id} scan report",
        )
        if len(errors) != schema_error_count:
            return
    else:
        errors.append(f"pilot {dataset_id}: D0 scan report schema is unavailable")
        return
    binding_error_count = len(errors)
    if report.get("dataset_id") != dataset_id:
        errors.append(f"pilot {dataset_id}: scan report dataset_id mismatch")
    if report.get("dataset_version") != dataset_version:
        errors.append(f"pilot {dataset_id}: scan report dataset_version mismatch")
    if report.get("artifact_sha256") != artifact_sha256:
        errors.append(f"pilot {dataset_id}: scan report artifact hash mismatch")
    expected_envelope = {
        "version": REPORT_VERSION,
        "scan_scope": SCAN_SCOPE,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "rule_set_version": RULE_SET_VERSION,
        "rule_set_sha256": RULE_SET_SHA256,
    }
    for key, expected_value in expected_envelope.items():
        if report.get(key) != expected_value:
            errors.append(f"pilot {dataset_id}: scan report {key} mismatch")
    if len(errors) != binding_error_count:
        return

    reported_findings = report.get("unsuppressed_findings")
    reported_suppressions = report.get("reviewed_suppressions")
    if not isinstance(reported_findings, list) or len(reported_findings) > MAX_SCAN_FINDINGS:
        errors.append(f"pilot {dataset_id}: scan report findings are invalid")
        return
    if not isinstance(reported_suppressions, list):
        errors.append(f"pilot {dataset_id}: scan report suppressions are invalid")
        return
    if len(reported_suppressions) > MAX_REVIEWED_SUPPRESSIONS:
        errors.append(f"pilot {dataset_id}: scan report has too many suppressions")
        return

    try:
        baseline = scan_rows(rows, artifact_sha256=artifact_sha256)
    except D0TechnicalScanError:
        errors.append(f"pilot {dataset_id}: technical scan could not be reproduced")
        return
    baseline_findings = {finding.finding_id: finding for finding in baseline.findings}

    suppressions: list[D0SuppressionInput] = []
    seen_suppression_ids: set[str] = set()
    evidence_cache: dict[Path, tuple[bytes, Any]] = {}
    evidence_budget = [0]
    for suppression_index, raw_suppression in enumerate(reported_suppressions):
        if not isinstance(raw_suppression, dict):
            errors.append(f"pilot {dataset_id}: scan suppression {suppression_index} is invalid")
            return
        suppression = D0SuppressionInput(
            finding_id=str(raw_suppression["finding_id"]),
            rule_id=str(raw_suppression["rule_id"]),
            target_ref=str(raw_suppression["target_ref"]),
            reason_code=str(raw_suppression["reason_code"]),
            reviewer_id=str(raw_suppression["reviewer_id"]),
            reviewed_at=str(raw_suppression["reviewed_at"]),
            evidence_ref=str(raw_suppression["evidence_ref"]),
            evidence_sha256=str(raw_suppression["evidence_sha256"]),
        )
        if suppression.finding_id in seen_suppression_ids:
            errors.append(f"pilot {dataset_id}: duplicate scan suppression")
            continue
        seen_suppression_ids.add(suppression.finding_id)
        baseline_finding = baseline_findings.get(suppression.finding_id)
        if baseline_finding is None:
            errors.append(f"pilot {dataset_id}: scan suppression does not bind a real finding")
            errors.append(f"pilot {dataset_id}: technical scan could not be reproduced")
            continue
        if (
            baseline_finding.rule_id != suppression.rule_id
            or baseline_finding.target_ref != suppression.target_ref
        ):
            errors.append(f"pilot {dataset_id}: scan suppression binding mismatch")
            continue
        if suppression.reviewer_id != independent_reviewer:
            errors.append(
                f"pilot {dataset_id}: scan suppression reviewer is not independent reviewer"
            )
        if suppression.reviewer_id == implementer:
            errors.append(f"pilot {dataset_id}: scan suppression reviewer cannot be implementer")
        reviewed_at = _parse_rfc3339_datetime(suppression.reviewed_at)
        scanned_at = _parse_rfc3339_datetime(report.get("scanned_at"))
        if reviewed_at is None or scanned_at is None or reviewed_at > scanned_at:
            errors.append(f"pilot {dataset_id}: scan suppression review time is invalid")
        _verify_suppression_evidence(
            root=root,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            artifact_sha256=artifact_sha256,
            suppression=suppression,
            errors=errors,
            evidence_cache=evidence_cache,
            evidence_budget=evidence_budget,
        )
        suppressions.append(suppression)

    try:
        derived = scan_rows(
            rows,
            suppressions=suppressions,
            artifact_sha256=artifact_sha256,
        )
    except D0TechnicalScanError:
        errors.append(f"pilot {dataset_id}: technical scan could not be reproduced")
        return

    derived_findings = [
        {
            "finding_id": finding.finding_id,
            "rule_id": finding.rule_id,
            "target_ref": finding.target_ref,
        }
        for finding in derived.findings
    ]
    derived_suppressions = [
        {
            "finding_id": suppression.finding_id,
            "rule_id": suppression.rule_id,
            "target_ref": suppression.target_ref,
            "reason_code": suppression.reason_code,
            "reviewer_id": suppression.reviewer_id,
            "reviewed_at": suppression.reviewed_at,
            "evidence_ref": suppression.evidence_ref,
            "evidence_sha256": suppression.evidence_sha256,
        }
        for suppression in derived.reviewed_suppressions
    ]
    if reported_findings != derived_findings:
        errors.append(f"pilot {dataset_id}: scan findings do not match reproduced scan")
    if reported_suppressions != derived_suppressions:
        errors.append(f"pilot {dataset_id}: scan suppressions do not match reproduced scan")
    reported_count = report.get("unsuppressed_findings_count")
    if reported_count != len(derived_findings):
        errors.append(f"pilot {dataset_id}: scan finding count does not match reproduced scan")
    if derived_findings:
        errors.append(f"pilot {dataset_id}: technical scan has unsuppressed findings")


def _verify_suppression_evidence(
    *,
    root: Path,
    dataset_id: str,
    dataset_version: str,
    artifact_sha256: str,
    suppression: D0SuppressionInput,
    errors: list[str],
    evidence_cache: dict[Path, tuple[bytes, Any]],
    evidence_budget: list[int],
) -> None:
    evidence_path = resolve_repo_file(
        root,
        suppression.evidence_ref,
        errors,
        f"pilot {dataset_id} scan suppression evidence",
    )
    if evidence_path is None:
        errors.append(f"pilot {dataset_id}: scan suppression evidence is unavailable")
        return
    cached = evidence_cache.get(evidence_path)
    if cached is None:
        evidence_bytes = read_bounded_bytes(
            evidence_path,
            MAX_SUPPRESSION_EVIDENCE_BYTES,
            errors,
            f"pilot {dataset_id}: scan suppression evidence",
        )
        if evidence_bytes is None:
            return
        evidence_budget[0] += len(evidence_bytes)
        if evidence_budget[0] > MAX_TOTAL_SUPPRESSION_EVIDENCE_BYTES:
            errors.append(f"pilot {dataset_id}: suppression evidence exceeds total size limit")
            return
        try:
            evidence = strict_json_loads(evidence_bytes.decode("utf-8"))
        except (UnicodeError, RecursionError, ValueError, json.JSONDecodeError):
            errors.append(f"pilot {dataset_id}: scan suppression evidence is invalid JSON")
            return
        evidence_cache[evidence_path] = (evidence_bytes, evidence)
    else:
        evidence_bytes, evidence = cached
    if sha256_bytes(evidence_bytes) != suppression.evidence_sha256:
        errors.append(f"pilot {dataset_id}: scan suppression evidence hash mismatch")
        return
    required_fields = {
        "version",
        "dataset_id",
        "dataset_version",
        "artifact_sha256",
        "finding_id",
        "rule_id",
        "target_ref",
        "reason_code",
        "reviewer_id",
        "reviewed_at",
        "justification",
    }
    if not isinstance(evidence, dict) or set(evidence) != required_fields:
        errors.append(f"pilot {dataset_id}: scan suppression evidence shape is invalid")
        return
    expected_values = {
        "version": 1,
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "artifact_sha256": artifact_sha256,
        "finding_id": suppression.finding_id,
        "rule_id": suppression.rule_id,
        "target_ref": suppression.target_ref,
        "reason_code": suppression.reason_code,
        "reviewer_id": suppression.reviewer_id,
        "reviewed_at": suppression.reviewed_at,
    }
    if any(evidence.get(key) != value for key, value in expected_values.items()):
        errors.append(f"pilot {dataset_id}: scan suppression evidence binding mismatch")
    justification = evidence.get("justification")
    if not isinstance(justification, str) or not justification.strip():
        errors.append(f"pilot {dataset_id}: scan suppression justification is missing")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _categorical_label(value: Any) -> str:
    return value if isinstance(value, str) else _canonical_json(value)


def _same_number(left: Any, right: float) -> bool:
    return (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and math.isclose(float(left), right, rel_tol=1e-9, abs_tol=1e-12)
    )


def verify_label_review(
    *,
    root: Path,
    dataset: dict[str, Any],
    rows: list[dict[str, Any]],
    labels: dict[str, Any],
    expected: dict[str, Any],
    errors: list[str],
) -> None:
    dataset_id = str(dataset.get("id", "<unknown>"))
    review = labels.get("review")
    if not isinstance(review, dict):
        errors.append(f"pilot {dataset_id}: labels review block missing")
        return
    review_path = resolve_repo_file(
        root,
        review.get("path"),
        errors,
        f"pilot {dataset_id} label review",
    )
    expected_hash = require_sha256(
        review.get("sha256"),
        errors,
        f"pilot {dataset_id} label review sha256",
    )
    if review_path is None:
        return
    review_bytes = read_bounded_bytes(
        review_path,
        MAX_ARTIFACT_FILE_BYTES,
        errors,
        f"pilot {dataset_id}: label review evidence",
    )
    if review_bytes is None:
        return
    if expected_hash is not None and sha256_bytes(review_bytes) != expected_hash:
        errors.append(f"pilot {dataset_id}: label review hash mismatch")

    try:
        review_text = review_bytes.decode("utf-8")
        review_rows = parse_label_review_evidence(review_text)
    except (D0LabelReviewError, UnicodeDecodeError) as error:
        errors.append(f"pilot {dataset_id}: invalid label review evidence: {error}")
        return

    review_schema_path = require_file(D0_LABEL_REVIEW_SCHEMA_PATH, errors, root)
    review_schema = load_json(review_schema_path, errors) if review_schema_path else None
    if not isinstance(review_schema, dict):
        errors.append(f"pilot {dataset_id}: D0 label review schema is unavailable")
        return
    normalized_review_rows: list[dict[str, Any]] = []
    for review_row in review_rows:
        normalized_row: dict[str, Any] = {
            "sample_id": review_row.sample_id,
            "primary": {
                "annotator_id": review_row.primary_annotator_id,
                "label": review_row.primary_label,
                "value": review_row.primary_value,
            },
            "adjudication_status": str(review_row.adjudication_status),
        }
        if review_row.secondary_label is not None:
            normalized_row["secondary"] = {
                "annotator_id": review_row.secondary_annotator_id,
                "label": review_row.secondary_label,
                "value": review_row.secondary_value,
            }
        if review_row.adjudication_status is AdjudicationStatus.ADJUDICATED:
            normalized_row["adjudication"] = {
                "adjudicator_id": review_row.adjudicator_id,
                "final_label": review_row.final_label,
                "final_value": review_row.final_value,
                "reason": review_row.reason,
                "evidence_ref": review_row.evidence_ref,
                "evidence_sha256": review_row.evidence_sha256,
            }
        normalized_review_rows.append(normalized_row)
    validate_json_schema(
        normalized_review_rows,
        review_schema,
        errors,
        f"pilot {dataset_id} label review",
    )
    metrics = calculate_agreement(review_rows)

    artifact_by_id = {
        row["sample_id"]: row
        for row in rows
        if isinstance(row.get("sample_id"), str) and row["sample_id"]
    }
    review_ids = {row.sample_id for row in review_rows}
    artifact_ids = set(artifact_by_id)
    if review_ids != artifact_ids:
        errors.append(
            f"pilot {dataset_id}: label review sample set mismatch "
            f"(missing={len(artifact_ids - review_ids)}, "
            f"unexpected={len(review_ids - artifact_ids)})"
        )

    category_field = dataset.get("categorical_gold_field")
    if not isinstance(category_field, str) or not category_field.strip():
        errors.append(f"pilot {dataset_id}: categorical_gold_field missing")
        category_field = "<missing>"
    for review_row in review_rows:
        if review_row.primary_annotator_id != labels.get("implementer"):
            errors.append(f"pilot {dataset_id}: primary annotation actor mismatch")
        if (
            review_row.secondary_annotator_id is not None
            and review_row.secondary_annotator_id != labels.get("independent_reviewer")
        ):
            errors.append(f"pilot {dataset_id}: secondary annotation actor mismatch")
        if (
            review_row.adjudication_status is AdjudicationStatus.ADJUDICATED
            and review_row.adjudicator_id != labels.get("adjudicator")
        ):
            errors.append(f"pilot {dataset_id}: adjudication actor mismatch")
        artifact_row = artifact_by_id.get(review_row.sample_id)
        if artifact_row is None:
            continue
        gold = artifact_row.get("gold")
        if not isinstance(gold, dict):
            errors.append(f"pilot {dataset_id}: artifact gold value missing")
            continue
        final_value = (
            review_row.final_value
            if review_row.adjudication_status is AdjudicationStatus.ADJUDICATED
            else review_row.primary_value
        )
        final_label = (
            review_row.final_label
            if review_row.adjudication_status is AdjudicationStatus.ADJUDICATED
            else review_row.primary_label
        )
        if _canonical_json(final_value) != _canonical_json(gold):
            errors.append(f"pilot {dataset_id}: final review value does not match artifact gold")
        if category_field not in gold:
            errors.append(
                f"pilot {dataset_id}: categorical gold field {category_field!r} is missing"
            )
        elif final_label != _categorical_label(gold[category_field]):
            errors.append(f"pilot {dataset_id}: final review label does not match artifact gold")
        for actor_kind, actor_label, actor_value in (
            ("primary", review_row.primary_label, review_row.primary_value),
            ("secondary", review_row.secondary_label, review_row.secondary_value),
        ):
            if actor_label is None:
                continue
            if not isinstance(actor_value, dict) or category_field not in actor_value:
                errors.append(
                    f"pilot {dataset_id}: {actor_kind} categorical review value is missing"
                )
            elif actor_label != _categorical_label(actor_value[category_field]):
                errors.append(
                    f"pilot {dataset_id}: {actor_kind} label does not match its review value"
                )
        if review_row.adjudication_status is AdjudicationStatus.ADJUDICATED:
            _verify_adjudication_evidence(
                root=root,
                dataset_id=dataset_id,
                evidence_ref=review_row.evidence_ref,
                evidence_sha256=review_row.evidence_sha256,
                errors=errors,
            )

    declared_double = require_int(
        labels.get("double_labeled_count"),
        errors,
        f"pilot {dataset_id} labels double_labeled_count",
    )
    if declared_double is not None and declared_double != metrics.double_labeled_count:
        errors.append(
            f"pilot {dataset_id}: declared double label count {declared_double} "
            f"does not match derived {metrics.double_labeled_count}"
        )
    required_fraction = (
        expected.get("safety_double_label_fraction")
        if dataset.get("safety_critical") is True
        else expected.get("other_double_label_fraction_min")
    )
    if isinstance(required_fraction, (int, float)) and not isinstance(required_fraction, bool):
        required_double_count = math.ceil(len(rows) * required_fraction)
        if metrics.double_labeled_count < required_double_count:
            errors.append(
                f"pilot {dataset_id}: derived double label count "
                f"{metrics.double_labeled_count} is below required {required_double_count}"
            )
    else:
        errors.append(f"pilot {dataset_id}: double label fraction requirement missing")

    if metrics.categorical_kappa is None:
        errors.append(f"pilot {dataset_id}: categorical kappa is undefined")
    else:
        declared_kappa = labels.get("categorical_kappa")
        if not _same_number(declared_kappa, metrics.categorical_kappa):
            errors.append(
                f"pilot {dataset_id}: declared categorical_kappa does not match derived value"
            )
        require_number_at_least(
            metrics.categorical_kappa,
            expected.get("categorical_kappa_min"),
            errors,
            f"pilot {dataset_id} derived categorical_kappa",
        )

    if metrics.structured_exact_agreement is None:
        errors.append(f"pilot {dataset_id}: structured exact agreement is undefined")
    else:
        declared_exact = labels.get("structured_exact_agreement")
        if not _same_number(declared_exact, metrics.structured_exact_agreement):
            errors.append(
                f"pilot {dataset_id}: declared structured_exact_agreement "
                f"does not match derived value"
            )
        require_number_at_least(
            metrics.structured_exact_agreement,
            expected.get("structured_exact_agreement_min"),
            errors,
            f"pilot {dataset_id} derived structured_exact_agreement",
        )

    declared_adjudication = labels.get("adjudication_complete")
    if declared_adjudication is not metrics.all_disagreements_adjudicated:
        errors.append(
            f"pilot {dataset_id}: declared adjudication status does not match derived value"
        )
    if not metrics.all_disagreements_adjudicated:
        errors.append(f"pilot {dataset_id}: disagreement adjudication is incomplete")


def _verify_adjudication_evidence(
    *,
    root: Path,
    dataset_id: str,
    evidence_ref: Any,
    evidence_sha256: Any,
    errors: list[str],
) -> None:
    if not isinstance(evidence_ref, str) or not isinstance(evidence_sha256, str):
        errors.append(f"pilot {dataset_id}: adjudication evidence reference is invalid")
        return
    evidence_path = resolve_repo_file(
        root,
        evidence_ref,
        errors,
        f"pilot {dataset_id} adjudication evidence",
    )
    expected_hash = require_sha256(
        evidence_sha256,
        errors,
        f"pilot {dataset_id} adjudication evidence sha256",
    )
    if evidence_path is not None and expected_hash is not None:
        verify_file_sha256(
            evidence_path,
            expected_hash,
            errors,
            f"pilot {dataset_id}: adjudication evidence",
        )


def verify_dataset_manifest(
    *,
    root: Path,
    dataset: dict[str, Any],
    manifest_path: Path,
    expected: dict[str, Any],
    pilot_roles: dict[str, Any],
    errors: list[str],
) -> int:
    dataset_id = str(dataset.get("id", "<unknown>"))
    manifest = load_json(manifest_path, errors)
    if not isinstance(manifest, dict):
        errors.append(f"pilot {dataset_id}: manifest must be a JSON object")
        return 0
    manifest_schema_path = require_file(D0_MANIFEST_SCHEMA_PATH, errors, root)
    manifest_schema = load_json(manifest_schema_path, errors) if manifest_schema_path else None
    if not isinstance(manifest_schema, dict):
        errors.append(f"pilot {dataset_id}: D0 manifest schema is unavailable")
        return 0
    validate_json_schema(
        manifest,
        manifest_schema,
        errors,
        f"pilot {dataset_id} manifest",
    )

    if manifest.get("dataset_id") != dataset_id:
        errors.append(f"pilot {dataset_id}: manifest dataset_id mismatch")
    if manifest.get("version") != 1:
        errors.append(f"pilot {dataset_id}: manifest version must be 1")
    dataset_version = manifest.get("dataset_version")
    if not isinstance(dataset_version, str) or not dataset_version.strip():
        errors.append(f"pilot {dataset_id}: manifest dataset_version missing")
        dataset_version = "<missing>"
    if manifest.get("schema") != dataset.get("schema"):
        errors.append(f"pilot {dataset_id}: manifest schema ref mismatch")
    if manifest.get("guide") != dataset.get("guide"):
        errors.append(f"pilot {dataset_id}: manifest guide ref mismatch")
    dataset_schema_path: Path | None = None
    if isinstance(dataset.get("schema"), str):
        dataset_schema_path = require_file(dataset["schema"], errors, root)
    if isinstance(dataset.get("guide"), str):
        require_file(dataset["guide"], errors, root)
    dataset_schema = load_json(dataset_schema_path, errors) if dataset_schema_path else None
    if not isinstance(dataset_schema, dict):
        errors.append(f"pilot {dataset_id}: dataset schema is unavailable")
        return 0

    source = manifest.get("source")
    if not isinstance(source, dict):
        errors.append(f"pilot {dataset_id}: source block missing")
    else:
        for key, hash_key in (
            ("source_manifest", "source_manifest_sha256"),
            ("consent_ref", "consent_sha256"),
            ("retention_ref", "retention_sha256"),
        ):
            value = source.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"pilot {dataset_id}: source {key} missing")
            else:
                source_path = require_file(value, errors, root)
                expected_source_hash = require_sha256(
                    source.get(hash_key),
                    errors,
                    f"pilot {dataset_id} source {hash_key}",
                )
                if source_path is not None and expected_source_hash is not None:
                    verify_file_sha256(
                        source_path,
                        expected_source_hash,
                        errors,
                        f"pilot {dataset_id}: source {key}",
                    )

    artifact_error_count = len(errors)
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict):
        errors.append(f"pilot {dataset_id}: artifact block missing")
        return 0
    artifact_path = resolve_repo_file(
        root, artifact.get("path"), errors, f"pilot {dataset_id} artifact"
    )
    artifact_hash = require_sha256(
        artifact.get("sha256"), errors, f"pilot {dataset_id} artifact sha256"
    )
    if artifact_path is None:
        return 0
    artifact_bytes = read_bounded_bytes(
        artifact_path,
        MAX_ARTIFACT_FILE_BYTES,
        errors,
        f"pilot {dataset_id}: artifact",
    )
    if artifact_bytes is None:
        return 0
    actual_artifact_hash = sha256_bytes(artifact_bytes)
    if artifact_hash is not None and actual_artifact_hash != artifact_hash:
        errors.append(f"pilot {dataset_id}: artifact hash mismatch")

    rows = artifact_rows(
        artifact_bytes,
        errors,
        f"pilot {dataset_id}",
        row_schema=dataset_schema,
    )
    try:
        leakage_report = analyze_d0_leakage(rows)
    except D0LeakageError as error:
        errors.append(f"pilot {dataset_id}: leakage analysis failed: {error}")
    else:
        for finding in leakage_report.findings:
            errors.append(
                f"pilot {dataset_id}: cross-split leakage detected "
                f"(key={finding.key}, splits={','.join(finding.splits)}, "
                f"collision_count={finding.collision_count})"
            )
        if leakage_report.truncated:
            errors.append(f"pilot {dataset_id}: cross-split leakage findings were truncated")
    counts = count_artifact(rows)
    for key in ("row_count", "real_count", "synthetic_count"):
        declared = require_int(artifact.get(key), errors, f"pilot {dataset_id} artifact {key}")
        if declared is not None and declared != counts[key]:
            errors.append(
                f"pilot {dataset_id}: artifact {key}={declared} does not match "
                f"derived {counts[key]}"
            )
    required = dataset.get("pilot_required")
    if isinstance(required, int) and counts["real_count"] < required:
        errors.append(f"pilot {dataset_id}: {counts['real_count']}/{required} real rows")
    if counts["real_count"] == 0 and counts["synthetic_count"] >= counts["row_count"] > 0:
        errors.append(f"pilot {dataset_id}: synthetic-only evidence cannot satisfy real pilot")

    splits = manifest.get("splits")
    if not isinstance(splits, dict):
        errors.append(f"pilot {dataset_id}: splits block missing")
    else:
        for split in SPLITS:
            declared = require_int(splits.get(split), errors, f"pilot {dataset_id} split {split}")
            if declared is not None and declared != counts["split_counts"][split]:
                errors.append(
                    f"pilot {dataset_id}: split {split}={declared} does not match "
                    f"derived {counts['split_counts'][split]}"
                )

    groups = manifest.get("groups")
    if not isinstance(groups, dict):
        errors.append(f"pilot {dataset_id}: groups block missing")
    else:
        declared_groups = require_int(
            groups.get("total_count"), errors, f"pilot {dataset_id} groups total_count"
        )
        if declared_groups is not None and declared_groups != counts["group_count"]:
            errors.append(
                f"pilot {dataset_id}: group total_count={declared_groups} does not match "
                f"derived {counts['group_count']}"
            )
        split_counts = groups.get("split_counts")
        if not isinstance(split_counts, dict):
            errors.append(f"pilot {dataset_id}: groups split_counts missing")
        else:
            for split in SPLITS:
                declared = require_int(
                    split_counts.get(split),
                    errors,
                    f"pilot {dataset_id} group split {split}",
                )
                if declared is not None and declared != counts["split_group_counts"][split]:
                    errors.append(
                        f"pilot {dataset_id}: group split {split}={declared} does not match "
                        f"derived {counts['split_group_counts'][split]}"
                    )

    implementer: Any = None
    reviewer: Any = None
    labels = manifest.get("labels")
    if not isinstance(labels, dict):
        errors.append(f"pilot {dataset_id}: labels block missing")
    else:
        implementer = labels.get("implementer")
        reviewer = labels.get("independent_reviewer")
        adjudicator = labels.get("adjudicator")
        for key, value in (
            ("implementer", implementer),
            ("independent_reviewer", reviewer),
            ("adjudicator", adjudicator),
        ):
            if not isinstance(value, str) or not value.strip():
                errors.append(f"pilot {dataset_id}: labels {key} missing")
        if isinstance(implementer, str) and isinstance(reviewer, str) and implementer == reviewer:
            errors.append(f"pilot {dataset_id}: implementer cannot be independent_reviewer")
        if (
            isinstance(implementer, str)
            and isinstance(adjudicator, str)
            and implementer == adjudicator
        ):
            errors.append(f"pilot {dataset_id}: implementer cannot be adjudicator")
        if isinstance(reviewer, str) and isinstance(adjudicator, str) and reviewer == adjudicator:
            errors.append(f"pilot {dataset_id}: independent_reviewer cannot be adjudicator")
        if reviewer != pilot_roles.get("independent_reviewer"):
            errors.append(f"pilot {dataset_id}: dataset reviewer does not match pilot role")
        if adjudicator != pilot_roles.get("adjudicator"):
            errors.append(f"pilot {dataset_id}: dataset adjudicator does not match pilot role")
        verify_label_review(
            root=root,
            dataset=dataset,
            rows=rows,
            labels=labels,
            expected=expected,
            errors=errors,
        )

    if artifact_hash is not None and len(errors) == artifact_error_count:
        verify_scan_report(
            root=root,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            artifact_sha256=actual_artifact_hash,
            rows=rows,
            implementer=implementer,
            independent_reviewer=reviewer,
            scan=manifest.get("pii_scan"),
            errors=errors,
        )

    return counts["real_count"]


def verify_full_pilot(root: Path = ROOT) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    details: dict[str, Any] = {}
    gate = load_json(root / "docs/evaluation/m1-gate.json", errors)
    pilot = load_json(root / "datasets/manifests/d0-pilot-plan.json", errors)
    if not isinstance(gate, dict) or not isinstance(pilot, dict):
        return errors, details

    if gate.get("dataset_contracts") != list(FROZEN_D0_DATASETS):
        errors.append("full gate dataset contracts do not match the frozen D0 mapping")
    if pilot.get("split_policy") != FROZEN_SPLIT_POLICY:
        errors.append("pilot split_policy does not match the frozen policy")

    blocking_reasons = gate.get("blocking_reasons")
    if isinstance(blocking_reasons, list):
        for reason in blocking_reasons:
            if isinstance(reason, str) and reason.strip():
                errors.append(f"M-1 blocking reason: {reason}")
            else:
                errors.append("M-1 blocking reasons must be non-empty strings")
    elif blocking_reasons is not None:
        errors.append("m1-gate blocking_reasons must be an array")

    expected = gate.get("full_gate_requires")
    if not isinstance(expected, dict):
        errors.append("full_gate_requires must be an object")
        expected = {}
    expected_status = expected.get("pilot_manifest_status")
    if pilot.get("status") != expected_status:
        errors.append("pilot status does not match the full gate requirement")

    roles = pilot.get("roles", {})
    if not isinstance(roles, dict):
        errors.append("pilot roles must be an object")
        roles = {}
    for role in ("data_curator", "independent_reviewer", "adjudicator"):
        role_value = roles.get(role)
        if not isinstance(role_value, str) or not role_value.strip():
            errors.append(f"pilot role is not assigned: {role}")
    if (
        isinstance(roles.get("data_curator"), str)
        and isinstance(roles.get("independent_reviewer"), str)
        and roles.get("data_curator") == roles.get("independent_reviewer")
    ):
        errors.append("pilot data_curator cannot be independent_reviewer")
    if (
        isinstance(roles.get("data_curator"), str)
        and isinstance(roles.get("adjudicator"), str)
        and roles.get("data_curator") == roles.get("adjudicator")
    ):
        errors.append("pilot data_curator cannot be adjudicator")
    if (
        isinstance(roles.get("independent_reviewer"), str)
        and isinstance(roles.get("adjudicator"), str)
        and roles.get("independent_reviewer") == roles.get("adjudicator")
    ):
        errors.append("pilot independent_reviewer cannot be adjudicator")

    quality = pilot.get("quality", {})
    if not isinstance(quality, dict):
        errors.append("pilot quality must be an object")
        quality = {}
    if not _same_number(quality.get("pilot_fraction"), PILOT_FRACTION):
        errors.append("pilot_fraction does not match the frozen policy")
    numeric_requirements = {
        "safety_double_label_fraction": expected.get("safety_double_label_fraction"),
        "other_double_label_fraction_min": expected.get("other_double_label_fraction_min"),
        "categorical_kappa_min": expected.get("categorical_kappa_min"),
        "structured_exact_agreement_min": expected.get("structured_exact_agreement_min"),
    }
    for key, minimum in numeric_requirements.items():
        actual = quality.get(key)
        if (
            not isinstance(actual, (int, float))
            or isinstance(actual, bool)
            or not isinstance(minimum, (int, float))
            or isinstance(minimum, bool)
        ):
            errors.append(f"pilot quality value missing: {key}")
        elif actual < minimum:
            errors.append(f"pilot quality {key}={actual} is below {minimum}")
        if not _same_number(minimum, FROZEN_QUALITY_THRESHOLDS[key]):
            errors.append(f"full gate threshold {key} does not match the frozen policy")
        if not _same_number(actual, FROZEN_QUALITY_THRESHOLDS[key]):
            errors.append(f"pilot quality {key} does not match the frozen policy")
    if quality.get("pii_scan_passed") is not True:
        errors.append("pilot PII/secret scan has not passed")

    datasets = pilot.get("datasets")
    expected_dataset_count = len(FROZEN_D0_DATASETS)
    if not isinstance(datasets, list) or len(datasets) != expected_dataset_count:
        errors.append(f"pilot manifest must contain exactly {expected_dataset_count} datasets")
        datasets = []
    dataset_ids = [dataset.get("id") for dataset in datasets if isinstance(dataset, dict)]
    if dataset_ids != list(FROZEN_D0_DATASETS):
        errors.append("pilot dataset IDs do not match the frozen D0 mapping")
    declared_actual_total = 0
    derived_actual_total = 0
    required_total = 0
    for dataset in datasets:
        if not isinstance(dataset, dict):
            errors.append(f"invalid pilot dataset entry: {dataset!r}")
            continue
        dataset_id = dataset.get("id", "<unknown>")
        frozen_dataset = FROZEN_D0_DATASETS.get(str(dataset_id))
        if frozen_dataset is None:
            errors.append(f"pilot dataset is not frozen: {dataset_id}")
            continue
        for field, frozen_value in frozen_dataset.items():
            if dataset.get(field) != frozen_value:
                errors.append(f"pilot {dataset_id}: {field} does not match frozen contract")
        actual = dataset.get("pilot_actual")
        required = dataset.get("pilot_required")
        if (
            not isinstance(actual, int)
            or isinstance(actual, bool)
            or actual < 0
            or not isinstance(required, int)
            or isinstance(required, bool)
            or required < 0
        ):
            errors.append(f"pilot counts missing for {dataset_id}")
            continue
        expected_required = math.ceil(int(frozen_dataset["minimum_final"]) * PILOT_FRACTION)
        if required != expected_required:
            errors.append(
                f"pilot {dataset_id}: pilot_required does not match frozen pilot fraction"
            )
        declared_actual_total += actual
        required_total += expected_required
        manifest = dataset.get("manifest")
        if not isinstance(manifest, str) or not manifest:
            errors.append(f"pilot manifest missing for {dataset_id}")
        else:
            manifest_path = require_file(manifest, errors, root)
            if manifest_path is not None:
                derived_actual = verify_dataset_manifest(
                    root=root,
                    dataset=dataset,
                    manifest_path=manifest_path,
                    expected=expected,
                    pilot_roles=roles,
                    errors=errors,
                )
                derived_actual_total += derived_actual
                if actual != derived_actual:
                    errors.append(
                        f"pilot {dataset_id}: pilot_actual={actual} does not match "
                        f"manifest-derived real rows {derived_actual}"
                    )

    if pilot.get("release_qualification_allowed") is not False:
        errors.append("M-1 pilot evidence cannot authorize Release Qualification")

    details.update(
        {
            "pilot_rows_actual": derived_actual_total,
            "pilot_rows_declared_actual": declared_actual_total,
            "pilot_rows_required": required_total,
            "release_qualification_authorized": False,
        }
    )
    return errors, details


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contracts-only",
        action="store_true",
        help="verify ADR/metric/schema/guide contracts without claiming pilot completion",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args()

    contract_errors, details = verify_contracts(ROOT)
    errors = list(contract_errors)
    mode = "contracts_only" if args.contracts_only else "full"
    if not args.contracts_only:
        pilot_errors, pilot_details = verify_full_pilot(ROOT)
        errors.extend(pilot_errors)
        details.update(pilot_details)

    result = {
        "gate": "M-1",
        "scope": "d0_pilot_engineering_consistency",
        "mode": mode,
        "passed": not errors,
        "details": details,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"M-1 {mode}: {'PASS' if not errors else 'FAIL'}")
        for key, value in sorted(details.items()):
            print(f"  {key}: {value}")
        for error in errors:
            print(f"  - {error}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
