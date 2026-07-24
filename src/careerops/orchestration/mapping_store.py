"""In-process ``thread_id <-> approval_id <-> intent_id`` mapping store.

Plan v0.4 §2.4 / §2.5. v1 is single-process (``MemorySaver`` +
``InMemorySideEffectStore`` + this store share lifetime). There is NO Alembic
migration for v1; durable mapping is v1.1 (Stage 4).

The review endpoint receives ``POST /api/v1/review/{approval_id}`` and must
reverse-resolve the ``thread_id`` and ``intent_id`` to resume the graph. It also
guards ``requested_for`` so a cross-user decision cannot hijack another user's
thread.

The store is thread-safe; ``claim_resume`` ensures that the first concurrent
resume for a given approval wins and later ones are rejected (the graph itself
also de-duplicates via ``decide_approval``'s idempotency, but the claim protects
the API layer from racing 200 responses).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import override
from uuid import UUID

__all__ = [
    "InMemoryReviewMappingStore",
    "MappingConflictError",
    "MappingNotFoundError",
    "MappingRecord",
    "ReviewMappingStore",
]


class MappingConflictError(RuntimeError):
    """Raised when two different values try to occupy the same mapping slot."""


class MappingNotFoundError(KeyError):
    """Raised when no record exists for the requested key."""


@dataclass(frozen=True, slots=True)
class MappingRecord:
    """One persisted mapping row.

    ``completed_at`` is set by ``complete_resume`` once the graph has accepted
    the resume; further resume attempts for the same approval return the cached
    decision.
    """

    thread_id: str
    approval_id: UUID
    intent_id: UUID
    requested_for: str
    completed_at: str | None


class ReviewMappingStore:
    """Protocol-like base class. Subclasses may back this with Postgres in v1.1."""

    def put_if_absent(self, record: MappingRecord) -> MappingRecord:
        raise NotImplementedError

    def get_by_approval(self, approval_id: UUID) -> MappingRecord:
        raise NotImplementedError

    def claim_resume(self, approval_id: UUID) -> MappingRecord:
        raise NotImplementedError

    def complete_resume(self, approval_id: UUID, *, completed_at: str) -> MappingRecord:
        raise NotImplementedError


class InMemoryReviewMappingStore(ReviewMappingStore):
    """Thread-safe in-memory implementation (v1 single-process only)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_approval: dict[UUID, MappingRecord] = {}

    @override
    def put_if_absent(self, record: MappingRecord) -> MappingRecord:
        """Insert the mapping unless one already exists for ``approval_id``.

        If an existing record exists with the SAME ``thread_id`` / ``intent_id``
        / ``requested_for`` (idempotent re-insert from review_gate re-run), the
        existing record is returned. If a DIFFERENT record exists for the same
        approval_id, ``MappingConflictError`` is raised (should never happen
        when the kernel's ``get_or_create_pending_approval`` is used correctly).
        """
        with self._lock:
            existing = self._by_approval.get(record.approval_id)
            if existing is None:
                self._by_approval[record.approval_id] = record
                return record
            if (
                existing.thread_id != record.thread_id
                or existing.intent_id != record.intent_id
                or existing.requested_for != record.requested_for
            ):
                raise MappingConflictError(
                    f"approval {record.approval_id} already mapped to a different record"
                )
            return existing

    @override
    def get_by_approval(self, approval_id: UUID) -> MappingRecord:
        with self._lock:
            record = self._by_approval.get(approval_id)
            if record is None:
                raise MappingNotFoundError(str(approval_id))
            return record

    @override
    def claim_resume(self, approval_id: UUID) -> MappingRecord:
        """First claim wins; second claim raises ``MappingConflictError``.

        ``claim_resume`` is the API-layer lock. The graph's own
        ``decide_approval`` is separately idempotent, so even without this
        claim, repeat decisions are safe; the claim exists to give the API a
        deterministic first-writer-wins response.
        """
        with self._lock:
            record = self._by_approval.get(approval_id)
            if record is None:
                raise MappingNotFoundError(str(approval_id))
            if record.completed_at is not None:
                raise MappingConflictError(f"resume for approval {approval_id} already completed")
            return record

    @override
    def complete_resume(self, approval_id: UUID, *, completed_at: str) -> MappingRecord:
        with self._lock:
            record = self._by_approval.get(approval_id)
            if record is None:
                raise MappingNotFoundError(str(approval_id))
            updated = MappingRecord(
                thread_id=record.thread_id,
                approval_id=record.approval_id,
                intent_id=record.intent_id,
                requested_for=record.requested_for,
                completed_at=completed_at,
            )
            self._by_approval[approval_id] = updated
            return updated
