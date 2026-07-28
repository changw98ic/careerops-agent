"""Context create/read/invalidate for the Agent Console.

Provides the service layer for agent operation contexts.  A context
freezes source version snapshots and digests so that an agent run can
be started against an immutable, verified set of inputs.

api-contract.md section 3 / 3.1 freezes the Context shape.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from careerops.agent_console.contracts import (
    Context,
    ContextSourceRef,
    CreateContext,
    ModelOperation,
    SourceDigests,
    SourceVersionSnapshot,
)
from careerops.api.errors import NotFoundError

__all__ = ["ContextRecord", "ContextService"]

_CONTEXT_TTL = timedelta(hours=1)
_SCHEMA_VERSION = "agent-context-v1"


@dataclass(frozen=True, slots=True)
class ContextRecord:
    """Internal representation of a persisted context."""

    context_id: UUID
    operation: ModelOperation
    candidate_id: UUID
    schema_version: str
    source_version_snapshot: SourceVersionSnapshot
    source_digests: SourceDigests
    allowed_operation: ModelOperation
    created_at: datetime
    expires_at: datetime | None
    state: str
    invalidation_reason: str | None = None
    source_refs: tuple[ContextSourceRef, ...] = ()


class ContextService:
    """Service for creating and reading agent operation contexts.

    v1 is an in-memory stub.  A production implementation would persist
    contexts to a database table keyed by (candidate_id, context_id).
    """

    def __init__(self) -> None:
        self._contexts: dict[UUID, ContextRecord] = {}

    def create(
        self,
        candidate_id: UUID,
        body: CreateContext,
        *,
        now: datetime | None = None,
    ) -> Context:
        """Create a new context for the given operation.

        Returns the frozen ``Context`` contract shape.  The context
        expires after a fixed TTL from creation.
        """
        occurred_at = now or datetime.now(UTC)
        context_id = uuid4()
        expires_at = occurred_at + _CONTEXT_TTL

        # Build source version snapshot from the request body.
        snapshot = SourceVersionSnapshot(
            job=body.job_version,
            profile=None,
            resume=None,
            evidence=None,
        )

        # Compute source digests as SHA-256 of the version snapshot JSON.
        snapshot_bytes = snapshot.model_dump_json().encode()
        job_digest = hashlib.sha256(
            b"job:" + str(body.job_id).encode() + b":" + snapshot_bytes
        ).hexdigest()

        digests = SourceDigests(
            job=job_digest,
            profile=None,
            resume=None,
        )

        record = ContextRecord(
            context_id=context_id,
            operation=body.operation,
            candidate_id=candidate_id,
            schema_version=_SCHEMA_VERSION,
            source_version_snapshot=snapshot,
            source_digests=digests,
            allowed_operation=body.operation,
            created_at=occurred_at,
            expires_at=expires_at,
            state="active",
            source_refs=tuple(body.source_refs),
        )
        self._contexts[context_id] = record
        return self._to_contract(record)

    def get(
        self,
        candidate_id: UUID,
        context_id: UUID,
        *,
        now: datetime | None = None,
    ) -> Context:
        """Read a context by ID.  Raises NotFoundError if missing or unowned."""
        record = self._contexts.get(context_id)
        if record is None or record.candidate_id != candidate_id:
            raise NotFoundError("context not found")
        # Check expiry.
        effective_now = now or datetime.now(UTC)
        if record.expires_at is not None and effective_now > record.expires_at:
            raise NotFoundError("context has expired")
        return self._to_contract(record)

    def invalidate(
        self,
        candidate_id: UUID,
        context_id: UUID,
        *,
        reason: str = "invalidated",
    ) -> None:
        """Invalidate (soft-delete) a context."""
        record = self._contexts.get(context_id)
        if record is None or record.candidate_id != candidate_id:
            raise NotFoundError("context not found")
        self._contexts[context_id] = ContextRecord(
            context_id=record.context_id,
            operation=record.operation,
            candidate_id=record.candidate_id,
            schema_version=record.schema_version,
            source_version_snapshot=record.source_version_snapshot,
            source_digests=record.source_digests,
            allowed_operation=record.allowed_operation,
            created_at=record.created_at,
            expires_at=record.expires_at,
            state="invalidated",
            invalidation_reason=reason,
            source_refs=record.source_refs,
        )

    def resolve_record(
        self,
        candidate_id: UUID,
        context_id: UUID,
    ) -> ContextRecord:
        """Return the raw record.  Raises NotFoundError if missing or unowned."""
        record = self._contexts.get(context_id)
        if record is None or record.candidate_id != candidate_id:
            raise NotFoundError("context not found")
        return record

    @staticmethod
    def _to_contract(record: ContextRecord) -> Context:
        return Context(
            context_id=str(record.context_id),
            operation=record.operation,
            schema_version=record.schema_version,
            source_version_snapshot=record.source_version_snapshot,
            source_digests=record.source_digests,
            allowed_operation=record.allowed_operation,
            created_at=record.created_at,
            expires_at=record.expires_at,
            state=record.state,
            invalidation_reason=record.invalidation_reason,
        )
