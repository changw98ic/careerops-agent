from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from careerops.application.application_adapters import (
    BrowserIsolationMode,
    FormFieldSpec,
    HardStopCategory,
    SandboxApplicationPayload,
    SandboxBrowserSession,
    SyntheticApplicationFixture,
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
    ReconciliationState,
    SyntheticProviderState,
    SyntheticReconciler,
    SyntheticSubmissionDispatchPlanner,
    SyntheticSubmissionDispatchService,
    SyntheticSubmissionReceipt,
)
from careerops.policy.autopilot import AutopilotOutcome

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
SOURCE_DRAFT_HASH = "a" * 64


def form_fields() -> MappingProxyType[str, JsonValue]:
    return MappingProxyType(
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "resume_sha256": "b" * 64,
        }
    )


def payload_hash_for(intent_id: UUID, *, host: str, channel: str) -> str:
    return SandboxApplicationPayload(
        action_intent_id=intent_id,
        target_host=host,
        channel=channel,
        fields=form_fields(),
        source_draft_payload_hash=SOURCE_DRAFT_HASH,
        approved_material_hashes=("b" * 64,),
    ).payload_hash


def authority(
    *,
    outcome: AutopilotOutcome = AutopilotOutcome.ALLOW_AUTOPILOT_SUBMISSION,
    target_host: str = "sandbox.greenhouse.test",
    action_kind: str = "submit_application",
    channel: str = "synthetic:greenhouse-sandbox",
) -> DispatchAuthority:
    intent_id = uuid4()
    return DispatchAuthority(
        campaign_id=uuid4(),
        grant_version_id=uuid4(),
        authorization_id=uuid4(),
        action_intent_id=intent_id,
        payload_version_id=uuid4(),
        policy_decision_id=uuid4(),
        action_kind=action_kind,
        channel=channel,
        release_version="release-v1",
        payload_hash=payload_hash_for(intent_id, host=target_host, channel=channel),
        target_host=target_host,
        company_key="example-inc",
        policy_outcome=outcome,
        authorized_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )


def fixture(
    *,
    host: str = "sandbox.greenhouse.test",
    policy_text: str = "Automated submissions are allowed for this synthetic sandbox.",
    hard_stops: tuple[HardStopCategory, ...] = (),
) -> SyntheticApplicationFixture:
    return SyntheticApplicationFixture(
        fixture_id="fixture-1",
        adapter_id="greenhouse-sandbox",
        allowed_host=host,
        site_policy_text=policy_text,
        allowed_fields=(
            FormFieldSpec("first_name"),
            FormFieldSpec("last_name"),
            FormFieldSpec("resume_sha256"),
        ),
        hard_stop_markers=hard_stops,
    )


def session(
    intent_id: UUID,
    *,
    isolation_mode: BrowserIsolationMode = BrowserIsolationMode.PER_INTENT_CONTEXT,
    uses_real_credentials: bool = False,
    can_submit_real_provider: bool = False,
) -> SandboxBrowserSession:
    return SandboxBrowserSession(
        session_id=uuid4(),
        action_intent_id=intent_id,
        isolation_mode=isolation_mode,
        host="sandbox.greenhouse.test",
        uses_real_credentials=uses_real_credentials,
        can_submit_real_provider=can_submit_real_provider,
    )


def payload(
    intent_id: UUID,
    *,
    host: str = "sandbox.greenhouse.test",
    channel: str = "synthetic:greenhouse-sandbox",
) -> SandboxApplicationPayload:
    return SandboxApplicationPayload(
        action_intent_id=intent_id,
        target_host=host,
        channel=channel,
        fields=form_fields(),
        source_draft_payload_hash=SOURCE_DRAFT_HASH,
        approved_material_hashes=("b" * 64,),
    )


def release_qualification(
    *,
    stage: AutopilotReleaseStage = AutopilotReleaseStage.SYNTHETIC_SANDBOX,
) -> SyntheticReleaseQualification:
    return SyntheticReleaseQualification(
        adapter_id="greenhouse-sandbox",
        fixture_id="fixture-1",
        release_version="release-v1",
        stage=stage,
        evidence_hash="c" * 64,
        expires_at=NOW + timedelta(hours=1),
    )


def request(**overrides: object) -> QualifiedSyntheticDispatchRequest:
    item_authority = overrides.pop("authority", authority())
    assert isinstance(item_authority, DispatchAuthority)
    values: dict[str, object] = {
        "authority": item_authority,
        "fixture": fixture(),
        "session": session(item_authority.action_intent_id),
        "payload": payload(item_authority.action_intent_id),
        "release_qualification": release_qualification(),
        "now": NOW,
    }
    values.update(overrides)
    return QualifiedSyntheticDispatchRequest(**values)  # type: ignore[arg-type]


def test_planner_reserves_only_qualified_synthetic_dispatch() -> None:
    decision = SyntheticSubmissionDispatchPlanner().plan(request())

    assert decision.state is DispatchDecisionState.RESERVE_SYNTHETIC_DISPATCH
    assert decision.can_reserve is True
    assert decision.reason_codes == ("SYNTHETIC_DISPATCH_QUALIFIED_FOR_RESERVATION",)
    assert decision.reservation_key is not None
    assert decision.outbox_event_key == f"synthetic-dispatch:{decision.reservation_key}"
    assert decision.reconciliation_key is not None


def test_release_evidence_is_part_of_reservation_idempotency() -> None:
    item = request()
    changed_evidence = SyntheticReleaseQualification(
        adapter_id=item.release_qualification.adapter_id,
        fixture_id=item.release_qualification.fixture_id,
        release_version=item.release_qualification.release_version,
        stage=item.release_qualification.stage,
        evidence_hash="d" * 64,
        expires_at=item.release_qualification.expires_at,
    )

    first = SyntheticSubmissionDispatchPlanner().plan(item)
    second = SyntheticSubmissionDispatchPlanner().plan(
        request(authority=item.authority, release_qualification=changed_evidence)
    )

    assert first.can_reserve
    assert second.can_reserve
    assert first.reservation_key != second.reservation_key
    assert first.outbox_event_key != second.outbox_event_key


def test_dispatch_service_reserves_only_after_qualified_plan() -> None:
    class RecordingReservationStore:
        def __init__(self) -> None:
            self.calls: list[tuple[DispatchDecisionState, QualifiedSyntheticDispatchRequest]] = []

        def reserve_and_enqueue(
            self,
            decision: DispatchDecision,
            request: QualifiedSyntheticDispatchRequest,
        ) -> UUID:
            self.calls.append((decision.state, request))
            return UUID("00000000-0000-0000-0000-000000000777")

    store = RecordingReservationStore()
    qualified = request()
    result = SyntheticSubmissionDispatchService(store).dispatch(qualified)
    stopped = SyntheticSubmissionDispatchService(store).dispatch(
        request(generic_approval_present=True)
    )

    assert result.reservation_id == UUID("00000000-0000-0000-0000-000000000777")
    assert result.decision.can_reserve
    assert stopped.reservation_id is None
    assert stopped.decision.state is DispatchDecisionState.STOPPED
    assert store.calls == [(DispatchDecisionState.RESERVE_SYNTHETIC_DISPATCH, qualified)]


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        (
            {"authority": authority(outcome=AutopilotOutcome.REQUIRE_APPROVAL)},
            "AUTOPILOT_POLICY_OUTCOME_REQUIRED",
        ),
        ({"generic_approval_present": True}, "GENERIC_APPROVAL_NOT_EXECUTION_AUTHORITY"),
        ({"model_output_approval_present": True}, "MODEL_OUTPUT_NOT_EXECUTION_AUTHORITY"),
        ({"global_kill_switch_active": True}, "GLOBAL_KILL_SWITCH_ACTIVE"),
        ({"campaign_kill_switch_active": True}, "CAMPAIGN_KILL_SWITCH_ACTIVE"),
        ({"provider_kill_switch_active": True}, "PROVIDER_KILL_SWITCH_ACTIVE"),
        (
            {"fixture": fixture(policy_text="Terms unavailable.")},
            "ADAPTER_NOT_RELEASE_QUALIFIED",
        ),
        (
            {"fixture": fixture(hard_stops=(HardStopCategory.LEGAL_ATTESTATION,))},
            "QUALIFICATION_HARD_STOP_PRESENT",
        ),
        (
            {"provider_state": SyntheticProviderState.AMBIGUOUS},
            "AMBIGUOUS_PROVIDER_STATE",
        ),
        (
            {"release_qualification": release_qualification(stage=AutopilotReleaseStage.SHADOW)},
            "SHADOW_STAGE_ONLY",
        ),
    ],
)
def test_planner_stops_on_non_autopilot_or_ambiguous_authority(
    override: dict[str, object],
    reason: str,
) -> None:
    decision = SyntheticSubmissionDispatchPlanner().plan(request(**override))

    assert decision.state is DispatchDecisionState.STOPPED
    assert reason in decision.reason_codes
    assert decision.reservation_key is None
    assert decision.outbox_event_key is None


def test_planner_stops_when_synthetic_boundary_or_intent_binding_is_wrong() -> None:
    real_host_authority = authority(target_host="greenhouse.io")
    mismatched_fixture_authority = authority()
    cross_intent_authority = authority()

    real_host = SyntheticSubmissionDispatchPlanner().plan(
        request(
            authority=real_host_authority, payload=payload(real_host_authority.action_intent_id)
        )
    )
    mismatched_fixture = SyntheticSubmissionDispatchPlanner().plan(
        request(
            authority=mismatched_fixture_authority,
            fixture=fixture(host="other-sandbox.test"),
            payload=payload(mismatched_fixture_authority.action_intent_id),
        )
    )
    cross_intent = SyntheticSubmissionDispatchPlanner().plan(
        request(
            authority=cross_intent_authority,
            payload=payload(uuid4()),
        )
    )

    assert "NON_SYNTHETIC_TARGET_HOST" in real_host.reason_codes
    assert "FIXTURE_TARGET_HOST_MISMATCH" in real_host.reason_codes
    assert "FIXTURE_TARGET_HOST_MISMATCH" in mismatched_fixture.reason_codes
    assert "PAYLOAD_INTENT_MISMATCH" in cross_intent.reason_codes
    assert "SYNTHETIC_DRY_RUN_NOT_READY" in cross_intent.reason_codes


def test_planner_stops_when_the_sandbox_payload_hash_drifted() -> None:
    item_authority = authority()
    drifted_payload = SandboxApplicationPayload(
        action_intent_id=item_authority.action_intent_id,
        target_host="sandbox.greenhouse.test",
        channel="synthetic:greenhouse-sandbox",
        fields=MappingProxyType(
            {
                "first_name": "Grace",
                "last_name": "Lovelace",
                "resume_sha256": "b" * 64,
            }
        ),
        source_draft_payload_hash=SOURCE_DRAFT_HASH,
        approved_material_hashes=("b" * 64,),
    )

    decision = SyntheticSubmissionDispatchPlanner().plan(
        request(authority=item_authority, payload=drifted_payload)
    )

    assert decision.state is DispatchDecisionState.STOPPED
    assert "PAYLOAD_HASH_MISMATCH" in decision.reason_codes


def test_planner_stops_before_reservation_when_dry_run_is_not_isolated() -> None:
    item_authority = authority()
    decision = SyntheticSubmissionDispatchPlanner().plan(
        request(
            authority=item_authority,
            session=session(
                item_authority.action_intent_id,
                isolation_mode=BrowserIsolationMode.SHARED_PROFILE,
                uses_real_credentials=True,
                can_submit_real_provider=True,
            ),
            payload=payload(item_authority.action_intent_id),
        )
    )

    assert decision.state is DispatchDecisionState.STOPPED
    assert "SYNTHETIC_DRY_RUN_NOT_READY" in decision.reason_codes
    assert "DRY_RUN_HARD_STOP_PRESENT" in decision.reason_codes


def test_planner_rejects_non_submission_action_and_wrong_synthetic_channel() -> None:
    not_submission = SyntheticSubmissionDispatchPlanner().plan(
        request(authority=authority(action_kind="create_internal_draft"))
    )
    wrong_channel = SyntheticSubmissionDispatchPlanner().plan(
        request(authority=authority(channel="synthetic:other-adapter"))
    )

    assert "ACTION_NOT_AUTOPILOT_ELIGIBLE" in not_submission.reason_codes
    assert "SYNTHETIC_CHANNEL_MISMATCH" in wrong_channel.reason_codes


def test_reconciler_requires_recovery_for_ambiguous_provider_state() -> None:
    receipt = SyntheticSubmissionReceipt(
        provider="synthetic",
        provider_resource_id="receipt-1",
        reconciliation_key="reconcile-1",
        provider_state=SyntheticProviderState.AMBIGUOUS,
        received_at=NOW,
    )

    decision = SyntheticReconciler().decide(receipt)

    assert decision.state is ReconciliationState.RECONCILIATION_REQUIRED
    assert decision.reason_code == "SYNTHETIC_PROVIDER_STATE_AMBIGUOUS"


def test_reconciler_rejects_non_synthetic_receipts() -> None:
    with pytest.raises(ValueError, match="must use the synthetic provider"):
        SyntheticSubmissionReceipt(
            provider="greenhouse",
            provider_resource_id="receipt-1",
            reconciliation_key="reconcile-1",
            provider_state=SyntheticProviderState.CONFIRMED,
            received_at=NOW,
        )
