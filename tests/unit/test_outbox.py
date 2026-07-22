from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from careerops.application.outbox import (
    ClaimedOutboxEvent,
    InternalDeliveryError,
    OutboxEventType,
    OutboxPublisher,
    PendingOutboxEvent,
)
from careerops.infrastructure.database.outbox import PostgresOutboxRepository


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
        event_key_prefix: str | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        del owner, now, lease_for, limit
        self.claim_count += 1
        if event_key_prefix is None:
            return self.events
        return tuple(event for event in self.events if event.event_key.startswith(event_key_prefix))

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


class RecordingDatabaseConnection:
    def __init__(self) -> None:
        self.statement: sa.ClauseElement | None = None

    def in_transaction(self) -> bool:
        return True

    def execute(self, statement: sa.ClauseElement) -> None:
        self.statement = statement

    def scalar(self, statement: sa.ClauseElement) -> UUID:
        self.statement = statement
        return uuid4()


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


def test_event_key_prefix_filters_claimed_events_before_delivery() -> None:
    crawler = event()
    crawler = ClaimedOutboxEvent(
        event_id=crawler.event_id,
        event_key="crawler-execution:abc",
        action_intent_id=crawler.action_intent_id,
        payload_version_id=crawler.payload_version_id,
        event_type=crawler.event_type,
        available_at=crawler.available_at,
        attempt_count=crawler.attempt_count,
        lease_token=crawler.lease_token,
        lease_until=crawler.lease_until,
    )
    other = event()
    store = RecordingStore((other, crawler))
    sink = SuccessfulSink()

    result = OutboxPublisher(
        store,
        sink,
        event_key_prefix="crawler-execution:",
    ).publish_batch(owner="publisher-1", now=datetime.now(UTC))

    assert result.claimed == 1
    assert sink.delivered == [crawler.event_id]
    assert store.published == [(crawler.event_id, crawler.lease_token)]


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


def test_enqueue_uses_database_default_for_pending_status_under_api_role() -> None:
    connection = RecordingDatabaseConnection()
    repository = PostgresOutboxRepository(connection)  # type: ignore[arg-type]
    pending = PendingOutboxEvent(
        event_id=uuid4(),
        event_key=f"crawler-execution:{uuid4().hex}",
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        event_type=OutboxEventType.WORKFLOW_SIGNAL,
        available_at=datetime.now(UTC),
    )

    repository.enqueue_once(pending)

    assert connection.statement is not None
    sql = str(connection.statement.compile(dialect=postgresql.dialect()))
    assert "INSERT INTO careerops.outbox_events" in sql
    assert "status" not in sql
