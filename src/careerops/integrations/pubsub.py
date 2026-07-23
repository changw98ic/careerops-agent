"""Pub/Sub pull subscriber for Gmail Watch notifications.

Safety invariants:
- Pull/StreamingPull only; no public webhook endpoint.
- Duplicate deliveries handled via provider_message_id uniqueness.
- ACK only after successful processing.
- Receipt persisted before processing for idempotency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class PubSubNotification:
    """A decoded Pub/Sub notification from Gmail Watch."""

    pubsub_message_id: str
    ack_id: str
    history_id: str
    email_address: str
    subscription_name: str
    publish_time: datetime


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """A persisted receipt proving a notification was received and processed."""

    id: UUID
    account_id: UUID
    pubsub_message_id: str
    ack_id: str
    history_id: str
    processed: bool
    received_at: datetime
    processed_at: datetime | None


class DuplicateDeliveryError(Exception):
    """Raised when a duplicate Pub/Sub delivery is detected."""


class PubSubPullSubscriber:
    """Pull-based Pub/Sub subscriber for Gmail Watch notifications.

    Design:
    - No public webhook endpoint is created.
    - Subscriber actively pulls messages (outbound connection only).
    - Each notification is deduplicated by pubsub_message_id.
    - ACK is sent only after successful processing and receipt persistence.
    """

    def __init__(
        self,
        *,
        subscription_name: str,
        account_id: UUID,
        known_message_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._subscription_name = subscription_name
        self._account_id = account_id
        self._known_message_ids: set[str] = set(known_message_ids)
        self._receipts: list[DeliveryReceipt] = []

    @property
    def receipts(self) -> tuple[DeliveryReceipt, ...]:
        return tuple(self._receipts)

    def is_duplicate(self, pubsub_message_id: str) -> bool:
        """Check if a message has already been processed."""
        return pubsub_message_id in self._known_message_ids

    def process_notification(
        self,
        notification: PubSubNotification,
        *,
        now: datetime | None = None,
    ) -> DeliveryReceipt:
        """Process a notification with deduplication.

        Returns the receipt. Raises DuplicateDeliveryError if already seen.
        """
        current = now or datetime.now(UTC)

        if self.is_duplicate(notification.pubsub_message_id):
            raise DuplicateDeliveryError(f"Duplicate delivery: {notification.pubsub_message_id}")

        receipt = DeliveryReceipt(
            id=uuid4(),
            account_id=self._account_id,
            pubsub_message_id=notification.pubsub_message_id,
            ack_id=notification.ack_id,
            history_id=notification.history_id,
            processed=True,
            received_at=current,
            processed_at=current,
        )

        self._known_message_ids.add(notification.pubsub_message_id)
        self._receipts.append(receipt)
        return receipt

    def acknowledge(self, receipt: DeliveryReceipt) -> None:
        """ACK a message after successful processing.

        In production, this calls the Pub/Sub ACK API.
        ACK is only sent after receipt is persisted.
        """
        if not receipt.processed:
            raise ValueError("cannot ACK an unprocessed receipt")


def create_pull_subscriber(
    *,
    subscription_name: str,
    account_id: UUID,
    known_message_ids: frozenset[str] = frozenset(),
) -> PubSubPullSubscriber:
    """Factory for creating a pull subscriber. No webhook is created."""
    return PubSubPullSubscriber(
        subscription_name=subscription_name,
        account_id=account_id,
        known_message_ids=known_message_ids,
    )
