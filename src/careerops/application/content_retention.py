from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from careerops.application.ports.storage import (
    StorageDeleteResult,
    StoragePort,
    StoredBlob,
)


@dataclass(frozen=True, slots=True)
class ContentDeletionCandidate:
    blob_id: UUID
    lease_token: UUID
    lease_until: datetime
    stored: StoredBlob


@dataclass(frozen=True, slots=True)
class ContentPurgeResult:
    blob_id: UUID
    delete_result: StorageDeleteResult


class ContentRetentionRepository(Protocol):
    """Every method must commit its own database transaction before returning."""

    def mark_due_expired(self, *, now: datetime) -> int: ...

    def claim_deletable(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
    ) -> ContentDeletionCandidate | None: ...

    def finalize_deletion(
        self,
        blob_id: UUID,
        *,
        lease_token: UUID,
        now: datetime,
        delete_result: StorageDeleteResult,
    ) -> None: ...


class ContentRetentionService:
    """Fences physical deletion with two committed database transitions.

    A claim is committed before unlinking bytes. Finalization is a second transaction,
    so a crash after unlink leaves a stale lease that can be reclaimed with a new token.
    The idempotent storage result is persisted rather than treating missing bytes as
    an implicit success.
    """

    def __init__(
        self,
        repository: ContentRetentionRepository,
        storage: StoragePort,
        *,
        owner: str = "content-retention",
        lease_for: timedelta = timedelta(minutes=5),
    ) -> None:
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        self._repository = repository
        self._storage = storage
        self._owner = owner
        self._lease_for = lease_for

    def mark_due_expired(self, *, now: datetime) -> int:
        return self._repository.mark_due_expired(now=now)

    def purge_next(self, *, now: datetime) -> ContentPurgeResult | None:
        candidate = self._repository.claim_deletable(
            owner=self._owner,
            now=now,
            lease_for=self._lease_for,
        )
        if candidate is None:
            return None
        delete_result = self._storage.delete_bytes(candidate.stored)
        self._repository.finalize_deletion(
            candidate.blob_id,
            lease_token=candidate.lease_token,
            now=now,
            delete_result=delete_result,
        )
        return ContentPurgeResult(
            blob_id=candidate.blob_id,
            delete_result=delete_result,
        )
