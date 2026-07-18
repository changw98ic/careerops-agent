from __future__ import annotations

from dataclasses import replace

import pytest

from careerops.evaluation.d0_scan import (
    D0ScanFinding,
    D0SuppressionInput,
    D0TechnicalScanError,
    scan_rows,
)


def _suppression(
    finding: D0ScanFinding,
    *,
    reason_code: str = "documented_false_positive",
) -> D0SuppressionInput:
    return D0SuppressionInput(
        finding_id=finding.finding_id,
        rule_id=finding.rule_id,
        target_ref=finding.target_ref,
        reason_code=reason_code,
        reviewer_id="independent-reviewer",
        reviewed_at="2026-07-18T10:30:00Z",
        evidence_ref="review-log/security-regression",
        evidence_sha256="a" * 64,
    )


def test_target_reference_redacts_sample_id_and_arbitrary_json_keys() -> None:
    sample_id = "private-candidate-identifier-73c6"
    sensitive_key = "field-owned-by-alice-at-example-dot-com"
    matched_email = "alice@example.com"

    report = scan_rows(
        [
            {
                "sample_id": sample_id,
                "nested": {sensitive_key: matched_email},
            }
        ]
    )

    assert len(report.findings) == 1
    target_ref = report.findings[0].target_ref
    assert sample_id not in target_ref
    assert sensitive_key not in target_ref
    assert matched_email not in target_ref


@pytest.mark.parametrize("artifact_sha256", [1, True, b"digest", object()])
def test_invalid_artifact_hash_types_raise_scan_error(artifact_sha256: object) -> None:
    with pytest.raises(D0TechnicalScanError, match="artifact_sha256"):
        scan_rows([], artifact_sha256=artifact_sha256)  # type: ignore[arg-type]


@pytest.mark.parametrize("suppressions", [None, "not-a-sequence", [object()]])
def test_invalid_suppression_container_or_entries_raise_scan_error(
    suppressions: object,
) -> None:
    with pytest.raises(D0TechnicalScanError, match="suppression"):
        scan_rows([], suppressions=suppressions)  # type: ignore[arg-type]


def test_finding_id_is_deterministic_but_bound_to_the_matched_value() -> None:
    first_rows = [{"sample_id": "stable-sample", "contact": "alice@example.com"}]
    changed_rows = [{"sample_id": "stable-sample", "contact": "bob@example.com"}]

    first = scan_rows(first_rows).findings[0]
    repeated = scan_rows(first_rows).findings[0]
    changed = scan_rows(changed_rows).findings[0]

    assert first.finding_id == repeated.finding_id
    assert first.finding_id != changed.finding_id

    stale_suppression = _suppression(first)
    with pytest.raises(D0TechnicalScanError, match="orphan suppression"):
        scan_rows(changed_rows, suppressions=[stale_suppression])


def test_repeated_matches_at_one_path_have_unique_ids_and_independent_suppressions() -> None:
    repeated_email = "person@example.com"
    rows = [
        {
            "sample_id": "multi-match",
            "contact": f"primary={repeated_email}; backup={repeated_email}",
        }
    ]

    initial = scan_rows(rows)
    email_findings = tuple(
        finding for finding in initial.findings if finding.rule_id == "d0.pii.email_like.v2"
    )

    assert len(email_findings) == 2
    assert len({finding.finding_id for finding in email_findings}) == 2
    assert len({finding.target_ref for finding in email_findings}) == 1

    rescanned = scan_rows(rows, suppressions=[_suppression(email_findings[0])])

    assert tuple(finding.finding_id for finding in rescanned.findings) == (
        email_findings[1].finding_id,
    )
    assert len(rescanned.reviewed_suppressions) == 1


def test_public_contact_reason_cannot_suppress_a_credential_finding() -> None:
    rows = [{"sample_id": "credential-case", "token": "AKIA" + "A" * 16}]
    finding = scan_rows(rows).findings[0]
    assert finding.rule_id.startswith("d0.credential.")

    with pytest.raises(D0TechnicalScanError, match="public_recruiting_contact"):
        scan_rows(
            rows,
            suppressions=[
                _suppression(finding, reason_code="public_recruiting_contact"),
            ],
        )


@pytest.mark.parametrize(
    ("tampered_field", "tampered_value"),
    [
        ("rule_id", "d0.pii.phone_like.v2"),
        ("target_ref", "opaque-but-wrong-target"),
    ],
)
def test_suppression_mapping_rejects_finding_id_rebinding(
    tampered_field: str,
    tampered_value: str,
) -> None:
    rows = [{"sample_id": "binding-case", "contact": "person@example.com"}]
    finding = scan_rows(rows).findings[0]
    valid = _suppression(finding)
    forged = replace(valid, **{tampered_field: tampered_value})

    with pytest.raises(D0TechnicalScanError, match="does not bind"):
        scan_rows(rows, suppressions=[forged])


def test_duplicate_suppression_for_one_finding_is_rejected() -> None:
    rows = [{"sample_id": "duplicate-case", "contact": "person@example.com"}]
    finding = scan_rows(rows).findings[0]
    suppression = _suppression(finding)

    with pytest.raises(D0TechnicalScanError, match="duplicate suppression"):
        scan_rows(rows, suppressions=[suppression, suppression])
