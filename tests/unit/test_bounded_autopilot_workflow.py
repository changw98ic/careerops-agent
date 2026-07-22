from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import UUID, uuid4

from careerops.application.application_adapters import (
    BrowserIsolationMode,
    FormFieldSpec,
    SandboxApplicationPayload,
    SandboxBrowserSession,
    SyntheticApplicationFixture,
)
from careerops.application.application_prep import (
    ApprovedMaterialRef,
    CandidateMatchProfile,
    EvidenceFirstApplicationPreparer,
    EvidenceRef,
    PublicJobDiscovery,
)
from careerops.application.release_qualification import (
    AutopilotReleaseStage,
    SyntheticReleaseQualification,
)
from careerops.application.submission_dispatch import (
    DispatchAuthority,
    DispatchDecision,
    DispatchDecisionState,
    QualifiedSyntheticDispatchRequest,
    SyntheticSubmissionDispatchPlanner,
    SyntheticSubmissionDispatchService,
)
from careerops.policy.autopilot import AutopilotOutcome

NOW = datetime(2026, 7, 20, 11, 0, tzinfo=UTC)


class RecordingReservationStore:
    def __init__(self) -> None:
        self.requests: list[QualifiedSyntheticDispatchRequest] = []
        self.decisions: list[DispatchDecision] = []

    def reserve_and_enqueue(
        self,
        decision: DispatchDecision,
        request: QualifiedSyntheticDispatchRequest,
    ) -> UUID:
        assert decision.state is DispatchDecisionState.RESERVE_SYNTHETIC_DISPATCH
        self.decisions.append(decision)
        self.requests.append(request)
        return UUID("00000000-0000-0000-0000-000000000999")


def test_bounded_autopilot_composes_draft_to_synthetic_reservation_without_provider_write() -> None:
    draft = EvidenceFirstApplicationPreparer().prepare(
        discovery=PublicJobDiscovery(
            canonical_job_id=UUID("00000000-0000-0000-0000-000000000201"),
            job_posting_id=UUID("00000000-0000-0000-0000-000000000202"),
            company_name="Example AI",
            title="Agent Engineer",
            canonical_url="https://jobs.example.com/123",
            source_type="public_ats",
            required_keywords=("python", "agents"),
            evidence=(
                EvidenceRef(
                    evidence_id=UUID("00000000-0000-0000-0000-000000000101"),
                    content_hash="a" * 64,
                    span_hash="b" * 64,
                    source_url="https://jobs.example.com/123",
                    provider_id=None,
                    sanitized_span="Python agents role.",
                ),
            ),
        ),
        profile=CandidateMatchProfile(
            candidate_id=UUID("00000000-0000-0000-0000-000000000301"),
            desired_keywords=("python", "agents"),
        ),
        materials=(
            ApprovedMaterialRef(
                material_id=UUID("00000000-0000-0000-0000-000000000401"),
                material_kind="resume",
                sha256="c" * 64,
                label_zh="简历",
            ),
        ),
        now=NOW,
    )
    assert draft.action_kind == "create_internal_draft"
    assert draft.payload["draft"] == {
        "status": "review_required",
        "summary_zh": "为 Example AI 的 Agent Engineer 生成内部申请草稿; 提交前必须经过人工审核。",
    }

    intent_id = uuid4()
    synthetic_payload = SandboxApplicationPayload(
        action_intent_id=intent_id,
        target_host="sandbox.greenhouse.test",
        channel="synthetic:greenhouse-sandbox",
        fields=MappingProxyType(
            {
                "first_name": "Ada",
                "last_name": "Lovelace",
                "resume_sha256": "c" * 64,
            }
        ),
        source_draft_payload_hash=draft.payload_hash,
        approved_material_hashes=("c" * 64,),
    )
    request = QualifiedSyntheticDispatchRequest(
        authority=DispatchAuthority(
            campaign_id=uuid4(),
            grant_version_id=uuid4(),
            authorization_id=uuid4(),
            action_intent_id=intent_id,
            payload_version_id=uuid4(),
            policy_decision_id=uuid4(),
            action_kind="submit_application",
            channel="synthetic:greenhouse-sandbox",
            release_version="release-v1",
            payload_hash=synthetic_payload.payload_hash,
            target_host="sandbox.greenhouse.test",
            company_key="example-ai",
            policy_outcome=AutopilotOutcome.ALLOW_AUTOPILOT_SUBMISSION,
            authorized_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        ),
        fixture=SyntheticApplicationFixture(
            fixture_id="fixture-1",
            adapter_id="greenhouse-sandbox",
            allowed_host="sandbox.greenhouse.test",
            site_policy_text="Automated submissions are allowed for this synthetic sandbox.",
            allowed_fields=(
                FormFieldSpec("first_name"),
                FormFieldSpec("last_name"),
                FormFieldSpec("resume_sha256"),
            ),
        ),
        session=SandboxBrowserSession(
            session_id=uuid4(),
            action_intent_id=intent_id,
            isolation_mode=BrowserIsolationMode.PER_INTENT_CONTEXT,
            host="sandbox.greenhouse.test",
        ),
        payload=synthetic_payload,
        release_qualification=SyntheticReleaseQualification(
            adapter_id="greenhouse-sandbox",
            fixture_id="fixture-1",
            release_version="release-v1",
            stage=AutopilotReleaseStage.SYNTHETIC_SANDBOX,
            evidence_hash="d" * 64,
            expires_at=NOW + timedelta(hours=1),
        ),
        now=NOW,
    )

    decision = SyntheticSubmissionDispatchPlanner().plan(request)
    store = RecordingReservationStore()
    result = SyntheticSubmissionDispatchService(
        store,
        planner=SyntheticSubmissionDispatchPlanner(),
    ).dispatch(request)

    assert decision.can_reserve
    assert result.reservation_id == UUID("00000000-0000-0000-0000-000000000999")
    assert result.decision == decision
    assert store.requests == [request]
    assert store.decisions == [decision]
    assert request.authority.target_host.endswith(".test")
    assert request.payload.source_draft_payload_hash == draft.payload_hash
    assert request.payload.payload_hash == request.authority.payload_hash
