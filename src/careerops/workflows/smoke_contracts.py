from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

SMOKE_STARTED_ACTIVITY = "careerops.smoke.record_started"
SMOKE_COMPLETED_ACTIVITY = "careerops.smoke.record_completed"


class SmokePhase(StrEnum):
    STARTING = "starting"
    WAITING_FOR_RELEASE = "waiting_for_release"
    RELEASED = "released"
    COMPLETING = "completing"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class SmokeWorkflowInput:
    operation_id: str
    completion_idempotency_key: str


@dataclass(frozen=True, slots=True)
class SmokeReleaseSignal:
    request_id: str


@dataclass(frozen=True, slots=True)
class SmokeStartCommand:
    operation_id: str


@dataclass(frozen=True, slots=True)
class SmokeCompletionCommand:
    operation_id: str
    release_request_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class SmokeActivityReceipt:
    receipt_id: str


@dataclass(frozen=True, slots=True)
class SmokeStatus:
    phase: SmokePhase
    release_request_id: str | None
    ignored_release_signals: int


@dataclass(frozen=True, slots=True)
class SmokeWorkflowResult:
    operation_id: str
    release_request_id: str
    start_receipt_id: str
    completion_receipt_id: str
