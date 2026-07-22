from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from types import MappingProxyType
from typing import cast
from uuid import UUID

import pytest
from pydantic import JsonValue

from careerops.application.application_prep import (
    ApprovedMaterialRef,
    CandidateMatchProfile,
    EvidenceFirstApplicationPreparer,
    EvidenceRef,
    PublicJobDiscovery,
)

NOW = datetime(2026, 7, 19, 16, 30, tzinfo=UTC)


def evidence() -> EvidenceRef:
    return EvidenceRef(
        evidence_id=UUID("00000000-0000-0000-0000-000000000101"),
        content_hash="a" * 64,
        span_hash="b" * 64,
        source_url="https://jobs.example.com/123",
        provider_id=None,
        sanitized_span="Senior Python role focused on agents and distributed systems.",
    )


def discovery() -> PublicJobDiscovery:
    return PublicJobDiscovery(
        canonical_job_id=UUID("00000000-0000-0000-0000-000000000201"),
        job_posting_id=UUID("00000000-0000-0000-0000-000000000202"),
        company_name="Example AI",
        title="Senior Agent Platform Engineer",
        canonical_url="https://jobs.example.com/123",
        source_type="public_ats",
        required_keywords=("Python", "Agents", "Distributed Systems"),
        evidence=(evidence(),),
    )


def profile() -> CandidateMatchProfile:
    return CandidateMatchProfile(
        candidate_id=UUID("00000000-0000-0000-0000-000000000301"),
        desired_keywords=("agents", "python", "distributed systems", "postgres"),
    )


def material() -> ApprovedMaterialRef:
    return ApprovedMaterialRef(
        material_id=UUID("00000000-0000-0000-0000-000000000401"),
        material_kind="resume",
        sha256="c" * 64,
        label_zh="中文简历 v1",
    )


def test_preparer_builds_deterministic_internal_draft_with_evidence() -> None:
    preparer = EvidenceFirstApplicationPreparer()

    first = preparer.prepare(
        discovery=discovery(),
        profile=profile(),
        materials=(material(),),
        now=NOW,
    )
    second = preparer.prepare(
        discovery=discovery(),
        profile=profile(),
        materials=(material(),),
        now=NOW,
    )

    assert first.action_kind == "create_internal_draft"
    assert first.resource_type == "canonical_job"
    assert first.payload_hash == second.payload_hash
    assert first.idempotency_key.endswith(first.payload_hash)
    assert first.prepared_at == NOW
    assert first.match_score == 1.0
    assert first.reason_codes == ("STRONG_KEYWORD_MATCH",)
    assert first.target["canonical_job_id"] == "00000000-0000-0000-0000-000000000201"
    assert first.target["candidate_id"] == "00000000-0000-0000-0000-000000000301"
    assert first.payload["draft"] == {
        "status": "review_required",
        "summary_zh": (
            "为 Example AI 的 Senior Agent Platform Engineer 生成内部申请草稿; "
            "提交前必须经过人工审核。"
        ),
    }
    evidence_payload = cast("list[dict[str, JsonValue]]", first.payload["evidence"])
    assert evidence_payload[0]["content_hash"] == "a" * 64
    assert first.attachment_refs[0]["sha256"] == "c" * 64
    assert isinstance(first.target, MappingProxyType)

    with pytest.raises(TypeError):
        first.target["company_name"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        first.payload["draft"]["status"] = "mutated"  # type: ignore[index]
    with pytest.raises(AttributeError):
        first.payload["evidence"].append({})  # type: ignore[union-attr]
    with pytest.raises(FrozenInstanceError):
        first.match_score = 0.0  # type: ignore[misc]


def test_preparer_deduplicates_across_run_times_and_input_order() -> None:
    preparer = EvidenceFirstApplicationPreparer()
    second_material = ApprovedMaterialRef(
        material_id=UUID("00000000-0000-0000-0000-000000000402"),
        material_kind="cover_letter",
        sha256="d" * 64,
        label_zh="求职信 v1",
    )
    first = preparer.prepare(
        discovery=discovery(),
        profile=profile(),
        materials=(material(), second_material),
        now=NOW,
    )
    second = preparer.prepare(
        discovery=discovery(),
        profile=profile(),
        materials=(second_material, material()),
        now=datetime(2026, 7, 19, 17, 30, tzinfo=UTC),
    )

    assert first.idempotency_key == second.idempotency_key
    assert first.payload_hash == second.payload_hash
    assert first.prepared_at != second.prepared_at


def test_preparer_rejects_duplicate_evidence_or_material_identity() -> None:
    with pytest.raises(ValueError, match="evidence ids"):
        PublicJobDiscovery(
            canonical_job_id=UUID("00000000-0000-0000-0000-000000000201"),
            job_posting_id=UUID("00000000-0000-0000-0000-000000000202"),
            company_name="Example AI",
            title="Engineer",
            canonical_url="https://jobs.example.com/123",
            source_type="public_ats",
            required_keywords=("python",),
            evidence=(evidence(), evidence()),
        )

    with pytest.raises(ValueError, match="material ids"):
        EvidenceFirstApplicationPreparer().prepare(
            discovery=discovery(),
            profile=profile(),
            materials=(material(), material()),
            now=NOW,
        )


def test_preparer_rejects_unproven_or_unapproved_inputs() -> None:
    preparer = EvidenceFirstApplicationPreparer()

    with pytest.raises(ValueError, match="evidence"):
        PublicJobDiscovery(
            canonical_job_id=UUID("00000000-0000-0000-0000-000000000201"),
            job_posting_id=UUID("00000000-0000-0000-0000-000000000202"),
            company_name="Example AI",
            title="Engineer",
            canonical_url="https://jobs.example.com/123",
            source_type="public_ats",
            required_keywords=("python",),
            evidence=(),
        )
    with pytest.raises(ValueError, match="source reference"):
        EvidenceRef(
            evidence_id=UUID("00000000-0000-0000-0000-000000000101"),
            content_hash="a" * 64,
            span_hash="b" * 64,
            source_url=None,
            provider_id=None,
            sanitized_span="Python role",
        )
    with pytest.raises(ValueError, match="approved material"):
        preparer.prepare(
            discovery=discovery(),
            profile=profile(),
            materials=(),
            now=NOW,
        )


def test_preparer_scores_exclusions_as_review_blocking_signal() -> None:
    draft = EvidenceFirstApplicationPreparer().prepare(
        discovery=discovery(),
        profile=CandidateMatchProfile(
            candidate_id=UUID("00000000-0000-0000-0000-000000000301"),
            desired_keywords=("python", "agents"),
            excluded_keywords=("distributed systems",),
        ),
        materials=(material(),),
        now=NOW,
    )

    assert draft.match_score == 0.0
    assert draft.reason_codes == ("EXCLUDED_KEYWORD_MATCH",)


def test_preparer_requires_timezone_aware_preparation_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        EvidenceFirstApplicationPreparer().prepare(
            discovery=discovery(),
            profile=profile(),
            materials=(material(),),
            now=datetime(2026, 7, 19, 16, 30),
        )
