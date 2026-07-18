from __future__ import annotations

from dataclasses import asdict

import pytest

from careerops.evaluation.d0_scan import (
    RULE_SET_SHA256,
    RULE_SET_VERSION,
    D0ScanFinding,
    D0SuppressionInput,
    D0TechnicalScanError,
    scan_rows,
)


def _sha() -> str:
    return "a" * 64


def _suppression(
    finding: D0ScanFinding,
    reason_code: str = "documented_false_positive",
    reviewed_at: str = "2026-07-18T10:30:00Z",
) -> D0SuppressionInput:
    return D0SuppressionInput(
        finding_id=finding.finding_id,
        rule_id=finding.rule_id,
        target_ref=finding.target_ref,
        reason_code=reason_code,
        reviewer_id="reviewer-1",
        reviewed_at=reviewed_at,
        evidence_ref="review-log/suppression-1",
        evidence_sha256=_sha(),
    )


def test_suppression_time_accepts_lowercase_rfc3339_z() -> None:
    rows = [{"sample_id": "s-1", "email": "recruiting@example.com"}]
    finding = scan_rows(rows).findings[0]

    report = scan_rows(
        rows,
        suppressions=[_suppression(finding, reviewed_at="2026-07-18T10:30:00z")],
    )

    assert report.findings == ()


def test_findings_do_not_leak_matched_secret_or_pii_text() -> None:
    secret = "gh" + "p_abcdefghijklmnopqrstuvwxyz0123456789"
    email = "founder@example.com"

    report = scan_rows(
        [
            {
                "sample_id": "s-1",
                "nested": {"token": f"credential={secret}", "contact": email},
            }
        ]
    )

    serialized = repr(report) + str(tuple(asdict(finding) for finding in report.findings))
    assert len(report.findings) == 2
    assert secret not in serialized
    assert email not in serialized
    assert {finding.rule_id for finding in report.findings} == {
        "d0.credential.github_token.v2",
        "d0.pii.email_like.v2",
    }
    assert len({finding.target_ref for finding in report.findings}) == 2
    assert all(finding.target_ref.startswith("row:0:locator:") for finding in report.findings)


def test_rejects_orphan_duplicate_and_unknown_reason_suppressions() -> None:
    report = scan_rows([{"sample_id": "s-1", "token": "AK" + "IA1234567890ABCDEF"}])
    finding = report.findings[0]

    with pytest.raises(D0TechnicalScanError, match="orphan suppression"):
        scan_rows(
            [{"sample_id": "s-1", "token": "AK" + "IA1234567890ABCDEF"}],
            suppressions=[
                D0SuppressionInput(
                    finding_id="missing",
                    rule_id=finding.rule_id,
                    target_ref=finding.target_ref,
                    reason_code="documented_false_positive",
                    reviewer_id="reviewer-1",
                    reviewed_at="2026-07-18T10:30:00Z",
                    evidence_ref="review-log/missing",
                    evidence_sha256=_sha(),
                )
            ],
        )

    duplicate = _suppression(finding)
    with pytest.raises(D0TechnicalScanError, match="duplicate suppression"):
        scan_rows(
            [{"sample_id": "s-1", "token": "AK" + "IA1234567890ABCDEF"}],
            suppressions=[duplicate, duplicate],
        )

    with pytest.raises(D0TechnicalScanError, match="unknown suppression reason_code"):
        scan_rows(
            [{"sample_id": "s-1", "token": "AK" + "IA1234567890ABCDEF"}],
            suppressions=[_suppression(finding, reason_code="manual_ok")],
        )


def test_legitimate_public_recruiting_contact_can_be_review_suppressed() -> None:
    initial = scan_rows([{"sample_id": "contact-1", "email": "recruiting@example.com"}])
    suppression = _suppression(initial.findings[0], reason_code="public_recruiting_contact")

    report = scan_rows(
        [{"sample_id": "contact-1", "email": "recruiting@example.com"}],
        suppressions=[suppression],
    )

    assert report.findings == ()
    assert report.reviewed_suppressions == (
        type(report.reviewed_suppressions[0])(**asdict(suppression)),
    )
    assert report.scan_scope == "technical_patterns_only"


def test_unsuppressed_findings_remain_in_report() -> None:
    rows = [
        {
            "sample_id": "mixed-1",
            "public_contact": "recruiting@example.com",
            "private_phone": "+1 (415) 555-1212",
        }
    ]
    initial = scan_rows(rows)
    email_finding = next(f for f in initial.findings if f.rule_id == "d0.pii.email_like.v2")
    phone_finding = next(f for f in initial.findings if f.rule_id == "d0.pii.phone_like.v2")

    report = scan_rows(
        rows,
        suppressions=[_suppression(email_finding, reason_code="public_recruiting_contact")],
    )

    assert report.findings == (phone_finding,)
    assert len(report.reviewed_suppressions) == 1


def test_rule_set_hash_and_finding_ids_are_deterministic() -> None:
    rows = [
        {"message": "call +1 415 555 1212"},
        {"sample_id": "s-2", "api_key": "AIza" + "a" * 35},
    ]

    first = scan_rows(rows)
    second = scan_rows(rows)

    assert first.rule_set_version == RULE_SET_VERSION == "d0-technical-scan-rules-v3"
    assert first.rule_set_sha256 == second.rule_set_sha256 == RULE_SET_SHA256
    assert [finding.finding_id for finding in first.findings] == [
        finding.finding_id for finding in second.findings
    ]
    assert [finding.rule_id for finding in first.findings] == [
        "d0.credential.google_api_key.v2",
        "d0.pii.phone_like.v2",
    ]
    assert all(len(finding.finding_id) == 64 for finding in first.findings)


def test_suppression_must_bind_rule_and_target_and_include_review_evidence() -> None:
    rows = [{"sample_id": "s-1", "email": "person@example.com"}]
    finding = scan_rows(rows).findings[0]

    with pytest.raises(D0TechnicalScanError, match="does not bind"):
        scan_rows(
            rows,
            suppressions=[
                _suppression(D0ScanFinding(finding.finding_id, finding.rule_id, "$.other"))
            ],
        )

    with pytest.raises(D0TechnicalScanError, match="reviewer_id"):
        bad = _suppression(finding)
        scan_rows(rows, suppressions=[D0SuppressionInput(**(asdict(bad) | {"reviewer_id": ""}))])

    with pytest.raises(D0TechnicalScanError, match="evidence_sha256"):
        bad = _suppression(finding)
        scan_rows(
            rows,
            suppressions=[D0SuppressionInput(**(asdict(bad) | {"evidence_sha256": "A" * 64}))],
        )
