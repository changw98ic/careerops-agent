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
