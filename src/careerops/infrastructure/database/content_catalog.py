from __future__ import annotations

import hashlib
import re
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, Engine, RowMapping

from careerops.application.content_ingestion import (
    ContentRegistration,
    OrphanClaimDecision,
    OrphanClaimStatus,
    OrphanDeletionClaim,
)
from careerops.application.ports.storage import (
    StorageDeleteResult,
    StoredBlob,
    StoredContent,
)
from careerops.infrastructure.database.schema import content_blobs, content_objects

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_DIGEST_QUERY = 1_000
_MAX_ORPHAN_BATCH = 1_000
_ORPHAN_LEASE_OWNER = "write-orphan-reconciler"


class ContentCatalogError(RuntimeError):
    pass


class BlobLifecycleConflict(ContentCatalogError):
    pass


class CatalogIntegrityError(ContentCatalogError):
    pass


class LogicalObjectConflict(ContentCatalogError):
    pass


class OrphanClaimStateError(ContentCatalogError):
    pass


class OrphanCleanupCapabilityMissing(ContentCatalogError):
    pass


class PostgresContentCatalogRepository:
    """Catalog operations scoped to one caller-owned PostgreSQL transaction."""

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError("content catalog operations require an explicit transaction")
        self._connection = connection

    def register(self, stored: StoredContent, object_id: UUID) -> ContentRegistration:
        self._validate_stored(stored)
        # The digest lock is shared with orphan claims. Whichever transaction commits first
        # determines whether this digest is active or permanently fenced for deletion.
        self._lock_digest(stored.sha256)
        self._lock_logical_object(object_id)

        proposed_blob_id = uuid4()
        self._connection.execute(
            postgresql.insert(content_blobs)
            .values(
                id=proposed_blob_id,
                sha256=stored.sha256,
                object_key=stored.object_key,
                byte_size=stored.byte_size,
            )
            .on_conflict_do_nothing(index_elements=[content_blobs.c.sha256])
        )
        blob_row = self._select_blob(stored.sha256)
        if blob_row is None:
            raise CatalogIntegrityError("content blob insert did not produce a catalog row")
        blob_id = self._validate_active_blob(blob_row, stored)

        logical_row = (
            self._connection.execute(
                sa.select(
                    content_objects.c.blob_id,
                    content_objects.c.media_type,
                    content_objects.c.classification,
                    content_objects.c.owner_resource_type,
                    content_objects.c.owner_resource_id,
                    content_objects.c.retention_until,
                    content_objects.c.expired_at,
                    content_objects.c.retired_at,
                ).where(content_objects.c.id == object_id)
            )
            .mappings()
            .first()
        )
        if logical_row is None:
            self._connection.execute(
                sa.insert(content_objects).values(
                    id=object_id,
                    blob_id=blob_id,
                    media_type=stored.media_type,
                    classification=stored.classification.value,
                    owner_resource_type=stored.owner.resource_type,
                    owner_resource_id=stored.owner.resource_id,
                    retention_until=stored.retention_until,
                )
            )
        else:
            self._validate_existing_logical_object(logical_row, blob_id, stored)

        return ContentRegistration(object_id=object_id, blob_id=blob_id, stored=stored)

    def claim_orphan(
        self,
        stored: StoredBlob,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> OrphanClaimDecision:
        """Atomically fence an unknown physical blob before filesystem deletion."""

        self._validate_blob_descriptor(stored)
        self._validate_lease(now=now, lease_until=lease_until)
        self._lock_digest(stored.sha256)

        existing = self._select_blob(stored.sha256)
        if existing is not None:
            return self._decision_for_existing_blob(existing, stored)

        blob_id = uuid4()
        lease_token = uuid4()
        inserted_id = self._connection.scalar(
            postgresql.insert(content_blobs)
            .values(
                id=blob_id,
                sha256=stored.sha256,
                object_key=stored.object_key,
                byte_size=stored.byte_size,
                deletion_state="deleting",
                delete_lease_owner=_ORPHAN_LEASE_OWNER,
                delete_lease_token=lease_token,
                delete_lease_until=lease_until,
            )
            .on_conflict_do_nothing(index_elements=[content_blobs.c.sha256])
            .returning(content_blobs.c.id)
        )
        if inserted_id is None:
            # The advisory lock serializes cooperating writers; the conflict fallback keeps
            # this fail-closed if an older/uncoordinated writer inserted concurrently.
            existing = self._select_blob(stored.sha256)
            if existing is None:
                raise OrphanClaimStateError("orphan claim conflict has no catalog row")
            return self._decision_for_existing_blob(existing, stored)

        return OrphanClaimDecision(
            status=OrphanClaimStatus.CLAIMED,
            claim=OrphanDeletionClaim(
                blob_id=cast("UUID", inserted_id),
                lease_token=lease_token,
                stored=stored,
            ),
        )

    def recover_orphan_claims(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> tuple[OrphanDeletionClaim, ...]:
        """Reclaim expired orphan tombstones left at a filesystem crash boundary."""

        self._validate_lease(now=now, lease_until=lease_until)
        if not 1 <= limit <= _MAX_ORPHAN_BATCH:
            raise ValueError(f"orphan recovery is limited to {_MAX_ORPHAN_BATCH} rows")
        has_logical_reference = sa.exists(
            sa.select(sa.literal(1)).where(content_objects.c.blob_id == content_blobs.c.id)
        )
        rows = (
            self._connection.execute(
                sa.select(
                    content_blobs.c.id,
                    content_blobs.c.object_key,
                    content_blobs.c.sha256,
                    content_blobs.c.byte_size,
                )
                .where(
                    content_blobs.c.deletion_state == "deleting",
                    content_blobs.c.delete_lease_owner == _ORPHAN_LEASE_OWNER,
                    content_blobs.c.delete_lease_until <= now,
                    ~has_logical_reference,
                )
                .order_by(content_blobs.c.delete_lease_until, content_blobs.c.id)
                .limit(limit)
                .with_for_update(of=content_blobs, skip_locked=True)
            )
            .mappings()
            .all()
        )
        claims: list[OrphanDeletionClaim] = []
        for row in rows:
            blob_id = cast("UUID", row["id"])
            lease_token = uuid4()
            result = self._connection.execute(
                sa.update(content_blobs)
                .where(
                    content_blobs.c.id == blob_id,
                    content_blobs.c.deletion_state == "deleting",
                    content_blobs.c.delete_lease_owner == _ORPHAN_LEASE_OWNER,
                )
                .values(
                    delete_lease_token=lease_token,
                    delete_lease_until=lease_until,
                )
            )
            if result.rowcount != 1:
                raise OrphanClaimStateError("orphan deletion lease was lost during recovery")
            stored = StoredBlob(
                object_key=cast("str", row["object_key"]),
                sha256=cast("str", row["sha256"]),
                byte_size=cast("int", row["byte_size"]),
            )
            self._validate_blob_descriptor(stored)
            claims.append(
                OrphanDeletionClaim(
                    blob_id=blob_id,
                    lease_token=lease_token,
                    stored=stored,
                )
            )
        return tuple(claims)

    def finalize_orphan_deletion(
        self,
        claim: OrphanDeletionClaim,
        *,
        now: datetime,
        result: StorageDeleteResult,
    ) -> None:
        self._require_aware(now, "now")
        self._connection.execute(
            sa.text(
                "SELECT set_config('careerops.content_delete_lease_token', :lease_token, true)"
            ),
            {"lease_token": str(claim.lease_token)},
        )
        update_result = self._connection.execute(
            sa.update(content_blobs)
            .where(
                content_blobs.c.id == claim.blob_id,
                content_blobs.c.deletion_state == "deleting",
                content_blobs.c.delete_lease_owner == _ORPHAN_LEASE_OWNER,
                content_blobs.c.delete_lease_token == claim.lease_token,
            )
            .values(
                deletion_state="deleted",
                delete_lease_owner=None,
                delete_lease_token=None,
                delete_lease_until=None,
                deleted_at=now,
                delete_result=result.value,
            )
        )
        if update_result.rowcount == 1:
            return
        existing = (
            self._connection.execute(
                sa.select(
                    content_blobs.c.deletion_state,
                    content_blobs.c.delete_result,
                ).where(content_blobs.c.id == claim.blob_id)
            )
            .mappings()
            .first()
        )
        if existing is not None and existing["deletion_state"] == "deleted":
            return
        raise OrphanClaimStateError("orphan deletion lease was lost or is not finalizable")

    def _select_blob(self, digest: str) -> RowMapping | None:
        return (
            self._connection.execute(
                sa.select(
                    content_blobs.c.id,
                    content_blobs.c.object_key,
                    content_blobs.c.byte_size,
                    content_blobs.c.deletion_state,
                ).where(content_blobs.c.sha256 == digest)
            )
            .mappings()
            .first()
        )

    @staticmethod
    def _validate_active_blob(row: RowMapping, stored: StoredContent) -> UUID:
        state = PostgresContentCatalogRepository._validate_catalog_blob(row, stored)
        if state != "active":
            raise BlobLifecycleConflict(
                f"content blob is {state}; deleting and deleted blobs cannot gain references"
            )
        return cast("UUID", row["id"])

    @staticmethod
    def _decision_for_existing_blob(
        row: RowMapping,
        stored: StoredBlob,
    ) -> OrphanClaimDecision:
        state = PostgresContentCatalogRepository._validate_catalog_blob(row, stored)
        if state == "active":
            return OrphanClaimDecision(status=OrphanClaimStatus.REGISTERED)
        if state == "deleting":
            return OrphanClaimDecision(status=OrphanClaimStatus.CATALOG_PROTECTED)
        if state == "deleted":
            # A deleted tombstone permanently rejects new references. If bytes reappear,
            # cleanup may safely remove them instead of treating the tombstone as live.
            return OrphanClaimDecision(status=OrphanClaimStatus.DELETE_TOMBSTONED)
        raise CatalogIntegrityError(f"unsupported content blob lifecycle state: {state}")

    @staticmethod
    def _validate_catalog_blob(row: RowMapping, stored: StoredBlob) -> str:
        if row["object_key"] != stored.object_key or row["byte_size"] != stored.byte_size:
            raise CatalogIntegrityError("catalog blob metadata disagrees with verified storage")
        return cast("str", row["deletion_state"])

    @staticmethod
    def _validate_existing_logical_object(
        row: RowMapping,
        blob_id: UUID,
        stored: StoredContent,
    ) -> None:
        expected = {
            "blob_id": blob_id,
            "media_type": stored.media_type,
            "classification": stored.classification.value,
            "owner_resource_type": stored.owner.resource_type,
            "owner_resource_id": stored.owner.resource_id,
            "retention_until": stored.retention_until,
        }
        mismatches = tuple(key for key, value in expected.items() if row[key] != value)
        if mismatches or row["expired_at"] is not None or row["retired_at"] is not None:
            raise LogicalObjectConflict(
                "logical content object cannot be reused with different or retired metadata"
            )

    @staticmethod
    def _validate_stored(stored: StoredContent) -> None:
        PostgresContentCatalogRepository._validate_blob_descriptor(stored)
        if stored.retention_until.tzinfo is None or stored.retention_until.utcoffset() is None:
            raise CatalogIntegrityError("catalog retention deadline must be timezone-aware")

    @staticmethod
    def _validate_blob_descriptor(stored: StoredBlob) -> None:
        if not _DIGEST.fullmatch(stored.sha256):
            raise CatalogIntegrityError("catalog digest must be lowercase SHA-256")
        expected_key = f"sha256/{stored.sha256[:2]}/{stored.sha256[2:4]}/{stored.sha256}"
        if stored.object_key != expected_key or stored.byte_size < 0:
            raise CatalogIntegrityError("catalog object key or byte size is invalid")

    @staticmethod
    def _validate_lease(*, now: datetime, lease_until: datetime) -> None:
        PostgresContentCatalogRepository._require_aware(now, "now")
        PostgresContentCatalogRepository._require_aware(lease_until, "lease_until")
        if lease_until <= now or lease_until > now + timedelta(hours=1):
            raise ValueError("orphan deletion lease must be positive and at most one hour")

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    def _lock_digest(self, digest: str) -> None:
        self._acquire_advisory_lock(_advisory_key("content-digest", digest))

    def _lock_logical_object(self, object_id: UUID) -> None:
        self._acquire_advisory_lock(_advisory_key("content-object", str(object_id)))

    def _acquire_advisory_lock(self, lock_key: int) -> None:
        self._connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )


class PostgresContentCatalogStore:
    """Engine-backed catalog with an explicit, least-privilege cleanup capability.

    ``engine`` is the registration/read capability. Orphan claim, recovery and finalize use
    ``orphan_cleanup_engine`` so callers can bind those operations to the retention role
    without granting lifecycle mutation to the API role.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        orphan_cleanup_engine: Engine | None = None,
    ) -> None:
        self._engine = engine
        self._orphan_cleanup_engine = orphan_cleanup_engine

    def register(self, stored: StoredContent, object_id: UUID) -> ContentRegistration:
        with self._engine.begin() as connection:
            return PostgresContentCatalogRepository(connection).register(stored, object_id)

    def known_digests(self, digests: Collection[str]) -> frozenset[str]:
        normalized = tuple(dict.fromkeys(digests))
        if len(normalized) > _MAX_DIGEST_QUERY:
            raise ValueError(f"digest query is limited to {_MAX_DIGEST_QUERY} entries")
        if any(_DIGEST.fullmatch(digest) is None for digest in normalized):
            raise ValueError("digest query contains a non-canonical SHA-256")
        if not normalized:
            return frozenset()
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(content_blobs.c.sha256).where(
                    content_blobs.c.sha256.in_(normalized),
                    content_blobs.c.deletion_state == "active",
                )
            ).scalars()
            return frozenset(cast("str", digest) for digest in rows)

    def claim_orphan(
        self,
        stored: StoredBlob,
        *,
        now: datetime,
        lease_until: datetime,
    ) -> OrphanClaimDecision:
        with self._cleanup_engine().begin() as connection:
            return PostgresContentCatalogRepository(connection).claim_orphan(
                stored,
                now=now,
                lease_until=lease_until,
            )

    def recover_orphan_claims(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        limit: int,
    ) -> tuple[OrphanDeletionClaim, ...]:
        with self._cleanup_engine().begin() as connection:
            return PostgresContentCatalogRepository(connection).recover_orphan_claims(
                now=now,
                lease_until=lease_until,
                limit=limit,
            )

    def finalize_orphan_deletion(
        self,
        claim: OrphanDeletionClaim,
        *,
        now: datetime,
        result: StorageDeleteResult,
    ) -> None:
        with self._cleanup_engine().begin() as connection:
            PostgresContentCatalogRepository(connection).finalize_orphan_deletion(
                claim,
                now=now,
                result=result,
            )

    def _cleanup_engine(self) -> Engine:
        if self._orphan_cleanup_engine is None:
            raise OrphanCleanupCapabilityMissing(
                "orphan cleanup requires an explicit retention-capability engine"
            )
        return self._orphan_cleanup_engine


def _advisory_key(namespace: str, value: str) -> int:
    digest = hashlib.sha256(f"{namespace}\0{value}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)
