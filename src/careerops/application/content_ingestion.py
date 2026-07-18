from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import BinaryIO, Protocol
from uuid import UUID

from careerops.application.ports.storage import (
    ContentClassification,
    ContentOwner,
    StorageDeleteResult,
    StoragePort,
    StoredBlob,
    StoredContent,
    VerifiedBlobStoragePort,
)


@dataclass(frozen=True, slots=True)
class ContentRegistration:
    object_id: UUID
    blob_id: UUID
    stored: StoredContent


class CatalogStore(Protocol):
    """Transactional metadata catalog paired with non-transactional byte storage."""

    def register(self, stored: StoredContent, object_id: UUID) -> ContentRegistration: ...

    def known_digests(self, digests: Collection[str]) -> frozenset[str]: ...

    def claim_orphan(
        self,
        stored: StoredBlob,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> OrphanClaimDecision: ...

    def recover_orphan_claims(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> tuple[OrphanDeletionClaim, ...]: ...

    def finalize_orphan_deletion(
        self,
        claim: OrphanDeletionClaim,
        *,
        now: datetime,
        result: StorageDeleteResult,
    ) -> None: ...


class ContentIngestionService:
    """Writes bytes, then registers their logical owner in one catalog transaction.

    PostgreSQL and the filesystem are not one atomic resource. If registration fails, the
    content-addressed bytes are deliberately left in place: they may already be shared by a
    committed owner. The grace-bounded orphan reconciler is the only cleanup path.
    """

    def __init__(self, storage: StoragePort, catalog: CatalogStore) -> None:
        self._storage = storage
        self._catalog = catalog

    def ingest(
        self,
        stream: BinaryIO,
        *,
        object_id: UUID,
        media_type: str,
        classification: ContentClassification,
        owner: ContentOwner,
        retention_until: datetime,
        expected_sha256: str | None = None,
    ) -> ContentRegistration:
        stored = self._storage.put(
            stream,
            media_type=media_type,
            classification=classification,
            owner=owner,
            retention_until=retention_until,
            expected_sha256=expected_sha256,
        )
        return self._catalog.register(stored, object_id)


@dataclass(frozen=True, slots=True)
class OrphanDeletion:
    blob: StoredBlob
    result: StorageDeleteResult


@dataclass(frozen=True, slots=True)
class OrphanDeletionClaim:
    blob_id: UUID
    lease_token: UUID
    stored: StoredBlob


class OrphanClaimStatus(StrEnum):
    CLAIMED = "claimed"
    REGISTERED = "registered"
    CATALOG_PROTECTED = "catalog_protected"
    DELETE_TOMBSTONED = "delete_tombstoned"


@dataclass(frozen=True, slots=True)
class OrphanClaimDecision:
    status: OrphanClaimStatus
    claim: OrphanDeletionClaim | None = None

    def __post_init__(self) -> None:
        if (self.status is OrphanClaimStatus.CLAIMED) != (self.claim is not None):
            raise ValueError("only a claimed orphan may carry a deletion claim")


@dataclass(frozen=True, slots=True)
class OrphanReconcileResult:
    scanned: int
    recovered_claims: int
    registered: int
    catalog_protected: int
    deletions: tuple[OrphanDeletion, ...]

    @property
    def deleted(self) -> int:
        return sum(item.result is StorageDeleteResult.DELETED for item in self.deletions)

    @property
    def already_missing(self) -> int:
        return sum(item.result is StorageDeleteResult.ALREADY_MISSING for item in self.deletions)


class OrphanReconciler:
    """Recovers write-before-register orphans without claiming cross-resource atomicity.

    The grace cutoff must be older than the maximum expected ingestion/register window. Before
    filesystem I/O, the catalog commits a ``deleting`` tombstone under the same per-digest
    advisory lock used by registration. Therefore a concurrent registration either commits
    first and protects the blob, or observes the tombstone and cannot create a live reference.
    Expired orphan leases are reclaimed before new filesystem candidates, covering a crash
    after claim commit and before delete/finalize.
    """

    def __init__(
        self,
        storage: VerifiedBlobStoragePort,
        catalog: CatalogStore,
        *,
        lease_for: timedelta = timedelta(minutes=5),
    ) -> None:
        if lease_for <= timedelta(0) or lease_for > timedelta(hours=1):
            raise ValueError("orphan deletion lease must be positive and at most one hour")
        self._storage = storage
        self._catalog = catalog
        self._lease_for = lease_for

    def reconcile(
        self,
        *,
        grace_cutoff: datetime,
        limit: int = 100,
        now: datetime | None = None,
    ) -> OrphanReconcileResult:
        current = now or datetime.now(UTC)
        self._require_aware(current, "now")
        self._require_aware(grace_cutoff, "grace_cutoff")
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        lease_until = current + self._lease_for
        recovered = self._catalog.recover_orphan_claims(
            now=current,
            lease_until=lease_until,
            limit=limit,
        )
        deletions = [self._delete_and_finalize(claim, now=current) for claim in recovered]
        remaining = limit - len(recovered)
        if remaining == 0:
            return OrphanReconcileResult(
                scanned=0,
                recovered_claims=len(recovered),
                registered=0,
                catalog_protected=0,
                deletions=tuple(deletions),
            )

        candidates = self._storage.enumerate_verified_blobs(
            older_than=grace_cutoff,
            limit=remaining,
        )
        digests = tuple(blob.sha256 for blob in candidates)
        known = self._catalog.known_digests(digests)
        registered = 0
        catalog_protected = 0

        for blob in candidates:
            if blob.sha256 in known:
                registered += 1
                continue
            decision = self._catalog.claim_orphan(
                blob,
                now=current,
                lease_until=lease_until,
            )
            if decision.status is OrphanClaimStatus.REGISTERED:
                registered += 1
                continue
            if decision.status is OrphanClaimStatus.CATALOG_PROTECTED:
                catalog_protected += 1
                continue
            if decision.status is OrphanClaimStatus.DELETE_TOMBSTONED:
                result = self._storage.delete_bytes(blob)
                deletions.append(OrphanDeletion(blob=blob, result=result))
                continue
            claim = decision.claim
            if claim is None:
                raise RuntimeError("claimed orphan is missing its deletion claim")
            deletions.append(self._delete_and_finalize(claim, now=current))

        return OrphanReconcileResult(
            scanned=len(candidates),
            recovered_claims=len(recovered),
            registered=registered,
            catalog_protected=catalog_protected,
            deletions=tuple(deletions),
        )

    def _delete_and_finalize(
        self,
        claim: OrphanDeletionClaim,
        *,
        now: datetime,
    ) -> OrphanDeletion:
        result = self._storage.delete_bytes(claim.stored)
        self._catalog.finalize_orphan_deletion(
            claim,
            now=now,
            result=result,
        )
        return OrphanDeletion(blob=claim.stored, result=result)

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
