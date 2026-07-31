"""Postgres-backed Tier 2 budget coordinator (real-autonomous-career-loop Phase 7.1).

Drop-in replacement for the in-memory ``Tier2Budget`` that persists leases
and daily consumption counters in Postgres so state survives worker restarts.

Uses ``SELECT ... FOR UPDATE`` for atomic slot acquisition and daily counter
updates.  Same fail-closed semantics as the in-memory version.

Tables (migration 0033):
- ``tier2_budget_daily``: one row per UTC day, stores ``consumed`` counter.
- ``tier2_budget_leases``: one row per active lease, stores ``source_id`` +
  ``acquired_at``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from threading import RLock
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from careerops.application.tier2_budget import Tier2Lease
from careerops.infrastructure.database.schema import (
    tier2_budget_daily,
    tier2_budget_leases,
)

__all__ = ["PostgresTier2Budget"]

_SLOT_LOCK_KEY = 0x434F5053  # "COPS"; shared by every worker/database session.
_fallback_lock = RLock()


class PostgresTier2Budget:
    """Postgres-backed Tier 2 budget coordinator.

    Same interface as the in-memory ``Tier2Budget`` but uses Postgres for
    durable state.  All operations are atomic via ``SELECT ... FOR UPDATE``.

    Args:
        engine: SQLAlchemy engine.
        max_concurrent_slots: maximum concurrent Tier 2 runs.
        daily_action_budget: maximum daily browser actions.
        stale_lease_timeout_s: seconds after which a lease is stale.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        max_concurrent_slots: int = 3,
        daily_action_budget: int = 500,
        stale_lease_timeout_s: float = 600.0,
    ) -> None:
        self._engine = engine
        self._max_slots = max_concurrent_slots
        self._daily_budget = daily_action_budget
        self._stale_timeout = stale_lease_timeout_s

    def _lock_slots(self, conn: sa.Connection) -> None:
        """Serialize slot-count changes across PostgreSQL worker processes."""
        if conn.dialect.name == "postgresql":
            conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _SLOT_LOCK_KEY},
            )

    # ------------------------------------------------------------------
    # Slot management
    # ------------------------------------------------------------------

    def acquire(self, source_id: str) -> Tier2Lease | None:
        """Try to acquire a concurrent Tier 2 slot.

        Returns a :class:`Tier2Lease` on success, or ``None`` when all slots
        are occupied.  Atomic via ``SELECT ... FOR UPDATE`` on the leases table.
        """
        now = datetime.now(tz=UTC)
        lease_id = f"t2-{uuid4()}"

        with _fallback_lock, self._engine.begin() as conn:
            self._lock_slots(conn)
            self._ensure_daily_row(conn, now)

            # The transaction-scoped advisory lock serializes this count+insert
            # across every worker, including when the table is initially empty.
            active = conn.execute(
                sa.select(sa.func.count())
                .select_from(tier2_budget_leases)
            ).scalar() or 0

            if active >= self._max_slots:
                return None

            conn.execute(
                tier2_budget_leases.insert().values(
                    lease_id=lease_id,
                    source_id=source_id,
                    acquired_at=now,
                )
            )

        return Tier2Lease(
            lease_id=lease_id,
            source_id=source_id,
            acquired_at=now,
        )

    def release(self, lease: Tier2Lease) -> None:
        """Release a previously acquired slot.  Idempotent."""
        with _fallback_lock, self._engine.begin() as conn:
            self._lock_slots(conn)
            conn.execute(
                tier2_budget_leases.delete().where(
                    tier2_budget_leases.c.lease_id == lease.lease_id
                )
            )

    @property
    def active_slots(self) -> int:
        with _fallback_lock, self._engine.begin() as conn:
            self._lock_slots(conn)
            return conn.execute(
                sa.select(sa.func.count()).select_from(tier2_budget_leases)
            ).scalar() or 0

    @property
    def max_concurrent_slots(self) -> int:
        return self._max_slots

    @property
    def daily_action_budget(self) -> int:
        return self._daily_budget

    @property
    def available_slots(self) -> int:
        return max(0, self._max_slots - self.active_slots)

    # ------------------------------------------------------------------
    # Daily action budget
    # ------------------------------------------------------------------

    def consume(self, count: int = 1) -> int:
        """Consume ``count`` browser actions from the daily budget.

        Returns the actual number consumed.  Returns 0 when the budget is
        fully spent.  Atomic via ``SELECT ... FOR UPDATE``.
        """
        if count <= 0:
            return 0

        now = datetime.now(tz=UTC)
        today = now.date()

        with self._engine.begin() as conn:
            self._ensure_daily_row(conn, now)

            # Lock the daily row.
            row = conn.execute(
                sa.select(tier2_budget_daily.c.consumed)
                .where(tier2_budget_daily.c.day == today)
                .with_for_update()
            ).first()

            consumed_so_far = row[0] if row else 0
            remaining = self._daily_budget - consumed_so_far
            if remaining <= 0:
                return 0

            actual = min(count, remaining)
            conn.execute(
                tier2_budget_daily.update()
                .where(tier2_budget_daily.c.day == today)
                .values(consumed=consumed_so_far + actual)
            )
            return actual

    @property
    def daily_remaining(self) -> int:
        now = datetime.now(tz=UTC)
        today = now.date()
        with self._engine.begin() as conn:
            self._ensure_daily_row(conn, now)
            row = conn.execute(
                sa.select(tier2_budget_daily.c.consumed)
                .where(tier2_budget_daily.c.day == today)
            ).first()
            consumed = row[0] if row else 0
            return max(0, self._daily_budget - consumed)

    @property
    def daily_consumed(self) -> int:
        now = datetime.now(tz=UTC)
        today = now.date()
        with self._engine.begin() as conn:
            self._ensure_daily_row(conn, now)
            row = conn.execute(
                sa.select(tier2_budget_daily.c.consumed)
                .where(tier2_budget_daily.c.day == today)
            ).first()
            return row[0] if row else 0

    # ------------------------------------------------------------------
    # Stale-lease recovery
    # ------------------------------------------------------------------

    def recover_stale_leases(self, active_source_ids: set[str]) -> int:
        """Release leases whose holders are no longer active.

        Called once on worker startup.  Returns the number of stale leases
        recovered.
        """
        now = datetime.now(tz=UTC)
        cutoff = now.timestamp() - self._stale_timeout

        with self._engine.begin() as conn:
            # Get all current leases.
            rows = conn.execute(
                sa.select(
                    tier2_budget_leases.c.lease_id,
                    tier2_budget_leases.c.source_id,
                    tier2_budget_leases.c.acquired_at,
                )
            ).all()

            stale_ids = []
            for row in rows:
                acquired = row[2]
                age_ts = acquired.timestamp() if hasattr(acquired, 'timestamp') else 0
                if row[1] not in active_source_ids or age_ts < cutoff:
                    stale_ids.append(row[0])

            if stale_ids:
                conn.execute(
                    tier2_budget_leases.delete().where(
                        tier2_budget_leases.c.lease_id.in_(stale_ids)
                    )
                )

        return len(stale_ids)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_daily_row(self, conn: sa.Connection, now: datetime) -> None:
        """Ensure today's daily counter row exists. Reset if stale."""
        today = now.date()
        if conn.dialect.name == "postgresql":
            conn.execute(
                pg_insert(tier2_budget_daily)
                .values(day=today, consumed=0)
                .on_conflict_do_nothing(
                    index_elements=[tier2_budget_daily.c.day]
                )
            )
            return
        row = conn.execute(
            sa.select(tier2_budget_daily.c.day)
            .where(tier2_budget_daily.c.day == today)
        ).first()
        if row is None:
            conn.execute(tier2_budget_daily.insert().values(day=today, consumed=0))
