"""Notification service: SSE channel + outbox-style persist + push.

Phase 9 tasks 9.1-9.2.  Provides:
- ``SSEChannel``: in-memory per-user pub/sub for Server-Sent Events.
- ``NotificationService``: outbox-style persistence + SSE push.
- Convenience helpers for crawl/agent progress notifications.

Storage is in-memory (single-user self-hosted).  No external broker.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

__all__ = [
    "NotificationEvent",
    "NotificationService",
    "SSEChannel",
]

_log = logging.getLogger(__name__)


@dataclass
class NotificationEvent:
    """One notification persisted in the outbox."""

    id: UUID
    user_id: str
    event_type: str  # "reminder", "crawl_progress", "agent_progress", "system"
    payload: dict[str, object]
    created_at: datetime
    delivered: bool = False


class SSEChannel:
    """In-memory per-user pub/sub using ``asyncio.Queue``.

    Each subscriber gets its own ``asyncio.Queue``.  ``publish`` pushes
    the event to every queue for the target user.  ``subscribe`` is an
    async generator that yields SSE-formatted strings until the client
    disconnects.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[str | None]]] = {}

    async def subscribe(self, user_id: str) -> AsyncGenerator[str, None]:
        """Yield SSE-formatted strings for *user_id*.

        The generator runs until the caller cancels it (client disconnect).
        A ``None`` sentinel on the queue signals shutdown.
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._subscribers.setdefault(user_id, []).append(queue)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            subs = self._subscribers.get(user_id, [])
            if queue in subs:
                subs.remove(queue)
            if not subs:
                self._subscribers.pop(user_id, None)

    def publish(self, user_id: str, event: NotificationEvent) -> None:
        """Push *event* to all current subscribers of *user_id*.

        Non-blocking: subscribers that are not connected are silently skipped.
        """
        sse_data = _format_sse(event)
        for queue in self._subscribers.get(user_id, []):
            try:
                queue.put_nowait(sse_data)
            except asyncio.QueueFull:  # pragma: no cover - unbounded queue
                _log.warning("SSE queue full for user %s, dropping event", user_id)

    def shutdown(self, user_id: str) -> None:
        """Send sentinel to all subscribers of *user_id* (graceful close)."""
        for queue in self._subscribers.pop(user_id, []):
            with suppress(asyncio.QueueFull):
                queue.put_nowait(None)


def _format_sse(event: NotificationEvent) -> str:
    """Format a notification as an SSE text block."""
    payload = {
        "id": str(event.id),
        "type": event.event_type,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    }
    return f"event: {event.event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class NotificationRepository(Protocol):
    def save(self, event: NotificationEvent) -> NotificationEvent: ...

    def list_pending(self, user_id: str) -> list[NotificationEvent]: ...

    def mark_delivered(self, user_id: str, event_id: UUID) -> None: ...


class NotificationService:
    """Outbox-style notification service with SSE push.

    Persists events in an in-memory outbox keyed by ``user_id`` and
    pushes each event to the ``SSEChannel`` for real-time delivery.
    Clients can recover missed events via ``get_pending``.
    """

    def __init__(
        self,
        sse_channel: SSEChannel,
        repository: NotificationRepository | None = None,
    ) -> None:
        self._channel = sse_channel
        self._outbox: dict[str, list[NotificationEvent]] = {}
        self._repository = repository

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def notify(
        self,
        user_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> NotificationEvent:
        """Persist to outbox, then push via SSE.  Returns the event."""
        event = NotificationEvent(
            id=uuid4(),
            user_id=user_id,
            event_type=event_type,
            payload=payload,
            created_at=datetime.now(UTC),
        )
        if self._repository is None:
            self._outbox.setdefault(user_id, []).append(event)
        else:
            event = self._repository.save(event)
        self._channel.publish(user_id, event)
        return event

    def get_pending(self, user_id: str) -> list[NotificationEvent]:
        """Return undelivered events for *user_id* (recovery)."""
        if self._repository is not None:
            return self._repository.list_pending(user_id)
        return [e for e in self._outbox.get(user_id, []) if not e.delivered]

    def mark_delivered(self, user_id: str, event_id: UUID) -> None:
        """Acknowledge delivery of *event_id* for *user_id*."""
        if self._repository is not None:
            self._repository.mark_delivered(user_id, event_id)
            return
        for event in self._outbox.get(user_id, []):
            if event.id == event_id:
                event.delivered = True
                return
    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def notify_crawl_progress(
        self,
        user_id: str,
        crawl_run_id: str | UUID,
        phase: str,
        postings_count: int = 0,
        *,
        source_name: str = "",
        message: str = "",
    ) -> NotificationEvent:
        """Emit a crawl-progress notification."""
        return self.notify(user_id, "crawl_progress", {
            "crawl_run_id": str(crawl_run_id),
            "phase": phase,
            "postings_count": postings_count,
            "source_name": source_name,
            "message": message,
        })

    def notify_agent_progress(
        self,
        user_id: str,
        agent_run_id: str | UUID,
        phase: str,
        action_count: int = 0,
        *,
        message: str = "",
    ) -> NotificationEvent:
        """Emit an agent-progress notification."""
        return self.notify(user_id, "agent_progress", {
            "agent_run_id": str(agent_run_id),
            "phase": phase,
            "action_count": action_count,
            "message": message,
        })
