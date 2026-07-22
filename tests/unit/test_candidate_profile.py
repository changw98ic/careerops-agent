from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from careerops.application.candidate_profile import (
    CandidateJobPreferencesV1,
    CandidateMaterialBundleDraft,
    CandidateMaterialKind,
    CandidateMaterialRefV1,
    CandidateProfileDocumentV1,
    CandidateProfileSnapshotDraft,
    CandidateSeniority,
    WorkAuthorizationStatus,
    WorkMode,
)

ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000002")


def material(
    *,
    material_id: int = 10,
    content_object_id: int = 20,
    digest: str = "a" * 64,
    kind: CandidateMaterialKind = CandidateMaterialKind.RESUME,
    filename: str = "resume.pdf",
) -> CandidateMaterialRefV1:
    return CandidateMaterialRefV1(
        material_id=UUID(int=material_id),
        content_object_id=UUID(int=content_object_id),
        kind=kind,
        label="简历" if kind is CandidateMaterialKind.RESUME else "作品集",
        filename=filename,
        media_type="application/pdf",
        sha256=digest,
        object_key=f"sha256/{digest[:2]}/{digest[2:4]}/{digest}",
        byte_size=2048,
    )


def profile() -> CandidateProfileDocumentV1:
    return CandidateProfileDocumentV1(
        skills=(" Python ", "agents", "python"),
        role_titles=("AI Engineer",),
        seniority=CandidateSeniority.SENIOR,
        years_experience=8,
        work_authorization=WorkAuthorizationStatus.UNKNOWN,
        requires_sponsorship=None,
    )


def preferences() -> CandidateJobPreferencesV1:
    return CandidateJobPreferencesV1(
        target_titles=("AI Engineer",),
        required_skills=("Python",),
        preferred_keywords=("agents",),
        excluded_keywords=("unpaid",),
        work_modes=(WorkMode.REMOTE,),
        sponsorship_allowed=None,
        minimum_match_score=0.75,
    )


def bundle(
    *,
    bundle_id: int = 30,
    item: CandidateMaterialRefV1 | None = None,
) -> CandidateMaterialBundleDraft:
    return CandidateMaterialBundleDraft(
        owner_user_id=ACTOR_ID,
        candidate_id=CANDIDATE_ID,
        bundle_id=UUID(int=bundle_id),
        materials=(item or material(),),
    )


def test_profile_and_preferences_are_normalized_and_keep_unknowns_explicit() -> None:
    candidate_profile = profile()
    candidate_preferences = preferences()

    assert candidate_profile.skills == ("agents", "python")
    assert candidate_profile.role_titles == ("ai engineer",)
    assert candidate_profile.to_json()["requires_sponsorship"] is None
    assert candidate_preferences.target_titles == ("ai engineer",)
    assert candidate_preferences.to_json()["sponsorship_allowed"] is None


def test_bundle_and_snapshot_hashes_ignore_generated_catalog_ids() -> None:
    first = bundle()
    second = bundle(
        bundle_id=31,
        item=material(material_id=11, content_object_id=21),
    )

    assert first.manifest_json != second.manifest_json
    assert first.canonical_json == second.canonical_json
    assert first.bundle_sha256 == second.bundle_sha256

    first_snapshot = CandidateProfileSnapshotDraft(
        owner_user_id=ACTOR_ID,
        candidate_id=CANDIDATE_ID,
        profile=profile(),
        preferences=preferences(),
        material_bundle=first,
    )
    second_snapshot = replace(first_snapshot, material_bundle=second)
    assert first_snapshot.snapshot_sha256 == second_snapshot.snapshot_sha256


def test_hash_chain_changes_when_material_bytes_change() -> None:
    first = bundle()
    changed = bundle(item=material(digest="b" * 64))

    assert first.bundle_sha256 != changed.bundle_sha256
    assert CandidateProfileSnapshotDraft(
        owner_user_id=ACTOR_ID,
        candidate_id=CANDIDATE_ID,
        profile=profile(),
        preferences=preferences(),
        material_bundle=first,
    ).snapshot_sha256 != CandidateProfileSnapshotDraft(
        owner_user_id=ACTOR_ID,
        candidate_id=CANDIDATE_ID,
        profile=profile(),
        preferences=preferences(),
        material_bundle=changed,
    ).snapshot_sha256


def test_bundle_requires_exactly_one_resume_and_owner_binding() -> None:
    with pytest.raises(ValueError, match="exactly one resume"):
        CandidateMaterialBundleDraft(
            owner_user_id=ACTOR_ID,
            candidate_id=CANDIDATE_ID,
            bundle_id=UUID(int=40),
            materials=(
                material(kind=CandidateMaterialKind.PORTFOLIO, filename="portfolio.pdf"),
            ),
        )

    with pytest.raises(ValueError, match="owner"):
        CandidateProfileSnapshotDraft(
            owner_user_id=UUID(int=999),
            candidate_id=CANDIDATE_ID,
            profile=profile(),
            preferences=preferences(),
            material_bundle=bundle(),
        )


def test_preferences_reject_contradictory_hard_filters() -> None:
    with pytest.raises(ValueError, match="overlap"):
        CandidateJobPreferencesV1(
            target_titles=("engineer",),
            required_skills=("python",),
            excluded_skills=("Python",),
        )


def test_material_rejects_paths_and_digest_mismatches() -> None:
    with pytest.raises(ValueError, match="path"):
        replace(material(), filename="private/resume.pdf")
    with pytest.raises(ValueError, match="CAS key"):
        replace(material(), object_key=f"sha256/cc/dd/{'d' * 64}")
