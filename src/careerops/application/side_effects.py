from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


@dataclass(frozen=True, slots=True)
class SideEffectEnvelope:
    action_intent_id: UUID
    payload_version_id: UUID
    policy_decision_id: UUID
    approval_request_id: UUID | None
    payload_hash: str


class SideEffectOutcomeState(StrEnum):
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class SideEffectOutcome:
    state: SideEffectOutcomeState
    reason_code: str


class DisabledSideEffectWorker:
    """M0 boundary: it has no provider adapter, token, or execution capability."""

    def execute(self, envelope: SideEffectEnvelope) -> SideEffectOutcome:
        del envelope
        return SideEffectOutcome(
            state=SideEffectOutcomeState.DENIED,
            reason_code="CAPABILITY_NOT_RELEASED",
        )
