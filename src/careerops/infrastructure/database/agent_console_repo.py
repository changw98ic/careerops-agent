"""PostgreSQL repositories for the Agent Console orchestration tables.

Four repositories covering the agent-console persistence layer:

* ``AgentContextRepository`` -- create, read, invalidate, staleness check
* ``AgentActionRepository`` -- upsert actions, query queue, transition state
* ``AgentAttemptRepository`` -- create attempt, acquire/renew lease, CAS ops
* ``AgentStageEventRepository`` -- append-only stage events with dedup key

Each repository:
- Uses the synchronous SQLAlchemy pattern from ``postgres_agent_run_repo.py``
- Scopes **all** queries by ``candidate_id`` (no cross-candidate reads)
- Uses the composite ``(id, candidate_id)`` FK pattern
- Returns domain objects, not raw rows
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.agent_console.contracts import (
    ActionKind,
    ActionState,
    ModelOperation,
    SourceDigests,
    SourceVersionSnapshot,
)
from careerops.infrastructure.database.schema import (
    agent_actions,
    agent_attempts,
    agent_contexts,
    agent_stage_events,
)

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

__all__ = [
    "AgentActionRecord",
    "AgentActionRepository",
    "AgentAttemptRecord",
    "AgentAttemptRepository",
    "AgentContextRecord",
    "AgentContextRepository",
    "AgentStageEventRecord",
    "AgentStageEventRepository",
]


# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentContextRecord:
    """Domain object for a persisted agent context."""

    id: UUID
    candidate_id: UUID
    operation: ModelOperation
    schema_version: str
    digest_algorithm: str
    source_version_snapshot: SourceVersionSnapshot
    source_digests: SourceDigests
    allowed_operation: ModelOperation
    state: str
    created_at: datetime
    expires_at: datetime
    invalidation_reason: str | None = None


@dataclass(frozen=True, slots=True)
class AgentActionRecord:
    """Domain object for a persisted agent action."""

    id: UUID
    candidate_id: UUID
    action_key: str
    source_event_key: UUID
    queue_version: int
    state: ActionState
    kind: ActionKind
    reason_code: str
    deterministic_rank: int
    source_refs: tuple[dict[str, str], ...]
    context_id: UUID | None = None
    snooze_until: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AgentAttemptRecord:
    """Domain object for a persisted agent attempt."""

    id: UUID
    run_id: UUID
    candidate_id: UUID
    attempt_no: int
    workflow_id: str
    worker_id: UUID | None = None
    lease_epoch: int = 0
    cancel_epoch: int = 0
    state: str = "queued"
    retry_budget: int = 0
    dead_lettered_at: datetime | None = None
    dead_letter_reason: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AgentStageEventRecord:
    """Domain object for a persisted agent stage event."""

    id: UUID
    attempt_id: UUID
    run_id: UUID
    candidate_id: UUID
    event_key: UUID
    sequence: int
    schema_version: str
    stage: str
    status: str
    terminal: bool = False
    retryable: bool = False
    cause: str | None = None
    provider_state: str | None = None
    occurred_at: datetime | None = None
    duration_ms: int | None = None
    redacted_payload: dict[str, object] | None = None
    retention_until: datetime | None = None


# ---------------------------------------------------------------------------
# Row-to-domain mappers
# ---------------------------------------------------------------------------


def _context(row: sa.RowMapping) -> AgentContextRecord:
    raw_snap = row.get("source_version_snapshot")
    snapshot = (
        SourceVersionSnapshot.model_validate(raw_snap)
        if isinstance(raw_snap, dict)
        else SourceVersionSnapshot()
    )
    raw_dig = row.get("source_digests")
    digests = (
        SourceDigests.model_validate(raw_dig) if isinstance(raw_dig, dict) else SourceDigests()
    )
    return AgentContextRecord(
        id=cast(UUID, row["id"]),
        candidate_id=cast(UUID, row["candidate_id"]),
        operation=ModelOperation(str(row["operation"])),
        schema_version=str(row["schema_version"]),
        digest_algorithm=str(row["digest_algorithm"]),
        source_version_snapshot=snapshot,
        source_digests=digests,
        allowed_operation=ModelOperation(str(row["allowed_operation"])),
        state=str(row["state"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        invalidation_reason=row.get("invalidation_reason"),
    )


def _action(row: sa.RowMapping) -> AgentActionRecord:
    raw_refs = row.get("source_refs")
    source_refs = (
        tuple(dict(r) for r in raw_refs if isinstance(r, dict))
        if isinstance(raw_refs, list)
        else ()
    )
    return AgentActionRecord(
        id=cast(UUID, row["id"]),
        candidate_id=cast(UUID, row["candidate_id"]),
        action_key=str(row["action_key"]),
        source_event_key=cast(UUID, row["source_event_key"]),
        queue_version=int(row["queue_version"]),
        state=ActionState(str(row["state"])),
        kind=ActionKind(str(row["kind"])),
        reason_code=str(row["reason_code"]),
        deterministic_rank=int(row["deterministic_rank"]),
        source_refs=source_refs,
        context_id=cast(UUID, row["context_id"]) if row.get("context_id") else None,
        snooze_until=row.get("snooze_until"),
        expires_at=row.get("expires_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _attempt(row: sa.RowMapping) -> AgentAttemptRecord:
    return AgentAttemptRecord(
        id=cast(UUID, row["id"]),
        run_id=cast(UUID, row["run_id"]),
        candidate_id=cast(UUID, row["candidate_id"]),
        attempt_no=int(row["attempt_no"]),
        workflow_id=str(row["workflow_id"]),
        worker_id=cast(UUID, row["worker_id"]) if row.get("worker_id") else None,
        lease_epoch=int(row["lease_epoch"]),
        cancel_epoch=int(row["cancel_epoch"]),
        state=str(row["state"]),
        retry_budget=int(row["retry_budget"]),
        dead_lettered_at=row.get("dead_lettered_at"),
        dead_letter_reason=row.get("dead_letter_reason"),
        lease_expires_at=row.get("lease_expires_at"),
        created_at=row.get("created_at"),
        finished_at=row.get("finished_at"),
    )


def _stage_event(row: sa.RowMapping) -> AgentStageEventRecord:
    raw_payload = row.get("redacted_payload")
    payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
    return AgentStageEventRecord(
        id=cast(UUID, row["id"]),
        attempt_id=cast(UUID, row["attempt_id"]),
        run_id=cast(UUID, row["run_id"]),
        candidate_id=cast(UUID, row["candidate_id"]),
        event_key=cast(UUID, row["event_key"]),
        sequence=int(row["sequence"]),
        schema_version=str(row["schema_version"]),
        stage=str(row["stage"]),
        status=str(row["status"]),
        terminal=bool(row.get("terminal", False)),
        retryable=bool(row.get("retryable", False)),
        cause=row.get("cause"),
        provider_state=row.get("provider_state"),
        occurred_at=row.get("occurred_at"),
        duration_ms=int(row["duration_ms"]) if row.get("duration_ms") is not None else None,
        redacted_payload=payload,
        retention_until=row.get("retention_until"),
    )


# ---------------------------------------------------------------------------
# 1. AgentContextRepository
# ---------------------------------------------------------------------------


class AgentContextRepository:
    """Create, read, invalidate, and check staleness for agent contexts."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(self, record: AgentContextRecord) -> AgentContextRecord:
        """Insert a new context row.  Returns the stored record."""
        with self._engine.begin() as conn:
            conn.execute(
                agent_contexts.insert().values(
                    id=record.id,
                    candidate_id=record.candidate_id,
                    operation=record.operation.value,
                    schema_version=record.schema_version,
                    digest_algorithm=record.digest_algorithm,
                    source_version_snapshot=record.source_version_snapshot.model_dump(mode="json"),
                    source_digests=record.source_digests.model_dump(mode="json"),
                    allowed_operation=record.allowed_operation.value,
                    state=record.state,
                    created_at=record.created_at,
                    expires_at=record.expires_at,
                    invalidation_reason=record.invalidation_reason,
                )
            )
        stored = self.get(record.candidate_id, record.id)
        if stored is None:  # pragma: no cover - database invariant
            raise RuntimeError("agent context was not persisted")
        return stored

    def get(self, candidate_id: UUID, context_id: UUID) -> AgentContextRecord | None:
        """Read a single context by composite (id, candidate_id)."""
        stmt = sa.select(agent_contexts).where(
            sa.and_(
                agent_contexts.c.id == context_id,
                agent_contexts.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _context(row) if row is not None else None

    def invalidate(
        self,
        candidate_id: UUID,
        context_id: UUID,
        *,
        reason: str = "invalidated",
    ) -> AgentContextRecord | None:
        """Transition a context to ``stale`` state with a reason.

        Returns the updated record, or ``None`` if not found.
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(agent_contexts)
                .where(
                    sa.and_(
                        agent_contexts.c.id == context_id,
                        agent_contexts.c.candidate_id == candidate_id,
                        agent_contexts.c.state == "active",
                    )
                )
                .values(state="stale", invalidation_reason=reason)
            )
            if result.rowcount == 0:
                return None
        return self.get(candidate_id, context_id)

    def is_stale(
        self,
        candidate_id: UUID,
        context_id: UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return ``True`` if the context is expired, invalidated, or missing."""
        from datetime import UTC

        record = self.get(candidate_id, context_id)
        if record is None:
            return True
        if record.state != "active":
            return True
        effective_now = now or datetime.now(UTC)
        return effective_now > record.expires_at


# ---------------------------------------------------------------------------
# 2. AgentActionRepository
# ---------------------------------------------------------------------------


class AgentActionRepository:
    """Upsert actions, query the queue, and transition action state."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def upsert(self, record: AgentActionRecord) -> AgentActionRecord:
        """Insert or update an action (matched by candidate_id + action_key + source_event_key).

        On conflict the mutable columns (state, queue_version, snooze_until,
        expires_at, updated_at) are updated to the new values.
        """
        now = record.updated_at or record.created_at
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(agent_actions)
                .values(
                    id=record.id,
                    candidate_id=record.candidate_id,
                    action_key=record.action_key,
                    source_event_key=record.source_event_key,
                    queue_version=record.queue_version,
                    state=record.state.value,
                    kind=record.kind.value,
                    reason_code=record.reason_code,
                    deterministic_rank=record.deterministic_rank,
                    source_refs=[dict(r) for r in record.source_refs],
                    context_id=str(record.context_id) if record.context_id else None,
                    snooze_until=record.snooze_until,
                    expires_at=record.expires_at,
                    created_at=record.created_at,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[
                        agent_actions.c.candidate_id,
                        agent_actions.c.action_key,
                        agent_actions.c.source_event_key,
                    ],
                    set_={
                        "state": record.state.value,
                        "queue_version": record.queue_version,
                        "snooze_until": record.snooze_until,
                        "expires_at": record.expires_at,
                        "updated_at": now,
                    },
                )
            )
        stored = self.get(record.candidate_id, record.id)
        if stored is None:  # pragma: no cover - database invariant
            raise RuntimeError("agent action was not persisted")
        return stored

    def get(self, candidate_id: UUID, action_id: UUID) -> AgentActionRecord | None:
        """Read a single action by composite (id, candidate_id)."""
        stmt = sa.select(agent_actions).where(
            sa.and_(
                agent_actions.c.id == action_id,
                agent_actions.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _action(row) if row is not None else None

    def find_by_key(self, candidate_id: UUID, action_key: str) -> AgentActionRecord | None:
        """Look up an action by its deterministic key."""
        stmt = (
            sa.select(agent_actions)
            .where(
                sa.and_(
                    agent_actions.c.candidate_id == candidate_id,
                    agent_actions.c.action_key == action_key,
                )
            )
            .order_by(agent_actions.c.created_at.desc())
            .limit(1)
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _action(row) if row is not None else None

    def list_queue(
        self,
        candidate_id: UUID,
        *,
        states: tuple[ActionState, ...] | None = None,
        limit: int = 3,
    ) -> list[AgentActionRecord]:
        """List actions for a candidate, optionally filtered by state.

        Ordered by ``deterministic_rank ASC`` then ``created_at DESC``.
        Hard-capped at ``limit`` (default 3, matching the API contract).
        """
        stmt = sa.select(agent_actions).where(agent_actions.c.candidate_id == candidate_id)
        if states is not None:
            stmt = stmt.where(agent_actions.c.state.in_([s.value for s in states]))
        stmt = stmt.order_by(
            agent_actions.c.deterministic_rank.asc(),
            agent_actions.c.created_at.desc(),
        ).limit(limit)
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_action(row) for row in rows]

    def transition_state(
        self,
        candidate_id: UUID,
        action_id: UUID,
        *,
        from_state: ActionState,
        to_state: ActionState,
        now: datetime | None = None,
        snooze_until: datetime | None = None,
    ) -> AgentActionRecord | None:
        """CAS-style state transition: only succeeds if current state matches ``from_state``.

        Returns the updated record, or ``None`` if the action was not in the
        expected state (stale conflict).
        """
        from datetime import UTC

        effective_now = now or datetime.now(UTC)
        values: dict[str, Any] = {
            "state": to_state.value,
            "updated_at": effective_now,
        }
        if snooze_until is not None:
            values["snooze_until"] = snooze_until

        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(agent_actions)
                .where(
                    sa.and_(
                        agent_actions.c.id == action_id,
                        agent_actions.c.candidate_id == candidate_id,
                        agent_actions.c.state == from_state.value,
                    )
                )
                .values(**values)
            )
            if result.rowcount == 0:
                return None
        return self.get(candidate_id, action_id)

    def accept(
        self, candidate_id: UUID, action_id: UUID, *, now: datetime | None = None
    ) -> AgentActionRecord | None:
        """Transition from proposed to accepted."""
        return self.transition_state(
            candidate_id,
            action_id,
            from_state=ActionState.PROPOSED,
            to_state=ActionState.ACCEPTED,
            now=now,
        )

    def snooze(
        self,
        candidate_id: UUID,
        action_id: UUID,
        *,
        until: datetime,
        now: datetime | None = None,
    ) -> AgentActionRecord | None:
        """Transition from proposed to snoozed with a snooze-until timestamp."""
        return self.transition_state(
            candidate_id,
            action_id,
            from_state=ActionState.PROPOSED,
            to_state=ActionState.SNOOZED,
            now=now,
            snooze_until=until,
        )

    def dismiss(
        self, candidate_id: UUID, action_id: UUID, *, now: datetime | None = None
    ) -> AgentActionRecord | None:
        """Transition from proposed to dismissed."""
        return self.transition_state(
            candidate_id,
            action_id,
            from_state=ActionState.PROPOSED,
            to_state=ActionState.DISMISSED,
            now=now,
        )

    def complete(
        self, candidate_id: UUID, action_id: UUID, *, now: datetime | None = None
    ) -> AgentActionRecord | None:
        """Transition from accepted to completed."""
        return self.transition_state(
            candidate_id,
            action_id,
            from_state=ActionState.ACCEPTED,
            to_state=ActionState.COMPLETED,
            now=now,
        )


# ---------------------------------------------------------------------------
# 3. AgentAttemptRepository
# ---------------------------------------------------------------------------


class AgentAttemptRepository:
    """Create attempts, acquire/renew leases, and perform CAS operations."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(self, record: AgentAttemptRecord) -> AgentAttemptRecord:
        """Insert a new attempt row.  Returns the stored record."""
        with self._engine.begin() as conn:
            conn.execute(
                agent_attempts.insert().values(
                    id=record.id,
                    run_id=record.run_id,
                    candidate_id=record.candidate_id,
                    attempt_no=record.attempt_no,
                    workflow_id=record.workflow_id,
                    worker_id=record.worker_id,
                    lease_epoch=record.lease_epoch,
                    cancel_epoch=record.cancel_epoch,
                    state=record.state,
                    retry_budget=record.retry_budget,
                    dead_lettered_at=record.dead_lettered_at,
                    dead_letter_reason=record.dead_letter_reason,
                    lease_expires_at=record.lease_expires_at,
                    created_at=record.created_at,
                    finished_at=record.finished_at,
                )
            )
        stored = self.get(record.candidate_id, record.id)
        if stored is None:  # pragma: no cover - database invariant
            raise RuntimeError("agent attempt was not persisted")
        return stored

    def get(self, candidate_id: UUID, attempt_id: UUID) -> AgentAttemptRecord | None:
        """Read a single attempt by composite (id, candidate_id)."""
        stmt = sa.select(agent_attempts).where(
            sa.and_(
                agent_attempts.c.id == attempt_id,
                agent_attempts.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _attempt(row) if row is not None else None

    def get_by_run(
        self, candidate_id: UUID, run_id: UUID, attempt_no: int
    ) -> AgentAttemptRecord | None:
        """Read an attempt by (run_id, attempt_no, candidate_id)."""
        stmt = sa.select(agent_attempts).where(
            sa.and_(
                agent_attempts.c.run_id == run_id,
                agent_attempts.c.attempt_no == attempt_no,
                agent_attempts.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _attempt(row) if row is not None else None

    def list_for_run(self, candidate_id: UUID, run_id: UUID) -> list[AgentAttemptRecord]:
        """List all attempts for a run, ordered by attempt_no."""
        stmt = (
            sa.select(agent_attempts)
            .where(
                sa.and_(
                    agent_attempts.c.run_id == run_id,
                    agent_attempts.c.candidate_id == candidate_id,
                )
            )
            .order_by(agent_attempts.c.attempt_no)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_attempt(row) for row in rows]

    def acquire_lease(
        self,
        candidate_id: UUID,
        attempt_id: UUID,
        *,
        worker_id: UUID,
        lease_expires_at: datetime,
        now: datetime | None = None,
    ) -> AgentAttemptRecord | None:
        """Atomically acquire a lease on a queued attempt.

        Only succeeds if the attempt is in ``queued`` state.  Increments
        ``lease_epoch`` and sets the worker + expiry.  Returns ``None`` if
        the attempt was not in ``queued`` state.
        """
        with self._engine.begin() as conn:
            # Read current lease_epoch inside the transaction for CAS.
            current = conn.execute(
                sa.select(agent_attempts.c.lease_epoch).where(
                    sa.and_(
                        agent_attempts.c.id == attempt_id,
                        agent_attempts.c.candidate_id == candidate_id,
                        agent_attempts.c.state == "queued",
                    )
                )
            ).first()
            if current is None:
                return None
            new_epoch = int(current[0]) + 1
            conn.execute(
                sa.update(agent_attempts)
                .where(
                    sa.and_(
                        agent_attempts.c.id == attempt_id,
                        agent_attempts.c.candidate_id == candidate_id,
                        agent_attempts.c.state == "queued",
                        agent_attempts.c.lease_epoch == current[0],
                    )
                )
                .values(
                    state="running",
                    worker_id=worker_id,
                    lease_epoch=new_epoch,
                    lease_expires_at=lease_expires_at,
                )
            )
        return self.get(candidate_id, attempt_id)

    def renew_lease(
        self,
        candidate_id: UUID,
        attempt_id: UUID,
        *,
        worker_id: UUID,
        lease_expires_at: datetime,
        expected_lease_epoch: int,
    ) -> AgentAttemptRecord | None:
        """Renew a lease if the epoch still matches (CAS).

        Returns ``None`` if the epoch has moved (another worker stole the
        lease) or the attempt is no longer running.
        """
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(agent_attempts)
                .where(
                    sa.and_(
                        agent_attempts.c.id == attempt_id,
                        agent_attempts.c.candidate_id == candidate_id,
                        agent_attempts.c.state == "running",
                        agent_attempts.c.worker_id == worker_id,
                        agent_attempts.c.lease_epoch == expected_lease_epoch,
                    )
                )
                .values(lease_expires_at=lease_expires_at)
            )
            if result.rowcount == 0:
                return None
        return self.get(candidate_id, attempt_id)

    def transition_state(
        self,
        candidate_id: UUID,
        attempt_id: UUID,
        *,
        from_state: str,
        to_state: str,
        expected_lease_epoch: int | None = None,
        now: datetime | None = None,
    ) -> AgentAttemptRecord | None:
        """CAS-style state transition on an attempt.

        If ``expected_lease_epoch`` is provided, the update also checks the
        epoch (optimistic concurrency).  Returns ``None`` if the attempt was
        not in the expected state (or epoch mismatch).
        """
        from datetime import UTC

        effective_now = now or datetime.now(UTC)
        conditions = [
            agent_attempts.c.id == attempt_id,
            agent_attempts.c.candidate_id == candidate_id,
            agent_attempts.c.state == from_state,
        ]
        if expected_lease_epoch is not None:
            conditions.append(agent_attempts.c.lease_epoch == expected_lease_epoch)
        values: dict[str, Any] = {
            "state": to_state,
        }
        if to_state in ("succeeded", "failed", "cancelled", "stale"):
            values["finished_at"] = effective_now

        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(agent_attempts).where(sa.and_(*conditions)).values(**values)
            )
            if result.rowcount == 0:
                return None
        return self.get(candidate_id, attempt_id)


# ---------------------------------------------------------------------------
# 4. AgentStageEventRepository
# ---------------------------------------------------------------------------


class AgentStageEventRepository:
    """Append-only stage event store with dedup-key support."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def append(self, record: AgentStageEventRecord) -> AgentStageEventRecord:
        """Append a stage event.  Deduplicates on ``event_key``.

        If an event with the same ``event_key`` already exists, the existing
        record is returned unchanged (idempotent append).
        """
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(agent_stage_events)
                .values(
                    id=record.id,
                    attempt_id=record.attempt_id,
                    run_id=record.run_id,
                    candidate_id=record.candidate_id,
                    event_key=record.event_key,
                    sequence=record.sequence,
                    schema_version=record.schema_version,
                    stage=record.stage,
                    status=record.status,
                    terminal=record.terminal,
                    retryable=record.retryable,
                    cause=record.cause,
                    provider_state=record.provider_state,
                    occurred_at=record.occurred_at,
                    duration_ms=record.duration_ms,
                    redacted_payload=record.redacted_payload or {},
                    retention_until=record.retention_until,
                )
                .on_conflict_do_nothing(index_elements=[agent_stage_events.c.event_key])
            )
        # Return the stored (or existing) record.
        stored = self.get_by_event_key(record.candidate_id, record.event_key)
        if stored is None:  # pragma: no cover - database invariant
            raise RuntimeError("agent stage event was not persisted")
        return stored

    def get(self, candidate_id: UUID, event_id: UUID) -> AgentStageEventRecord | None:
        """Read a single stage event by composite (id, candidate_id)."""
        stmt = sa.select(agent_stage_events).where(
            sa.and_(
                agent_stage_events.c.id == event_id,
                agent_stage_events.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _stage_event(row) if row is not None else None

    def get_by_event_key(self, candidate_id: UUID, event_key: UUID) -> AgentStageEventRecord | None:
        """Look up a stage event by its dedup key."""
        stmt = sa.select(agent_stage_events).where(
            sa.and_(
                agent_stage_events.c.event_key == event_key,
                agent_stage_events.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return _stage_event(row) if row is not None else None

    def list_for_attempt(
        self,
        candidate_id: UUID,
        attempt_id: UUID,
        *,
        limit: int = 100,
    ) -> list[AgentStageEventRecord]:
        """List stage events for an attempt, ordered by sequence."""
        stmt = (
            sa.select(agent_stage_events)
            .where(
                sa.and_(
                    agent_stage_events.c.attempt_id == attempt_id,
                    agent_stage_events.c.candidate_id == candidate_id,
                )
            )
            .order_by(agent_stage_events.c.sequence)
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_stage_event(row) for row in rows]

    def list_for_run(
        self,
        candidate_id: UUID,
        run_id: UUID,
        *,
        limit: int = 200,
    ) -> list[AgentStageEventRecord]:
        """List stage events for a run (across all attempts), ordered by occurred_at."""
        stmt = (
            sa.select(agent_stage_events)
            .where(
                sa.and_(
                    agent_stage_events.c.run_id == run_id,
                    agent_stage_events.c.candidate_id == candidate_id,
                )
            )
            .order_by(agent_stage_events.c.occurred_at, agent_stage_events.c.sequence)
            .limit(limit)
        )
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_stage_event(row) for row in rows]

    def next_sequence(self, candidate_id: UUID, attempt_id: UUID) -> int:
        """Return the next sequence number for an attempt (max + 1, or 1 if empty)."""
        stmt = sa.select(sa.func.max(agent_stage_events.c.sequence)).where(
            sa.and_(
                agent_stage_events.c.attempt_id == attempt_id,
                agent_stage_events.c.candidate_id == candidate_id,
            )
        )
        with self._engine.begin() as conn:
            row = conn.execute(stmt).first()
        if row is None or row[0] is None:
            return 1
        return int(row[0]) + 1
