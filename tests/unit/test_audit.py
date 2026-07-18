from datetime import UTC, datetime
from uuid import UUID

import pytest

from careerops.application.audit import AuditActorType, AuditEventDraft


def _draft() -> AuditEventDraft:
    return AuditEventDraft(
        event_id=UUID("4eef79b2-e11f-4fb7-95c6-a169b6726a0d"),
        occurred_at=datetime(2026, 7, 17, 9, 0, tzinfo=UTC),
        actor_type=AuditActorType.POLICY,
        actor_id="default-deny-v1",
        event_type="policy_denied",
        resource_type="action_intent",
        resource_id=UUID("c53ae1a6-3416-4297-820b-ce52ae2f01c7"),
        trace_id="trace-1",
        event_data={"reason": "unknown_action", "attempt": 1},
    )


def test_audit_event_accepts_structured_json_payload() -> None:
    draft = _draft()

    assert draft.actor_type == AuditActorType.POLICY
    assert draft.event_data == {"reason": "unknown_action", "attempt": 1}


def test_audit_event_rejects_naive_time_and_blank_identity() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        AuditEventDraft(
            event_type="test",
            actor_type=AuditActorType.WORKER,
            resource_type="test",
            resource_id=UUID("c53ae1a6-3416-4297-820b-ce52ae2f01c7"),
            trace_id="trace",
            occurred_at=datetime(2026, 7, 17),
        )

    with pytest.raises(ValueError, match="event_type"):
        AuditEventDraft(
            event_type=" ",
            actor_type=AuditActorType.WORKER,
            resource_type="test",
            resource_id=UUID("c53ae1a6-3416-4297-820b-ce52ae2f01c7"),
            trace_id="trace",
        )
