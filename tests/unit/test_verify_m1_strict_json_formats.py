from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]


def _load_verify_m1_module() -> ModuleType:
    script = ROOT / "scripts" / "verify_m1.py"
    spec = importlib.util.spec_from_file_location("verify_m1_strict_json_formats", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _discovery_parser_row() -> dict[str, Any]:
    return {
        "sample_id": "discovery-001",
        "group_id": "example.com",
        "split": "development",
        "synthetic": False,
        "source": {
            "kind": "greenhouse",
            "url": "https://example.com/careers/engineering",
            "captured_at": "2026-07-18T12:34:56Z",
            "content_sha256": "a" * 64,
            "terms_status": "allowed",
        },
        "gold": {
            "official_careers_entry": "https://example.com/careers",
            "adapter": "greenhouse",
            "postings": [],
        },
    }


def _discovery_parser_schema(verifier: ModuleType) -> dict[str, Any]:
    schema = verifier.strict_json_loads(
        (ROOT / "datasets/schemas/discovery_parser.schema.json").read_text(encoding="utf-8")
    )
    assert isinstance(schema, dict)
    return schema


def _contact_row(kind: str, source_ref: str) -> dict[str, Any]:
    return {
        "sample_id": "contact-1",
        "group_id": "group-1",
        "split": "development",
        "synthetic": False,
        "source": {
            "kind": kind,
            "url_or_thread_ref": source_ref,
            "captured_at": "2026-07-18T12:34:56Z",
            "evidence_text": "Evidence",
            "content_sha256": "a" * 64,
        },
        "candidate": {
            "address": "recruiting-contact-1",
            "display_name": "Recruiting",
            "domain_matches_company": True,
        },
        "gold": {
            "classification": "recruiting_contact",
            "publicly_listed": True,
            "guessed": False,
            "allowed_actions": ["display"],
        },
    }


def test_duplicate_json_key_is_rejected_without_echoing_key_or_values() -> None:
    verifier = _load_verify_m1_module()
    document = (
        '{"super_secret_duplicate_key":"first-secret-value",'
        '"super_secret_duplicate_key":"second-secret-value"}'
    )

    with pytest.raises(ValueError) as caught:
        verifier.strict_json_loads(document)

    message = str(caught.value)
    assert message == "duplicate JSON object key is forbidden"
    for sensitive_text in (
        "super_secret_duplicate_key",
        "first-secret-value",
        "second-secret-value",
    ):
        assert sensitive_text not in message


@pytest.mark.parametrize(
    "invalid_uri",
    [
        "https://example.com/careers/job 1",
        "https://[2001:db8::1/careers",
        "https://example.com/careers/%ZZ",
    ],
    ids=["space", "malformed-ipv6", "bad-percent-escape"],
)
def test_real_discovery_parser_row_rejects_malformed_uri_without_crashing(
    invalid_uri: str,
) -> None:
    verifier = _load_verify_m1_module()
    schema = _discovery_parser_schema(verifier)
    baseline = _discovery_parser_row()
    baseline_errors: list[str] = []
    verifier.validate_json_schema(baseline, schema, baseline_errors, "discovery row")
    assert baseline_errors == []

    row = copy.deepcopy(baseline)
    row["source"]["url"] = invalid_uri
    errors: list[str] = []

    verifier.validate_json_schema(row, schema, errors, "discovery row")

    assert errors == ["discovery row.source.url: value is not an absolute URI"]


def test_date_time_format_rejects_iso_week_date() -> None:
    verifier = _load_verify_m1_module()
    errors: list[str] = []

    verifier.validate_json_schema(
        "2026-W29-6T12:34:56Z",
        {"type": "string", "format": "date-time"},
        errors,
        "captured_at",
    )

    assert errors == ["captured_at: value is not an RFC3339 date-time"]


@pytest.mark.parametrize(
    "timestamp",
    ["2026-07-18T12:34:56Z", "2026-07-18t12:34:56.123+08:00"],
)
def test_date_time_format_accepts_rfc3339(timestamp: str) -> None:
    verifier = _load_verify_m1_module()
    errors: list[str] = []

    verifier.validate_json_schema(
        timestamp,
        {"type": "string", "format": "date-time"},
        errors,
        "captured_at",
    )

    assert errors == []


def test_unique_items_uses_json_numeric_equality() -> None:
    verifier = _load_verify_m1_module()
    errors: list[str] = []

    verifier.validate_json_schema(
        [1, 1.0],
        {"type": "array", "uniqueItems": True},
        errors,
        "values",
    )

    assert errors == ["values: array items must be unique"]


def test_unique_items_distinguishes_boolean_from_number() -> None:
    verifier = _load_verify_m1_module()
    errors: list[str] = []

    verifier.validate_json_schema(
        [True, 1, False, 0.0],
        {"type": "array", "uniqueItems": True},
        errors,
        "values",
    )

    assert errors == []


def test_boolean_false_items_schema_is_enforced() -> None:
    verifier = _load_verify_m1_module()
    non_empty_errors: list[str] = []
    empty_errors: list[str] = []
    schema = {"type": "array", "items": False}

    verifier.validate_json_schema(["forbidden"], schema, non_empty_errors, "values")
    verifier.validate_json_schema([], schema, empty_errors, "values")

    assert non_empty_errors == ["values[0]: value is forbidden by schema"]
    assert empty_errors == []


def test_unknown_validation_keyword_fails_closed() -> None:
    verifier = _load_verify_m1_module()
    errors: list[str] = []

    verifier.validate_json_schema(
        "value",
        {"type": "string", "not": {"const": "forbidden"}},
        errors,
        "field",
    )

    assert errors == ["field: schema contains unsupported validation keyword"]


@pytest.mark.parametrize("invalid_type", [[], ["string", "string"], ["mystery"]])
def test_invalid_schema_type_declaration_fails_closed(invalid_type: list[str]) -> None:
    verifier = _load_verify_m1_module()
    errors: list[str] = []

    verifier.validate_json_schema("value", {"type": invalid_type}, errors, "field")

    assert errors == ["field: schema type declaration is invalid"]


def test_cyclic_local_schema_reference_fails_closed() -> None:
    verifier = _load_verify_m1_module()
    schema = {"$ref": "#/$defs/loop", "$defs": {"loop": {"$ref": "#/$defs/loop"}}}
    errors: list[str] = []

    verifier.validate_json_schema("value", schema, errors, "field")

    assert errors == ["field: cyclic JSON Schema reference"]


def test_uri_rejects_raw_reserved_braces() -> None:
    verifier = _load_verify_m1_module()
    errors: list[str] = []

    verifier.validate_json_schema(
        "https://example.test/{private}",
        {"type": "string", "format": "uri"},
        errors,
        "source",
    )

    assert errors == ["source: value is not an absolute URI"]


def test_unexpected_property_error_does_not_echo_untrusted_key() -> None:
    verifier = _load_verify_m1_module()
    secret_key = "private-person@example.com"
    errors: list[str] = []

    verifier.validate_json_schema(
        {secret_key: "secret"},
        {"type": "object", "properties": {}, "additionalProperties": False},
        errors,
        "row",
    )

    assert errors == ["row: 1 unexpected property"]
    assert secret_key not in "\n".join(errors)


def test_unique_items_handles_large_array_without_pairwise_comparison() -> None:
    verifier = _load_verify_m1_module()
    errors: list[str] = []

    verifier.validate_json_schema(
        [{"finding_id": index} for index in range(5_000)],
        {"type": "array", "uniqueItems": True, "maxItems": 5_000},
        errors,
        "findings",
    )

    assert errors == []


def test_repo_path_errors_do_not_echo_untrusted_path(tmp_path: Path) -> None:
    verifier = _load_verify_m1_module()
    private_path = "private-person@example.com"
    errors: list[str] = []

    result = verifier.resolve_repo_file(tmp_path, private_path, errors, "evidence")

    assert result is None
    assert errors == ["missing required file: evidence"]
    assert private_path not in "\n".join(errors)


def test_oversized_path_component_fails_closed_without_os_error(tmp_path: Path) -> None:
    verifier = _load_verify_m1_module()
    errors: list[str] = []

    result = verifier.resolve_repo_file(tmp_path, "x" * 300, errors, "evidence")

    assert result is None
    assert errors == ["evidence path component exceeds length limit"]


@pytest.mark.parametrize(
    ("kind", "source_ref"),
    [
        ("established_thread", "18f0abc123"),
        ("job_page", "https://example.test/jobs/1"),
    ],
)
def test_contact_source_kind_and_reference_contract_accepts_matching_pairs(
    kind: str,
    source_ref: str,
) -> None:
    verifier = _load_verify_m1_module()
    schema = verifier.strict_json_loads(
        (ROOT / "datasets/schemas/contact.schema.json").read_text(encoding="utf-8")
    )
    errors: list[str] = []

    verifier.validate_json_schema(_contact_row(kind, source_ref), schema, errors, "contact")

    assert errors == []


@pytest.mark.parametrize(
    ("kind", "source_ref"),
    [
        ("established_thread", "https://example.test/thread/1"),
        ("job_page", "18f0abc123"),
    ],
)
def test_contact_source_kind_and_reference_contract_rejects_cross_namespace_pairs(
    kind: str,
    source_ref: str,
) -> None:
    verifier = _load_verify_m1_module()
    schema = verifier.strict_json_loads(
        (ROOT / "datasets/schemas/contact.schema.json").read_text(encoding="utf-8")
    )
    errors: list[str] = []

    verifier.validate_json_schema(_contact_row(kind, source_ref), schema, errors, "contact")

    assert errors == ["contact: value must match exactly one schema branch (matched 0)"]
