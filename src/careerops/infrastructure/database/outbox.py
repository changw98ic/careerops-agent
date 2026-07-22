from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, Engine, RowMapping

from careerops.application.outbox import (
    ClaimedOutboxEvent,
    PendingOutboxEvent,
)
from careerops.infrastructure.database.schema import outbox_events

_OWNER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")
_EVENT_KEY = re.compile(r"^[A-Za-z0-9._:/-]{1,255}$")
_M0_EVENT_TYPES = ("workflow_signal", "internal_notification")


class OutboxLeaseLostError(RuntimeError):
    pass


class PostgresOutboxRepository:
    """Outbox operations scoped to the caller's explicit transaction."""

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError("outbox operations require an explicit database transaction")
        self._connection = connection

    def enqueue(self, event: PendingOutboxEvent) -> None:
        if not _EVENT_KEY.fullmatch(event.event_key):
            raise ValueError("event_key must be a bounded machine identifier")
        self._require_aware(event.available_at, "available_at")
        self._connection.execute(
            sa.insert(outbox_events).values(
                id=event.event_id,
                event_key=event.event_key,
                action_intent_id=event.action_intent_id,
                payload_version_id=event.payload_version_id,
                event_type=event.event_type.value,
                available_at=event.available_at,
            )
        )

    def enqueue_once(self, event: PendingOutboxEvent) -> UUID:
        """Create an internal event once, or return the matching prior event.

        This is deliberately narrower than ``enqueue``: a caller must provide a stable event
        key and can only replay the same intent/payload/event-type tuple. It is used by the
        synthetic dispatch reservation so a crashed caller cannot consume another cap slot or
        produce a second internal workflow signal on retry.
        """

        if not _EVENT_KEY.fullmatch(event.event_key):
            raise ValueError("event_key must be a bounded machine identifier")
        self._require_aware(event.available_at, "available_at")
        event_id = self._connection.scalar(
            postgresql.insert(outbox_events)
            .values(
                id=event.event_id,
                event_key=event.event_key,
                action_intent_id=event.action_intent_id,
                payload_version_id=event.payload_version_id,
                event_type=event.event_type.value,
                available_at=event.available_at,
            )
            .on_conflict_do_nothing(index_elements=[outbox_events.c.event_key])
            .returning(outbox_events.c.id)
        )
        if isinstance(event_id, UUID):
            return event_id

        existing = (
            self._connection.execute(
                sa.select(
                    outbox_events.c.id,
                    outbox_events.c.action_intent_id,
                    outbox_events.c.payload_version_id,
                    outbox_events.c.event_type,
                ).where(outbox_events.c.event_key == event.event_key)
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:
            raise RuntimeError("outbox idempotency conflict did not yield an existing event")
        if (
            existing["action_intent_id"] != event.action_intent_id
            or existing["payload_version_id"] != event.payload_version_id
            or existing["event_type"] != event.event_type.value
        ):
            raise ValueError("outbox event key is already bound to a different event identity")
        return cast("UUID", existing["id"])

    def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
        event_key_prefix: str | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        self._validate_claim(owner=owner, now=now, lease_for=lease_for, limit=limit)
        if event_key_prefix is not None and not _EVENT_KEY.fullmatch(event_key_prefix):
            raise ValueError("event_key_prefix must be a bounded machine identifier")
        lease_until = now + lease_for
        lease_token = uuid4()
        predicates = [
            outbox_events.c.event_type.in_(_M0_EVENT_TYPES),
            sa.or_(
                sa.and_(
                    outbox_events.c.status == "pending",
                    outbox_events.c.available_at <= now,
                ),
                sa.and_(
                    outbox_events.c.status == "leased",
                    outbox_events.c.lease_until <= now,
                ),
            ),
        ]
        if event_key_prefix is not None:
            predicates.append(
                outbox_events.c.event_key.startswith(event_key_prefix, autoescape=True)
            )
        candidates = (
            sa.select(outbox_events.c.id)
            .where(*predicates)
            .order_by(outbox_events.c.available_at, outbox_events.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("claimable_outbox_events")
        )
        statement = (
            sa.update(outbox_events)
            .where(outbox_events.c.id.in_(sa.select(candidates.c.id)))
            .values(
                status="leased",
                lease_owner=owner,
                lease_token=lease_token,
                lease_until=lease_until,
                attempt_count=outbox_events.c.attempt_count + 1,
                last_error_code=None,
            )
            .returning(
                outbox_events.c.id,
                outbox_events.c.event_key,
                outbox_events.c.action_intent_id,
                outbox_events.c.payload_version_id,
                outbox_events.c.event_type,
                outbox_events.c.available_at,
                outbox_events.c.attempt_count,
                outbox_events.c.lease_token,
                outbox_events.c.lease_until,
            )
        )
        rows = self._connection.execute(statement).mappings()
        events = tuple(self._to_claimed(row) for row in rows)
        return tuple(sorted(events, key=lambda event: (event.available_at, str(event.event_id))))

    def mark_published(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
    ) -> None:
        self._validate_owner_and_time(owner, now)
        self._bind_lease(owner=owner, lease_token=lease_token)
        result = self._connection.execute(
            sa.update(outbox_events)
            .where(
                outbox_events.c.id == event_id,
                outbox_events.c.status == "leased",
                outbox_events.c.lease_owner == owner,
                outbox_events.c.lease_token == lease_token,
                outbox_events.c.lease_until > now,
            )
            .values(
                status="published",
                lease_owner=None,
                lease_token=None,
                lease_until=None,
                published_at=now,
                last_error_code=None,
            )
        )
        self._require_updated(result.rowcount)

    def release(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
        retry_at: datetime,
        error_code: str,
        terminal: bool,
    ) -> None:
        self._validate_owner_and_time(owner, now)
        self._require_aware(retry_at, "retry_at")
        if retry_at < now:
            raise ValueError("retry_at must not precede now")
        if not _ERROR_CODE.fullmatch(error_code):
            raise ValueError("error_code must be a bounded machine code")
        self._bind_lease(owner=owner, lease_token=lease_token)
        result = self._connection.execute(
            sa.update(outbox_events)
            .where(
                outbox_events.c.id == event_id,
                outbox_events.c.status == "leased",
                outbox_events.c.lease_owner == owner,
                outbox_events.c.lease_token == lease_token,
                outbox_events.c.lease_until > now,
            )
            .values(
                status="failed" if terminal else "pending",
                available_at=retry_at,
                lease_owner=None,
                lease_token=None,
                lease_until=None,
                last_error_code=error_code,
            )
        )
        self._require_updated(result.rowcount)

    @staticmethod
    def _to_claimed(row: RowMapping) -> ClaimedOutboxEvent:
        return ClaimedOutboxEvent(
            event_id=cast("UUID", row["id"]),
            event_key=cast("str", row["event_key"]),
            action_intent_id=cast("UUID", row["action_intent_id"]),
            payload_version_id=cast("UUID", row["payload_version_id"]),
            event_type=cast("str", row["event_type"]),
            available_at=cast("datetime", row["available_at"]),
            attempt_count=cast("int", row["attempt_count"]),
            lease_token=cast("UUID", row["lease_token"]),
            lease_until=cast("datetime", row["lease_until"]),
        )

    def _bind_lease(self, *, owner: str, lease_token: UUID) -> None:
        self._connection.execute(
            sa.text(
                "SELECT set_config('careerops.outbox_lease_owner', :owner, true), "
                "set_config('careerops.outbox_lease_token', :lease_token, true)"
            ),
            {"owner": owner, "lease_token": str(lease_token)},
        )

    @staticmethod
    def _validate_claim(*, owner: str, now: datetime, lease_for: timedelta, limit: int) -> None:
        PostgresOutboxRepository._validate_owner_and_time(owner, now)
        if lease_for <= timedelta(0) or lease_for > timedelta(minutes=10):
            raise ValueError("lease_for must be positive and at most ten minutes")
        if not 1 <= limit <= 100:
            raise ValueError("claim limit must be between 1 and 100")

    @staticmethod
    def _validate_owner_and_time(owner: str, now: datetime) -> None:
        if not _OWNER.fullmatch(owner):
            raise ValueError("lease owner must be a bounded machine identifier")
        PostgresOutboxRepository._require_aware(now, "now")

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    @staticmethod
    def _require_updated(rowcount: int) -> None:
        if rowcount != 1:
            raise OutboxLeaseLostError("outbox lease is missing, expired, or owned elsewhere")


class PostgresOutboxStore:
    """Commits each publisher state transition before crossing the sink boundary."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def claim(
        self,
        *,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        limit: int,
        event_key_prefix: str | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        with self._engine.begin() as connection:
            return PostgresOutboxRepository(connection).claim(
                owner=owner,
                now=now,
                lease_for=lease_for,
                limit=limit,
                event_key_prefix=event_key_prefix,
            )

    def mark_published(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
    ) -> None:
        with self._engine.begin() as connection:
            PostgresOutboxRepository(connection).mark_published(
                event_id,
                owner=owner,
                lease_token=lease_token,
                now=now,
            )

    def release(
        self,
        event_id: UUID,
        *,
        owner: str,
        lease_token: UUID,
        now: datetime,
        retry_at: datetime,
        error_code: str,
        terminal: bool,
    ) -> None:
        with self._engine.begin() as connection:
            PostgresOutboxRepository(connection).release(
                event_id,
                owner=owner,
                lease_token=lease_token,
                now=now,
                retry_at=retry_at,
                error_code=error_code,
                terminal=terminal,
            )
