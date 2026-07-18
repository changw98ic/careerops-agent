from uuid import uuid4

from careerops.application.side_effects import (
    DisabledSideEffectWorker,
    SideEffectEnvelope,
    SideEffectOutcomeState,
)


def test_m0_side_effect_worker_is_unconditionally_disabled() -> None:
    envelope = SideEffectEnvelope(
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        policy_decision_id=uuid4(),
        approval_request_id=uuid4(),
        payload_hash="a" * 64,
    )

    result = DisabledSideEffectWorker().execute(envelope)

    assert result.state is SideEffectOutcomeState.DENIED
    assert result.reason_code == "CAPABILITY_NOT_RELEASED"
