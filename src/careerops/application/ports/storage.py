from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import BinaryIO, Protocol
from uuid import UUID


class ContentClassification(StrEnum):
    RAW_WEBPAGE = "raw_webpage"
    RAW_HEADERS = "raw_headers"
    RECRUITING_EMAIL = "recruiting_email"
    ACCEPTED_ATTACHMENT = "accepted_attachment"
    QUARANTINED_ATTACHMENT = "quarantined_attachment"
    MODEL_DEBUG = "model_debug"
    DECISION_EVIDENCE = "decision_evidence"


@dataclass(frozen=True, slots=True)
class ContentOwner:
    resource_type: str
    resource_id: UUID


@dataclass(frozen=True, slots=True)
class StoredBlob:
    object_key: str
    sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class StoredContent(StoredBlob):
    media_type: str
    classification: ContentClassification
    owner: ContentOwner
    retention_until: datetime


class StorageDeleteResult(StrEnum):
    DELETED = "deleted"
    ALREADY_MISSING = "already_missing"


class StorageError(Exception):
    """Base class for fail-closed content storage errors."""


class InvalidContentMetadata(StorageError):
    pass


class InvalidObjectKey(StorageError):
    pass


class SizeLimitExceeded(StorageError):
    pass


class DigestMismatch(StorageError):
    pass


class ObjectIntegrityError(StorageError):
    pass


class ObjectNotFound(StorageError):
    pass


class ExpiredObject(StorageError):
    pass


class UnsafeStoragePath(StorageError):
    pass


class StoragePort(Protocol):
    """Content-addressed byte storage; database metadata remains a separate concern."""

    def put(
        self,
        stream: BinaryIO,
        *,
        media_type: str,
        classification: ContentClassification,
        owner: ContentOwner,
        retention_until: datetime,
        expected_sha256: str | None = None,
    ) -> StoredContent: ...

    def read_bytes(self, stored: StoredContent, *, now: datetime | None = None) -> bytes: ...

    def delete_bytes(self, stored: StoredBlob) -> StorageDeleteResult: ...


class VerifiedBlobStoragePort(StoragePort, Protocol):
    """Storage capability used only by bounded orphan recovery."""

    def enumerate_verified_blobs(
        self,
        *,
        older_than: datetime,
        limit: int,
    ) -> tuple[StoredBlob, ...]: ...
