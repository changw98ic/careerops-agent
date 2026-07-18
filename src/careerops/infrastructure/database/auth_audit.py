from __future__ import annotations

from uuid import UUID

from sqlalchemy.engine import Engine

from careerops.application.audit import AuditActorType, AuditEventDraft
from careerops.auth.contracts import AuthAuditEvent
from careerops.infrastructure.database.audit import PostgresAuditWriter

_AUTH_SYSTEM_RESOURCE_ID = UUID(int=0)


class PostgresAuthAuditSink:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, event: AuthAuditEvent) -> None:
        resource_id = event.session_id or event.user_id or _AUTH_SYSTEM_RESOURCE_ID
        with self._engine.begin() as connection:
            PostgresAuditWriter(connection).append(
                AuditEventDraft(
                    actor_type=(
                        AuditActorType.USER if event.user_id is not None else AuditActorType.WORKER
                    ),
                    actor_id=str(event.user_id) if event.user_id is not None else "console-auth",
                    event_type=f"auth_{event.action.value}_{event.outcome.value}",
                    resource_type="console_session" if event.session_id else "console_auth",
                    resource_id=resource_id,
                    trace_id=event.trace_id,
                    occurred_at=event.occurred_at,
                    event_data={
                        "reason_code": event.reason_code,
                        "subject_hash": event.subject_hash,
                    },
                )
            )
