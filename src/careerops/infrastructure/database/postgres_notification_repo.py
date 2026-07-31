"""PostgreSQL persistence for recoverable in-app notifications."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.application.notification_service import NotificationEvent
from careerops.infrastructure.database.schema import notification_outbox


class PostgresNotificationRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, event: NotificationEvent) -> NotificationEvent:
        candidate_id = UUID(event.user_id)
        with self._engine.begin() as conn:
            conn.execute(
                pg_insert(notification_outbox)
                .values(
                    id=event.id,
                    candidate_id=candidate_id,
                    event_type=event.event_type,
                    payload=event.payload,
                    created_at=event.created_at,
                    delivered_at=event.created_at if event.delivered else None,
                )
                .on_conflict_do_nothing(index_elements=[notification_outbox.c.id])
            )
        return event

    def list_pending(self, user_id: str) -> list[NotificationEvent]:
        candidate_id = UUID(user_id)
        stmt = (
            sa.select(notification_outbox)
            .where(
                notification_outbox.c.candidate_id == candidate_id,
                notification_outbox.c.delivered_at.is_(None),
            )
            .order_by(notification_outbox.c.created_at)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [
            NotificationEvent(
                id=row["id"],
                user_id=str(row["candidate_id"]),
                event_type=row["event_type"],
                payload=dict(row["payload"]),
                created_at=row["created_at"],
                delivered=False,
            )
            for row in rows
        ]

    def mark_delivered(self, user_id: str, event_id: UUID) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(notification_outbox)
                .where(
                    notification_outbox.c.candidate_id == UUID(user_id),
                    notification_outbox.c.id == event_id,
                    notification_outbox.c.delivered_at.is_(None),
                )
                .values(delivered_at=datetime.now(UTC))
            )
