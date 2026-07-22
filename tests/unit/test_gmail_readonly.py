from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from careerops.application.gmail_readonly import (
    GMAIL_REDACTED_SENDER_MAX_CHARS,
    GMAIL_REDACTED_SNIPPET_EXCERPT_MAX_CHARS,
    GMAIL_REDACTED_SUBJECT_MAX_CHARS,
    GMAIL_REDACTION_VERSION,
    GmailEvidenceRedactor,
    GmailJobMatchCandidate,
    GmailMetadataSignal,
    GmailRecruitingClassification,
    GmailRecruitingClassifier,
    GmailReviewPriority,
    GmailSignalReviewPlanner,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def metadata_signal(
    *,
    subject: str = "Interview with ExampleCorp for Senior Agent Engineer",
    sender: str = "Recruiter <recruiter@example.com>",
    snippet: str = "Hi Jane, are you available Tuesday at jane@example.com or +1 415 555 1212?",
) -> GmailMetadataSignal:
    return GmailMetadataSignal(
        provider_message_id="msg-123",
        provider_thread_id="thread-123",
        provider_history_id="987654321",
        account_subject="candidate@example.com",
        headers={
            "subject": subject,
            "from": sender,
            "date": "Mon, 20 Jul 2026 12:00:00 +0000",
        },
        label_ids=("INBOX",),
        snippet=snippet,
        received_at=NOW,
    )


def test_metadata_signal_rejects_raw_body_and_normalizes_headers() -> None:
    signal = metadata_signal()

    assert signal.subject == "Interview with ExampleCorp for Senior Agent Engineer"
    assert signal.sender == "Recruiter <recruiter@example.com>"
    assert tuple(signal.headers) == ("date", "from", "subject")

    with pytest.raises(ValueError, match="raw body"):
        GmailMetadataSignal(
            provider_message_id="msg-123",
            provider_thread_id="thread-123",
            provider_history_id="987654321",
            account_subject="candidate@example.com",
            headers={"subject": "Role", "body": "full email body is forbidden"},
            label_ids=("INBOX",),
            snippet="snippet only",
            received_at=NOW,
        )


def test_redactor_removes_pii_from_subject_headers_and_snippet() -> None:
    evidence = GmailEvidenceRedactor().redact(metadata_signal())

    rendered = f"{evidence.redacted_subject} {evidence.redacted_sender} {evidence.redacted_snippet}"
    assert "jane@example.com" not in rendered
    assert "415 555 1212" not in rendered
    assert "candidate@example.com" not in rendered
    assert "[email]" in rendered
    assert "[phone]" in rendered
    assert evidence.provider_message_id == "msg-123"
    assert evidence.content_sha256 != evidence.span_sha256


def test_classifier_matches_job_and_flags_review_required_ambiguity() -> None:
    classifier = GmailRecruitingClassifier()
    jobs = (
        GmailJobMatchCandidate(
            canonical_job_id=UUID("00000000-0000-0000-0000-000000000111"),
            company_name="ExampleCorp",
            title="Senior Agent Engineer",
            aliases=("Example",),
        ),
        GmailJobMatchCandidate(
            canonical_job_id=UUID("00000000-0000-0000-0000-000000000222"),
            company_name="OtherCorp",
            title="Backend Engineer",
        ),
    )

    decision = classifier.classify(metadata_signal(), candidate_jobs=jobs)

    assert decision.classification is GmailRecruitingClassification.INTERVIEW_INVITATION
    assert decision.review_priority is GmailReviewPriority.HIGH
    assert decision.matched_job_id == jobs[0].canonical_job_id
    assert decision.requires_review is True
    assert "JOB_MATCH_COMPANY_AND_TITLE" in decision.reason_codes

    ambiguous = classifier.classify(
        metadata_signal(subject="Interview for Engineer role"),
        candidate_jobs=jobs,
    )
    assert ambiguous.high_risk_ambiguity is True
    assert ambiguous.requires_review is True
    assert "AMBIGUOUS_JOB_MATCH" in ambiguous.reason_codes


def test_review_proposal_contains_only_redacted_evidence_not_raw_metadata() -> None:
    planner = GmailSignalReviewPlanner()
    signal = metadata_signal()
    decision = GmailRecruitingClassifier().classify(signal, candidate_jobs=())

    proposal = planner.build_review_proposal(
        signal=signal,
        decision=decision,
        actor_id=UUID("00000000-0000-0000-0000-000000000333"),
        signal_id=UUID("00000000-0000-0000-0000-000000000444"),
        created_at=NOW,
    )

    assert proposal is not None
    assert proposal.action_kind == "gmail_readonly_signal_review"
    assert proposal.payload["version"] == "gmail-readonly-review.v1"
    evidence = cast("dict[str, object]", proposal.payload["evidence"])
    assert evidence["redacted_subject"] == signal.subject
    assert evidence["redaction_version"] == GMAIL_REDACTION_VERSION
    assert "snippet" not in proposal.payload
    assert "headers" not in proposal.payload
    assert "Hi Jane" not in str(proposal.payload)
    assert "jane@example.com" not in str(proposal.payload)
    assert proposal.payload_hash


def test_review_proposal_aggressively_redacts_sensitive_metadata() -> None:
    signal = metadata_signal(
        subject="Interview with Alice Johnson on July 22, 2026 at 3:30 PM",
        sender='"Marcus Lee" <marcus.lee@agency.example>',
        snippet=(
            "Contact Priya Shah at 123 Main Street, Springfield, CA 94105. "
            "Calendar: https://calendar.google.com/calendar/u/0/r/eventedit?token=secret. "
            "Assessment: hackerrank.com/tests/private-abc. Compensation USD 185,000\u2013205,000."
        ),
    )
    decision = GmailRecruitingClassifier().classify(signal, candidate_jobs=())

    proposal = GmailSignalReviewPlanner().build_review_proposal(
        signal=signal,
        decision=decision,
        actor_id=UUID("00000000-0000-0000-0000-000000000333"),
        signal_id=UUID("00000000-0000-0000-0000-000000000444"),
        created_at=NOW,
    )

    assert proposal is not None
    rendered = str(proposal.payload)
    for secret in (
        "Alice Johnson",
        "July 22, 2026",
        "3:30 PM",
        "Marcus Lee",
        "marcus.lee@agency.example",
        "Priya Shah",
        "123 Main Street",
        "Springfield",
        "94105",
        "calendar.google.com",
        "token=secret",
        "hackerrank.com",
        "private-abc",
        "185,000",
        "205,000",
    ):
        assert secret not in rendered
    for marker in ("[name]", "[date]", "[time]", "[email]", "[address]", "[link]"):
        assert marker in rendered
    assert "[compensation]" in rendered
    assert GMAIL_REDACTION_VERSION in rendered


def test_redacted_persisted_fields_have_strict_documented_bounds() -> None:
    signal = metadata_signal(
        subject="Interview " + ("S" * 1900),
        sender="Recruiter " + ("r" * 1900),
        snippet="Assessment " + ("x" * 1989),
    )

    evidence = GmailEvidenceRedactor().redact(signal)

    assert len(evidence.redacted_subject) == GMAIL_REDACTED_SUBJECT_MAX_CHARS
    assert len(evidence.redacted_sender) == GMAIL_REDACTED_SENDER_MAX_CHARS
    assert len(evidence.redacted_snippet) == GMAIL_REDACTED_SNIPPET_EXCERPT_MAX_CHARS
    assert evidence.redacted_subject.endswith("…")
    assert evidence.redacted_sender.endswith("…")
    assert evidence.redacted_snippet.endswith("…")
    assert evidence.redaction_version == GMAIL_REDACTION_VERSION


def test_unrelated_message_does_not_create_review_proposal() -> None:
    signal = metadata_signal(
        subject="Your grocery receipt",
        sender="receipts@example.net",
        snippet="Thanks for shopping with us.",
    )
    decision = GmailRecruitingClassifier().classify(signal, candidate_jobs=())

    assert decision.classification is GmailRecruitingClassification.UNRELATED
    assert decision.requires_review is False
    assert (
        GmailSignalReviewPlanner().build_review_proposal(
            signal=signal,
            decision=decision,
            actor_id=UUID("00000000-0000-0000-0000-000000000333"),
            signal_id=UUID("00000000-0000-0000-0000-000000000444"),
            created_at=NOW,
        )
        is None
    )
