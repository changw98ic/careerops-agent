from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping

from careerops.application.content_retention import ContentDeletionCandidate
from careerops.application.ports.storage import StorageDeleteResult, StoredBlob
from careerops.infrastructure.database.schema import (
    content_blobs,
    content_objects,
    evidence_records,
    job_posting_versions,
)

_OWNER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class RetentionStateError(RuntimeError):
    pass


def deletion_candidate_statement() -> sa.Select[tuple[object, ...]]:
    """Lock one blob only when its complete logical reference graph is disposable."""

    evidence_source_missing = sa.and_(
        sa.or_(
            evidence_records.c.source_url.is_(None),
            sa.func.btrim(evidence_records.c.source_url) == "",
        ),
        sa.or_(
            evidence_records.c.provider_id.is_(None),
            sa.func.btrim(evidence_records.c.provider_id) == "",
        ),
    )
    unsafe_evidence_reference = sa.exists(
        sa.select(sa.literal(1))
        .select_from(
            content_objects.join(
                evidence_records,
                evidence_records.c.content_object_id == content_objects.c.id,
            )
        )
        .where(
            content_objects.c.blob_id == content_blobs.c.id,
            sa.or_(
                evidence_source_missing,
                sa.func.btrim(evidence_records.c.sanitized_span) == "",
                sa.func.btrim(evidence_records.c.extractor_version) == "",
                evidence_records.c.content_hash != content_blobs.c.sha256,
            ),
        )
    )
    unsafe_job_snapshot_reference = sa.exists(
        sa.select(sa.literal(1))
        .select_from(
            content_objects.join(
                job_posting_versions,
                job_posting_versions.c.raw_snapshot_id == content_objects.c.id,
            )
        )
        .where(
            content_objects.c.blob_id == content_blobs.c.id,
            sa.or_(
                sa.func.btrim(job_posting_versions.c.source_url) == "",
                sa.func.btrim(job_posting_versions.c.parser_version) == "",
                job_posting_versions.c.content_hash != content_blobs.c.sha256,
            ),
        )
    )
    has_logical_reference = sa.exists(
        sa.select(sa.literal(1)).where(content_objects.c.blob_id == content_blobs.c.id)
    )
    has_unretired_logical_reference = sa.exists(
        sa.select(sa.literal(1)).where(
            content_objects.c.blob_id == content_blobs.c.id,
            content_objects.c.retired_at.is_(None),
        )
    )
    claim_now = sa.bindparam("claim_now", type_=sa.DateTime(timezone=True))
    claimable_state = sa.or_(
        content_blobs.c.deletion_state == "active",
        sa.and_(
            content_blobs.c.deletion_state == "deleting",
            content_blobs.c.delete_lease_until <= claim_now,
        ),
    )
    return (
        sa.select(
            content_blobs.c.id,
            content_blobs.c.object_key,
            content_blobs.c.sha256,
            content_blobs.c.byte_size,
        )
        .where(
            claimable_state,
            has_logical_reference,
            ~has_unretired_logical_reference,
            ~unsafe_evidence_reference,
            ~unsafe_job_snapshot_reference,
        )
        .order_by(
            content_blobs.c.delete_lease_until.asc().nullsfirst(),
            content_blobs.c.created_at,
            content_blobs.c.id,
        )
        .limit(1)
        .with_for_update(of=content_blobs, skip_locked=True)
    )


class PostgresContentRetentionRepository:
    """Retention operations scoped to the caller's explicit transaction."""

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError("content retention requires an explicit database transaction")
        self._connection = connection

    def mark_due_expired(self, *, now: datetime) -> int:
        self._require_aware(now, "now")
        result = self._connection.execute(
            sa.update(content_objects)
            .where(
                content_objects.c.retention_until <= now,
                content_objects.c.expired_at.is_(None),
                content_objects.c.retired_at.is_(None),
            )
            .values(expired_at=now)
        )
        return result.rowcount

    def claim_deletable(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
    ) -> ContentDeletionCandidate | None:
        self._validate_claim(owner=owner, now=now, lease_for=lease_for)
        candidate_id = self._connection.scalar(
            deletion_candidate_statement().with_only_columns(content_blobs.c.id),
            {"claim_now": now},
        )
        if candidate_id is None:
            return None
        # The first READ COMMITTED statement locks a likely candidate. Reference
        # guard triggers now block on that parent row; a second statement gets a
        # fresh snapshot after any earlier writer and proves the graph again.
        row = (
            self._connection.execute(
                deletion_candidate_statement().where(content_blobs.c.id == candidate_id),
                {"claim_now": now},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None

        blob_id = cast("UUID", row["id"])
        lease_token = uuid4()
        lease_until = now + lease_for
        result = self._connection.execute(
            sa.update(content_blobs)
            .where(content_blobs.c.id == blob_id)
            .values(
                deletion_state="deleting",
                delete_lease_owner=owner,
                delete_lease_token=lease_token,
                delete_lease_until=lease_until,
            )
        )
        if result.rowcount != 1:
            raise RetentionStateError("claimed content blob disappeared")
        return self._to_candidate(
            row,
            lease_token=lease_token,
            lease_until=lease_until,
        )

    def finalize_deletion(
        self,
        blob_id: UUID,
        *,
        lease_token: UUID,
        now: datetime,
        delete_result: StorageDeleteResult,
    ) -> None:
        self._require_aware(now, "now")
        self._connection.execute(
            sa.text(
                "SELECT set_config('careerops.content_delete_lease_token', :lease_token, true)"
            ),
            {"lease_token": str(lease_token)},
        )
        result = self._connection.execute(
            sa.update(content_blobs)
            .where(
                content_blobs.c.id == blob_id,
                content_blobs.c.deletion_state == "deleting",
                content_blobs.c.delete_lease_token == lease_token,
            )
            .values(
                deletion_state="deleted",
                delete_lease_owner=None,
                delete_lease_token=None,
                delete_lease_until=None,
                deleted_at=now,
                delete_result=delete_result.value,
            )
        )
        if result.rowcount != 1:
            raise RetentionStateError("content deletion lease was lost or already finalized")

    @staticmethod
    def _to_candidate(
        row: RowMapping,
        *,
        lease_token: UUID,
        lease_until: datetime,
    ) -> ContentDeletionCandidate:
        return ContentDeletionCandidate(
            blob_id=cast("UUID", row["id"]),
            lease_token=lease_token,
            lease_until=lease_until,
            stored=StoredBlob(
                object_key=cast("str", row["object_key"]),
                sha256=cast("str", row["sha256"]),
                byte_size=cast("int", row["byte_size"]),
            ),
        )

    @staticmethod
    def _validate_claim(*, owner: str, now: datetime, lease_for: timedelta) -> None:
        if not _OWNER.fullmatch(owner):
            raise ValueError("deletion lease owner must be a bounded machine identifier")
        PostgresContentRetentionRepository._require_aware(now, "now")
        if lease_for <= timedelta(0) or lease_for > timedelta(hours=1):
            raise ValueError("deletion lease must be positive and at most one hour")

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")


class PostgresContentRetentionStore:
    """Commits claim and finalize around the non-transactional filesystem boundary."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def mark_due_expired(self, *, now: datetime) -> int:
        with self._engine.begin() as connection:
            return PostgresContentRetentionRepository(connection).mark_due_expired(now=now)

    def claim_deletable(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
    ) -> ContentDeletionCandidate | None:
        with self._engine.begin() as connection:
            return PostgresContentRetentionRepository(connection).claim_deletable(
                owner=owner,
                now=now,
                lease_for=lease_for,
            )

    def finalize_deletion(
        self,
        blob_id: UUID,
        *,
        lease_token: UUID,
        now: datetime,
        delete_result: StorageDeleteResult,
    ) -> None:
        with self._engine.begin() as connection:
            PostgresContentRetentionRepository(connection).finalize_deletion(
                blob_id,
                lease_token=lease_token,
                now=now,
                delete_result=delete_result,
            )
