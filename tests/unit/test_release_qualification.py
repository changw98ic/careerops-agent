from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from careerops.application.release_qualification import (
    AutopilotReleaseStage,
    QualifiedReleaseGate,
    ReleaseGateOutcome,
    ReleaseQualification,
    ReleaseQualificationActorRole,
    ReleaseQualificationBinding,
    ReleaseQualificationDecision,
    ReleaseQualificationEvidence,
    ReleaseQualificationLifecycle,
    ReleaseQualificationRequestBinding,
    ReleaseQualificationStatus,
    RolloutActualDecision,
    RolloutExpectedDecision,
    RolloutMeasurementSummary,
    RolloutObservation,
    SyntheticReleaseGate,
    SyntheticReleaseQualification,
    summarize_rollout_observations,
)

NOW = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


def qualification(
    *,
    stage: AutopilotReleaseStage = AutopilotReleaseStage.SYNTHETIC_SANDBOX,
    expires_at: datetime = NOW + timedelta(hours=1),
    declares_real_provider_write: bool = False,
) -> SyntheticReleaseQualification:
    return SyntheticReleaseQualification(
        adapter_id="greenhouse-sandbox",
        fixture_id="fixture-1",
        release_version="release-v1",
        stage=stage,
        evidence_hash="a" * 64,
        expires_at=expires_at,
        declares_real_provider_write=declares_real_provider_write,
    )


def request_binding(
    *,
    mode: AutopilotReleaseStage = AutopilotReleaseStage.REVIEW_REQUIRED,
    config_hash: str = "b" * 64,
) -> ReleaseQualificationRequestBinding:
    return ReleaseQualificationRequestBinding(
        capability="application-submit",
        action_kind="submit_application",
        mode=mode,
        adapter_id="greenhouse-adapter",
        provider="greenhouse",
        provider_release="greenhouse-2026-07",
        implementation_hash="a" * 64,
        config_hash=config_hash,
        policy_hash="c" * 64,
        dataset_hash="d" * 64,
        git_commit="abc1234",
        image_digest="sha256:release",
        migration_version="0009",
        oauth_scope_hash="e" * 64,
        credential_profile_hash="f" * 64,
        network_policy_hash="0" * 64,
        reconcile_strategy_hash="1" * 64,
        hard_stop_version="hard-stop-v1",
        sensitive_policy_version="sensitive-v1",
        kill_switch_version="kill-v1",
        fixture_manifest_hash="2" * 64,
        fault_manifest_hash="3" * 64,
        holdout_manifest_hash="4" * 64,
        live_sample_manifest_hash="5" * 64,
    )


def release_binding(
    *,
    mode: AutopilotReleaseStage = AutopilotReleaseStage.REVIEW_REQUIRED,
) -> ReleaseQualificationBinding:
    binding = request_binding(mode=mode)
    return ReleaseQualificationBinding(
        qualification_id="rq-2026-07-greenhouse",
        created_at=NOW,
        expires_at=NOW + timedelta(days=30),
        **asdict(binding),
    )


def measurement_summary() -> RolloutMeasurementSummary:
    return summarize_rollout_observations(
        (
            RolloutObservation(
                observation_id="shadow-1",
                stage=AutopilotReleaseStage.SHADOW,
                expected_decision=RolloutExpectedDecision.BLOCK,
                actual_decision=RolloutActualDecision.BLOCK,
            ),
            RolloutObservation(
                observation_id="review-1",
                stage=AutopilotReleaseStage.REVIEW_REQUIRED,
                expected_decision=RolloutExpectedDecision.REQUIRE_REVIEW,
                actual_decision=RolloutActualDecision.REQUIRE_REVIEW,
                human_override_required=True,
            ),
        )
    )


def evidence(
    *,
    runner_actor: str = "runner-agent",
    evidence_id: str = "evidence-1",
) -> ReleaseQualificationEvidence:
    return ReleaseQualificationEvidence(
        evidence_id=evidence_id,
        qualification_id="rq-2026-07-greenhouse",
        runner_actor=runner_actor,
        run_started_at=NOW + timedelta(minutes=1),
        run_completed_at=NOW + timedelta(minutes=2),
        artifact_hash="6" * 64,
        metrics_hash="7" * 64,
        measurement_summary=measurement_summary(),
    )


def decision(
    *,
    from_status: ReleaseQualificationStatus,
    to_status: ReleaseQualificationStatus,
    actor_id: str,
    actor_role: ReleaseQualificationActorRole,
    offset_minutes: int,
    evidence_ids: tuple[str, ...] = (),
) -> ReleaseQualificationDecision:
    return ReleaseQualificationDecision(
        decision_id=f"decision-{to_status.value}",
        qualification_id="rq-2026-07-greenhouse",
        from_status=from_status,
        to_status=to_status,
        actor_id=actor_id,
        actor_role=actor_role,
        decided_at=NOW + timedelta(minutes=offset_minutes),
        reason_code=f"{to_status.value}-reason",
        evidence_ids=evidence_ids,
    )


def test_release_gate_allows_only_current_synthetic_sandbox_evidence() -> None:
    decision = SyntheticReleaseGate().decide(
        qualification(),
        adapter_id="greenhouse-sandbox",
        fixture_id="fixture-1",
        target_host="sandbox.greenhouse.test",
        release_version="release-v1",
        now=NOW,
    )

    assert decision.outcome is ReleaseGateOutcome.ALLOW_SYNTHETIC_RESERVATION
    assert decision.reason_code == "SYNTHETIC_RELEASE_QUALIFIED"


@pytest.mark.parametrize(
    ("value", "expected_outcome", "reason_code"),
    [
        (
            qualification(stage=AutopilotReleaseStage.SHADOW),
            ReleaseGateOutcome.REVIEW_REQUIRED,
            "SHADOW_STAGE_ONLY",
        ),
        (
            qualification(stage=AutopilotReleaseStage.REVIEW_REQUIRED),
            ReleaseGateOutcome.REVIEW_REQUIRED,
            "RELEASE_STAGE_REQUIRES_REVIEW",
        ),
        (
            qualification(stage=AutopilotReleaseStage.LIMITED_AUTOPILOT),
            ReleaseGateOutcome.BLOCKED,
            "RELEASE_STAGE_NOT_ENABLED_FOR_THIS_RUNTIME",
        ),
        (
            qualification(expires_at=NOW - timedelta(seconds=1)),
            ReleaseGateOutcome.BLOCKED,
            "RELEASE_QUALIFICATION_EXPIRED",
        ),
        (
            qualification(declares_real_provider_write=True),
            ReleaseGateOutcome.BLOCKED,
            "REAL_PROVIDER_WRITE_CAPABILITY_FORBIDDEN",
        ),
    ],
)
def test_release_gate_fails_closed_for_non_sandbox_stages_or_bad_evidence(
    value: SyntheticReleaseQualification,
    expected_outcome: ReleaseGateOutcome,
    reason_code: str,
) -> None:
    decision = SyntheticReleaseGate().decide(
        value,
        adapter_id="greenhouse-sandbox",
        fixture_id="fixture-1",
        target_host="sandbox.greenhouse.test",
        release_version="release-v1",
        now=NOW,
    )

    assert decision.outcome is expected_outcome
    assert decision.reason_code == reason_code


def test_release_gate_rejects_real_host_or_stale_identity() -> None:
    gate = SyntheticReleaseGate()
    real_host = gate.decide(
        qualification(),
        adapter_id="greenhouse-sandbox",
        fixture_id="fixture-1",
        target_host="greenhouse.io",
        release_version="release-v1",
        now=NOW,
    )
    stale_release = gate.decide(
        qualification(),
        adapter_id="greenhouse-sandbox",
        fixture_id="fixture-1",
        target_host="sandbox.greenhouse.test",
        release_version="release-v2",
        now=NOW,
    )

    assert real_host.reason_code == "NON_SYNTHETIC_TARGET_HOST"
    assert stale_release.reason_code == "STALE_RELEASE_QUALIFICATION"


def test_qualification_rejects_noncanonical_evidence_or_time() -> None:
    with pytest.raises(ValueError, match="evidence_hash"):
        SyntheticReleaseQualification(
            adapter_id="greenhouse-sandbox",
            fixture_id="fixture-1",
            release_version="release-v1",
            stage=AutopilotReleaseStage.SYNTHETIC_SANDBOX,
            evidence_hash="not-a-hash",
            expires_at=NOW,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        SyntheticReleaseQualification(
            adapter_id="greenhouse-sandbox",
            fixture_id="fixture-1",
            release_version="release-v1",
            stage=AutopilotReleaseStage.SYNTHETIC_SANDBOX,
            evidence_hash="a" * 64,
            expires_at=datetime(2026, 7, 20, 10, 0),
        )


def test_release_qualification_lifecycle_requires_runner_reviewer_and_operator_chain() -> None:
    lifecycle = ReleaseQualificationLifecycle()
    item = ReleaseQualification.draft(release_binding(), created_by="release-owner")
    opened = decision(
        from_status=ReleaseQualificationStatus.DRAFT,
        to_status=ReleaseQualificationStatus.EVALUATING,
        actor_id="runner-agent",
        actor_role=ReleaseQualificationActorRole.RUNNER,
        offset_minutes=1,
    )
    evaluating = lifecycle.apply(item, opened)
    run_evidence = evidence(runner_actor="runner-agent")
    reviewed = decision(
        from_status=ReleaseQualificationStatus.EVALUATING,
        to_status=ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW,
        actor_id="reviewer-1",
        actor_role=ReleaseQualificationActorRole.REVIEWER,
        offset_minutes=3,
        evidence_ids=("evidence-1",),
    )
    pending = lifecycle.apply(evaluating, reviewed, evidence=(run_evidence,))
    qualified_decision = decision(
        from_status=ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW,
        to_status=ReleaseQualificationStatus.QUALIFIED,
        actor_id="operator-1",
        actor_role=ReleaseQualificationActorRole.OPERATOR,
        offset_minutes=4,
        evidence_ids=("evidence-1",),
    )

    qualified = lifecycle.apply(
        pending,
        qualified_decision,
        evidence=(run_evidence,),
        previous_decisions=(opened, reviewed),
    )

    assert evaluating.status is ReleaseQualificationStatus.EVALUATING
    assert pending.status is ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW
    assert qualified.status is ReleaseQualificationStatus.QUALIFIED


def test_release_qualification_lifecycle_rejects_operator_opening_evaluation() -> None:
    lifecycle = ReleaseQualificationLifecycle()
    item = ReleaseQualification.draft(release_binding(), created_by="release-owner")
    opened_by_operator = decision(
        from_status=ReleaseQualificationStatus.DRAFT,
        to_status=ReleaseQualificationStatus.EVALUATING,
        actor_id="operator-1",
        actor_role=ReleaseQualificationActorRole.OPERATOR,
        offset_minutes=1,
    )

    with pytest.raises(ValueError, match="only a runner can open"):
        lifecycle.apply(item, opened_by_operator)


def test_release_qualification_lifecycle_rejects_runner_or_reviewer_final_decision() -> None:
    lifecycle = ReleaseQualificationLifecycle()
    item = ReleaseQualification.draft(release_binding(), created_by="release-owner")
    opened = decision(
        from_status=ReleaseQualificationStatus.DRAFT,
        to_status=ReleaseQualificationStatus.EVALUATING,
        actor_id="runner-agent",
        actor_role=ReleaseQualificationActorRole.RUNNER,
        offset_minutes=1,
    )
    evaluating = lifecycle.apply(item, opened)
    run_evidence = evidence(runner_actor="runner-agent")

    runner_review = decision(
        from_status=ReleaseQualificationStatus.EVALUATING,
        to_status=ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW,
        actor_id="runner-agent",
        actor_role=ReleaseQualificationActorRole.REVIEWER,
        offset_minutes=3,
        evidence_ids=("evidence-1",),
    )
    with pytest.raises(ValueError, match="independent from evidence runners"):
        lifecycle.apply(evaluating, runner_review, evidence=(run_evidence,))

    reviewer_review = decision(
        from_status=ReleaseQualificationStatus.EVALUATING,
        to_status=ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW,
        actor_id="reviewer-1",
        actor_role=ReleaseQualificationActorRole.REVIEWER,
        offset_minutes=3,
        evidence_ids=("evidence-1",),
    )
    pending = lifecycle.apply(evaluating, reviewer_review, evidence=(run_evidence,))
    reviewer_final = decision(
        from_status=ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW,
        to_status=ReleaseQualificationStatus.QUALIFIED,
        actor_id="reviewer-1",
        actor_role=ReleaseQualificationActorRole.OPERATOR,
        offset_minutes=4,
        evidence_ids=("evidence-1",),
    )

    with pytest.raises(ValueError, match="operator must differ from reviewer"):
        lifecycle.apply(
            pending,
            reviewer_final,
            evidence=(run_evidence,),
            previous_decisions=(opened, reviewer_review),
        )


def test_release_qualification_lifecycle_requires_final_to_reuse_complete_reviewed_set() -> None:
    lifecycle = ReleaseQualificationLifecycle()
    evaluating = ReleaseQualification(
        binding=release_binding(),
        status=ReleaseQualificationStatus.EVALUATING,
        created_by="release-owner",
        status_updated_at=NOW + timedelta(minutes=1),
    )
    first = evidence(evidence_id="evidence-1")
    second = evidence(evidence_id="evidence-2", runner_actor="runner-agent-2")
    incomplete_review = decision(
        from_status=ReleaseQualificationStatus.EVALUATING,
        to_status=ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW,
        actor_id="reviewer-1",
        actor_role=ReleaseQualificationActorRole.REVIEWER,
        offset_minutes=3,
        evidence_ids=("evidence-1",),
    )

    with pytest.raises(ValueError, match="complete evidence set"):
        lifecycle.apply(evaluating, incomplete_review, evidence=(first, second))

    reviewed = decision(
        from_status=ReleaseQualificationStatus.EVALUATING,
        to_status=ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW,
        actor_id="reviewer-1",
        actor_role=ReleaseQualificationActorRole.REVIEWER,
        offset_minutes=3,
        evidence_ids=("evidence-1",),
    )
    pending = lifecycle.apply(evaluating, reviewed, evidence=(first,))
    switched = decision(
        from_status=ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW,
        to_status=ReleaseQualificationStatus.QUALIFIED,
        actor_id="operator-1",
        actor_role=ReleaseQualificationActorRole.OPERATOR,
        offset_minutes=4,
        evidence_ids=("evidence-2",),
    )

    with pytest.raises(ValueError, match="independently reviewed evidence set"):
        lifecycle.apply(
            pending,
            switched,
            evidence=(second,),
            previous_decisions=(reviewed,),
        )


def test_release_qualification_lifecycle_rejects_missing_or_fail_open_evidence() -> None:
    lifecycle = ReleaseQualificationLifecycle()
    evaluating = ReleaseQualification(
        binding=release_binding(),
        status=ReleaseQualificationStatus.EVALUATING,
        created_by="release-owner",
        status_updated_at=NOW + timedelta(minutes=1),
    )
    missing = decision(
        from_status=ReleaseQualificationStatus.EVALUATING,
        to_status=ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW,
        actor_id="reviewer-1",
        actor_role=ReleaseQualificationActorRole.REVIEWER,
        offset_minutes=3,
        evidence_ids=("missing-evidence",),
    )
    with pytest.raises(ValueError, match="missing evidence"):
        lifecycle.apply(evaluating, missing)

    unsafe_summary = summarize_rollout_observations(
        (
            RolloutObservation(
                observation_id="unsafe-shadow-allow",
                stage=AutopilotReleaseStage.SHADOW,
                expected_decision=RolloutExpectedDecision.BLOCK,
                actual_decision=RolloutActualDecision.ALLOW_WRITE,
            ),
        )
    )
    unsafe_evidence = ReleaseQualificationEvidence(
        evidence_id="unsafe-evidence",
        qualification_id="rq-2026-07-greenhouse",
        runner_actor="runner-agent",
        run_started_at=NOW + timedelta(minutes=1),
        run_completed_at=NOW + timedelta(minutes=2),
        artifact_hash="8" * 64,
        metrics_hash="9" * 64,
        measurement_summary=unsafe_summary,
    )
    pending = ReleaseQualification(
        binding=evaluating.binding,
        status=ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW,
        created_by=evaluating.created_by,
        status_updated_at=NOW + timedelta(minutes=3),
    )
    reviewed = decision(
        from_status=ReleaseQualificationStatus.EVALUATING,
        to_status=ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW,
        actor_id="reviewer-1",
        actor_role=ReleaseQualificationActorRole.REVIEWER,
        offset_minutes=3,
        evidence_ids=("unsafe-evidence",),
    )
    qualify = decision(
        from_status=ReleaseQualificationStatus.PENDING_INDEPENDENT_REVIEW,
        to_status=ReleaseQualificationStatus.QUALIFIED,
        actor_id="operator-1",
        actor_role=ReleaseQualificationActorRole.OPERATOR,
        offset_minutes=4,
        evidence_ids=("unsafe-evidence",),
    )
    with pytest.raises(ValueError, match="fail-open observation"):
        lifecycle.apply(
            pending,
            qualify,
            evidence=(unsafe_evidence,),
            previous_decisions=(reviewed,),
        )


def test_qualified_release_gate_defaults_to_deny_on_status_expiry_or_binding_mismatch() -> None:
    gate = QualifiedReleaseGate()
    binding = release_binding(mode=AutopilotReleaseStage.REVIEW_REQUIRED)
    item = ReleaseQualification.draft(binding, created_by="release-owner")

    not_qualified = gate.decide(
        item,
        requested_binding=binding.request_binding,
        now=NOW + timedelta(minutes=1),
    )
    mismatch = gate.decide(
        item,
        requested_binding=request_binding(config_hash="8" * 64),
        now=NOW + timedelta(minutes=1),
    )
    expired = gate.decide(
        ReleaseQualification(
            binding=ReleaseQualificationBinding(
                qualification_id="rq-expired",
                created_at=NOW,
                expires_at=NOW + timedelta(minutes=1),
                **asdict(request_binding()),
            ),
            status=ReleaseQualificationStatus.QUALIFIED,
            created_by="release-owner",
            status_updated_at=NOW,
        ),
        requested_binding=request_binding(),
        now=NOW + timedelta(minutes=2),
    )

    assert not_qualified.outcome is ReleaseGateOutcome.REVIEW_REQUIRED
    assert not_qualified.reason_code == "RELEASE_STAGE_REQUIRES_REVIEW"
    assert mismatch.outcome is ReleaseGateOutcome.BLOCKED
    assert mismatch.reason_code == "RELEASE_BINDING_MISMATCH"
    assert expired.outcome is ReleaseGateOutcome.BLOCKED
    assert expired.reason_code == "RELEASE_QUALIFICATION_EXPIRED"


def test_rollout_measurement_summary_counts_precision_overrides_and_autonomous_writes() -> None:
    summary = summarize_rollout_observations(
        (
            RolloutObservation(
                observation_id="tp-block",
                stage=AutopilotReleaseStage.SHADOW,
                expected_decision=RolloutExpectedDecision.BLOCK,
                actual_decision=RolloutActualDecision.BLOCK,
            ),
            RolloutObservation(
                observation_id="tp-review",
                stage=AutopilotReleaseStage.REVIEW_REQUIRED,
                expected_decision=RolloutExpectedDecision.REQUIRE_REVIEW,
                actual_decision=RolloutActualDecision.REQUIRE_REVIEW,
                human_override_required=True,
            ),
            RolloutObservation(
                observation_id="fp-block",
                stage=AutopilotReleaseStage.REVIEW_REQUIRED,
                expected_decision=RolloutExpectedDecision.ALLOW_WRITE,
                actual_decision=RolloutActualDecision.BLOCK,
            ),
            RolloutObservation(
                observation_id="fn-write",
                stage=AutopilotReleaseStage.SHADOW,
                expected_decision=RolloutExpectedDecision.BLOCK,
                actual_decision=RolloutActualDecision.ALLOW_WRITE,
            ),
            RolloutObservation(
                observation_id="manual-write-attempt",
                stage=AutopilotReleaseStage.SYNTHETIC_SANDBOX,
                expected_decision=RolloutExpectedDecision.ALLOW_WRITE,
                actual_decision=RolloutActualDecision.ALLOW_WRITE,
                autonomous_provider_write_attempted=True,
            ),
        )
    )

    assert summary.total_observations == 5
    assert summary.shadow_observations == 2
    assert summary.review_required_observations == 2
    assert summary.predicted_positive == 3
    assert summary.true_positive == 2
    assert summary.false_positive == 1
    assert summary.false_negative == 1
    assert summary.human_overrides_required == 1
    assert summary.autonomous_provider_write_attempts == 2
    assert summary.decision_precision == pytest.approx(2 / 3)
    assert summary.no_autonomous_writes is False
