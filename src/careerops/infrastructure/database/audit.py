from __future__ import annotations

import json
from typing import cast
from uuid import UUID

from sqlalchemy import Connection, text

from careerops.application.audit import (
    AppendedAuditEvent,
    AuditEventDraft,
)


class PostgresAuditWriter:
    """Append one audit event through the database-owned hash-chain function."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def append(self, draft: AuditEventDraft) -> AppendedAuditEvent:
        row = self._connection.execute(
            text(
                """
                SELECT sequence, event_id, previous_hash, event_hash
                FROM careerops.append_audit_event(
                    :event_id,
                    :occurred_at,
                    :actor_type,
                    :actor_id,
                    :event_type,
                    :resource_type,
                    :resource_id,
                    :trace_id,
                    CAST(:event_data AS jsonb)
                )
                """
            ),
            {
                "event_id": draft.event_id,
                "occurred_at": draft.occurred_at,
                "actor_type": draft.actor_type.value,
                "actor_id": draft.actor_id,
                "event_type": draft.event_type,
                "resource_type": draft.resource_type,
                "resource_id": draft.resource_id,
                "trace_id": draft.trace_id,
                "event_data": json.dumps(
                    draft.event_data,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        )
        appended = row.mappings().one_or_none()
        if appended is None:
            raise RuntimeError("database did not return the appended audit event")
        return AppendedAuditEvent(
            sequence=cast("int", appended["sequence"]),
            event_id=cast("UUID", appended["event_id"]),
            previous_hash=cast("str | None", appended["previous_hash"]),
            event_hash=cast("str", appended["event_hash"]),
        )


class PostgresAuditWriterEngine:
    """Engine-level AuditWriter that delegates to PostgresAuditWriter per connection.

    Implements the ``AuditWriter`` protocol (append -> int, list_for_resource)
    so the ``SideEffectKernel`` can use a persistent append-only audit in
    production instead of ``InMemoryAuditWriter``.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def append(self, draft: AuditEventDraft) -> int:
        with self._engine.begin() as conn:
            result = PostgresAuditWriter(conn).append(draft)
            return result.sequence

    def list_for_resource(
        self, resource_type: str, resource_id: UUID
    ) -> tuple[AuditEventDraft, ...]:
        import sqlalchemy as sa

        from careerops.infrastructure.database.schema import audit_events

        stmt = (
            sa.select(audit_events)
            .where(
                sa.and_(
                    audit_events.c.resource_type == resource_type,
                    audit_events.c.resource_id == resource_id,
                )
            )
            .order_by(audit_events.c.sequence)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return tuple(
            AuditEventDraft(
                event_type=row["event_type"],
                actor_type=row["actor_type"],
                resource_type=row["resource_type"],
                resource_id=row["resource_id"],
                trace_id=row["trace_id"],
                actor_id=row.get("actor_id"),
                event_data=row.get("event_data") or {},
                occurred_at=row["occurred_at"],
                event_id=row["event_id"],
            )
            for row in rows
        )
