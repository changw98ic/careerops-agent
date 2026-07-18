from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from careerops.application.content_ingestion import (
    ContentIngestionService,
    ContentRegistration,
    OrphanClaimDecision,
    OrphanClaimStatus,
    OrphanDeletionClaim,
    OrphanReconciler,
)
from careerops.application.ports.storage import (
    ContentClassification,
    ContentOwner,
    StorageDeleteResult,
    StoredBlob,
    StoredContent,
)
from careerops.infrastructure.storage.local import LocalContentAddressedStorage


class SimulatedDatabaseFailure(RuntimeError):
    pass


class FakeCatalog:
    def __init__(
        self,
        *,
        fail_registration: bool = False,
        before_lookup: Callable[[int], None] | None = None,
        before_claim: Callable[[StoredBlob], None] | None = None,
    ) -> None:
        self.fail_registration = fail_registration
        self.before_lookup = before_lookup
        self.before_claim = before_claim
        self.blob_ids: dict[str, UUID] = {}
        self.objects: dict[UUID, ContentRegistration] = {}
        self.orphan_claims: dict[str, tuple[OrphanDeletionClaim, datetime]] = {}
        self.deleted_digests: set[str] = set()
        self.finalized_results: list[StorageDeleteResult] = []
        self.lookup_count = 0

    def register(self, stored: StoredContent, object_id: UUID) -> ContentRegistration:
        if self.fail_registration:
            raise SimulatedDatabaseFailure("simulated transaction rollback")
        if stored.sha256 in self.orphan_claims or stored.sha256 in self.deleted_digests:
            raise SimulatedDatabaseFailure("digest is fenced by an orphan tombstone")
        blob_id = self.blob_ids.setdefault(stored.sha256, uuid4())
        registration = ContentRegistration(
            object_id=object_id,
            blob_id=blob_id,
            stored=stored,
        )
        self.objects[object_id] = registration
        return registration

    def known_digests(self, digests: Collection[str]) -> frozenset[str]:
        self.lookup_count += 1
        if self.before_lookup is not None:
            self.before_lookup(self.lookup_count)
        return frozenset(digest for digest in digests if digest in self.blob_ids)

    def claim_orphan(
        self,
        stored: StoredBlob,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> OrphanClaimDecision:
        del now
        if self.before_claim is not None:
            self.before_claim(stored)
        if stored.sha256 in self.blob_ids:
            return OrphanClaimDecision(status=OrphanClaimStatus.REGISTERED)
        if stored.sha256 in self.deleted_digests:
            return OrphanClaimDecision(status=OrphanClaimStatus.DELETE_TOMBSTONED)
        if stored.sha256 in self.orphan_claims:
            return OrphanClaimDecision(status=OrphanClaimStatus.CATALOG_PROTECTED)
        claim = OrphanDeletionClaim(
            blob_id=uuid4(),
            lease_token=uuid4(),
            stored=stored,
        )
        self.orphan_claims[stored.sha256] = (claim, lease_until)
        return OrphanClaimDecision(status=OrphanClaimStatus.CLAIMED, claim=claim)

    def recover_orphan_claims(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> tuple[OrphanDeletionClaim, ...]:
        recovered: list[OrphanDeletionClaim] = []
        for digest, (claim, current_lease_until) in tuple(self.orphan_claims.items()):
            if len(recovered) == limit:
                break
            if current_lease_until > now:
                continue
            renewed = OrphanDeletionClaim(
                blob_id=claim.blob_id,
                lease_token=uuid4(),
                stored=claim.stored,
            )
            self.orphan_claims[digest] = (renewed, lease_until)
            recovered.append(renewed)
        return tuple(recovered)

    def finalize_orphan_deletion(
        self,
        claim: OrphanDeletionClaim,
        *,
        now: datetime,
        result: StorageDeleteResult,
    ) -> None:
        del now
        current, _ = self.orphan_claims[claim.stored.sha256]
        if current.lease_token != claim.lease_token:
            raise RuntimeError("stale fake orphan lease")
        del self.orphan_claims[claim.stored.sha256]
        self.deleted_digests.add(claim.stored.sha256)
        self.finalized_results.append(result)


def make_storage(tmp_path: Path) -> tuple[Path, LocalContentAddressedStorage]:
    root = tmp_path / "objects"
    return root, LocalContentAddressedStorage(root, max_object_bytes=1024)


def object_path(root: Path, object_key: str) -> Path:
    return root.joinpath(*object_key.split("/"))


def ingest(
    service: ContentIngestionService,
    payload: bytes,
    *,
    object_id: UUID | None = None,
) -> ContentRegistration:
    return service.ingest(
        io.BytesIO(payload),
        object_id=object_id or uuid4(),
        media_type="text/plain",
        classification=ContentClassification.RAW_WEBPAGE,
        owner=ContentOwner(resource_type="job_posting", resource_id=uuid4()),
        retention_until=datetime.now(UTC) + timedelta(days=1),
    )


def mark_older_than_grace(path: Path, cutoff: datetime) -> None:
    old = cutoff - timedelta(hours=1)
    os.utime(path, (old.timestamp(), old.timestamp()))


def orphan_descriptor(payload: bytes) -> StoredContent:
    digest = hashlib.sha256(payload).hexdigest()
    return StoredContent(
        object_key=LocalContentAddressedStorage.object_key_for_digest(digest),
        sha256=digest,
        byte_size=len(payload),
        media_type="text/plain",
        classification=ContentClassification.RAW_WEBPAGE,
        owner=ContentOwner(resource_type="job_posting", resource_id=uuid4()),
        retention_until=datetime.now(UTC) + timedelta(days=1),
    )


def test_database_rollback_leaves_bytes_for_eventual_orphan_recovery(
    tmp_path: Path,
) -> None:
    root, storage = make_storage(tmp_path)
    catalog = FakeCatalog(fail_registration=True)
    service = ContentIngestionService(storage, catalog)
    payload = b"write succeeds before the database rolls back"
    orphan = orphan_descriptor(payload)

    with pytest.raises(SimulatedDatabaseFailure, match="transaction rollback"):
        ingest(service, payload)

    path = object_path(root, orphan.object_key)
    assert path.read_bytes() == payload

    cutoff = datetime.now(UTC) - timedelta(hours=1)
    mark_older_than_grace(path, cutoff)
    result = OrphanReconciler(storage, FakeCatalog()).reconcile(
        grace_cutoff=cutoff,
        limit=10,
    )

    assert result.scanned == 1
    assert result.registered == 0
    assert result.deleted == 1
    assert result.already_missing == 0
    assert result.deletions[0].result is StorageDeleteResult.DELETED
    assert not path.exists()


def test_registered_shared_blob_is_retained(tmp_path: Path) -> None:
    root, storage = make_storage(tmp_path)
    catalog = FakeCatalog()
    service = ContentIngestionService(storage, catalog)
    payload = b"shared content-addressed bytes"

    first = ingest(service, payload)
    second = ingest(service, payload)
    assert first.blob_id == second.blob_id

    path = object_path(root, first.stored.object_key)
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    mark_older_than_grace(path, cutoff)
    result = OrphanReconciler(storage, catalog).reconcile(
        grace_cutoff=cutoff,
        limit=10,
    )

    assert result.scanned == 1
    assert result.registered == 1
    assert result.deletions == ()
    assert storage.read_bytes(first.stored) == payload


def test_orphan_newer_than_grace_cutoff_is_untouched(tmp_path: Path) -> None:
    root, storage = make_storage(tmp_path)
    catalog = FakeCatalog(fail_registration=True)
    service = ContentIngestionService(storage, catalog)
    payload = b"still inside the ingestion grace window"
    orphan = orphan_descriptor(payload)

    with pytest.raises(SimulatedDatabaseFailure):
        ingest(service, payload)

    path = object_path(root, orphan.object_key)
    result = OrphanReconciler(storage, FakeCatalog()).reconcile(
        grace_cutoff=datetime.now(UTC) - timedelta(hours=1),
        limit=10,
    )

    assert result.scanned == 0
    assert result.deletions == ()
    assert path.read_bytes() == payload


def test_reconciler_reports_already_missing_idempotently(tmp_path: Path) -> None:
    root, storage = make_storage(tmp_path)
    payload = b"concurrent orphan cleanup"
    stored = storage.put(
        io.BytesIO(payload),
        media_type="text/plain",
        classification=ContentClassification.RAW_WEBPAGE,
        owner=ContentOwner(resource_type="job_posting", resource_id=uuid4()),
        retention_until=datetime.now(UTC) + timedelta(days=1),
    )
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    mark_older_than_grace(object_path(root, stored.object_key), cutoff)

    def remove_during_claim(_candidate: StoredBlob) -> None:
        assert storage.delete_bytes(stored) is StorageDeleteResult.DELETED

    result = OrphanReconciler(
        storage,
        FakeCatalog(before_claim=remove_during_claim),
    ).reconcile(grace_cutoff=cutoff, limit=10)

    assert result.scanned == 1
    assert result.deleted == 0
    assert result.already_missing == 1
    assert result.deletions[0].result is StorageDeleteResult.ALREADY_MISSING


def test_registration_winning_after_batch_lookup_prevents_orphan_delete(
    tmp_path: Path,
) -> None:
    root, storage = make_storage(tmp_path)
    payload = b"old orphan adopted by a concurrent registration"
    stored = storage.put(
        io.BytesIO(payload),
        media_type="text/plain",
        classification=ContentClassification.RAW_WEBPAGE,
        owner=ContentOwner(resource_type="job_posting", resource_id=uuid4()),
        retention_until=datetime.now(UTC) + timedelta(days=1),
    )
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    path = object_path(root, stored.object_key)
    mark_older_than_grace(path, cutoff)
    catalog = FakeCatalog()

    def register_before_atomic_claim(candidate: StoredBlob) -> None:
        catalog.blob_ids[candidate.sha256] = uuid4()

    catalog.before_claim = register_before_atomic_claim
    result = OrphanReconciler(storage, catalog).reconcile(
        grace_cutoff=cutoff,
        limit=10,
    )

    assert result.scanned == 1
    assert result.registered == 1
    assert result.deletions == ()
    assert path.read_bytes() == payload


def test_committed_orphan_claim_is_recovered_after_lease_expiry(
    tmp_path: Path,
) -> None:
    root, storage = make_storage(tmp_path)
    payload = b"crash after claim commit but before filesystem delete"
    stored = storage.put(
        io.BytesIO(payload),
        media_type="text/plain",
        classification=ContentClassification.RAW_WEBPAGE,
        owner=ContentOwner(resource_type="job_posting", resource_id=uuid4()),
        retention_until=datetime.now(UTC) + timedelta(days=1),
    )
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    path = object_path(root, stored.object_key)
    mark_older_than_grace(path, cutoff)
    catalog = FakeCatalog()
    claimed_at = datetime.now(UTC)
    decision = catalog.claim_orphan(
        stored,
        now=claimed_at,
        lease_until=claimed_at + timedelta(minutes=5),
    )
    assert decision.status is OrphanClaimStatus.CLAIMED

    result = OrphanReconciler(storage, catalog).reconcile(
        grace_cutoff=cutoff,
        limit=10,
        now=claimed_at + timedelta(minutes=6),
    )

    assert result.recovered_claims == 1
    assert result.scanned == 0
    assert result.deleted == 1
    assert catalog.finalized_results == [StorageDeleteResult.DELETED]
    assert not path.exists()


def test_deleted_tombstone_is_not_treated_as_live_registration(
    tmp_path: Path,
) -> None:
    root, storage = make_storage(tmp_path)
    payload = b"bytes that reappeared behind a deleted tombstone"
    stored = storage.put(
        io.BytesIO(payload),
        media_type="text/plain",
        classification=ContentClassification.RAW_WEBPAGE,
        owner=ContentOwner(resource_type="job_posting", resource_id=uuid4()),
        retention_until=datetime.now(UTC) + timedelta(days=1),
    )
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    path = object_path(root, stored.object_key)
    mark_older_than_grace(path, cutoff)
    catalog = FakeCatalog()
    catalog.deleted_digests.add(stored.sha256)

    result = OrphanReconciler(storage, catalog).reconcile(
        grace_cutoff=cutoff,
        limit=10,
    )

    assert result.registered == 0
    assert result.deleted == 1
    assert not path.exists()
