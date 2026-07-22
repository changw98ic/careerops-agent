from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

_SOURCE_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class CrawlerSourceLease:
    registry_id: UUID
    source_id: str
    run_id: UUID
    source_row_id: UUID
    worker_id: str
    lease_token: UUID
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id)
        _validate_worker_id(self.worker_id)
        _validate_aware(self.lease_expires_at, "lease_expires_at")


@dataclass(frozen=True, slots=True)
class CrawlerSourceCompletion:
    run_id: UUID
    source_row_id: UUID
    worker_id: str
    lease_token: UUID
    output_manifest_sha256: str | None
    cursor: str | None
    result: str

    def __post_init__(self) -> None:
        _validate_worker_id(self.worker_id)
        if (
            self.output_manifest_sha256 is not None
            and _SHA256.fullmatch(self.output_manifest_sha256) is None
        ):
            raise ValueError("output_manifest_sha256 must be a lowercase sha256 hex digest")
        if self.cursor is not None and len(self.cursor) > 160:
            raise ValueError("cursor must be at most 160 characters")
        _validate_text(self.result, "result", max_length=160)
        if self.result.startswith("failed:"):
            raise ValueError("result must not use the reserved failed prefix")


@dataclass(frozen=True, slots=True)
class CrawlerSourceFailure:
    run_id: UUID
    source_row_id: UUID
    worker_id: str
    lease_token: UUID
    error: str

    def __post_init__(self) -> None:
        _validate_worker_id(self.worker_id)
        _validate_text(self.error, "error", max_length=500)


class CrawlerSourceRegistryStore(Protocol):
    def claim_source(
        self,
        registry_id: UUID,
        source_id: str,
        *,
        worker_id: str,
        lease_for: timedelta,
    ) -> Mapping[str, object]: ...

    def claim_due_source(
        self,
        registry_id: UUID,
        *,
        worker_id: str,
        lease_for: timedelta,
    ) -> Mapping[str, object] | None: ...

    def complete_source(
        self,
        *,
        source_row_id: UUID,
        run_id: UUID,
        worker_id: str,
        lease_token: UUID,
        output_manifest_sha256: str | None,
        cursor: str | None,
        result: str,
    ) -> Mapping[str, object]: ...

    def fail_source(
        self,
        *,
        source_row_id: UUID,
        run_id: UUID,
        worker_id: str,
        lease_token: UUID,
        error: str,
    ) -> Mapping[str, object]: ...


def _validate_worker_id(value: str) -> None:
    if _WORKER_ID.fullmatch(value) is None:
        raise ValueError("worker_id must be a bounded worker identifier")


def _validate_source_id(value: str) -> None:
    if _SOURCE_ID.fullmatch(value) is None:
        raise ValueError("source_id must be a bounded source identifier")


def _validate_text(value: str, field: str, *, max_length: int) -> None:
    if not 1 <= len(value.strip()) <= max_length:
        raise ValueError(f"{field} must be between 1 and {max_length} characters")


def _validate_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


__all__ = [
    "CrawlerSourceCompletion",
    "CrawlerSourceFailure",
    "CrawlerSourceLease",
    "CrawlerSourceRegistryStore",
]
