from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from careerops.application.greenhouse_submit import (
    GreenhouseJobSchemaSnapshot,
    GreenhouseSubmissionPayload,
    qualify_greenhouse_submission,
)
from careerops.application.outbox import (
    ClaimedOutboxEvent,
    InternalDeliveryError,
    OutboxEventType,
)
from careerops.infrastructure.greenhouse.client import (
    GreenhouseApiError,
    GreenhouseBrokerPrePostUnavailable,
    GreenhouseResolvedAttachment,
    GreenhouseSubmissionEvidence,
    GreenhouseSubmissionOutcome,
)
from careerops.infrastructure.greenhouse.credentials import GreenhouseCredentialHandle

GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX = "greenhouse-submit:"

_BOUNDED_ID = re.compile(r"^[A-Za-z0-9._:@/+\\=<>-]{1,512}$")
_BOUNDED_ERROR = re.compile(r"^[A-Z0-9_]{1,160}$")


class GreenhouseSubmitOutboxError(RuntimeError):
    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        if _BOUNDED_ERROR.fullmatch(error_code) is None:
            raise ValueError("error_code must be a bounded machine code")
        self.error_code = error_code
        self.retryable = retryable


class PreparedGreenhouseSubmitState(StrEnum):
    READY = "ready"
    ALREADY_TERMINAL = "already_terminal"


@dataclass(frozen=True, slots=True)
class PreparedGreenhouseSubmitDispatch:
    event_id: UUID
    event_key: str
    action_intent_id: UUID
    payload_version_id: UUID
    reservation_key: str
    reconciliation_key: str
    account_id: UUID
    credential_handle: GreenhouseCredentialHandle
    schema: GreenhouseJobSchemaSnapshot
    payload: GreenhouseSubmissionPayload
    state: PreparedGreenhouseSubmitState = PreparedGreenhouseSubmitState.READY
    terminal_evidence: GreenhouseSubmissionEvidence | None = None

    def __post_init__(self) -> None:
        _validate_bounded(self.reservation_key, "reservation_key")
        _validate_bounded(self.reconciliation_key, "reconciliation_key")
        if self.event_key != greenhouse_submit_event_key(self.reservation_key):
            raise ValueError("event_key must be bound to the reservation_key")
        if self.schema.target != self.payload.target:
            raise ValueError("schema and payload must target the same Greenhouse job")
        if self.payload.schema_hash != self.schema.schema_hash:
            raise ValueError("payload must bind the prepared Greenhouse schema")
        if self.credential_handle.board_token != self.payload.target.board_token:
            raise ValueError("credential handle must bind the payload board token")


class GreenhouseSubmitCoordinator(Protocol):
    def prepare(self, event: ClaimedOutboxEvent) -> PreparedGreenhouseSubmitDispatch: ...

    def record_accepted_unverified(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence,
    ) -> None: ...

    def record_rejected(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence | None,
        error_code: str,
        reason_codes: Sequence[str],
    ) -> None: ...

    def record_ambiguous(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence | None,
        error_code: str,
    ) -> None: ...

    def record_prepost_failure(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        error_code: str,
    ) -> None: ...


class GreenhouseSubmissionBroker(Protocol):
    def submit(
        self,
        handle: GreenhouseCredentialHandle,
        *,
        schema: GreenhouseJobSchemaSnapshot,
        payload: GreenhouseSubmissionPayload,
        attachments: Sequence[GreenhouseResolvedAttachment],
        reconciliation_key: str,
    ) -> GreenhouseSubmissionEvidence: ...


class GreenhouseAttachmentResolver(Protocol):
    def resolve(self, ref: object) -> GreenhouseResolvedAttachment: ...


class GreenhouseSubmitOutboxSink:
    def __init__(
        self,
        coordinator: GreenhouseSubmitCoordinator,
        broker: GreenhouseSubmissionBroker,
        attachment_resolver: GreenhouseAttachmentResolver,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._broker = broker
        self._attachment_resolver = attachment_resolver
        self._clock = clock or _utc_now

    def deliver(self, event: ClaimedOutboxEvent) -> None:
        try:
            self._deliver(event)
        except GreenhouseSubmitOutboxError as error:
            raise InternalDeliveryError(error.error_code, retryable=error.retryable) from error

    def _deliver(self, event: ClaimedOutboxEvent) -> None:
        _assert_supported_event(event)
        dispatch = self._coordinator.prepare(event)
        _assert_prepare_state(dispatch)
        _assert_dispatch_matches_event(dispatch, event)
        if dispatch.state is PreparedGreenhouseSubmitState.ALREADY_TERMINAL:
            if dispatch.terminal_evidence is not None:
                _assert_evidence_matches_dispatch(dispatch.terminal_evidence, dispatch)
            return
        if dispatch.terminal_evidence is not None:
            raise GreenhouseSubmitOutboxError(
                "GREENHOUSE_SUBMIT_READY_DISPATCH_HAS_TERMINAL_EVIDENCE",
                "ready Greenhouse submit dispatch must not include terminal evidence",
            )

        qualification = qualify_greenhouse_submission(
            schema=dispatch.schema,
            payload=dispatch.payload,
        )
        if not qualification.can_submit:
            self._record_rejected_or_terminal(
                dispatch,
                evidence=None,
                error_code="GREENHOUSE_SUBMIT_NOT_QUALIFIED",
                reason_codes=qualification.reason_codes,
            )
            return

        attachments = self._resolve_attachments_before_post(dispatch)
        try:
            _assert_resolved_attachments_match(dispatch, attachments)
        except GreenhouseSubmitOutboxError as error:
            self._record_prepost_failure_or_raise(dispatch, error_code=error.error_code)
            raise
        _validate_aware(self._clock(), "Greenhouse submit dispatch time")
        try:
            evidence = self._broker.submit(
                dispatch.credential_handle,
                schema=dispatch.schema,
                payload=dispatch.payload,
                attachments=attachments,
                reconciliation_key=dispatch.reconciliation_key,
            )
        except GreenhouseBrokerPrePostUnavailable as error:
            self._record_prepost_failure_or_raise(dispatch, error_code=error.error_code)
            raise GreenhouseSubmitOutboxError(
                error.error_code,
                "Greenhouse broker was unavailable before any provider-start boundary",
                retryable=True,
            ) from error
        except GreenhouseApiError as error:
            self._record_rejected_or_terminal(
                dispatch,
                evidence=None,
                error_code=error.error_code,
                reason_codes=error.reason_codes or (error.error_code,),
            )
            return
        except Exception as error:
            error_code = "GREENHOUSE_SUBMIT_BROKER_BOUNDARY_AMBIGUOUS"
            self._record_ambiguous_or_terminal(dispatch, evidence=None, error_code=error_code)
            raise GreenhouseSubmitOutboxError(
                error_code,
                "Greenhouse provider outcome is ambiguous after broker boundary",
            ) from error

        try:
            _assert_evidence_matches_dispatch(evidence, dispatch)
        except GreenhouseSubmitOutboxError as error:
            error_code = "GREENHOUSE_SUBMIT_BROKER_EVIDENCE_MISMATCH_AMBIGUOUS"
            self._record_ambiguous_or_terminal(
                dispatch,
                evidence=None,
                error_code=error_code,
            )
            raise GreenhouseSubmitOutboxError(
                error_code,
                "Greenhouse provider outcome is ambiguous because broker evidence drifted",
            ) from error
        if evidence.outcome is GreenhouseSubmissionOutcome.ACCEPTED_UNVERIFIED:
            self._record_accepted_or_terminal(dispatch, evidence=evidence)
            return
        if evidence.outcome is GreenhouseSubmissionOutcome.REJECTED:
            self._record_rejected_or_terminal(
                dispatch,
                evidence=evidence,
                error_code=evidence.reason_code,
                reason_codes=(evidence.reason_code,),
            )
            return
        if evidence.outcome is GreenhouseSubmissionOutcome.AMBIGUOUS:
            self._record_ambiguous_or_terminal(
                dispatch,
                evidence=evidence,
                error_code=evidence.reason_code,
            )
            return
        raise GreenhouseSubmitOutboxError(
            "GREENHOUSE_SUBMIT_OUTCOME_UNSUPPORTED",
            "Greenhouse broker returned an unsupported submission outcome",
        )

    def _resolve_attachments_before_post(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
    ) -> tuple[GreenhouseResolvedAttachment, ...]:
        try:
            return tuple(
                self._attachment_resolver.resolve(ref) for ref in dispatch.payload.attachment_refs
            )
        except Exception as error:
            self._record_prepost_failure_or_raise(
                dispatch,
                error_code="GREENHOUSE_SUBMIT_ATTACHMENT_UNAVAILABLE",
            )
            raise GreenhouseSubmitOutboxError(
                "GREENHOUSE_SUBMIT_ATTACHMENT_UNAVAILABLE",
                "approved Greenhouse attachment could not be resolved before provider POST",
                retryable=True,
            ) from error

    def _record_accepted_or_terminal(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence,
    ) -> None:
        try:
            self._coordinator.record_accepted_unverified(dispatch, evidence=evidence)
        except Exception as error:
            raise GreenhouseSubmitOutboxError(
                "GREENHOUSE_SUBMIT_OUTCOME_PERSISTENCE_AMBIGUOUS",
                "accepted-unverified Greenhouse outcome could not be durably recorded",
            ) from error

    def _record_rejected_or_terminal(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence | None,
        error_code: str,
        reason_codes: Sequence[str],
    ) -> None:
        try:
            self._coordinator.record_rejected(
                dispatch,
                evidence=evidence,
                error_code=error_code,
                reason_codes=tuple(reason_codes),
            )
        except Exception as error:
            raise GreenhouseSubmitOutboxError(
                "GREENHOUSE_SUBMIT_OUTCOME_PERSISTENCE_AMBIGUOUS",
                "rejected Greenhouse outcome could not be durably recorded",
            ) from error

    def _record_ambiguous_or_terminal(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        evidence: GreenhouseSubmissionEvidence | None,
        error_code: str,
    ) -> None:
        try:
            self._coordinator.record_ambiguous(
                dispatch,
                evidence=evidence,
                error_code=error_code,
            )
        except Exception as error:
            raise GreenhouseSubmitOutboxError(
                "GREENHOUSE_SUBMIT_OUTCOME_PERSISTENCE_AMBIGUOUS",
                "ambiguous Greenhouse outcome could not be durably recorded",
            ) from error

    def _record_prepost_failure_or_raise(
        self,
        dispatch: PreparedGreenhouseSubmitDispatch,
        *,
        error_code: str,
    ) -> None:
        try:
            self._coordinator.record_prepost_failure(dispatch, error_code=error_code)
        except Exception as error:
            raise GreenhouseSubmitOutboxError(
                "GREENHOUSE_SUBMIT_PREPOST_FAILURE_RECORD_FAILED",
                "pre-POST Greenhouse failure could not be durably recorded",
            ) from error


def greenhouse_submit_event_key(reservation_key: str) -> str:
    if reservation_key.startswith(GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX):
        return reservation_key
    return f"{GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX}{reservation_key}"


def _assert_supported_event(event: ClaimedOutboxEvent) -> None:
    if event.event_type != OutboxEventType.WORKFLOW_SIGNAL.value:
        raise GreenhouseSubmitOutboxError(
            "GREENHOUSE_SUBMIT_EVENT_TYPE_UNSUPPORTED",
            "Greenhouse submit outbox sink only accepts workflow_signal events",
        )
    if not event.event_key.startswith(GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX):
        raise GreenhouseSubmitOutboxError(
            "GREENHOUSE_SUBMIT_EVENT_KEY_UNSUPPORTED",
            "Greenhouse submit outbox sink received a non-greenhouse-submit event",
        )


def _assert_prepare_state(dispatch: PreparedGreenhouseSubmitDispatch) -> None:
    try:
        PreparedGreenhouseSubmitState(dispatch.state)
    except ValueError as error:
        raise GreenhouseSubmitOutboxError(
            "GREENHOUSE_SUBMIT_PREPARE_STATE_UNSUPPORTED",
            "Greenhouse coordinator returned an unsupported prepare state",
        ) from error


def _assert_dispatch_matches_event(
    dispatch: PreparedGreenhouseSubmitDispatch,
    event: ClaimedOutboxEvent,
) -> None:
    if (
        dispatch.event_id != event.event_id
        or dispatch.event_key != event.event_key
        or dispatch.action_intent_id != event.action_intent_id
        or dispatch.payload_version_id != event.payload_version_id
    ):
        raise GreenhouseSubmitOutboxError(
            "GREENHOUSE_SUBMIT_DISPATCH_EVENT_MISMATCH",
            "Greenhouse submit metadata does not match the claimed outbox event",
        )


def _assert_evidence_matches_dispatch(
    evidence: GreenhouseSubmissionEvidence,
    dispatch: PreparedGreenhouseSubmitDispatch,
) -> None:
    if (
        evidence.submission_identity != dispatch.payload.submission_identity
        or evidence.reconciliation_key != dispatch.reconciliation_key
        or evidence.expected_schema_hash != dispatch.schema.schema_hash
        or evidence.payload_hash != dispatch.payload.payload_hash
        or evidence.material_hash != dispatch.payload.material_hash
    ):
        raise GreenhouseSubmitOutboxError(
            "GREENHOUSE_SUBMIT_EVIDENCE_BINDING_MISMATCH",
            "Greenhouse evidence is not bound to the prepared dispatch",
        )
    if evidence.next_attempt_allowed:
        raise GreenhouseSubmitOutboxError(
            "GREENHOUSE_SUBMIT_EVIDENCE_RETRY_FORBIDDEN",
            "Greenhouse evidence must forbid replay after provider-start boundary",
        )
    if (
        evidence.outcome is GreenhouseSubmissionOutcome.ACCEPTED_UNVERIFIED
        and not evidence.reconciliation_required
    ):
        raise GreenhouseSubmitOutboxError(
            "GREENHOUSE_SUBMIT_ACCEPTED_RECONCILIATION_MISSING",
            "accepted-unverified Greenhouse evidence must require reconciliation",
        )


def _assert_resolved_attachments_match(
    dispatch: PreparedGreenhouseSubmitDispatch,
    attachments: Sequence[GreenhouseResolvedAttachment],
) -> None:
    if tuple(item.ref for item in attachments) != dispatch.payload.attachment_refs:
        raise GreenhouseSubmitOutboxError(
            "GREENHOUSE_SUBMIT_ATTACHMENT_BINDING_MISMATCH",
            "resolved Greenhouse attachments must exactly match approved payload refs",
            retryable=True,
        )


def _validate_bounded(value: str, field_name: str) -> None:
    if _BOUNDED_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be bounded")


def _validate_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GreenhouseSubmitOutboxError(
            "GREENHOUSE_SUBMIT_TIME_INVALID",
            f"{field_name} must be timezone-aware",
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "GREENHOUSE_SUBMIT_EVENT_KEY_PREFIX",
    "GreenhouseAttachmentResolver",
    "GreenhouseSubmissionBroker",
    "GreenhouseSubmitCoordinator",
    "GreenhouseSubmitOutboxError",
    "GreenhouseSubmitOutboxSink",
    "PreparedGreenhouseSubmitDispatch",
    "PreparedGreenhouseSubmitState",
    "greenhouse_submit_event_key",
]
