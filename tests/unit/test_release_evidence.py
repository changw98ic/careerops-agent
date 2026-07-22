from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from careerops.application.release_evidence import (
    ReleaseEvidenceArtifact,
    ReleaseQualificationBinding,
    ReleaseQualificationDecision,
    ReleaseQualificationDrilldown,
    redact_json,
)

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


def _binding() -> ReleaseQualificationBinding:
    return ReleaseQualificationBinding(
        id=uuid4(),
        capability="application_submission",
        action_name="submit_application",
        rollout_mode="review_required",
        provider="greenhouse",
        adapter_id="greenhouse-reviewed",
        adapter_version="adapter-v1",
        implementation_hash="a" * 64,
        config_hash="b" * 64,
        policy_hash="c" * 64,
        dataset_hash="d" * 64,
        git_commit="e" * 40,
        image_digest=None,
        migration_revision="0009",
        oauth_scope_hash="f" * 64,
        credential_ref_hash="1" * 64,
        network_policy_hash="2" * 64,
        reconcile_policy_hash="3" * 64,
        hard_stop_hash="4" * 64,
        sensitive_field_hash="5" * 64,
        kill_switch_hash="6" * 64,
        fixture_manifest_sha256="7" * 64,
        fault_manifest_sha256="8" * 64,
        holdout_manifest_sha256=None,
        live_sample_manifest_sha256=None,
        status="pending_independent_review",
        requested_by_user_id=uuid4(),
        created_by="release-bot",
        expires_at=NOW,
        created_at=NOW,
    )


def test_release_evidence_drilldown_exposes_hash_bindings_and_limitations() -> None:
    evidence = ReleaseEvidenceArtifact(
        id=uuid4(),
        evidence_kind="metrics",
        artifact_uri="artifacts/release/metrics.json",
        artifact_sha256="9" * 64,
        run_id="run-1",
        runner_user_id=uuid4(),
        runner_actor="synthetic-runner",
        metrics={"sample_count": 50, "refresh_token": "raw-token"},
        sample_manifest_sha256="a" * 64,
        created_at=NOW,
    )
    decision = ReleaseQualificationDecision(
        sequence=1,
        id=uuid4(),
        from_status="evaluating",
        to_status="pending_independent_review",
        decision_role="reviewer",
        actor_id="independent-reviewer-agent",
        decided_by_user_id=None,
        reason="fixture and fault evidence complete",
        evidence_sha256="b" * 64,
        evidence_ids=(evidence.id,),
        created_at=NOW,
    )

    payload = ReleaseQualificationDrilldown(_binding(), (evidence,), (decision,)).to_json()

    qualification = payload["qualification"]
    assert isinstance(qualification, dict)
    assert qualification["implementation_hash"] == "a" * 64
    assert qualification["credential_ref_hash"] == "1" * 64
    assert qualification["status"] == "pending_independent_review"
    effective_state = payload["effective_state"]
    assert isinstance(effective_state, dict)
    assert effective_state["lifecycle_status"] == "pending_independent_review"
    assert effective_state["external_write_authorized"] is False
    evidence_rows = payload["evidence"]
    assert isinstance(evidence_rows, list)
    first_evidence = evidence_rows[0]
    assert isinstance(first_evidence, dict)
    assert first_evidence["metrics"] == {
        "sample_count": 50,
        "refresh_token": "[redacted]",
    }
    limitations = payload["limitations"]
    assert isinstance(limitations, list)
    assert "no_provider_write_authority" in limitations


def test_release_evidence_effective_state_expires_qualified_decision() -> None:
    decision = ReleaseQualificationDecision(
        sequence=1,
        id=uuid4(),
        from_status="pending_independent_review",
        to_status="qualified",
        decision_role="operator",
        actor_id="human-operator",
        decided_by_user_id=uuid4(),
        reason="synthetic evidence accepted",
        evidence_sha256="b" * 64,
        evidence_ids=(uuid4(),),
        created_at=NOW,
    )
    drilldown = ReleaseQualificationDrilldown(_binding(), (), (decision,))

    assert drilldown.effective_lifecycle_status(now=NOW) == "expired"
    state = drilldown.to_json(now=NOW)["effective_state"]
    assert isinstance(state, dict)
    assert state["lifecycle_status"] == "expired"
    assert state["external_write_authorized"] is False


def test_redact_json_removes_secret_like_nested_values_without_dropping_hashes() -> None:
    redacted = redact_json(
        {
            "payload_hash": "a" * 64,
            "authorization": "Bearer raw",
            "nested": [{"session_cookie": "raw"}, {"safe": True}],
        }
    )

    assert redacted == {
        "payload_hash": "a" * 64,
        "authorization": "[redacted]",
        "nested": [{"session_cookie": "[redacted]"}, {"safe": True}],
    }
