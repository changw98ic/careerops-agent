from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest

from careerops.application.application_handoff import (
    ManualApplicationHandoffBuilder,
    ManualSubmissionAttestation,
)
from careerops.application.application_prep import (
    ApprovedMaterialRef,
    CandidateMatchProfile,
    EvidenceFirstApplicationPreparer,
    EvidenceRef,
    PreparedApplicationDraft,
    PublicJobDiscovery,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def prepared_draft(
    *,
    canonical_url: str = "https://jobs.example.test/openings/123",
    source_url: str | None = None,
):
    discovery = PublicJobDiscovery(
        canonical_job_id=uuid4(),
        job_posting_id=uuid4(),
        company_name="Example",
        title="Platform Engineer",
        canonical_url=canonical_url,
        source_type="official_careers",
        required_keywords=("python",),
        evidence=(
            EvidenceRef(
                evidence_id=uuid4(),
                content_hash=HASH_A,
                span_hash=HASH_B,
                source_url=source_url or canonical_url,
                provider_id=None,
                sanitized_span="Platform Engineer application page",
            ),
        ),
    )
    return EvidenceFirstApplicationPreparer().prepare(
        discovery=discovery,
        profile=CandidateMatchProfile(candidate_id=uuid4(), desired_keywords=("python",)),
        materials=(
            ApprovedMaterialRef(
                material_id=uuid4(),
                material_kind="resume",
                sha256=HASH_C,
                label_zh="简历",
            ),
        ),
        now=NOW,
    )


def draft_candidate_id(draft: PreparedApplicationDraft) -> UUID:
    candidate_id = draft.target.get("candidate_id")
    assert isinstance(candidate_id, str)
    return UUID(candidate_id)


def test_manual_handoff_binds_reviewed_draft_target_evidence_and_materials() -> None:
    draft = prepared_draft()

    handoff = ManualApplicationHandoffBuilder().prepare(
        draft=draft,
        candidate_id=draft_candidate_id(draft),
        now=NOW,
    )

    assert handoff.target_url == "https://jobs.example.test/openings/123"
    assert handoff.source_draft_payload_hash == draft.payload_hash
    assert handoff.payload["provider_execution"] == "disabled"
    assert handoff.payload["mode"] == "human_final_submission"
    assert handoff.attachment_refs[0]["sha256"] == HASH_C
    assert handoff.idempotency_key.endswith(handoff.payload_hash)
    assert "MFA" in handoff.instructions_zh[1]


def test_manual_handoff_rejects_source_evidence_from_an_unrelated_origin() -> None:
    draft = prepared_draft(source_url="https://unrelated.example.test/job/123")

    with pytest.raises(ValueError, match="same-origin"):
        ManualApplicationHandoffBuilder().prepare(
            draft=draft,
            candidate_id=draft_candidate_id(draft),
            now=NOW,
        )


def test_manual_handoff_rejects_a_candidate_other_than_the_draft_owner() -> None:
    draft = prepared_draft()

    with pytest.raises(ValueError, match="candidate must match"):
        ManualApplicationHandoffBuilder().prepare(
            draft=draft,
            candidate_id=uuid4(),
            now=NOW,
        )


@pytest.mark.parametrize(
    "canonical_url",
    (
        "http://jobs.example.test/openings/123",
        "https://user:secret@jobs.example.test/openings/123",
        "https://jobs.example.test/openings/123#fragment",
        "https://localhost/openings/123",
        "https://127.0.0.1/openings/123",
    ),
)
def test_manual_handoff_rejects_non_public_or_non_https_target_urls(canonical_url: str) -> None:
    draft = prepared_draft(canonical_url=canonical_url, source_url=canonical_url)

    with pytest.raises(ValueError, match=r"HTTPS|public DNS|credentials or a fragment"):
        ManualApplicationHandoffBuilder().prepare(
            draft=draft,
            candidate_id=draft_candidate_id(draft),
            now=NOW,
        )


def test_manual_handoff_normalizes_source_evidence_url_before_hashing() -> None:
    draft = prepared_draft(source_url="https://JOBS.EXAMPLE.TEST")

    handoff = ManualApplicationHandoffBuilder().prepare(
        draft=draft,
        candidate_id=draft_candidate_id(draft),
        now=NOW,
    )

    assert handoff.evidence[0].source_url == "https://jobs.example.test/"


def test_manual_handoff_rejects_provider_only_evidence_without_a_public_source_url() -> None:
    discovery = PublicJobDiscovery(
        canonical_job_id=uuid4(),
        job_posting_id=uuid4(),
        company_name="Example",
        title="Platform Engineer",
        canonical_url="https://jobs.example.test/openings/123",
        source_type="official_careers",
        required_keywords=("python",),
        evidence=(
            EvidenceRef(
                evidence_id=uuid4(),
                content_hash=HASH_A,
                span_hash=HASH_B,
                source_url=None,
                provider_id="official_provider",
                sanitized_span="Platform Engineer application page",
            ),
        ),
    )
    profile = CandidateMatchProfile(candidate_id=uuid4(), desired_keywords=("python",))
    draft = EvidenceFirstApplicationPreparer().prepare(
        discovery=discovery,
        profile=profile,
        materials=(
            ApprovedMaterialRef(
                material_id=uuid4(),
                material_kind="resume",
                sha256=HASH_C,
                label_zh="简历",
            ),
        ),
        now=NOW,
    )

    with pytest.raises(ValueError, match="same-origin"):
        ManualApplicationHandoffBuilder().prepare(
            draft=draft,
            candidate_id=profile.candidate_id,
            now=NOW,
        )


def test_manual_handoff_rejects_a_tampered_prepared_draft() -> None:
    draft = prepared_draft()
    tampered = type(draft)(
        prepared_at=draft.prepared_at,
        action_kind=draft.action_kind,
        resource_type=draft.resource_type,
        resource_id=draft.resource_id,
        idempotency_key=draft.idempotency_key,
        target=draft.target,
        payload=MappingProxyType({"evidence": []}),
        attachment_refs=draft.attachment_refs,
        payload_hash=draft.payload_hash,
        match_score=draft.match_score,
        reason_codes=draft.reason_codes,
    )

    with pytest.raises(ValueError, match="payload hash"):
        ManualApplicationHandoffBuilder().prepare(
            draft=tampered,
            candidate_id=draft_candidate_id(draft),
            now=NOW,
        )


def test_manual_handoff_hash_changes_with_target_evidence_or_materials() -> None:
    draft = prepared_draft()
    candidate_id = draft_candidate_id(draft)
    handoff = ManualApplicationHandoffBuilder().prepare(
        draft=draft,
        candidate_id=candidate_id,
        now=NOW,
    )
    changed_evidence = type(handoff.evidence[0])(
        evidence_id=handoff.evidence[0].evidence_id,
        content_hash=HASH_B,
        span_hash=handoff.evidence[0].span_hash,
        source_url=handoff.evidence[0].source_url,
        provider_id=handoff.evidence[0].provider_id,
        sanitized_span=handoff.evidence[0].sanitized_span,
    )
    changed_material = ApprovedMaterialRef(
        material_id=uuid4(),
        material_kind="resume",
        sha256=HASH_A,
        label_zh="新简历",
    )

    changed_evidence_handoff = type(handoff)(
        prepared_at=handoff.prepared_at,
        candidate_id=handoff.candidate_id,
        canonical_job_id=handoff.canonical_job_id,
        job_posting_id=handoff.job_posting_id,
        target_url=handoff.target_url,
        source_draft_payload_hash=handoff.source_draft_payload_hash,
        evidence=(changed_evidence,),
        approved_materials=handoff.approved_materials,
    )
    changed_material_handoff = type(handoff)(
        prepared_at=handoff.prepared_at,
        candidate_id=handoff.candidate_id,
        canonical_job_id=handoff.canonical_job_id,
        job_posting_id=handoff.job_posting_id,
        target_url=handoff.target_url,
        source_draft_payload_hash=handoff.source_draft_payload_hash,
        evidence=handoff.evidence,
        approved_materials=(changed_material,),
    )

    assert changed_evidence_handoff.payload_hash != handoff.payload_hash
    assert changed_material_handoff.payload_hash != handoff.payload_hash


def test_manual_submission_attestation_requires_a_reference_and_aware_time() -> None:
    with pytest.raises(ValueError, match="receipt_reference"):
        ManualSubmissionAttestation(receipt_reference="", submitted_at=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualSubmissionAttestation(
            receipt_reference="confirmation-123",
            submitted_at=datetime(2026, 7, 20, 12, 0),
        )
