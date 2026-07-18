from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from careerops.application.content_retention import (
    ContentDeletionCandidate,
    ContentRetentionService,
)
from careerops.application.ports.storage import (
    StorageDeleteResult,
    StoredBlob,
    StoredContent,
)
from careerops.infrastructure.database.content_retention import deletion_candidate_statement


def candidate() -> ContentDeletionCandidate:
    now = datetime.now(UTC)
    return ContentDeletionCandidate(
        blob_id=uuid4(),
        lease_token=uuid4(),
        lease_until=now + timedelta(minutes=5),
        stored=StoredBlob(
            object_key="sha256/00/00/" + "0" * 64,
            sha256="0" * 64,
            byte_size=1,
        ),
    )


class RecordingRepository:
    def __init__(self, item: ContentDeletionCandidate | None) -> None:
        self.item = item
        self.calls: list[str] = []

    def mark_due_expired(self, *, now: datetime) -> int:
        self.calls.append(f"expire:{now.isoformat()}")
        return 2

    def claim_deletable(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
    ) -> ContentDeletionCandidate | None:
        self.calls.append(f"claim:{owner}:{now.isoformat()}:{lease_for.total_seconds()}")
        return self.item

    def finalize_deletion(
        self,
        blob_id: UUID,
        *,
        lease_token: UUID,
        now: datetime,
        delete_result: StorageDeleteResult,
    ) -> None:
        self.calls.append(
            f"finalize:{blob_id}:{lease_token}:{now.isoformat()}:{delete_result.value}"
        )


class RecordingStorage:
    def __init__(
        self,
        calls: list[str],
        *,
        result: StorageDeleteResult = StorageDeleteResult.DELETED,
        fail: bool = False,
    ) -> None:
        self.calls = calls
        self.result = result
        self.fail = fail

    def put(self, *args: object, **kwargs: object) -> StoredContent:
        raise AssertionError("not used")

    def read_bytes(self, stored: StoredContent, *, now: datetime | None = None) -> bytes:
        raise AssertionError("not used")

    def delete_bytes(self, stored: StoredBlob) -> StorageDeleteResult:
        self.calls.append(f"bytes:{stored.sha256}")
        if self.fail:
            raise OSError("disk unavailable")
        return self.result


def test_purge_commits_claim_before_bytes_and_finalizes_result() -> None:
    item = candidate()
    repository = RecordingRepository(item)
    storage = RecordingStorage(repository.calls)
    service = ContentRetentionService(repository, storage)
    now = datetime.now(UTC)

    result = service.purge_next(now=now)

    assert result is not None
    assert result.blob_id == item.blob_id
    assert result.delete_result is StorageDeleteResult.DELETED
    assert repository.calls == [
        f"claim:content-retention:{now.isoformat()}:300.0",
        f"bytes:{item.stored.sha256}",
        f"finalize:{item.blob_id}:{item.lease_token}:{now.isoformat()}:deleted",
    ]


def test_failed_byte_delete_leaves_committed_lease_for_stale_recovery() -> None:
    repository = RecordingRepository(candidate())
    service = ContentRetentionService(repository, RecordingStorage(repository.calls, fail=True))

    try:
        service.purge_next(now=datetime.now(UTC))
    except OSError:
        pass
    else:
        raise AssertionError("storage failure must propagate")

    assert repository.calls[0].startswith("claim:")
    assert repository.calls[1].startswith("bytes:")
    assert not any(call.startswith("finalize:") for call in repository.calls)


def test_crash_after_unlink_recovery_persists_already_missing_result() -> None:
    item = candidate()
    repository = RecordingRepository(item)
    service = ContentRetentionService(
        repository,
        RecordingStorage(
            repository.calls,
            result=StorageDeleteResult.ALREADY_MISSING,
        ),
        owner="recovery-worker",
    )
    now = item.lease_until + timedelta(seconds=1)

    result = service.purge_next(now=now)

    assert result is not None
    assert result.delete_result is StorageDeleteResult.ALREADY_MISSING
    assert repository.calls[-1].endswith(":already_missing")


def test_no_candidate_causes_no_storage_side_effect() -> None:
    repository = RecordingRepository(None)
    service = ContentRetentionService(repository, RecordingStorage(repository.calls))

    assert service.purge_next(now=datetime.now(UTC)) is None
    assert len(repository.calls) == 1
    assert repository.calls[0].startswith("claim:")


def test_deletion_query_checks_complete_logical_reference_graph() -> None:
    sql = str(deletion_candidate_statement())

    assert "content_blobs" in sql
    assert "content_objects" in sql
    assert "evidence_records" in sql
    assert "job_posting_versions" in sql
    assert "retired_at" in sql
    assert "delete_lease_until" in sql
    assert "FOR UPDATE" in sql
