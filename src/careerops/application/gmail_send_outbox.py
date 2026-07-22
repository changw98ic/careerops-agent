from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from careerops.application.gmail_send import (
    GmailSendAttachmentRef,
    GmailSendPayload,
    GmailSendProviderState,
)
from careerops.application.outbox import (
    ClaimedOutboxEvent,
    InternalDeliveryError,
    OutboxEventType,
)

GMAIL_SEND_EVENT_KEY_PREFIX = "gmail-send:"
GMAIL_SEND_PROVIDER = "gmail"

_BOUNDED_ID = re.compile(r"^[A-Za-z0-9._:@/+\\=<>-]{1,512}$")
_BOUNDED_ERROR = re.compile(r"^[A-Z0-9_]{1,64}$")
_EMAIL = re.compile(r"^[^@\s]{1,160}@[^@\s]{1,160}$")


class GmailSendOutboxError(RuntimeError):
    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        if not _BOUNDED_ERROR.fullmatch(error_code):
            raise ValueError("error_code must be a bounded machine code")
        self.error_code = error_code
        self.retryable = retryable


class PreparedGmailSendState(StrEnum):
    READY = "ready"
    ALREADY_CONFIRMED = "already_confirmed"


@dataclass(frozen=True, slots=True)
class GmailSendProviderReceipt:
    provider_message_id: str
    provider_thread_id: str
    rfc_message_id: str
    provider_state: GmailSendProviderState
    received_at: datetime

    def __post_init__(self) -> None:
        _validate_bounded(self.provider_message_id, "provider_message_id")
        _validate_bounded(self.provider_thread_id, "provider_thread_id")
        _validate_bounded(self.rfc_message_id, "rfc_message_id")
        _validate_aware(self.received_at, "received_at")


@dataclass(frozen=True, slots=True)
class GmailSentMetadata:
    provider_message_id: str
    provider_thread_id: str
    label_ids: tuple[str, ...]
    headers: Mapping[str, str]

    def __post_init__(self) -> None:
        _validate_bounded(self.provider_message_id, "provider_message_id")
        _validate_bounded(self.provider_thread_id, "provider_thread_id")


@dataclass(frozen=True, slots=True)
class PreparedGmailSendDispatch:
    event_id: UUID
    event_key: str
    action_intent_id: UUID
    payload_version_id: UUID
    reservation_key: str
    reconciliation_key: str
    send_account_id: UUID
    send_credential_handle: str
    readonly_credential_handle: str
    account_subject: str
    payload: GmailSendPayload
    state: PreparedGmailSendState = PreparedGmailSendState.READY
    confirmed_receipt: GmailSendProviderReceipt | None = None

    def __post_init__(self) -> None:
        _validate_bounded(self.reservation_key, "reservation_key")
        _validate_bounded(self.reconciliation_key, "reconciliation_key")
        _validate_bounded(self.send_credential_handle, "send_credential_handle")
        _validate_bounded(self.readonly_credential_handle, "readonly_credential_handle")
        _validate_email(self.account_subject, "account_subject")
        if self.account_subject.strip().lower() != self.payload.sender.strip().lower():
            raise ValueError("account_subject must match reviewed Gmail sender")


class GmailSendAccessToken(Protocol):
    account_subject: str


class GmailResolvedAttachment(Protocol):
    @property
    def ref(self) -> GmailSendAttachmentRef: ...

    @property
    def data(self) -> bytes: ...


class GmailSendCredentialError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        if not _BOUNDED_ERROR.fullmatch(error_code):
            raise ValueError("error_code must be a bounded machine code")
        self.error_code = error_code


class GmailSendCredentialResolver(Protocol):
    def resolve_send(self, dispatch: PreparedGmailSendDispatch) -> GmailSendAccessToken: ...

    def resolve_readonly(self, dispatch: PreparedGmailSendDispatch) -> GmailSendAccessToken: ...


class GmailSendClient(Protocol):
    def send_message(
        self,
        *,
        token: GmailSendAccessToken,
        dispatch: PreparedGmailSendDispatch,
        attachments: Sequence[GmailResolvedAttachment],
        now: datetime,
    ) -> GmailSendProviderReceipt: ...


class GmailSentMetadataClient(Protocol):
    def get_sent_metadata(
        self,
        *,
        token: GmailSendAccessToken,
        dispatch: PreparedGmailSendDispatch,
        receipt: GmailSendProviderReceipt,
    ) -> GmailSentMetadata: ...

    def find_sent_by_rfc_message_id(
        self,
        *,
        token: GmailSendAccessToken,
        job: GmailSendReconciliationJob,
    ) -> GmailSentMetadata | None: ...


class GmailAttachmentResolver(Protocol):
    def resolve(self, ref: GmailSendAttachmentRef) -> GmailResolvedAttachment: ...


class GmailSendCoordinator(Protocol):
    def prepare(self, event: ClaimedOutboxEvent) -> PreparedGmailSendDispatch: ...

    def reconcile(
        self,
        dispatch: PreparedGmailSendDispatch,
        *,
        receipt: GmailSendProviderReceipt,
        metadata: GmailSentMetadata,
        reconciled_at: datetime,
    ) -> None: ...

    def record_receipt(
        self,
        dispatch: PreparedGmailSendDispatch,
        receipt: GmailSendProviderReceipt,
    ) -> None: ...

    def record_ambiguous(
        self,
        dispatch: PreparedGmailSendDispatch,
        *,
        error_code: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class GmailSendReconciliationJob:
    event_id: UUID
    event_key: str
    action_intent_id: UUID
    payload_version_id: UUID
    reservation_key: str
    reconciliation_key: str
    send_account_id: UUID
    readonly_credential_handle: str
    account_subject: str
    payload: GmailSendPayload
    lease_owner: str
    lease_token: UUID
    attempt_count: int = 0

    def __post_init__(self) -> None:
        _validate_bounded(self.reservation_key, "reservation_key")
        _validate_bounded(self.reconciliation_key, "reconciliation_key")
        _validate_bounded(self.readonly_credential_handle, "readonly_credential_handle")
        _validate_bounded(self.lease_owner, "lease_owner")
        _validate_email(self.account_subject, "account_subject")
        if self.account_subject.strip().lower() != self.payload.sender.strip().lower():
            raise ValueError("account_subject must match reviewed Gmail sender")


class GmailSendReconciliationRepository(Protocol):
    def claim_due(
        self,
        *,
        owner: str,
        now: datetime,
        limit: int,
    ) -> tuple[GmailSendReconciliationJob, ...]: ...

    def record_confirmed(
        self,
        *,
        job: GmailSendReconciliationJob,
        metadata: GmailSentMetadata,
        now: datetime,
    ) -> None: ...

    def keep_ambiguous(
        self,
        *,
        job: GmailSendReconciliationJob,
        error_code: str,
        now: datetime,
    ) -> None: ...


class GmailSendOutboxSink:
    def __init__(
        self,
        coordinator: GmailSendCoordinator,
        credential_resolver: GmailSendCredentialResolver,
        send_client: GmailSendClient,
        sent_metadata_client: GmailSentMetadataClient,
        attachment_resolver: GmailAttachmentResolver,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._credential_resolver = credential_resolver
        self._send_client = send_client
        self._sent_metadata_client = sent_metadata_client
        self._attachment_resolver = attachment_resolver
        self._clock = clock or _utc_now

    def deliver(self, event: ClaimedOutboxEvent) -> None:
        try:
            self._deliver(event)
        except GmailSendOutboxError as error:
            raise InternalDeliveryError(error.error_code, retryable=error.retryable) from error

    def _deliver(self, event: ClaimedOutboxEvent) -> None:
        _assert_supported_event(event)
        dispatch = self._coordinator.prepare(event)
        _assert_prepare_state(dispatch)
        _assert_dispatch_matches_event(dispatch, event)
        if dispatch.state is PreparedGmailSendState.ALREADY_CONFIRMED:
            if dispatch.confirmed_receipt is None:
                raise GmailSendOutboxError(
                    "GMAIL_SEND_CONFIRMED_RECEIPT_MISSING",
                    "confirmed Gmail send dispatch is missing its provider receipt",
                )
            _assert_receipt_matches_dispatch(dispatch.confirmed_receipt, dispatch)
            return
        if dispatch.confirmed_receipt is not None:
            raise GmailSendOutboxError(
                "GMAIL_SEND_READY_DISPATCH_HAS_RECEIPT",
                "ready Gmail send dispatch must not include a confirmed receipt",
            )

        send_token = self._resolve_before_post(
            lambda: self._credential_resolver.resolve_send(dispatch),
            "GMAIL_SEND_CREDENTIAL_UNAVAILABLE",
        )
        _assert_token_matches_dispatch(send_token, dispatch, "GMAIL_SEND_TOKEN_ACCOUNT_MISMATCH")
        readonly_token = self._resolve_before_post(
            lambda: self._credential_resolver.resolve_readonly(dispatch),
            "GMAIL_SEND_READONLY_CREDENTIAL_UNAVAILABLE",
        )
        _assert_token_matches_dispatch(
            readonly_token,
            dispatch,
            "GMAIL_SEND_READONLY_TOKEN_ACCOUNT_MISMATCH",
        )
        attachments = self._resolve_attachments_before_post(dispatch)
        _assert_resolved_attachments_match(dispatch, attachments)
        now = self._clock()
        _validate_aware(now, "Gmail send receipt time")
        try:
            receipt = self._send_client.send_message(
                token=send_token,
                dispatch=dispatch,
                attachments=attachments,
                now=now,
            )
            _assert_receipt_matches_dispatch(receipt, dispatch)
            metadata = self._sent_metadata_client.get_sent_metadata(
                token=readonly_token,
                dispatch=dispatch,
                receipt=receipt,
            )
            _assert_sent_metadata_matches(dispatch, receipt, metadata)
            self._coordinator.reconcile(
                dispatch,
                receipt=receipt,
                metadata=metadata,
                reconciled_at=self._clock(),
            )
            self._coordinator.record_receipt(dispatch, receipt)
        except Exception as error:
            error_code = _error_code_after_post(error)
            self._record_ambiguous_or_raise(dispatch, error_code=error_code)
            raise GmailSendOutboxError(
                error_code,
                "Gmail send provider outcome is ambiguous after POST boundary",
            ) from error

    def _resolve_before_post(
        self,
        callback: Callable[[], GmailSendAccessToken],
        fallback_error_code: str,
    ) -> GmailSendAccessToken:
        try:
            return callback()
        except GmailSendCredentialError as error:
            raise GmailSendOutboxError(
                error.error_code,
                "Gmail credential broker is unavailable before provider POST",
                retryable=True,
            ) from error
        except Exception as error:
            raise GmailSendOutboxError(
                fallback_error_code,
                "Gmail credential could not be resolved before provider POST",
                retryable=True,
            ) from error

    def _resolve_attachments_before_post(
        self,
        dispatch: PreparedGmailSendDispatch,
    ) -> tuple[GmailResolvedAttachment, ...]:
        try:
            return tuple(
                self._attachment_resolver.resolve(ref) for ref in dispatch.payload.attachment_refs
            )
        except Exception as error:
            raise GmailSendOutboxError(
                "GMAIL_SEND_ATTACHMENT_UNAVAILABLE",
                "approved attachment could not be resolved before provider POST",
                retryable=True,
            ) from error

    def _record_ambiguous_or_raise(
        self,
        dispatch: PreparedGmailSendDispatch,
        *,
        error_code: str,
    ) -> None:
        try:
            self._coordinator.record_ambiguous(dispatch, error_code=error_code)
        except Exception as error:
            raise GmailSendOutboxError(
                "GMAIL_SEND_AMBIGUITY_RECORD_FAILED",
                "Gmail send ambiguity could not be durably recorded after POST boundary",
            ) from error


def gmail_send_event_key(reservation_key: str) -> str:
    if reservation_key.startswith(GMAIL_SEND_EVENT_KEY_PREFIX):
        return reservation_key
    return f"{GMAIL_SEND_EVENT_KEY_PREFIX}{reservation_key}"


class GmailSendReconciliationService:
    """Resolve ambiguous Gmail sends through Sent metadata without re-POSTing."""

    def __init__(
        self,
        repository: GmailSendReconciliationRepository,
        credential_resolver: GmailSendCredentialResolver,
        sent_metadata_client: GmailSentMetadataClient,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._credential_resolver = credential_resolver
        self._sent_metadata_client = sent_metadata_client
        self._clock = clock or _utc_now

    def reconcile_batch(
        self,
        *,
        owner: str,
        now: datetime,
        limit: int = 25,
    ) -> tuple[int, int, int]:
        _validate_aware(now, "now")
        jobs = self._repository.claim_due(owner=owner, now=now, limit=limit)
        confirmed = 0
        still_ambiguous = 0
        failed = 0
        for job in jobs:
            try:
                dispatch = _dispatch_from_job(job)
                token = self._credential_resolver.resolve_readonly(dispatch)
                _assert_token_matches_dispatch(
                    token,
                    dispatch,
                    "GMAIL_SEND_READONLY_TOKEN_ACCOUNT_MISMATCH",
                )
                metadata = self._sent_metadata_client.find_sent_by_rfc_message_id(
                    token=token,
                    job=job,
                )
                if metadata is None:
                    self._repository.keep_ambiguous(
                        job=job,
                        error_code="GMAIL_SEND_SENT_MESSAGE_NOT_FOUND",
                        now=self._clock(),
                    )
                    still_ambiguous += 1
                    continue
                receipt = GmailSendProviderReceipt(
                    provider_message_id=metadata.provider_message_id,
                    provider_thread_id=metadata.provider_thread_id,
                    rfc_message_id=job.payload.message_id_header,
                    provider_state=GmailSendProviderState.SENT_CONFIRMED,
                    received_at=self._clock(),
                )
                _assert_sent_metadata_matches(dispatch, receipt, metadata)
                self._repository.record_confirmed(job=job, metadata=metadata, now=self._clock())
                confirmed += 1
            except GmailSendOutboxError as error:
                self._repository.keep_ambiguous(
                    job=job,
                    error_code=error.error_code,
                    now=self._clock(),
                )
                failed += 1
            except Exception:
                self._repository.keep_ambiguous(
                    job=job,
                    error_code="GMAIL_SEND_RECONCILIATION_FAILED",
                    now=self._clock(),
                )
                failed += 1
        return (len(jobs), confirmed, still_ambiguous + failed)


def _dispatch_from_job(job: GmailSendReconciliationJob) -> PreparedGmailSendDispatch:
    return PreparedGmailSendDispatch(
        event_id=job.event_id,
        event_key=job.event_key,
        action_intent_id=job.action_intent_id,
        payload_version_id=job.payload_version_id,
        reservation_key=job.reservation_key,
        reconciliation_key=job.reconciliation_key,
        send_account_id=job.send_account_id,
        send_credential_handle="reconciliation-only-send-handle",
        readonly_credential_handle=job.readonly_credential_handle,
        account_subject=job.account_subject,
        payload=job.payload,
    )


def _assert_supported_event(event: ClaimedOutboxEvent) -> None:
    if event.event_type != OutboxEventType.WORKFLOW_SIGNAL.value:
        raise GmailSendOutboxError(
            "GMAIL_SEND_EVENT_TYPE_UNSUPPORTED",
            "Gmail send outbox sink only accepts workflow_signal events",
        )
    if not event.event_key.startswith(GMAIL_SEND_EVENT_KEY_PREFIX):
        raise GmailSendOutboxError(
            "GMAIL_SEND_EVENT_KEY_UNSUPPORTED",
            "Gmail send outbox sink received a non-gmail-send event",
        )


def _assert_prepare_state(dispatch: PreparedGmailSendDispatch) -> None:
    try:
        PreparedGmailSendState(dispatch.state)
    except ValueError as error:
        raise GmailSendOutboxError(
            "GMAIL_SEND_PREPARE_STATE_UNSUPPORTED",
            "Gmail send coordinator returned an unsupported prepare state",
        ) from error


def _assert_dispatch_matches_event(
    dispatch: PreparedGmailSendDispatch,
    event: ClaimedOutboxEvent,
) -> None:
    if (
        dispatch.event_id != event.event_id
        or dispatch.event_key != event.event_key
        or dispatch.action_intent_id != event.action_intent_id
        or dispatch.payload_version_id != event.payload_version_id
    ):
        raise GmailSendOutboxError(
            "GMAIL_SEND_DISPATCH_EVENT_MISMATCH",
            "Gmail send metadata does not match the claimed outbox event",
        )
    if dispatch.event_key != gmail_send_event_key(dispatch.reservation_key):
        raise GmailSendOutboxError(
            "GMAIL_SEND_EVENT_KEY_BINDING_MISMATCH",
            "Gmail send event key is not bound to the persisted reservation key",
        )


def _assert_receipt_matches_dispatch(
    receipt: GmailSendProviderReceipt,
    dispatch: PreparedGmailSendDispatch,
) -> None:
    if receipt.provider_state is not GmailSendProviderState.SENT_CONFIRMED:
        raise GmailSendOutboxError(
            "GMAIL_SEND_PROVIDER_STATE_AMBIGUOUS",
            "Gmail send provider receipt is not confirmed",
        )
    if receipt.rfc_message_id != dispatch.payload.message_id_header:
        raise GmailSendOutboxError(
            "GMAIL_SEND_RECEIPT_MESSAGE_ID_MISMATCH",
            "Gmail send receipt is not bound to the approved Message-ID",
        )


def _assert_sent_metadata_matches(
    dispatch: PreparedGmailSendDispatch,
    receipt: GmailSendProviderReceipt,
    metadata: GmailSentMetadata,
) -> None:
    headers = {key.lower(): value for key, value in metadata.headers.items()}
    if metadata.provider_message_id != receipt.provider_message_id:
        raise GmailSendOutboxError(
            "GMAIL_SEND_SENT_MESSAGE_ID_MISMATCH",
            "Gmail Sent metadata did not return the provider message id",
        )
    if metadata.provider_thread_id != receipt.provider_thread_id:
        raise GmailSendOutboxError(
            "GMAIL_SEND_SENT_THREAD_ID_MISMATCH",
            "Gmail Sent metadata did not return the provider thread id",
        )
    if "SENT" not in metadata.label_ids:
        raise GmailSendOutboxError(
            "GMAIL_SEND_SENT_LABEL_MISSING",
            "Gmail Sent metadata is missing the SENT label",
        )
    if headers.get("from", "").strip().lower() != dispatch.payload.sender.strip().lower():
        raise GmailSendOutboxError(
            "GMAIL_SEND_SENT_FROM_MISMATCH",
            "Gmail Sent metadata From header does not match the approved payload",
        )
    if headers.get("to", "").strip().lower() != dispatch.payload.recipient.strip().lower():
        raise GmailSendOutboxError(
            "GMAIL_SEND_SENT_TO_MISMATCH",
            "Gmail Sent metadata To header does not match the approved payload",
        )
    if headers.get("subject", "") != dispatch.payload.subject:
        raise GmailSendOutboxError(
            "GMAIL_SEND_SENT_SUBJECT_MISMATCH",
            "Gmail Sent metadata Subject header does not match the approved payload",
        )
    if headers.get("message-id", "") != dispatch.payload.message_id_header:
        raise GmailSendOutboxError(
            "GMAIL_SEND_SENT_RFC_MESSAGE_ID_MISMATCH",
            "Gmail Sent metadata Message-ID header does not match the approved payload",
        )


def _error_code_after_post(error: Exception) -> str:
    if isinstance(error, GmailSendOutboxError):
        return error.error_code
    if isinstance(error, GmailSendCredentialError):
        return error.error_code
    return "GMAIL_SEND_POST_OUTCOME_AMBIGUOUS"


def _assert_token_matches_dispatch(
    token: GmailSendAccessToken,
    dispatch: PreparedGmailSendDispatch,
    error_code: str,
) -> None:
    if token.account_subject.strip().lower() != dispatch.account_subject.strip().lower():
        raise GmailSendOutboxError(
            error_code,
            "resolved Gmail token account does not match the prepared dispatch account",
            retryable=True,
        )


def _assert_resolved_attachments_match(
    dispatch: PreparedGmailSendDispatch,
    attachments: Sequence[GmailResolvedAttachment],
) -> None:
    approved = {ref.object_key: ref for ref in dispatch.payload.attachment_refs}
    resolved = {attachment.ref.object_key: attachment for attachment in attachments}
    if set(approved) != set(resolved):
        raise GmailSendOutboxError(
            "GMAIL_SEND_ATTACHMENT_BINDING_MISMATCH",
            "resolved attachments do not exactly match the approved payload refs",
            retryable=True,
        )
    for object_key, approved_ref in approved.items():
        attachment = resolved[object_key]
        if attachment.ref != approved_ref:
            raise GmailSendOutboxError(
                "GMAIL_SEND_ATTACHMENT_REF_MISMATCH",
                "resolved attachment ref differs from the approved payload ref",
                retryable=True,
            )
        if len(attachment.data) != approved_ref.size_bytes:
            raise GmailSendOutboxError(
                "GMAIL_SEND_ATTACHMENT_SIZE_MISMATCH",
                "resolved attachment size differs from the approved payload ref",
                retryable=True,
            )
        if hashlib.sha256(attachment.data).hexdigest() != approved_ref.sha256:
            raise GmailSendOutboxError(
                "GMAIL_SEND_ATTACHMENT_SHA256_MISMATCH",
                "resolved attachment digest differs from the approved payload ref",
                retryable=True,
            )


def _validate_email(value: str, label: str) -> None:
    if not _EMAIL.fullmatch(value):
        raise ValueError(f"{label} must be a bounded email address")


def _validate_bounded(value: str, label: str) -> None:
    if not _BOUNDED_ID.fullmatch(value):
        raise ValueError(f"{label} must be a bounded identifier")


def _validate_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GmailSendOutboxError(
            "GMAIL_SEND_PROVIDER_RECEIPT_TIME_INVALID",
            f"{label} must be timezone-aware",
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "GMAIL_SEND_EVENT_KEY_PREFIX",
    "GMAIL_SEND_PROVIDER",
    "GmailAttachmentResolver",
    "GmailResolvedAttachment",
    "GmailSendAccessToken",
    "GmailSendClient",
    "GmailSendCoordinator",
    "GmailSendCredentialError",
    "GmailSendCredentialResolver",
    "GmailSendOutboxError",
    "GmailSendOutboxSink",
    "GmailSendProviderReceipt",
    "GmailSendReconciliationJob",
    "GmailSendReconciliationRepository",
    "GmailSendReconciliationService",
    "GmailSentMetadata",
    "GmailSentMetadataClient",
    "PreparedGmailSendDispatch",
    "PreparedGmailSendState",
    "gmail_send_event_key",
]
