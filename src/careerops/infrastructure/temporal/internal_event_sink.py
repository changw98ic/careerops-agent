"""Logging-only ``InternalEventSink`` for outbox event delivery.

The kernel's ``execute()`` already called the provider before enqueuing the
outbox event, so this sink is an **audit/receipt notification layer** — not
the actual side effect.  v1 is logging-only; future versions can fan out to
webhooks, metrics, or a notification bus.
"""

from __future__ import annotations

import logging
from uuid import UUID

from careerops.application.outbox import (
    ClaimedOutboxEvent,
    InternalDeliveryError,
    OutboxEventType,
)
from careerops.application.side_effect_kernel import SideEffectStore

_log = logging.getLogger(__name__)


class LoggingInternalEventSink:
    """Idempotent, logging-only implementation of ``InternalEventSink``.

    Tracks delivered ``event_id`` values to guarantee idempotency.  For
    ``PROVIDER_WRITE`` events the kernel already executed the provider, so
    delivery is a no-op log entry.  Unknown event types emit a warning and
    raise ``InternalDeliveryError`` (terminal).
    """

    def __init__(self, store: SideEffectStore | None = None) -> None:
        self._store = store
        self._delivered: set[UUID] = set()

    def deliver(self, event: ClaimedOutboxEvent) -> None:
        """Deliver (log) an outbox event.  Idempotent on ``event.event_id``."""
        # -- idempotency gate --
        if event.event_id in self._delivered:
            _log.info(
                "outbox.duplicate event_id=%s event_key=%s",
                event.event_id,
                event.event_key,
            )
            return

        # -- validate event type --
        try:
            event_type = OutboxEventType(event.event_type)
        except ValueError:
            _log.warning(
                "outbox.unknown_type event_id=%s event_key=%s event_type=%s",
                event.event_id,
                event.event_key,
                event.event_type,
            )
            raise InternalDeliveryError("UNKNOWN_EVENT_TYPE", retryable=False) from None

        # -- dispatch --
        if event_type is OutboxEventType.PROVIDER_WRITE:
            self._deliver_provider_write(event)
        else:
            # WORKFLOW_SIGNAL / INTERNAL_NOTIFICATION — just log receipt.
            _log.info(
                "outbox.delivered event_id=%s event_key=%s event_type=%s",
                event.event_id,
                event.event_key,
                event.event_type,
            )

        self._delivered.add(event.event_id)

    # -- internals ----------------------------------------------------------

    def _deliver_provider_write(self, event: ClaimedOutboxEvent) -> None:
        """Dispatch a PROVIDER_WRITE event based on action_intent kind."""
        action_kind = self._lookup_action_kind(event)

        if action_kind == "send_email":
            # Kernel already executed the Gmail send — this is an audit receipt.
            _log.info(
                "outbox.provider_write.email event_id=%s event_key=%s intent=%s",
                event.event_id,
                event.event_key,
                event.action_intent_id,
            )
        else:
            _log.warning(
                "outbox.provider_write.unhandled event_id=%s event_key=%s intent=%s action_kind=%s",
                event.event_id,
                event.event_key,
                event.action_intent_id,
                action_kind,
            )

    def _lookup_action_kind(self, event: ClaimedOutboxEvent) -> str:
        """Resolve the ``action_kind`` from the event's action_intent.

        Returns ``"<unknown>"`` if no store is wired or the intent is missing.
        """
        store = self._store
        if store is None:
            return "<unknown>"
        try:
            intent = store.get_intent(event.action_intent_id)
        except Exception:
            _log.warning(
                "outbox.intent_lookup_failed event_id=%s intent_id=%s",
                event.event_id,
                event.action_intent_id,
            )
            return "<unknown>"
        return intent.action_kind
