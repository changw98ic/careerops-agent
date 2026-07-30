"""Tier 2 budget coordinator (real-autonomous-career-loop Phase 7.1).

Manages two finite resources for Tier 2 browser-agent execution:

1. **Concurrent slots** — at most N Tier 2 runs execute simultaneously.
   A slot is acquired before a source run starts and released when it
   finishes (success, failure, or stop).  The coordinator enforces atomic
   acquire/release so two workers never exceed the configured limit.

2. **Daily browser-action budget** — at most M browser actions (navigate,
   click, scroll, capture) across all Tier 2 sources per calendar day
   (UTC).  The counter is monotonic within a day and resets at UTC
   midnight.  ``consume`` decrements the remaining budget atomically.

**Stale-lease recovery**: when a worker crashes mid-run, its acquired slot
is never released.  The coordinator exposes ``recover_stale_leases`` which
takes the current active-lease set and releases any lease whose holder is
no longer running (determined by the caller — e.g. a heartbeat or
run-identity check).  This is called once on worker startup.

Design constraints:
- Pure in-process with no external database dependency.  For the single-user
  self-hosted deployment this is sufficient; a multi-worker deployment would
  back this with a Postgres advisory-lock or Redis counter.
- Thread-safe via ``threading.Lock`` so Temporal activity workers sharing
  the same process do not race.
- Fail-closed: ``acquire`` returns ``False`` when slots are exhausted;
  ``consume`` returns ``0`` when the daily budget is spent.

Iron Rules honored:
- 3 (fail-closed: exhausted budgets return zero/false, never negative).
- 4 (idempotent: release of an un-held slot is a no-op).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

__all__ = ["Tier2Budget", "Tier2Lease"]


@dataclass(frozen=True, slots=True)
class Tier2Lease:
    """An acquired concurrent Tier 2 slot.

    ``lease_id`` uniquely identifies this acquisition; ``source_id`` is the
    source occupying the slot; ``acquired_at`` is the UTC timestamp.
    """

    lease_id: str
    source_id: str
    acquired_at: datetime


class Tier2Budget:
    """Manages concurrent Tier 2 slots and daily browser-action budget.

    Args:
        max_concurrent_slots: maximum number of Tier 2 runs that may execute
            simultaneously.  Default 3.
        daily_action_budget: maximum total browser actions across all Tier 2
            sources per UTC day.  Default 500.
        stale_lease_timeout_s: seconds after which a lease is considered stale
            if the holder has not released it.  Default 600 (10 minutes).
    """

    def __init__(
        self,
        *,
        max_concurrent_slots: int = 3,
        daily_action_budget: int = 500,
        stale_lease_timeout_s: float = 600.0,
    ) -> None:
        self._max_slots = max_concurrent_slots
        self._daily_budget = daily_action_budget
        self._stale_timeout = stale_lease_timeout_s

        # Active leases: lease_id -> Tier2Lease.
        self._leases: dict[str, Tier2Lease] = {}
        # Daily consumed counter, reset at UTC midnight.
        self._day: date = self._today()
        self._consumed: int = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Slot management
    # ------------------------------------------------------------------

    def acquire(self, source_id: str) -> Tier2Lease | None:
        """Try to acquire a concurrent Tier 2 slot.

        Returns a :class:`Tier2Lease` on success, or ``None`` when all slots
        are occupied.  Thread-safe.
        """
        with self._lock:
            self._maybe_reset_day()
            if len(self._leases) >= self._max_slots:
                return None
            lease_id = f"t2-{source_id}-{int(time.monotonic() * 1000)}"
            lease = Tier2Lease(
                lease_id=lease_id,
                source_id=source_id,
                acquired_at=datetime.now(tz=UTC),
            )
            self._leases[lease_id] = lease
            return lease

    def release(self, lease: Tier2Lease) -> None:
        """Release a previously acquired slot.

        Idempotent: releasing an already-released or unknown lease is a no-op.
        """
        with self._lock:
            self._leases.pop(lease.lease_id, None)

    @property
    def active_slots(self) -> int:
        """Number of currently held concurrent slots."""
        with self._lock:
            return len(self._leases)

    @property
    def available_slots(self) -> int:
        """Number of slots available for acquisition."""
        with self._lock:
            return max(0, self._max_slots - len(self._leases))

    # ------------------------------------------------------------------
    # Daily action budget
    # ------------------------------------------------------------------

    def consume(self, count: int = 1) -> int:
        """Consume ``count`` browser actions from the daily budget.

        Returns the actual number consumed (may be less than requested when
        the budget is nearly exhausted).  Returns 0 when the budget is fully
        spent.
        """
        if count <= 0:
            return 0
        with self._lock:
            self._maybe_reset_day()
            remaining = self._daily_budget - self._consumed
            if remaining <= 0:
                return 0
            actual = min(count, remaining)
            self._consumed += actual
            return actual

    @property
    def daily_remaining(self) -> int:
        """Remaining browser actions for today."""
        with self._lock:
            self._maybe_reset_day()
            return max(0, self._daily_budget - self._consumed)

    @property
    def daily_consumed(self) -> int:
        """Browser actions consumed today."""
        with self._lock:
            self._maybe_reset_day()
            return self._consumed

    # ------------------------------------------------------------------
    # Stale-lease recovery
    # ------------------------------------------------------------------

    def recover_stale_leases(self, active_source_ids: set[str]) -> int:
        """Release leases whose holders are no longer active.

        Called once on worker startup.  ``active_source_ids`` is the set of
        source IDs that are currently running a Tier 2 crawl (determined by
        the caller from the execution layer).  Any lease whose ``source_id``
        is NOT in this set — OR whose ``acquired_at`` is older than
        ``stale_lease_timeout_s`` — is released.

        Returns the number of stale leases recovered.
        """
        now = datetime.now(tz=UTC)
        stale_ids: list[str] = []
        with self._lock:
            for lid, lease in self._leases.items():
                age = (now - lease.acquired_at).total_seconds()
                if lease.source_id not in active_source_ids or age > self._stale_timeout:
                    stale_ids.append(lid)
            for lid in stale_ids:
                del self._leases[lid]
        return len(stale_ids)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _today() -> date:
        return datetime.now(tz=UTC).date()

    def _maybe_reset_day(self) -> None:
        """Reset the daily counter if a new UTC day has started."""
        today = self._today()
        if today != self._day:
            self._day = today
            self._consumed = 0
