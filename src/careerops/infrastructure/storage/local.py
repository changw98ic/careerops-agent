from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections.abc import Generator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

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
    StoredBlob,
    StoredContent,
    UnsafeStoragePath,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HEX_PAIR = re.compile(r"^[0-9a-f]{2}$")
_OBJECT_KEY = re.compile(r"^sha256/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})$")
_CHUNK_SIZE = 64 * 1024
_MAX_ENUMERATION_LIMIT = 1_000
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class LocalContentAddressedStorage:
    """Local content-addressed storage using descriptor-relative, no-follow I/O."""

    def __init__(self, root: Path, *, max_object_bytes: int) -> None:
        if max_object_bytes < 1:
            raise ValueError("max_object_bytes must be positive")
        self._root = Path(os.path.abspath(root))
        self._max_object_bytes = max_object_bytes
        with self._root_fd(create=True) as root_fd:
            temp_fd = self._open_child_directory(root_fd, "tmp", create=True)
            os.close(temp_fd)

    def put(
        self,
        stream: BinaryIO,
        *,
        media_type: str,
        classification: ContentClassification,
        owner: ContentOwner,
        retention_until: datetime,
        expected_sha256: str | None = None,
    ) -> StoredContent:
        self._validate_metadata(
            media_type,
            classification,
            owner,
            retention_until,
            now=datetime.now(UTC),
        )
        if expected_sha256 is not None and not _DIGEST.fullmatch(expected_sha256):
            raise InvalidContentMetadata("expected SHA-256 must be 64 lowercase hex characters")

        with self._root_fd() as root_fd:
            temp_dir_fd = self._open_child_directory(root_fd, "tmp")
            temp_name = f"{uuid4().hex}.tmp"
            temp_created = False
            try:
                temp_fd = os.open(
                    temp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW,
                    0o600,
                    dir_fd=temp_dir_fd,
                )
                temp_created = True
                digest, byte_size = self._write_stream(stream, temp_fd)
                if expected_sha256 is not None and digest != expected_sha256:
                    raise DigestMismatch("stored content did not match the expected SHA-256")

                leaf_fd = self._open_digest_directory(root_fd, digest, create=True)
                try:
                    existing = self._inspect_existing(leaf_fd, digest)
                    if existing is not None:
                        existing_digest, existing_size = existing
                        if existing_digest != digest or existing_size != byte_size:
                            raise ObjectIntegrityError(
                                "content-addressed destination already exists with different bytes"
                            )
                    else:
                        os.replace(
                            temp_name,
                            digest,
                            src_dir_fd=temp_dir_fd,
                            dst_dir_fd=leaf_fd,
                        )
                        os.fsync(leaf_fd)
                        temp_created = False
                finally:
                    os.close(leaf_fd)
            finally:
                if temp_created:
                    with suppress(FileNotFoundError):
                        os.unlink(temp_name, dir_fd=temp_dir_fd)
                os.close(temp_dir_fd)

        return StoredContent(
            object_key=self.object_key_for_digest(digest),
            sha256=digest,
            byte_size=byte_size,
            media_type=media_type,
            classification=classification,
            owner=owner,
            retention_until=retention_until,
        )

    def read_bytes(self, stored: StoredContent, *, now: datetime | None = None) -> bytes:
        current = now or datetime.now(UTC)
        self._require_aware(current, "now")
        self._require_aware(stored.retention_until, "retention_until")
        if stored.retention_until <= current:
            raise ExpiredObject("expired content is not readable")

        digest = self._digest_from_descriptor(stored)
        with self._root_fd() as root_fd:
            try:
                leaf_fd = self._open_digest_directory(root_fd, digest)
            except FileNotFoundError as error:
                raise ObjectNotFound("content object does not exist") from error
            try:
                try:
                    file_fd = os.open(
                        digest,
                        os.O_RDONLY | _FILE_NOFOLLOW,
                        dir_fd=leaf_fd,
                    )
                except FileNotFoundError as error:
                    raise ObjectNotFound("content object does not exist") from error
                except OSError as error:
                    self._raise_path_error(error)
                    raise
                try:
                    payload, actual_digest, actual_size = self._read_file(file_fd)
                finally:
                    os.close(file_fd)
            finally:
                os.close(leaf_fd)

        if actual_digest != digest or actual_size != stored.byte_size:
            raise ObjectIntegrityError("stored object failed digest or size verification")
        return payload

    def delete_bytes(self, stored: StoredBlob) -> StorageDeleteResult:
        digest = self._digest_from_descriptor(stored)
        with self._root_fd() as root_fd:
            try:
                leaf_fd = self._open_digest_directory(root_fd, digest)
            except FileNotFoundError:
                return StorageDeleteResult.ALREADY_MISSING
            try:
                existing = self._inspect_existing(leaf_fd, digest)
                if existing is None:
                    return StorageDeleteResult.ALREADY_MISSING
                actual_digest, actual_size = existing
                if actual_digest != digest or actual_size != stored.byte_size:
                    raise ObjectIntegrityError(
                        "refusing to delete an object that failed verification"
                    )
                os.unlink(digest, dir_fd=leaf_fd)
                os.fsync(leaf_fd)
            finally:
                os.close(leaf_fd)
        return StorageDeleteResult.DELETED

    def enumerate_verified_blobs(
        self,
        *,
        older_than: datetime,
        limit: int,
    ) -> tuple[StoredBlob, ...]:
        """Return a bounded batch of old, fully verified physical blobs.

        Enumeration is descriptor-relative and refuses symlinks or non-canonical
        entries.  The complete result tuple is built before it is returned, so a
        caller cannot begin deleting an earlier candidate if a later candidate in
        the same batch fails verification.
        """

        self._require_aware(older_than, "older_than")
        if not 1 <= limit <= _MAX_ENUMERATION_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_ENUMERATION_LIMIT}")
        cutoff_timestamp = older_than.timestamp()
        verified: list[StoredBlob] = []

        with self._root_fd() as root_fd:
            try:
                sha256_fd = self._open_child_directory(root_fd, "sha256")
            except FileNotFoundError:
                return ()
            try:
                for first in sorted(os.listdir(sha256_fd)):
                    if _HEX_PAIR.fullmatch(first) is None:
                        raise UnsafeStoragePath(
                            "SHA-256 storage contains a non-canonical directory"
                        )
                    try:
                        first_fd = self._open_child_directory(sha256_fd, first)
                    except FileNotFoundError:
                        continue
                    try:
                        for second in sorted(os.listdir(first_fd)):
                            if _HEX_PAIR.fullmatch(second) is None:
                                raise UnsafeStoragePath(
                                    "SHA-256 storage contains a non-canonical directory"
                                )
                            try:
                                leaf_fd = self._open_child_directory(first_fd, second)
                            except FileNotFoundError:
                                continue
                            try:
                                for digest in sorted(os.listdir(leaf_fd)):
                                    if (
                                        _DIGEST.fullmatch(digest) is None
                                        or digest[:2] != first
                                        or digest[2:4] != second
                                    ):
                                        raise UnsafeStoragePath(
                                            "SHA-256 storage contains a non-canonical object"
                                        )
                                    candidate = self._inspect_enumeration_candidate(
                                        leaf_fd,
                                        digest,
                                        cutoff_timestamp=cutoff_timestamp,
                                    )
                                    if candidate is None:
                                        continue
                                    verified.append(candidate)
                                    if len(verified) == limit:
                                        return tuple(verified)
                            finally:
                                os.close(leaf_fd)
                    finally:
                        os.close(first_fd)
            finally:
                os.close(sha256_fd)

        return tuple(verified)

    @staticmethod
    def object_key_for_digest(digest: str) -> str:
        if not _DIGEST.fullmatch(digest):
            raise InvalidObjectKey("SHA-256 must be 64 lowercase hex characters")
        return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    def path_for_digest(self, digest: str) -> Path | None:
        """Return the on-disk path of a stored blob by digest, or None if absent.

        Read-only resolution for providers (e.g. the Gmail send adapter) that
        need a real file path to attach. The canonical content-addressed layout
        is ``<root>/sha256/<digest[:2]>/<digest[2:4]>/<digest>``. The digest is
        validated against the canonical pattern; an invalid or missing blob
        returns None rather than raising.
        """
        if not _DIGEST.fullmatch(digest):
            return None
        path = self._root / "sha256" / digest[:2] / digest[2:4] / digest
        return path if path.is_file() else None

    @contextmanager
    def _root_fd(self, *, create: bool = False) -> Generator[int]:
        descriptor = os.open(self._root.anchor, _DIRECTORY_FLAGS)
        try:
            for part in self._root.parts[1:]:
                child = self._open_child_directory(descriptor, part, create=create)
                os.close(descriptor)
                descriptor = child
        except BaseException:
            os.close(descriptor)
            raise
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def _open_digest_directory(self, root_fd: int, digest: str, *, create: bool = False) -> int:
        parent_fd = os.dup(root_fd)
        try:
            for name in ("sha256", digest[:2], digest[2:4]):
                child_fd = self._open_child_directory(parent_fd, name, create=create)
                os.close(parent_fd)
                parent_fd = child_fd
            return parent_fd
        except BaseException:
            os.close(parent_fd)
            raise

    @staticmethod
    def _open_child_directory(parent_fd: int, name: str, *, create: bool = False) -> int:
        if "/" in name or name in {"", ".", ".."}:
            raise UnsafeStoragePath("invalid storage directory segment")
        if create:
            with suppress(FileExistsError):
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        try:
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as error:
            LocalContentAddressedStorage._raise_path_error(error)
            raise
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise UnsafeStoragePath("storage path component is not a directory")
        return descriptor

    def _write_stream(self, stream: BinaryIO, file_fd: int) -> tuple[str, int]:
        hasher = hashlib.sha256()
        byte_size = 0
        with os.fdopen(file_fd, "wb") as destination:
            while True:
                chunk = self._require_bytes(stream.read(_CHUNK_SIZE))
                if chunk == b"":
                    break
                byte_size += len(chunk)
                if byte_size > self._max_object_bytes:
                    raise SizeLimitExceeded("content exceeds the configured byte limit")
                hasher.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        return hasher.hexdigest(), byte_size

    @staticmethod
    def _require_bytes(value: object) -> bytes:
        if not isinstance(value, bytes):
            raise TypeError("storage streams must return bytes")
        return value

    def _inspect_existing(self, directory_fd: int, digest: str) -> tuple[str, int] | None:
        try:
            file_fd = os.open(digest, os.O_RDONLY | _FILE_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            LocalContentAddressedStorage._raise_path_error(error)
            raise
        try:
            _, actual_digest, actual_size = self._read_file(file_fd)
            return actual_digest, actual_size
        finally:
            os.close(file_fd)

    def _inspect_enumeration_candidate(
        self,
        directory_fd: int,
        digest: str,
        *,
        cutoff_timestamp: float,
    ) -> StoredBlob | None:
        try:
            file_fd = os.open(digest, os.O_RDONLY | _FILE_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            self._raise_path_error(error)
            raise
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise UnsafeStoragePath("content object is not a regular file")
            if before.st_mtime >= cutoff_timestamp:
                return None
            if before.st_size > self._max_object_bytes:
                raise ObjectIntegrityError("stored object exceeds the configured byte limit")

            hasher = hashlib.sha256()
            actual_size = 0
            while True:
                chunk = os.read(file_fd, _CHUNK_SIZE)
                if not chunk:
                    break
                hasher.update(chunk)
                actual_size += len(chunk)
                if actual_size > self._max_object_bytes:
                    raise ObjectIntegrityError("stored object exceeds the configured byte limit")

            after = os.fstat(file_fd)
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                raise ObjectIntegrityError("stored object changed while it was being enumerated")
            if actual_size != before.st_size or hasher.hexdigest() != digest:
                raise ObjectIntegrityError("stored object failed digest or size verification")

            stored = StoredBlob(
                object_key=self.object_key_for_digest(digest),
                sha256=digest,
                byte_size=actual_size,
            )
            self._digest_from_descriptor(stored)
            return stored
        finally:
            os.close(file_fd)

    def _read_file(self, file_fd: int) -> tuple[bytes, str, int]:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeStoragePath("content object is not a regular file")
        if info.st_size > self._max_object_bytes:
            raise ObjectIntegrityError("stored object exceeds the configured byte limit")
        chunks: list[bytes] = []
        hasher = hashlib.sha256()
        byte_size = 0
        while True:
            chunk = os.read(file_fd, _CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
            hasher.update(chunk)
            byte_size += len(chunk)
            if byte_size > self._max_object_bytes:
                raise ObjectIntegrityError("stored object exceeds the configured byte limit")
        return b"".join(chunks), hasher.hexdigest(), byte_size

    @staticmethod
    def _validate_metadata(
        media_type: str,
        classification: ContentClassification,
        owner: ContentOwner,
        retention_until: datetime,
        *,
        now: datetime,
    ) -> None:
        if not media_type.strip() or len(media_type) > 255:
            raise InvalidContentMetadata("media type must be non-empty and bounded")
        if not owner.resource_type.strip() or len(owner.resource_type) > 32:
            raise InvalidContentMetadata("owner resource type must be non-empty and bounded")
        LocalContentAddressedStorage._require_aware(retention_until, "retention_until")
        if classification is ContentClassification.MODEL_DEBUG:
            raise InvalidContentMetadata("model debug storage is disabled until M2 qualification")
        if (
            classification is ContentClassification.QUARANTINED_ATTACHMENT
            and retention_until > now + timedelta(hours=24)
        ):
            raise InvalidContentMetadata(
                "quarantined attachments may be retained for at most 24 hours"
            )

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise InvalidContentMetadata(f"{name} must be timezone-aware")

    @staticmethod
    def _digest_from_descriptor(stored: StoredBlob) -> str:
        match = _OBJECT_KEY.fullmatch(stored.object_key)
        if match is None:
            raise InvalidObjectKey("object key is not a canonical SHA-256 path")
        first, second, digest = match.groups()
        if first != digest[:2] or second != digest[2:4] or digest != stored.sha256:
            raise InvalidObjectKey("object key and digest disagree")
        return digest

    @staticmethod
    def _raise_path_error(error: OSError) -> None:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafeStoragePath("storage path contains a symlink or non-directory") from error
