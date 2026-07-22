from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class OutboxEventType(StrEnum):
    WORKFLOW_SIGNAL = "workflow_signal"
    INTERNAL_NOTIFICATION = "internal_notification"


@dataclass(frozen=True, slots=True)
class PendingOutboxEvent:
    event_id: UUID
    event_key: str
    action_intent_id: UUID
    payload_version_id: UUID
    event_type: OutboxEventType
    available_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    event_id: UUID
    event_key: str
    action_intent_id: UUID
    payload_version_id: UUID
    event_type: str
    available_at: datetime
    attempt_count: int
    lease_token: UUID
    lease_until: datetime
    lease_owner: str = ""


class OutboxStore(Protocol):
    """Every method is an independently committed database transaction."""

    def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
        event_key_prefix: str | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]: ...

    def mark_published(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
    ) -> None: ...

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
    ) -> None: ...


class InternalEventSink(Protocol):
    """Internal delivery must be idempotent on ``event.event_id``."""

    def deliver(self, event: ClaimedOutboxEvent) -> None: ...


class InternalDeliveryError(RuntimeError):
    def __init__(self, error_code: str, *, retryable: bool) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class PublishBatchResult:
    claimed: int
    published: int
    deferred: int
    failed: int


class OutboxPublisher:
    """Lease-based internal publisher with a disabled-by-default delivery capability."""

    def __init__(
        self,
        store: OutboxStore,
        sink: InternalEventSink | None = None,
        *,
        retry_delay: timedelta = timedelta(minutes=5),
        max_attempts: int = 10,
        event_key_prefix: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if retry_delay <= timedelta(0):
            raise ValueError("retry_delay must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if event_key_prefix == "":
            raise ValueError("event_key_prefix must be non-empty when provided")
        self._store = store
        self._sink = sink
        self._retry_delay = retry_delay
        self._max_attempts = max_attempts
        self._event_key_prefix = event_key_prefix
        self._clock = clock or _utc_now

    def publish_batch(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta = timedelta(seconds=30),
        limit: int = 25,
    ) -> PublishBatchResult:
        sink = self._sink
        if sink is None:
            return PublishBatchResult(claimed=0, published=0, deferred=0, failed=0)
        events = self._store.claim(
            owner=owner,
            now=now,
            lease_for=lease_for,
            limit=limit,
            event_key_prefix=self._event_key_prefix,
        )
        published = 0
        deferred = 0
        failed = 0
        for event in events:
            try:
                OutboxEventType(event.event_type)
            except ValueError:
                completed_at = self._clock()
                self._store.release(
                    event.event_id,
                    owner=owner,
                    lease_token=event.lease_token,
                    now=completed_at,
                    retry_at=completed_at + self._retry_delay,
                    error_code="UNKNOWN_EVENT_TYPE",
                    terminal=True,
                )
                failed += 1
                continue
            try:
                sink.deliver(event)
            except InternalDeliveryError as error:
                terminal = not error.retryable or event.attempt_count >= self._max_attempts
                completed_at = self._clock()
                self._store.release(
                    event.event_id,
                    owner=owner,
                    lease_token=event.lease_token,
                    now=completed_at,
                    retry_at=completed_at + self._retry_delay,
                    error_code=error.error_code,
                    terminal=terminal,
                )
                if terminal:
                    failed += 1
                else:
                    deferred += 1
            else:
                self._store.mark_published(
                    event.event_id,
                    owner=owner,
                    lease_token=event.lease_token,
                    now=self._clock(),
                )
                published += 1
        return PublishBatchResult(
            claimed=len(events),
            published=published,
            deferred=deferred,
            failed=failed,
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)
