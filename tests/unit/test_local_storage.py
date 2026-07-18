from __future__ import annotations

import hashlib
import io
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

import pytest

from careerops.application.ports.storage import (
    ContentClassification,
    ContentOwner,
    DigestMismatch,
    ExpiredObject,
    InvalidContentMetadata,
    InvalidObjectKey,
    ObjectIntegrityError,
    ObjectNotFound,
    SizeLimitExceeded,
    StorageDeleteResult,
    UnsafeStoragePath,
)
from careerops.infrastructure.storage.local import LocalContentAddressedStorage


def owner() -> ContentOwner:
    return ContentOwner(resource_type="job_posting", resource_id=uuid4())


def future() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


def put(
    storage: LocalContentAddressedStorage,
    payload: bytes = b"career evidence",
):
    return storage.put(
        io.BytesIO(payload),
        media_type="text/plain",
        classification=ContentClassification.RAW_WEBPAGE,
        owner=owner(),
        retention_until=future(),
    )


def object_path(root: Path, object_key: str) -> Path:
    return root.joinpath(*object_key.split("/"))


def test_put_read_and_content_addressed_deduplication(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    storage = LocalContentAddressedStorage(root, max_object_bytes=1024)

    first = put(storage)
    first_inode = object_path(root, first.object_key).stat().st_ino
    second = put(storage)

    assert first.object_key == second.object_key
    assert first.sha256 == hashlib.sha256(b"career evidence").hexdigest()
    assert first.byte_size == len(b"career evidence")
    assert object_path(root, second.object_key).stat().st_ino == first_inode
    assert storage.read_bytes(first) == b"career evidence"
    assert list((root / "tmp").iterdir()) == []


def test_expected_digest_mismatch_removes_partial_object(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    storage = LocalContentAddressedStorage(root, max_object_bytes=1024)

    with pytest.raises(DigestMismatch):
        storage.put(
            io.BytesIO(b"actual"),
            media_type="text/plain",
            classification=ContentClassification.RAW_WEBPAGE,
            owner=owner(),
            retention_until=future(),
            expected_sha256="0" * 64,
        )

    assert list((root / "tmp").iterdir()) == []
    assert not (root / "sha256").exists()


def test_size_limit_removes_partial_object(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    storage = LocalContentAddressedStorage(root, max_object_bytes=4)

    with pytest.raises(SizeLimitExceeded):
        put(storage, b"too large")

    assert list((root / "tmp").iterdir()) == []


class FailingStream:
    def __init__(self) -> None:
        self._read_count = 0

    def read(self, _size: int = -1) -> bytes:
        self._read_count += 1
        if self._read_count == 1:
            return b"partial"
        raise OSError("simulated source failure")


def test_source_failure_removes_partial_object(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    storage = LocalContentAddressedStorage(root, max_object_bytes=1024)

    with pytest.raises(OSError, match="simulated source failure"):
        storage.put(
            cast("BinaryIO", FailingStream()),
            media_type="text/plain",
            classification=ContentClassification.RAW_WEBPAGE,
            owner=owner(),
            retention_until=future(),
        )

    assert list((root / "tmp").iterdir()) == []


def test_forged_object_key_cannot_escape_storage_root(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    storage = LocalContentAddressedStorage(root, max_object_bytes=1024)
    stored = put(storage)
    forged = replace(stored, object_key="sha256/../../outside")

    with pytest.raises(InvalidObjectKey):
        storage.read_bytes(forged)


def test_symlinked_root_component_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = tmp_path / "linked"
    symlink.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeStoragePath):
        LocalContentAddressedStorage(symlink / "objects", max_object_bytes=1024)


def test_symlinked_object_is_never_followed(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    storage = LocalContentAddressedStorage(root, max_object_bytes=1024)
    stored = put(storage)
    path = object_path(root, stored.object_key)
    target = tmp_path / "outside-secret"
    target.write_bytes(b"outside")
    path.unlink()
    path.symlink_to(target)

    with pytest.raises(UnsafeStoragePath):
        storage.read_bytes(stored)
    assert target.read_bytes() == b"outside"


def test_corrupted_existing_object_is_not_overwritten_or_returned(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    storage = LocalContentAddressedStorage(root, max_object_bytes=1024)
    stored = put(storage)
    path = object_path(root, stored.object_key)
    path.write_bytes(b"corrupt")

    with pytest.raises(ObjectIntegrityError):
        put(storage)
    with pytest.raises(ObjectIntegrityError):
        storage.read_bytes(stored)
    assert path.read_bytes() == b"corrupt"


def test_oversized_existing_object_is_rejected_before_reading(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    storage = LocalContentAddressedStorage(root, max_object_bytes=16)
    stored = put(storage, b"small")
    object_path(root, stored.object_key).write_bytes(b"x" * 17)

    with pytest.raises(ObjectIntegrityError, match="byte limit"):
        storage.read_bytes(stored)


def test_expired_object_is_unreadable_even_when_bytes_remain(tmp_path: Path) -> None:
    storage = LocalContentAddressedStorage(tmp_path / "objects", max_object_bytes=1024)
    stored = put(storage)

    with pytest.raises(ExpiredObject):
        storage.read_bytes(stored, now=stored.retention_until)


def test_delete_verifies_then_removes_bytes(tmp_path: Path) -> None:
    storage = LocalContentAddressedStorage(tmp_path / "objects", max_object_bytes=1024)
    stored = put(storage)

    assert storage.delete_bytes(stored) is StorageDeleteResult.DELETED
    assert storage.delete_bytes(stored) is StorageDeleteResult.ALREADY_MISSING

    with pytest.raises(ObjectNotFound):
        storage.read_bytes(stored)


def test_naive_retention_and_invalid_expected_digest_fail_closed(tmp_path: Path) -> None:
    storage = LocalContentAddressedStorage(tmp_path / "objects", max_object_bytes=1024)
    kwargs = {
        "media_type": "text/plain",
        "classification": ContentClassification.RAW_WEBPAGE,
        "owner": owner(),
    }

    with pytest.raises(InvalidContentMetadata, match="timezone-aware"):
        storage.put(
            io.BytesIO(b"payload"),
            retention_until=datetime.now(),
            **kwargs,
        )
    with pytest.raises(InvalidContentMetadata, match="lowercase hex"):
        storage.put(
            io.BytesIO(b"payload"),
            retention_until=future(),
            expected_sha256="../not-a-digest",
            **kwargs,
        )


def test_m0_rejects_model_debug_and_bounds_quarantine_retention(tmp_path: Path) -> None:
    storage = LocalContentAddressedStorage(tmp_path / "objects", max_object_bytes=1024)

    with pytest.raises(InvalidContentMetadata, match="M2 qualification"):
        storage.put(
            io.BytesIO(b"model trace"),
            media_type="application/json",
            classification=ContentClassification.MODEL_DEBUG,
            owner=owner(),
            retention_until=datetime.now(UTC) + timedelta(hours=1),
        )
    with pytest.raises(InvalidContentMetadata, match="at most 24 hours"):
        storage.put(
            io.BytesIO(b"quarantined"),
            media_type="application/octet-stream",
            classification=ContentClassification.QUARANTINED_ATTACHMENT,
            owner=owner(),
            retention_until=datetime.now(UTC) + timedelta(hours=25),
        )

    accepted = storage.put(
        io.BytesIO(b"quarantined"),
        media_type="application/octet-stream",
        classification=ContentClassification.QUARANTINED_ATTACHMENT,
        owner=owner(),
        retention_until=datetime.now(UTC) + timedelta(hours=23),
    )
    assert accepted.classification is ContentClassification.QUARANTINED_ATTACHMENT


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW")
def test_object_directory_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    storage = LocalContentAddressedStorage(root, max_object_bytes=1024)
    digest = hashlib.sha256(b"payload").hexdigest()
    (root / "sha256").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "sha256" / digest[:2]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeStoragePath):
        put(storage, b"payload")


def test_verified_blob_enumeration_is_old_only_and_bounded(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    storage = LocalContentAddressedStorage(root, max_object_bytes=1024)
    old_first = put(storage, b"old first")
    old_second = put(storage, b"old second")
    young = put(storage, b"young")
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    old_timestamp = (cutoff - timedelta(hours=1)).timestamp()
    for stored in (old_first, old_second):
        os.utime(
            object_path(root, stored.object_key),
            (old_timestamp, old_timestamp),
        )

    one = storage.enumerate_verified_blobs(older_than=cutoff, limit=1)
    all_old = storage.enumerate_verified_blobs(older_than=cutoff, limit=10)

    assert len(one) == 1
    assert {blob.sha256 for blob in all_old} == {
        old_first.sha256,
        old_second.sha256,
    }
    assert young.sha256 not in {blob.sha256 for blob in all_old}
    with pytest.raises(ValueError, match="between 1 and 1000"):
        storage.enumerate_verified_blobs(older_than=cutoff, limit=0)


def test_verified_blob_enumeration_rejects_corruption_before_returning_batch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "objects"
    storage = LocalContentAddressedStorage(root, max_object_bytes=1024)
    valid = put(storage, b"valid old candidate")
    corrupt = put(storage, b"candidate that will be corrupted")
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    old_timestamp = (cutoff - timedelta(hours=1)).timestamp()
    valid_path = object_path(root, valid.object_key)
    corrupt_path = object_path(root, corrupt.object_key)
    os.utime(valid_path, (old_timestamp, old_timestamp))
    corrupt_path.write_bytes(b"tampered")
    os.utime(corrupt_path, (old_timestamp, old_timestamp))

    with pytest.raises(ObjectIntegrityError, match="digest or size verification"):
        storage.enumerate_verified_blobs(older_than=cutoff, limit=10)

    assert valid_path.read_bytes() == b"valid old candidate"
    assert corrupt_path.read_bytes() == b"tampered"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform lacks O_NOFOLLOW")
def test_verified_blob_enumeration_rejects_symlink_before_returning_batch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "objects"
    storage = LocalContentAddressedStorage(root, max_object_bytes=1024)
    valid = put(storage, b"valid old candidate")
    replaced = put(storage, b"candidate replaced by a symlink")
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    old_timestamp = (cutoff - timedelta(hours=1)).timestamp()
    valid_path = object_path(root, valid.object_key)
    os.utime(valid_path, (old_timestamp, old_timestamp))
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside data")
    replaced_path = object_path(root, replaced.object_key)
    replaced_path.unlink()
    replaced_path.symlink_to(outside)

    with pytest.raises(UnsafeStoragePath, match="symlink"):
        storage.enumerate_verified_blobs(older_than=cutoff, limit=10)

    assert valid_path.read_bytes() == b"valid old candidate"
    assert outside.read_bytes() == b"outside data"
