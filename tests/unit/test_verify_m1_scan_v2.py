from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from careerops.evaluation.d0_scan import (
    REPORT_VERSION,
    RULE_SET_SHA256,
    RULE_SET_VERSION,
    SCAN_SCOPE,
    TOOL_NAME,
    TOOL_VERSION,
    scan_rows,
)

REPOSITORY_ROOT = Path(__file__).parents[2]
DATASET_ID = "contact"
DATASET_VERSION = "contact-pilot-v1"
IMPLEMENTER = "implementer"
INDEPENDENT_REVIEWER = "reviewer"
REVIEWED_AT = "2026-07-18T10:30:00Z"


def _load_verify_m1_module() -> ModuleType:
    script = REPOSITORY_ROOT / "scripts/verify_m1.py"
    spec = importlib.util.spec_from_file_location("verify_m1_scan_v2", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_sha256(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prepare_root(tmp_path: Path) -> Path:
    schema_source = REPOSITORY_ROOT / "datasets/schemas/d0_scan_report.schema.json"
    schema_target = tmp_path / "datasets/schemas/d0_scan_report.schema.json"
    schema_target.parent.mkdir(parents=True, exist_ok=True)
    schema_target.write_bytes(schema_source.read_bytes())
    return tmp_path


def _report(
    artifact_sha256: str,
    *,
    findings: list[dict[str, Any]] | None = None,
    suppressions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reported_findings = [] if findings is None else findings
    return {
        "version": REPORT_VERSION,
        "scan_scope": SCAN_SCOPE,
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "artifact_sha256": artifact_sha256,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "rule_set_version": RULE_SET_VERSION,
        "rule_set_sha256": RULE_SET_SHA256,
        "scanned_at": "2026-07-18T10:31:00Z",
        "unsuppressed_findings_count": len(reported_findings),
        "unsuppressed_findings": reported_findings,
        "reviewed_suppressions": [] if suppressions is None else suppressions,
    }


def _verify(
    verifier: ModuleType,
    root: Path,
    rows: list[dict[str, Any]],
    artifact_sha256: str,
    report: dict[str, Any],
) -> list[str]:
    report_path = root / "datasets/scans/contact-v1.scan.json"
    _write_json(report_path, report)
    errors: list[str] = []
    verifier.verify_scan_report(
        root=root,
        dataset_id=DATASET_ID,
        dataset_version=DATASET_VERSION,
        artifact_sha256=artifact_sha256,
        rows=rows,
        implementer=IMPLEMENTER,
        independent_reviewer=INDEPENDENT_REVIEWER,
        scan={
            "path": "datasets/scans/contact-v1.scan.json",
            "sha256": _sha256_file(report_path),
        },
        errors=errors,
    )
    return errors


def _rows_with_public_contact(address: str = "recruiting@example.com") -> list[dict[str, Any]]:
    return [
        {
            "sample_id": "contact-1",
            "group_id": "group-1",
            "split": "development",
            "synthetic": False,
            "candidate": {"address": address},
        }
    ]


def _suppression_with_evidence(
    root: Path,
    rows: list[dict[str, Any]],
    artifact_sha256: str,
    *,
    reason_code: str,
    reviewer_id: str = INDEPENDENT_REVIEWER,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    finding = scan_rows(rows, artifact_sha256=artifact_sha256).findings[0]
    evidence_ref = f"datasets/scans/reviews/{finding.finding_id}.json"
    evidence = {
        "version": 1,
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "artifact_sha256": artifact_sha256,
        "finding_id": finding.finding_id,
        "rule_id": finding.rule_id,
        "target_ref": finding.target_ref,
        "reason_code": reason_code,
        "reviewer_id": reviewer_id,
        "reviewed_at": REVIEWED_AT,
        "justification": "Independently reviewed technical suppression.",
    }
    evidence_path = root / evidence_ref
    _write_json(evidence_path, evidence)
    suppression = {
        "finding_id": finding.finding_id,
        "rule_id": finding.rule_id,
        "target_ref": finding.target_ref,
        "reason_code": reason_code,
        "reviewer_id": reviewer_id,
        "reviewed_at": REVIEWED_AT,
        "evidence_ref": evidence_ref,
        "evidence_sha256": _sha256_file(evidence_path),
    }
    return suppression, evidence_path, evidence


def _assert_safe_error(errors: list[str], expected: str, *sensitive_values: str) -> None:
    assert any(expected in error for error in errors), errors
    for sensitive_value in sensitive_values:
        assert all(sensitive_value not in error for error in errors), errors


def test_rejects_v1_scan_report(tmp_path: Path) -> None:
    verifier = _load_verify_m1_module()
    root = _prepare_root(tmp_path)
    rows = [{"sample_id": "clean", "value": "no technical finding"}]
    artifact_sha256 = _artifact_sha256(rows)
    report = _report(artifact_sha256)
    report["version"] = 1

    errors = _verify(verifier, root, rows, artifact_sha256, report)

    _assert_safe_error(errors, "scan report.version: value does not match const")


@pytest.mark.parametrize(
    ("field", "forged_value", "expected"),
    [
        ("tool", "untrusted-scanner", "scan report tool mismatch"),
        ("rule_set_sha256", "0" * 64, "scan report rule_set_sha256 mismatch"),
    ],
)
def test_rejects_tool_or_rule_hash_mismatch(
    tmp_path: Path,
    field: str,
    forged_value: str,
    expected: str,
) -> None:
    verifier = _load_verify_m1_module()
    root = _prepare_root(tmp_path)
    rows = [{"sample_id": "clean", "value": "no technical finding"}]
    artifact_sha256 = _artifact_sha256(rows)
    report = _report(artifact_sha256)
    report[field] = forged_value

    errors = _verify(verifier, root, rows, artifact_sha256, report)

    _assert_safe_error(errors, expected)


def test_rejects_old_suppression_replayed_after_artifact_change(tmp_path: Path) -> None:
    verifier = _load_verify_m1_module()
    root = _prepare_root(tmp_path)
    old_rows = _rows_with_public_contact()
    old_artifact_sha256 = _artifact_sha256(old_rows)
    suppression, evidence_path, evidence = _suppression_with_evidence(
        root,
        old_rows,
        old_artifact_sha256,
        reason_code="public_recruiting_contact",
    )

    changed_address = "changed-contact@example.net"
    changed_rows = _rows_with_public_contact(changed_address)
    changed_artifact_sha256 = _artifact_sha256(changed_rows)
    evidence["artifact_sha256"] = changed_artifact_sha256
    _write_json(evidence_path, evidence)
    suppression["evidence_sha256"] = _sha256_file(evidence_path)
    report = _report(changed_artifact_sha256, suppressions=[suppression])

    errors = _verify(
        verifier,
        root,
        changed_rows,
        changed_artifact_sha256,
        report,
    )

    _assert_safe_error(
        errors,
        "technical scan could not be reproduced",
        changed_address,
    )


def test_rejects_missing_suppression_evidence(tmp_path: Path) -> None:
    verifier = _load_verify_m1_module()
    root = _prepare_root(tmp_path)
    rows = _rows_with_public_contact()
    artifact_sha256 = _artifact_sha256(rows)
    suppression, evidence_path, _evidence = _suppression_with_evidence(
        root,
        rows,
        artifact_sha256,
        reason_code="public_recruiting_contact",
    )
    evidence_path.unlink()

    errors = _verify(
        verifier,
        root,
        rows,
        artifact_sha256,
        _report(artifact_sha256, suppressions=[suppression]),
    )

    _assert_safe_error(errors, "scan suppression evidence is unavailable")


def test_rejects_suppression_evidence_hash_mismatch(tmp_path: Path) -> None:
    verifier = _load_verify_m1_module()
    root = _prepare_root(tmp_path)
    rows = _rows_with_public_contact()
    artifact_sha256 = _artifact_sha256(rows)
    suppression, _evidence_path, _evidence = _suppression_with_evidence(
        root,
        rows,
        artifact_sha256,
        reason_code="public_recruiting_contact",
    )
    suppression["evidence_sha256"] = "0" * 64

    errors = _verify(
        verifier,
        root,
        rows,
        artifact_sha256,
        _report(artifact_sha256, suppressions=[suppression]),
    )

    _assert_safe_error(errors, "scan suppression evidence hash mismatch")


def test_rejects_suppression_evidence_structured_binding_mismatch(tmp_path: Path) -> None:
    verifier = _load_verify_m1_module()
    root = _prepare_root(tmp_path)
    rows = _rows_with_public_contact()
    artifact_sha256 = _artifact_sha256(rows)
    suppression, evidence_path, evidence = _suppression_with_evidence(
        root,
        rows,
        artifact_sha256,
        reason_code="public_recruiting_contact",
    )
    evidence["dataset_version"] = "different-version"
    _write_json(evidence_path, evidence)
    suppression["evidence_sha256"] = _sha256_file(evidence_path)

    errors = _verify(
        verifier,
        root,
        rows,
        artifact_sha256,
        _report(artifact_sha256, suppressions=[suppression]),
    )

    _assert_safe_error(errors, "scan suppression evidence binding mismatch")


def test_rejects_suppression_reviewer_mismatch(tmp_path: Path) -> None:
    verifier = _load_verify_m1_module()
    root = _prepare_root(tmp_path)
    rows = _rows_with_public_contact()
    artifact_sha256 = _artifact_sha256(rows)
    suppression, _evidence_path, _evidence = _suppression_with_evidence(
        root,
        rows,
        artifact_sha256,
        reason_code="public_recruiting_contact",
        reviewer_id="different-reviewer",
    )

    errors = _verify(
        verifier,
        root,
        rows,
        artifact_sha256,
        _report(artifact_sha256, suppressions=[suppression]),
    )

    _assert_safe_error(errors, "scan suppression reviewer is not independent reviewer")


def test_rejects_public_contact_reason_for_credential(tmp_path: Path) -> None:
    verifier = _load_verify_m1_module()
    root = _prepare_root(tmp_path)
    credential = "AKIA" + "A" * 16
    rows = [{"sample_id": "credential-case", "token": credential}]
    artifact_sha256 = _artifact_sha256(rows)
    suppression, _evidence_path, _evidence = _suppression_with_evidence(
        root,
        rows,
        artifact_sha256,
        reason_code="public_recruiting_contact",
    )

    errors = _verify(
        verifier,
        root,
        rows,
        artifact_sha256,
        _report(artifact_sha256, suppressions=[suppression]),
    )

    _assert_safe_error(
        errors,
        "technical scan could not be reproduced",
        credential,
    )


def test_accepts_replayed_reviewed_public_contact_with_zero_unsuppressed(
    tmp_path: Path,
) -> None:
    verifier = _load_verify_m1_module()
    root = _prepare_root(tmp_path)
    rows = _rows_with_public_contact()
    artifact_sha256 = _artifact_sha256(rows)
    suppression, _evidence_path, _evidence = _suppression_with_evidence(
        root,
        rows,
        artifact_sha256,
        reason_code="public_recruiting_contact",
    )

    errors = _verify(
        verifier,
        root,
        rows,
        artifact_sha256,
        _report(artifact_sha256, suppressions=[suppression]),
    )

    assert errors == []


@pytest.mark.parametrize(
    ("findings", "reported_count", "expected"),
    [
        (
            [
                {
                    "finding_id": "a" * 64,
                    "rule_id": "d0.pii.email_like.v2",
                    "target_ref": "row:0:locator:" + "b" * 64,
                }
            ],
            0,
            "scan findings do not match reproduced scan",
        ),
        ([], 1, "scan finding count does not match reproduced scan"),
    ],
)
def test_rejects_reported_finding_list_or_count_mismatch(
    tmp_path: Path,
    findings: list[dict[str, Any]],
    reported_count: int,
    expected: str,
) -> None:
    verifier = _load_verify_m1_module()
    root = _prepare_root(tmp_path)
    rows = [{"sample_id": "clean", "value": "no technical finding"}]
    artifact_sha256 = _artifact_sha256(rows)
    report = _report(artifact_sha256, findings=findings)
    report["unsuppressed_findings_count"] = reported_count

    errors = _verify(verifier, root, rows, artifact_sha256, report)

    _assert_safe_error(errors, expected)
