from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from careerops.application.gmail_send import (
    GmailSendAttachmentRef,
    GmailSendPayload,
    GmailSendProviderState,
)
from careerops.application.gmail_send_outbox import (
    GMAIL_SEND_EVENT_KEY_PREFIX,
    GmailResolvedAttachment,
    GmailSendAccessToken,
    GmailSendCredentialError,
    GmailSendOutboxSink,
    GmailSendProviderReceipt,
    GmailSendReconciliationJob,
    GmailSendReconciliationService,
    GmailSentMetadata,
    PreparedGmailSendDispatch,
    PreparedGmailSendState,
)
from careerops.application.outbox import ClaimedOutboxEvent, InternalDeliveryError, OutboxPublisher
from careerops.infrastructure.gmail.send_client import GmailSendResolvedAttachment

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 7, 20, 9, 5, tzinfo=UTC)
RECONCILED_AT = datetime(2026, 7, 20, 9, 6, tzinfo=UTC)


def _payload(*, with_attachment: bool = False) -> GmailSendPayload:
    attachment_refs = ()
    if with_attachment:
        attachment_refs = (
            GmailSendAttachmentRef(
                object_key="resume-1",
                filename="resume.pdf",
                content_type="application/pdf",
                size_bytes=4,
                sha256="3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7",
            ),
        )
    return GmailSendPayload(
        sender="alice@example.com",
        recipient="hr@example.com",
        subject="Application follow-up",
        text_body="Hello, this is a reviewed draft.",
        attachment_refs=attachment_refs,
    )


def _claimed_event() -> ClaimedOutboxEvent:
    return ClaimedOutboxEvent(
        event_id=uuid4(),
        event_key=f"{GMAIL_SEND_EVENT_KEY_PREFIX}{'a' * 64}",
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        event_type="workflow_signal",
        available_at=NOW,
        attempt_count=1,
        lease_token=uuid4(),
        lease_until=NOW + timedelta(seconds=30),
    )


def _prepared(
    event: ClaimedOutboxEvent,
    *,
    payload: GmailSendPayload | None = None,
    state: PreparedGmailSendState = PreparedGmailSendState.READY,
    confirmed_receipt: GmailSendProviderReceipt | None = None,
) -> PreparedGmailSendDispatch:
    return PreparedGmailSendDispatch(
        event_id=event.event_id,
        event_key=event.event_key,
        action_intent_id=event.action_intent_id,
        payload_version_id=event.payload_version_id,
        reservation_key=event.event_key.removeprefix(GMAIL_SEND_EVENT_KEY_PREFIX),
        reconciliation_key="b" * 64,
        send_account_id=uuid4(),
        send_credential_handle="send-handle-1",
        readonly_credential_handle="readonly-handle-1",
        account_subject="alice@example.com",
        payload=payload or _payload(),
        state=state,
        confirmed_receipt=confirmed_receipt,
    )


def _receipt(dispatch: PreparedGmailSendDispatch) -> GmailSendProviderReceipt:
    return GmailSendProviderReceipt(
        provider_message_id="gmail-message-1",
        provider_thread_id="gmail-thread-1",
        rfc_message_id=dispatch.payload.message_id_header,
        provider_state=GmailSendProviderState.SENT_CONFIRMED,
        received_at=RECEIVED_AT,
    )


def _metadata(dispatch: PreparedGmailSendDispatch) -> GmailSentMetadata:
    return GmailSentMetadata(
        provider_message_id="gmail-message-1",
        provider_thread_id="gmail-thread-1",
        label_ids=("SENT",),
        headers={
            "From": dispatch.payload.sender,
            "To": dispatch.payload.recipient,
            "Subject": dispatch.payload.subject,
            "Message-ID": dispatch.payload.message_id_header,
        },
    )


class _Token:
    def __init__(self, account_subject: str = "alice@example.com") -> None:
        self.account_subject = account_subject


class _Coordinator:
    def __init__(
        self,
        prepared: PreparedGmailSendDispatch,
        *,
        record_receipt_error: Exception | None = None,
        record_ambiguous_error: Exception | None = None,
    ) -> None:
        self.prepared = prepared
        self.record_receipt_error = record_receipt_error
        self.record_ambiguous_error = record_ambiguous_error
        self.prepared_events: list[ClaimedOutboxEvent] = []
        self.reconciliations: list[
            tuple[PreparedGmailSendDispatch, GmailSendProviderReceipt, GmailSentMetadata]
        ] = []
        self.receipts: list[tuple[PreparedGmailSendDispatch, GmailSendProviderReceipt]] = []
        self.ambiguous: list[tuple[PreparedGmailSendDispatch, str]] = []

    def prepare(self, event: ClaimedOutboxEvent) -> PreparedGmailSendDispatch:
        self.prepared_events.append(event)
        return self.prepared

    def reconcile(
        self,
        dispatch: PreparedGmailSendDispatch,
        *,
        receipt: GmailSendProviderReceipt,
        metadata: GmailSentMetadata,
        reconciled_at: datetime,
    ) -> None:
        assert reconciled_at == RECONCILED_AT
        self.reconciliations.append((dispatch, receipt, metadata))

    def record_receipt(
        self,
        dispatch: PreparedGmailSendDispatch,
        receipt: GmailSendProviderReceipt,
    ) -> None:
        if self.record_receipt_error is not None:
            raise self.record_receipt_error
        self.receipts.append((dispatch, receipt))

    def record_ambiguous(
        self,
        dispatch: PreparedGmailSendDispatch,
        *,
        error_code: str,
    ) -> None:
        if self.record_ambiguous_error is not None:
            raise self.record_ambiguous_error
        self.ambiguous.append((dispatch, error_code))


class _Resolver:
    def __init__(
        self,
        *,
        send_error: Exception | None = None,
        readonly_error: Exception | None = None,
        send_subject: str = "alice@example.com",
        readonly_subject: str = "alice@example.com",
    ) -> None:
        self.send_error = send_error
        self.readonly_error = readonly_error
        self.send_subject = send_subject
        self.readonly_subject = readonly_subject
        self.calls: list[str] = []

    def resolve_send(self, dispatch: PreparedGmailSendDispatch) -> GmailSendAccessToken:
        del dispatch
        self.calls.append("send")
        if self.send_error is not None:
            raise self.send_error
        return _Token(self.send_subject)

    def resolve_readonly(self, dispatch: PreparedGmailSendDispatch) -> GmailSendAccessToken:
        del dispatch
        self.calls.append("readonly")
        if self.readonly_error is not None:
            raise self.readonly_error
        return _Token(self.readonly_subject)


class _AttachmentResolver:
    def __init__(self, *, bad_attachment: bool = False) -> None:
        self.bad_attachment = bad_attachment
        self.resolved: list[GmailSendAttachmentRef] = []

    def resolve(self, ref: GmailSendAttachmentRef) -> GmailResolvedAttachment:
        self.resolved.append(ref)
        if self.bad_attachment:
            return _BadAttachment(ref=replace(ref, sha256="0" * 64), data=b"data")
        return GmailSendResolvedAttachment(ref=ref, data=b"data")


class _BadAttachment:
    def __init__(self, *, ref: GmailSendAttachmentRef, data: bytes) -> None:
        self.ref = ref
        self.data = data


class _SendClient:
    def __init__(self, receipt: GmailSendProviderReceipt) -> None:
        self.receipt = receipt
        self.calls: list[PreparedGmailSendDispatch] = []

    def send_message(
        self,
        *,
        token: GmailSendAccessToken,
        dispatch: PreparedGmailSendDispatch,
        attachments: Sequence[GmailResolvedAttachment],
        now: datetime,
    ) -> GmailSendProviderReceipt:
        del token, attachments
        assert now == RECEIVED_AT
        self.calls.append(dispatch)
        return self.receipt


class _MetadataClient:
    def __init__(
        self,
        metadata: GmailSentMetadata | None,
        *,
        find_error: Exception | None = None,
    ) -> None:
        self.metadata = metadata
        self.find_error = find_error
        self.get_calls: list[GmailSendProviderReceipt] = []
        self.find_calls: list[GmailSendReconciliationJob] = []

    def get_sent_metadata(
        self,
        *,
        token: GmailSendAccessToken,
        dispatch: PreparedGmailSendDispatch,
        receipt: GmailSendProviderReceipt,
    ) -> GmailSentMetadata:
        del token, dispatch
        self.get_calls.append(receipt)
        if self.metadata is None:
            raise RuntimeError("metadata unavailable")
        return self.metadata

    def find_sent_by_rfc_message_id(
        self,
        *,
        token: GmailSendAccessToken,
        job: GmailSendReconciliationJob,
    ) -> GmailSentMetadata | None:
        del token
        self.find_calls.append(job)
        if self.find_error is not None:
            raise self.find_error
        return self.metadata


class _TerminalStore:
    def __init__(self, event: ClaimedOutboxEvent) -> None:
        self.event = event
        self.releases: list[tuple[UUID, str, bool, UUID]] = []
        self.published: list[tuple[UUID, UUID]] = []

    def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
        event_key_prefix: str | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        del owner, now, lease_for, limit
        assert event_key_prefix == GMAIL_SEND_EVENT_KEY_PREFIX
        return (self.event,)

    def mark_published(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
    ) -> None:
        del owner, now
        self.published.append((event_id, lease_token))

    def release(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
        retry_at: datetime,
        error_code: str,
        terminal: bool,
    ) -> None:
        del owner, now, retry_at
        self.releases.append((event_id, error_code, terminal, lease_token))


def test_send_outbox_resolves_all_inputs_before_post_then_records_reconcile_and_receipt() -> None:
    event = _claimed_event()
    payload = _payload(with_attachment=True)
    prepared = _prepared(event, payload=payload)
    coordinator = _Coordinator(prepared)
    resolver = _Resolver()
    attachment_resolver = _AttachmentResolver()
    send_client = _SendClient(_receipt(prepared))
    metadata_client = _MetadataClient(_metadata(prepared))

    GmailSendOutboxSink(
        coordinator,
        resolver,
        send_client,
        metadata_client,
        attachment_resolver,
        clock=_clock(),
    ).deliver(event)

    assert resolver.calls == ["send", "readonly"]
    assert attachment_resolver.resolved == list(payload.attachment_refs)
    assert send_client.calls == [prepared]
    assert len(coordinator.reconciliations) == 1
    assert len(coordinator.receipts) == 1
    assert coordinator.ambiguous == []


def test_existing_confirmed_send_skips_tokens_attachments_and_provider_post() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    confirmed = _receipt(prepared)
    replay = replace(
        prepared,
        state=PreparedGmailSendState.ALREADY_CONFIRMED,
        confirmed_receipt=confirmed,
    )
    resolver = _Resolver()
    send_client = _SendClient(confirmed)

    GmailSendOutboxSink(
        _Coordinator(replay),
        resolver,
        send_client,
        _MetadataClient(_metadata(prepared)),
        _AttachmentResolver(),
        clock=_clock(),
    ).deliver(event)

    assert resolver.calls == []
    assert send_client.calls == []


def test_readonly_token_failure_before_post_is_retryable_and_not_ambiguous() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    coordinator = _Coordinator(prepared)
    resolver = _Resolver(readonly_error=GmailSendCredentialError("GMAIL_READONLY_BROKER_DOWN"))
    send_client = _SendClient(_receipt(prepared))

    with pytest.raises(InternalDeliveryError) as error:
        GmailSendOutboxSink(
            coordinator,
            resolver,
            send_client,
            _MetadataClient(_metadata(prepared)),
            _AttachmentResolver(),
            clock=_clock(),
        ).deliver(event)

    assert error.value.error_code == "GMAIL_READONLY_BROKER_DOWN"
    assert error.value.retryable is True
    assert send_client.calls == []
    assert coordinator.ambiguous == []


def test_token_account_mismatch_before_post_is_retryable_and_not_ambiguous() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    coordinator = _Coordinator(prepared)
    send_client = _SendClient(_receipt(prepared))

    with pytest.raises(InternalDeliveryError) as error:
        GmailSendOutboxSink(
            coordinator,
            _Resolver(send_subject="wrong@example.com"),
            send_client,
            _MetadataClient(_metadata(prepared)),
            _AttachmentResolver(),
            clock=_clock(),
        ).deliver(event)

    assert error.value.error_code == "GMAIL_SEND_TOKEN_ACCOUNT_MISMATCH"
    assert error.value.retryable is True
    assert send_client.calls == []
    assert coordinator.ambiguous == []


def test_attachment_binding_mismatch_before_post_is_retryable_and_not_ambiguous() -> None:
    event = _claimed_event()
    payload = _payload(with_attachment=True)
    prepared = _prepared(event, payload=payload)
    coordinator = _Coordinator(prepared)
    send_client = _SendClient(_receipt(prepared))

    with pytest.raises(InternalDeliveryError) as error:
        GmailSendOutboxSink(
            coordinator,
            _Resolver(),
            send_client,
            _MetadataClient(_metadata(prepared)),
            _AttachmentResolver(bad_attachment=True),
            clock=_clock(),
        ).deliver(event)

    assert error.value.error_code == "GMAIL_SEND_ATTACHMENT_REF_MISMATCH"
    assert error.value.retryable is True
    assert send_client.calls == []
    assert coordinator.ambiguous == []


def test_post_then_sent_metadata_mismatch_records_terminal_ambiguity() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    bad_metadata = replace(_metadata(prepared), label_ids=("INBOX",))
    coordinator = _Coordinator(prepared)
    store = _TerminalStore(event)
    publisher = OutboxPublisher(
        store,
        GmailSendOutboxSink(
            coordinator,
            _Resolver(),
            _SendClient(_receipt(prepared)),
            _MetadataClient(bad_metadata),
            _AttachmentResolver(),
            clock=_clock(),
        ),
        event_key_prefix=GMAIL_SEND_EVENT_KEY_PREFIX,
        clock=lambda: RECONCILED_AT,
    )

    result = publisher.publish_batch(owner="gmail-send", now=NOW)

    assert result.failed == 1
    assert coordinator.ambiguous == [(prepared, "GMAIL_SEND_SENT_LABEL_MISSING")]
    assert store.releases == [
        (event.event_id, "GMAIL_SEND_SENT_LABEL_MISSING", True, event.lease_token)
    ]
    assert store.published == []


def test_receipt_recording_failure_after_post_is_terminal_ambiguity() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    coordinator = _Coordinator(
        prepared,
        record_receipt_error=RuntimeError("receipt write failed"),
    )
    store = _TerminalStore(event)
    publisher = OutboxPublisher(
        store,
        GmailSendOutboxSink(
            coordinator,
            _Resolver(),
            _SendClient(_receipt(prepared)),
            _MetadataClient(_metadata(prepared)),
            _AttachmentResolver(),
            clock=_clock(),
        ),
        event_key_prefix=GMAIL_SEND_EVENT_KEY_PREFIX,
        clock=lambda: RECONCILED_AT,
    )

    result = publisher.publish_batch(owner="gmail-send", now=NOW)

    assert result.failed == 1
    assert len(coordinator.reconciliations) == 1
    assert coordinator.receipts == []
    assert coordinator.ambiguous == [(prepared, "GMAIL_SEND_POST_OUTCOME_AMBIGUOUS")]
    assert store.releases == [
        (event.event_id, "GMAIL_SEND_POST_OUTCOME_AMBIGUOUS", True, event.lease_token)
    ]
    assert store.published == []


def test_ambiguity_record_failure_after_post_is_visible_terminal_release_code() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    coordinator = _Coordinator(
        prepared,
        record_ambiguous_error=RuntimeError("ambiguity write failed"),
    )
    store = _TerminalStore(event)
    publisher = OutboxPublisher(
        store,
        GmailSendOutboxSink(
            coordinator,
            _Resolver(),
            _SendClient(_receipt(prepared)),
            _MetadataClient(replace(_metadata(prepared), label_ids=("INBOX",))),
            _AttachmentResolver(),
            clock=_clock(),
        ),
        event_key_prefix=GMAIL_SEND_EVENT_KEY_PREFIX,
        clock=lambda: RECONCILED_AT,
    )

    result = publisher.publish_batch(owner="gmail-send", now=NOW)

    assert result.failed == 1
    assert coordinator.ambiguous == []
    assert store.releases == [
        (event.event_id, "GMAIL_SEND_AMBIGUITY_RECORD_FAILED", True, event.lease_token)
    ]
    assert store.published == []


def test_successful_send_marks_outbox_published_only_after_receipt_recorded() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    coordinator = _Coordinator(prepared)
    store = _TerminalStore(event)
    publisher = OutboxPublisher(
        store,
        GmailSendOutboxSink(
            coordinator,
            _Resolver(),
            _SendClient(_receipt(prepared)),
            _MetadataClient(_metadata(prepared)),
            _AttachmentResolver(),
            clock=_clock(),
        ),
        event_key_prefix=GMAIL_SEND_EVENT_KEY_PREFIX,
        clock=lambda: RECONCILED_AT,
    )

    result = publisher.publish_batch(owner="gmail-send", now=NOW)

    assert result.published == 1
    assert len(coordinator.reconciliations) == 1
    assert len(coordinator.receipts) == 1
    assert store.published == [(event.event_id, event.lease_token)]
    assert store.releases == []


class _ReconciliationRepository:
    def __init__(self, job: GmailSendReconciliationJob) -> None:
        self.job = job
        self.confirmed: list[tuple[GmailSendReconciliationJob, GmailSentMetadata]] = []
        self.ambiguous: list[tuple[GmailSendReconciliationJob, str]] = []

    def claim_due(
        self,
        *,
        owner: str,
        now: datetime,
        limit: int,
    ) -> tuple[GmailSendReconciliationJob, ...]:
        del owner, now, limit
        return (self.job,)

    def record_confirmed(
        self,
        *,
        job: GmailSendReconciliationJob,
        metadata: GmailSentMetadata,
        now: datetime,
    ) -> None:
        assert now == RECONCILED_AT
        self.confirmed.append((job, metadata))

    def keep_ambiguous(
        self,
        *,
        job: GmailSendReconciliationJob,
        error_code: str,
        now: datetime,
    ) -> None:
        assert now == RECONCILED_AT
        self.ambiguous.append((job, error_code))


def _reconciliation_job(prepared: PreparedGmailSendDispatch) -> GmailSendReconciliationJob:
    return GmailSendReconciliationJob(
        event_id=prepared.event_id,
        event_key=prepared.event_key,
        action_intent_id=prepared.action_intent_id,
        payload_version_id=prepared.payload_version_id,
        reservation_key=prepared.reservation_key,
        reconciliation_key=prepared.reconciliation_key,
        send_account_id=prepared.send_account_id,
        readonly_credential_handle=prepared.readonly_credential_handle,
        account_subject=prepared.account_subject,
        payload=prepared.payload,
        lease_owner="gmail-send-reconciler",
        lease_token=uuid4(),
    )


def test_reconciliation_service_confirms_by_rfc_message_id_without_repost() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    job = _reconciliation_job(prepared)
    repo = _ReconciliationRepository(job)
    metadata_client = _MetadataClient(_metadata(prepared))

    result = GmailSendReconciliationService(
        repo,
        _Resolver(),
        metadata_client,
        clock=lambda: RECONCILED_AT,
    ).reconcile_batch(owner="gmail-send-reconciler", now=NOW)

    assert result == (1, 1, 0)
    assert metadata_client.find_calls == [job]
    assert len(repo.confirmed) == 1
    assert repo.ambiguous == []


def test_reconciliation_service_preserves_domain_error_code_when_still_ambiguous() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    job = _reconciliation_job(prepared)
    repo = _ReconciliationRepository(job)
    metadata_client = _MetadataClient(replace(_metadata(prepared), label_ids=("INBOX",)))

    result = GmailSendReconciliationService(
        repo,
        _Resolver(),
        metadata_client,
        clock=lambda: RECONCILED_AT,
    ).reconcile_batch(owner="gmail-send-reconciler", now=NOW)

    assert result == (1, 0, 1)
    assert repo.confirmed == []
    assert repo.ambiguous == [(job, "GMAIL_SEND_SENT_LABEL_MISSING")]


def test_reconciliation_service_uses_generic_code_for_unexpected_failures() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    job = _reconciliation_job(prepared)
    repo = _ReconciliationRepository(job)
    metadata_client = _MetadataClient(None, find_error=RuntimeError("gmail outage"))

    result = GmailSendReconciliationService(
        repo,
        _Resolver(),
        metadata_client,
        clock=lambda: RECONCILED_AT,
    ).reconcile_batch(owner="gmail-send-reconciler", now=NOW)

    assert result == (1, 0, 1)
    assert repo.confirmed == []
    assert repo.ambiguous == [(job, "GMAIL_SEND_RECONCILIATION_FAILED")]


def _clock() -> Callable[[], datetime]:
    moments = iter((RECEIVED_AT, RECONCILED_AT, RECONCILED_AT))
    return lambda: next(moments)
