from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from careerops.application.outbox import (
    ClaimedOutboxEvent,
    InternalDeliveryError,
    OutboxPublisher,
)


def event(*, event_type: str = "workflow_signal", attempt_count: int = 1) -> ClaimedOutboxEvent:
    now = datetime.now(UTC)
    return ClaimedOutboxEvent(
        event_id=uuid4(),
        event_key=f"unit:{uuid4()}",
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        event_type=event_type,
        available_at=now,
        attempt_count=attempt_count,
        lease_token=uuid4(),
        lease_until=now + timedelta(seconds=30),
    )


class RecordingStore:
    def __init__(self, events: tuple[ClaimedOutboxEvent, ...]) -> None:
        self.events = events
        self.published: list[tuple[UUID, UUID]] = []
        self.released: list[tuple[UUID, str, bool, UUID]] = []
        self.claim_count = 0

    def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        del owner, now, lease_for, limit
        self.claim_count += 1
        return self.events

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
        self.released.append((event_id, error_code, terminal, lease_token))


class SuccessfulSink:
    def __init__(self) -> None:
        self.delivered: list[UUID] = []

    def deliver(self, event: ClaimedOutboxEvent) -> None:
        self.delivered.append(event.event_id)


class FailingSink:
    def __init__(self, *, retryable: bool) -> None:
        self.retryable = retryable

    def deliver(self, event: ClaimedOutboxEvent) -> None:
        del event
        raise InternalDeliveryError("INTERNAL_SINK_FAILURE", retryable=self.retryable)


def test_default_publisher_does_not_claim_or_consume_attempts() -> None:
    item = event()
    store = RecordingStore((item,))

    result = OutboxPublisher(store).publish_batch(owner="publisher-1", now=datetime.now(UTC))

    assert result.claimed == 0
    assert result.deferred == 0
    assert result.published == 0
    assert store.claim_count == 0
    assert store.published == []
    assert store.released == []


def test_successful_internal_delivery_is_marked_published() -> None:
    item = event()
    store = RecordingStore((item,))
    sink = SuccessfulSink()

    result = OutboxPublisher(store, sink).publish_batch(owner="publisher-1", now=datetime.now(UTC))

    assert result.published == 1
    assert sink.delivered == [item.event_id]
    assert store.published == [(item.event_id, item.lease_token)]
    assert store.released == []


def test_unknown_event_type_fails_without_reaching_sink() -> None:
    item = event(event_type="provider.write")
    store = RecordingStore((item,))
    sink = SuccessfulSink()

    result = OutboxPublisher(store, sink).publish_batch(owner="publisher-1", now=datetime.now(UTC))

    assert result.failed == 1
    assert sink.delivered == []
    assert store.released == [(item.event_id, "UNKNOWN_EVENT_TYPE", True, item.lease_token)]


def test_retry_limit_and_nonretryable_errors_become_terminal() -> None:
    retry_exhausted = event(attempt_count=3)
    nonretryable = event()

    exhausted_store = RecordingStore((retry_exhausted,))
    exhausted = OutboxPublisher(
        exhausted_store,
        FailingSink(retryable=True),
        max_attempts=3,
    ).publish_batch(owner="publisher-1", now=datetime.now(UTC))
    terminal_store = RecordingStore((nonretryable,))
    terminal = OutboxPublisher(
        terminal_store,
        FailingSink(retryable=False),
    ).publish_batch(owner="publisher-1", now=datetime.now(UTC))

    assert exhausted.failed == 1
    assert exhausted_store.released[-1][2] is True
    assert terminal.failed == 1
    assert terminal_store.released[-1][2] is True
