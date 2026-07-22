from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from careerops.application.outbox import (
    ClaimedOutboxEvent,
    InternalDeliveryError,
    OutboxEventType,
)
from careerops.application.submission_dispatch import (
    ReconciliationState,
    SyntheticProviderState,
    SyntheticReconciler,
    SyntheticSubmissionReceipt,
)

SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX = "synthetic-dispatch:"
SYNTHETIC_PROVIDER = "synthetic"


class SyntheticSubmissionDispatchError(RuntimeError):
    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class PreparedSyntheticSubmissionState(StrEnum):
    READY = "ready"
    ALREADY_CONFIRMED = "already_confirmed"


@dataclass(frozen=True, slots=True)
class PreparedSyntheticSubmission:
    event_id: UUID
    event_key: str
    action_intent_id: UUID
    payload_version_id: UUID
    reservation_key: str
    reconciliation_key: str
    state: PreparedSyntheticSubmissionState = PreparedSyntheticSubmissionState.READY
    confirmed_receipt: SyntheticSubmissionReceipt | None = None


BoundSyntheticSubmissionDispatch = PreparedSyntheticSubmission


class SyntheticSubmissionCoordinator(Protocol):
    """Durable boundary for loading dispatch identity and recording synthetic receipts."""

    def prepare(
        self,
        event: ClaimedOutboxEvent,
    ) -> PreparedSyntheticSubmission: ...

    def record_receipt(
        self,
        dispatch: PreparedSyntheticSubmission,
        receipt: SyntheticSubmissionReceipt,
    ) -> None: ...

    def record_ambiguous(
        self,
        dispatch: PreparedSyntheticSubmission,
        *,
        error_code: str,
    ) -> None: ...


class SyntheticSubmissionProvider(Protocol):
    """Synthetic provider boundary; implementations must not call real providers."""

    def submit(
        self,
        dispatch: PreparedSyntheticSubmission,
        *,
        now: datetime,
    ) -> SyntheticSubmissionReceipt: ...


class DeterministicSyntheticSubmissionProvider:
    """No-network synthetic provider with stable receipt identity for a bound dispatch."""

    def __init__(
        self,
        *,
        provider_state: SyntheticProviderState = SyntheticProviderState.CONFIRMED,
    ) -> None:
        if provider_state is SyntheticProviderState.NOT_STARTED:
            raise ValueError("synthetic provider result must be confirmed or ambiguous")
        self._provider_state = provider_state

    def submit(
        self,
        dispatch: PreparedSyntheticSubmission,
        *,
        now: datetime,
    ) -> SyntheticSubmissionReceipt:
        provider_resource_id = _stable_provider_resource_id(dispatch)
        return SyntheticSubmissionReceipt(
            provider="synthetic",
            provider_resource_id=provider_resource_id,
            reconciliation_key=dispatch.reconciliation_key,
            provider_state=self._provider_state,
            received_at=now,
        )


class SyntheticSubmissionOutboxSink:
    def __init__(
        self,
        coordinator: SyntheticSubmissionCoordinator,
        provider: SyntheticSubmissionProvider | None = None,
        reconciler: SyntheticReconciler | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._provider = provider or DeterministicSyntheticSubmissionProvider()
        self._reconciler = reconciler or SyntheticReconciler()
        self._clock = clock or _utc_now

    def deliver(self, event: ClaimedOutboxEvent) -> None:
        try:
            self._deliver(event)
        except SyntheticSubmissionDispatchError as error:
            raise InternalDeliveryError(error.error_code, retryable=error.retryable) from error

    def _deliver(self, event: ClaimedOutboxEvent) -> None:
        _assert_supported_event(event)
        dispatch = self._coordinator.prepare(event)
        _assert_prepare_state(dispatch)
        _assert_dispatch_matches_event(dispatch, event)

        if dispatch.state is PreparedSyntheticSubmissionState.ALREADY_CONFIRMED:
            if dispatch.confirmed_receipt is None:
                raise SyntheticSubmissionDispatchError(
                    "SYNTHETIC_CONFIRMED_RECEIPT_MISSING",
                    "confirmed synthetic dispatch is missing its receipt",
                )
            _assert_receipt_matches_dispatch(dispatch.confirmed_receipt, dispatch)
            return
        if dispatch.confirmed_receipt is not None:
            raise SyntheticSubmissionDispatchError(
                "SYNTHETIC_READY_DISPATCH_HAS_RECEIPT",
                "ready synthetic dispatch must not include a confirmed receipt",
            )

        now = self._clock()
        _validate_aware(now, "synthetic provider receipt time")
        try:
            receipt = self._provider.submit(dispatch, now=now)
        except Exception as error:
            error_code = "SYNTHETIC_PROVIDER_BOUNDARY_AMBIGUOUS"
            self._record_ambiguous_best_effort(dispatch, error_code=error_code)
            raise SyntheticSubmissionDispatchError(
                error_code,
                "synthetic provider outcome is ambiguous after dispatch preparation",
            ) from error
        try:
            _assert_receipt_matches_dispatch(receipt, dispatch)
            self._coordinator.record_receipt(dispatch, receipt)
        except Exception as error:
            error_code = "SYNTHETIC_RECEIPT_PERSISTENCE_AMBIGUOUS"
            self._record_ambiguous_best_effort(dispatch, error_code=error_code)
            raise SyntheticSubmissionDispatchError(
                error_code,
                "synthetic receipt could not be durably validated or recorded",
            ) from error
        self._raise_if_receipt_is_ambiguous(dispatch, receipt)

    def _raise_if_receipt_is_ambiguous(
        self,
        dispatch: PreparedSyntheticSubmission,
        receipt: SyntheticSubmissionReceipt,
    ) -> None:
        decision = self._reconciler.decide(receipt)
        if decision.state is ReconciliationState.RECONCILIATION_REQUIRED:
            self._record_ambiguous_best_effort(
                dispatch,
                error_code=decision.reason_code,
            )
            raise SyntheticSubmissionDispatchError(
                decision.reason_code,
                "synthetic provider state is ambiguous after the outbox event was claimed",
            )

    def _record_ambiguous_best_effort(
        self,
        dispatch: PreparedSyntheticSubmission,
        *,
        error_code: str,
    ) -> None:
        try:
            self._coordinator.record_ambiguous(dispatch, error_code=error_code)
        except Exception:
            return


def synthetic_dispatch_event_key(reservation_key: str) -> str:
    if reservation_key.startswith(SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX):
        return reservation_key
    return f"{SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX}{reservation_key}"


def _assert_supported_event(event: ClaimedOutboxEvent) -> None:
    if event.event_type != OutboxEventType.WORKFLOW_SIGNAL.value:
        raise SyntheticSubmissionDispatchError(
            "SYNTHETIC_EVENT_TYPE_UNSUPPORTED",
            "synthetic submission outbox sink only accepts workflow_signal events",
        )
    if not event.event_key.startswith(SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX):
        raise SyntheticSubmissionDispatchError(
            "SYNTHETIC_EVENT_KEY_UNSUPPORTED",
            "synthetic submission outbox sink received a non-synthetic-dispatch event",
        )


def _assert_dispatch_matches_event(
    dispatch: BoundSyntheticSubmissionDispatch,
    event: ClaimedOutboxEvent,
) -> None:
    if (
        dispatch.event_id != event.event_id
        or dispatch.event_key != event.event_key
        or dispatch.action_intent_id != event.action_intent_id
        or dispatch.payload_version_id != event.payload_version_id
    ):
        raise SyntheticSubmissionDispatchError(
            "SYNTHETIC_DISPATCH_EVENT_MISMATCH",
            "synthetic dispatch metadata does not match the claimed outbox event",
        )
    if dispatch.event_key != synthetic_dispatch_event_key(dispatch.reservation_key):
        raise SyntheticSubmissionDispatchError(
            "SYNTHETIC_EVENT_KEY_BINDING_MISMATCH",
            "synthetic event key is not bound to the persisted reservation key",
        )


def _assert_receipt_matches_dispatch(
    receipt: SyntheticSubmissionReceipt,
    dispatch: BoundSyntheticSubmissionDispatch,
) -> None:
    if (
        receipt.provider != SYNTHETIC_PROVIDER
        or receipt.reconciliation_key != dispatch.reconciliation_key
    ):
        raise SyntheticSubmissionDispatchError(
            "SYNTHETIC_RECEIPT_BINDING_MISMATCH",
            "synthetic receipt is not bound to the dispatched reconciliation key",
        )


def _assert_prepare_state(dispatch: PreparedSyntheticSubmission) -> None:
    try:
        PreparedSyntheticSubmissionState(dispatch.state)
    except ValueError as error:
        raise SyntheticSubmissionDispatchError(
            "SYNTHETIC_PREPARE_STATE_UNSUPPORTED",
            "synthetic coordinator returned an unsupported prepare state",
        ) from error


def _validate_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SyntheticSubmissionDispatchError(
            "SYNTHETIC_PROVIDER_RECEIPT_TIME_INVALID",
            f"{label} must be timezone-aware",
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stable_provider_resource_id(dispatch: PreparedSyntheticSubmission) -> str:
    encoded = json.dumps(
        {
            "action_intent_id": str(dispatch.action_intent_id),
            "event_id": str(dispatch.event_id),
            "event_key": dispatch.event_key,
            "payload_version_id": str(dispatch.payload_version_id),
            "reconciliation_key": dispatch.reconciliation_key,
            "reservation_key": dispatch.reservation_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "synthetic-receipt-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX",
    "SYNTHETIC_PROVIDER",
    "BoundSyntheticSubmissionDispatch",
    "DeterministicSyntheticSubmissionProvider",
    "PreparedSyntheticSubmission",
    "PreparedSyntheticSubmissionState",
    "SyntheticSubmissionCoordinator",
    "SyntheticSubmissionDispatchError",
    "SyntheticSubmissionOutboxSink",
    "SyntheticSubmissionProvider",
    "synthetic_dispatch_event_key",
]
