"""Gmail receipt store: Protocol, domain model, and in-memory implementation.

The ``GmailSideEffectProvider`` uses a ``GmailReceiptStore`` to persist
reconciliation-keyed send receipts so that idempotency survives process
restarts.  The in-memory implementation is the default for tests; production
wires the Postgres implementation from
``infrastructure.database.gmail_receipt_postgres``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class GmailReceipt:
    """A single persisted gmail send receipt."""

    reconciliation_key: str
    provider_message_id: str
    thread_id: str
    sent_at: datetime


@runtime_checkable
class GmailReceiptStore(Protocol):
    """Protocol for gmail send receipt persistence.

    Implementations must be safe for single-process use (``RLock``-guarded).
    Multi-process safety is delegated to the database layer.
    """

    def get_by_reconciliation_key(self, key: str) -> GmailReceipt | None:
        """Return the receipt for *key*, or ``None`` if not found."""
        ...

    def put(self, receipt: GmailReceipt) -> None:
        """Store a receipt.  Overwrites any existing receipt with the same key."""
        ...

    def list(self) -> tuple[GmailReceipt, ...]:
        """Return all stored receipts, in insertion order."""
        ...


class InMemoryGmailReceiptStore:
    """Thread-safe in-memory receipt store (default for tests)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._receipts: dict[str, GmailReceipt] = {}

    def get_by_reconciliation_key(self, key: str) -> GmailReceipt | None:
        with self._lock:
            return self._receipts.get(key)

    def put(self, receipt: GmailReceipt) -> None:
        with self._lock:
            self._receipts[receipt.reconciliation_key] = receipt

    def list(self) -> tuple[GmailReceipt, ...]:
        with self._lock:
            return tuple(self._receipts.values())
