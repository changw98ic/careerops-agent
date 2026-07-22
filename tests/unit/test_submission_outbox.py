from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from careerops.application.outbox import (
    ClaimedOutboxEvent,
    InternalDeliveryError,
    OutboxPublisher,
)
from careerops.application.submission_dispatch import (
    SyntheticProviderState,
    SyntheticSubmissionReceipt,
)
from careerops.application.submission_outbox import (
    SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX,
    DeterministicSyntheticSubmissionProvider,
    PreparedSyntheticSubmission,
    PreparedSyntheticSubmissionState,
    SyntheticSubmissionOutboxSink,
)

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 7, 20, 9, 5, tzinfo=UTC)
RESERVATION_KEY = "b" * 64
RECONCILIATION_KEY = "c" * 64


def _claimed_event() -> ClaimedOutboxEvent:
    return ClaimedOutboxEvent(
        event_id=uuid4(),
        event_key=f"{SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX}{RESERVATION_KEY}",
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
    state: PreparedSyntheticSubmissionState = PreparedSyntheticSubmissionState.READY,
    confirmed_receipt: SyntheticSubmissionReceipt | None = None,
) -> PreparedSyntheticSubmission:
    return PreparedSyntheticSubmission(
        event_id=event.event_id,
        event_key=event.event_key,
        action_intent_id=event.action_intent_id,
        payload_version_id=event.payload_version_id,
        reservation_key=RESERVATION_KEY,
        reconciliation_key=RECONCILIATION_KEY,
        state=state,
        confirmed_receipt=confirmed_receipt,
    )


def _receipt(
    *,
    provider_state: SyntheticProviderState = SyntheticProviderState.CONFIRMED,
    reconciliation_key: str = RECONCILIATION_KEY,
    received_at: datetime = RECEIVED_AT,
) -> SyntheticSubmissionReceipt:
    return SyntheticSubmissionReceipt(
        provider="synthetic",
        provider_resource_id="receipt-1",
        reconciliation_key=reconciliation_key,
        provider_state=provider_state,
        received_at=received_at,
    )


class _RecordingCoordinator:
    def __init__(
        self,
        prepared: PreparedSyntheticSubmission,
        *,
        record_receipt_error: Exception | None = None,
        record_ambiguous_error: Exception | None = None,
    ) -> None:
        self.prepared = prepared
        self.record_receipt_error = record_receipt_error
        self.record_ambiguous_error = record_ambiguous_error
        self.prepared_events: list[ClaimedOutboxEvent] = []
        self.receipts: list[tuple[PreparedSyntheticSubmission, SyntheticSubmissionReceipt]] = []
        self.ambiguous: list[tuple[PreparedSyntheticSubmission, str]] = []

    def prepare(self, event: ClaimedOutboxEvent) -> PreparedSyntheticSubmission:
        self.prepared_events.append(event)
        return self.prepared

    def record_receipt(
        self,
        dispatch: PreparedSyntheticSubmission,
        receipt: SyntheticSubmissionReceipt,
    ) -> None:
        if self.record_receipt_error is not None:
            raise self.record_receipt_error
        self.receipts.append((dispatch, receipt))

    def record_ambiguous(
        self,
        dispatch: PreparedSyntheticSubmission,
        *,
        error_code: str,
    ) -> None:
        if self.record_ambiguous_error is not None:
            raise self.record_ambiguous_error
        self.ambiguous.append((dispatch, error_code))


class _RecordingProvider:
    def __init__(
        self,
        *,
        receipt: SyntheticSubmissionReceipt | None = None,
        error: Exception | None = None,
    ) -> None:
        self.receipt = receipt or _receipt()
        self.error = error
        self.calls: list[tuple[PreparedSyntheticSubmission, datetime]] = []

    def submit(
        self,
        dispatch: PreparedSyntheticSubmission,
        *,
        now: datetime,
    ) -> SyntheticSubmissionReceipt:
        self.calls.append((dispatch, now))
        if self.error is not None:
            raise self.error
        return replace(self.receipt, received_at=now)


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
        assert event_key_prefix == SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX
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


def test_sink_rejects_claimed_event_identity_mismatch_before_provider_call() -> None:
    event = _claimed_event()
    coordinator = _RecordingCoordinator(replace(_prepared(event), event_id=uuid4()))
    provider = _RecordingProvider()

    with pytest.raises(InternalDeliveryError) as error:
        SyntheticSubmissionOutboxSink(coordinator, provider).deliver(event)

    assert error.value.error_code == "SYNTHETIC_DISPATCH_EVENT_MISMATCH"
    assert error.value.retryable is False
    assert coordinator.prepared_events == [event]
    assert provider.calls == []
    assert coordinator.receipts == []


def test_existing_confirmed_receipt_skips_provider_call() -> None:
    event = _claimed_event()
    confirmed = _receipt()
    prepared = _prepared(
        event,
        state=PreparedSyntheticSubmissionState.ALREADY_CONFIRMED,
        confirmed_receipt=confirmed,
    )
    coordinator = _RecordingCoordinator(prepared)
    provider = _RecordingProvider()

    SyntheticSubmissionOutboxSink(coordinator, provider).deliver(event)

    assert provider.calls == []
    assert coordinator.receipts == []
    assert coordinator.ambiguous == []


def test_deterministic_provider_records_confirmed_receipt_with_fresh_clock_time() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    coordinator = _RecordingCoordinator(prepared)
    sink = SyntheticSubmissionOutboxSink(
        coordinator,
        DeterministicSyntheticSubmissionProvider(),
        clock=lambda: RECEIVED_AT,
    )

    sink.deliver(event)

    assert len(coordinator.receipts) == 1
    recorded_dispatch, recorded_receipt = coordinator.receipts[0]
    assert recorded_dispatch == prepared
    assert recorded_receipt.provider == "synthetic"
    assert recorded_receipt.provider_resource_id.startswith("synthetic-receipt-")
    assert recorded_receipt.reconciliation_key == RECONCILIATION_KEY
    assert recorded_receipt.provider_state is SyntheticProviderState.CONFIRMED
    assert recorded_receipt.received_at == RECEIVED_AT
    assert recorded_receipt.received_at != event.available_at
    assert coordinator.ambiguous == []


def test_ambiguous_provider_receipt_is_recorded_and_terminal_in_filtered_publisher() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    coordinator = _RecordingCoordinator(prepared)
    store = _TerminalStore(event)
    sink = SyntheticSubmissionOutboxSink(
        coordinator,
        DeterministicSyntheticSubmissionProvider(provider_state=SyntheticProviderState.AMBIGUOUS),
        clock=lambda: RECEIVED_AT,
    )
    publisher = OutboxPublisher(
        store,
        sink,
        event_key_prefix=SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX,
        clock=lambda: RECEIVED_AT,
    )

    result = publisher.publish_batch(owner="synthetic-outbox", now=NOW)

    assert result.claimed == 1
    assert result.published == 0
    assert result.deferred == 0
    assert result.failed == 1
    assert len(coordinator.receipts) == 1
    assert coordinator.receipts[0][1].provider_state is SyntheticProviderState.AMBIGUOUS
    assert coordinator.ambiguous == [
        (prepared, "SYNTHETIC_PROVIDER_STATE_AMBIGUOUS"),
    ]
    assert store.releases == [
        (
            event.event_id,
            "SYNTHETIC_PROVIDER_STATE_AMBIGUOUS",
            True,
            event.lease_token,
        )
    ]
    assert store.published == []


def test_ambiguous_receipt_still_releases_terminally_if_secondary_recording_fails() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    coordinator = _RecordingCoordinator(
        prepared,
        record_ambiguous_error=RuntimeError("secondary ambiguity write failed"),
    )
    store = _TerminalStore(event)
    publisher = OutboxPublisher(
        store,
        SyntheticSubmissionOutboxSink(
            coordinator,
            DeterministicSyntheticSubmissionProvider(
                provider_state=SyntheticProviderState.AMBIGUOUS
            ),
            clock=lambda: RECEIVED_AT,
        ),
        event_key_prefix=SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX,
        clock=lambda: RECEIVED_AT,
    )

    result = publisher.publish_batch(owner="synthetic-outbox", now=NOW)

    assert result.failed == 1
    assert len(coordinator.receipts) == 1
    assert store.releases == [
        (
            event.event_id,
            "SYNTHETIC_PROVIDER_STATE_AMBIGUOUS",
            True,
            event.lease_token,
        )
    ]


def test_provider_exception_after_prepare_records_ambiguity_and_never_retries() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    coordinator = _RecordingCoordinator(prepared)
    store = _TerminalStore(event)
    sink = SyntheticSubmissionOutboxSink(
        coordinator,
        _RecordingProvider(error=RuntimeError("provider boundary failed")),
        clock=lambda: RECEIVED_AT,
    )
    publisher = OutboxPublisher(
        store,
        sink,
        event_key_prefix=SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX,
        clock=lambda: RECEIVED_AT,
    )

    result = publisher.publish_batch(owner="synthetic-outbox", now=NOW)

    assert result.claimed == 1
    assert result.published == 0
    assert result.deferred == 0
    assert result.failed == 1
    assert coordinator.receipts == []
    assert coordinator.ambiguous == [
        (prepared, "SYNTHETIC_PROVIDER_BOUNDARY_AMBIGUOUS"),
    ]
    assert store.releases == [
        (
            event.event_id,
            "SYNTHETIC_PROVIDER_BOUNDARY_AMBIGUOUS",
            True,
            event.lease_token,
        )
    ]


def test_receipt_recording_failure_after_provider_result_is_terminal_ambiguity() -> None:
    event = _claimed_event()
    prepared = _prepared(event)
    coordinator = _RecordingCoordinator(
        prepared,
        record_receipt_error=RuntimeError("database receipt write failed"),
    )
    store = _TerminalStore(event)
    sink = SyntheticSubmissionOutboxSink(
        coordinator,
        _RecordingProvider(),
        clock=lambda: RECEIVED_AT,
    )
    publisher = OutboxPublisher(
        store,
        sink,
        event_key_prefix=SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX,
        clock=lambda: RECEIVED_AT,
    )

    result = publisher.publish_batch(owner="synthetic-outbox", now=NOW)

    assert result.claimed == 1
    assert result.published == 0
    assert result.deferred == 0
    assert result.failed == 1
    assert coordinator.receipts == []
    assert coordinator.ambiguous == [
        (prepared, "SYNTHETIC_RECEIPT_PERSISTENCE_AMBIGUOUS"),
    ]
    assert store.releases == [
        (
            event.event_id,
            "SYNTHETIC_RECEIPT_PERSISTENCE_AMBIGUOUS",
            True,
            event.lease_token,
        )
    ]
