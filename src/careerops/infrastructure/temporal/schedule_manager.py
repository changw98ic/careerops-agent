"""Idempotent lifecycle management for Temporal Schedules (Phase 2.4).

The career loop is meant to self-drive: inbound mail polled every ~5-10 min,
crawl on each source's interval, the Gmail token refreshed hourly. Temporal is
already the durable-execution backbone, so registering *Schedules* (not cron,
not in-process loops) gives resumable, crash-safe auto-advancement and finally
consumes the previously-dead ``interval_seconds`` field. This module is the
single place that owns Schedule create/update/pause/resume/delete.

Design:

- **Idempotent** create/update: a deterministic ``schedule_id`` is derived for
  each managed schedule (e.g. ``mail-sync:{account_id}``); ``ensure_schedule``
  creates when absent and updates the spec/paused-state when present, so it is
  safe to call on every boot without duplicating schedules.
- **Pause/resume map to schedule state**: the domain layer's pause/resume now
  also flips ``Schedule.state.paused`` rather than only toggling a DB flag, so a
  paused source actually stops firing. Crawl schedules are created paused until
  the readiness gate (Phase 8) passes.
- **One client**: built on ``runtime.get_temporal_client()``; no new infra.
- **Testable**: ``ScheduleManager`` takes a ``Client`` (or a fake) so the
  lifecycle can be unit-tested without a live Temporal server.

Schedules are designed for at-least-once delivery; existing idempotency
(``run_identity``, send idempotency keys, reconciliation keys) makes the
workers safe under duplicate fires.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.service import RPCError, RPCStatusCode

# Default cadences (spec: proactive-trigger-loop / real-provider-integration).
MAIL_SYNC_POLL_INTERVAL = timedelta(minutes=5)
GMAIL_TOKEN_REFRESH_INTERVAL = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class ScheduleDescriptor:
    """Resolved description of a managed schedule.

    ``paused`` reflects the live ``Schedule.state.paused``. ``interval_seconds``
    is the first interval spec's period (0 when the schedule is cron-based or
    has no interval).
    """

    schedule_id: str
    paused: bool
    interval_seconds: float
    note: str


class ScheduleManager:
    """Owns the lifecycle of Temporal Schedules for the trigger loop.

    Construct with a Temporal ``Client`` (typically from
    ``runtime.get_temporal_client()``). Every method is idempotent in the sense
    that it tolerates the schedule already existing / not existing; callers can
    reconcile on every boot.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Idempotent create / update
    # ------------------------------------------------------------------

    async def ensure_schedule(
        self,
        *,
        schedule_id: str,
        workflow: Callable[..., Awaitable[Any]] | str,
        arg: Any,
        interval: timedelta,
        task_queue: str,
        paused: bool = False,
        note: str = "",
        workflow_id: str | None = None,
        offset: timedelta | None = None,
        jitter: timedelta | None = None,
    ) -> bool:
        """Create the schedule if absent, otherwise reconcile its spec/state.

        Returns True when a new schedule was created, False when an existing
        one was updated. The schedule fires ``workflow(arg)`` every
        ``interval``. Use a deterministic ``schedule_id`` (e.g.
        ``mail-sync:{account_id}``) so repeated calls converge.

        ``workflow_id`` is the workflow execution id for runs started by this
        schedule (Temporal appends a per-fire suffix so periodic runs do not
        collide). It defaults to the ``schedule_id``.
        """
        action = ScheduleActionStartWorkflow(
            workflow=workflow,
            arg=arg,
            task_queue=task_queue,
            id=workflow_id or schedule_id,
        )
        spec = ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=interval, offset=offset)],
            jitter=jitter,
        )
        desired = Schedule(
            action=action,
            spec=spec,
            state=ScheduleState(paused=paused, note=note or None),
        )

        existing = await self._describe_or_none(schedule_id)
        if existing is None:
            await self._client.create_schedule(schedule_id, desired)
            return True

        # Reconcile spec + paused state to the desired values.
        await self._client.get_schedule_handle(schedule_id).update(
            _make_reconcile_updater(desired)
        )
        return False

    # ------------------------------------------------------------------
    # Pause / resume
    # ------------------------------------------------------------------

    async def pause(self, schedule_id: str, *, note: str = "paused") -> bool:
        """Set ``Schedule.state.paused = True``. Returns False if absent."""
        return await self._set_paused(schedule_id, paused=True, note=note)

    async def resume(self, schedule_id: str) -> bool:
        """Set ``Schedule.state.paused = False``. Returns False if absent."""
        return await self._set_paused(schedule_id, paused=False, note="resumed")

    async def delete(self, schedule_id: str) -> bool:
        """Delete the schedule. Returns False if it did not exist."""
        handle = self._client.get_schedule_handle(schedule_id)
        try:
            await handle.delete()
            return True
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return False
            raise

    async def trigger(self, schedule_id: str) -> bool:
        """Manually fire the schedule once (run-now). Returns False if absent."""
        handle = self._client.get_schedule_handle(schedule_id)
        try:
            await handle.trigger()
            return True
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return False
            raise

    async def describe(self, schedule_id: str) -> ScheduleDescriptor | None:
        """Return a descriptor for the schedule, or None if it does not exist."""
        desc = await self._describe_or_none(schedule_id)
        if desc is None:
            return None
        schedule: Schedule = desc.schedule
        interval_seconds = 0.0
        if schedule.spec.intervals:
            interval_seconds = schedule.spec.intervals[0].every.total_seconds()
        return ScheduleDescriptor(
            schedule_id=schedule_id,
            paused=bool(schedule.state.paused),
            interval_seconds=interval_seconds,
            note=schedule.state.note or "",
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _describe_or_none(self, schedule_id: str) -> Any:
        handle = self._client.get_schedule_handle(schedule_id)
        try:
            return await handle.describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return None
            raise

    async def _set_paused(
        self, schedule_id: str, *, paused: bool, note: str
    ) -> bool:
        handle = self._client.get_schedule_handle(schedule_id)
        try:
            await handle.update(_make_paused_updater(paused=paused, note=note))
            return True
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return False
            raise


def _make_reconcile_updater(
    desired: Schedule,
) -> Callable[[ScheduleUpdateInput], ScheduleUpdate]:
    """Build an updater that rewrites spec/state to ``desired``."""

    def updater(input: ScheduleUpdateInput) -> ScheduleUpdate:
        current: Schedule = input.description.schedule
        return ScheduleUpdate(
            schedule=Schedule(
                action=current.action,
                spec=desired.spec,
                state=ScheduleState(
                    paused=desired.state.paused,
                    note=desired.state.note,
                    limited_actions=current.state.limited_actions,
                    remaining_actions=current.state.remaining_actions,
                ),
            )
        )

    return updater


def _make_paused_updater(
    *, paused: bool, note: str
) -> Callable[[ScheduleUpdateInput], ScheduleUpdate]:
    """Build an updater that flips ``state.paused`` and keeps the rest."""

    def updater(input: ScheduleUpdateInput) -> ScheduleUpdate:
        current: Schedule = input.description.schedule
        return ScheduleUpdate(
            schedule=Schedule(
                action=current.action,
                spec=current.spec,
                state=ScheduleState(
                    paused=paused,
                    note=note,
                    limited_actions=current.state.limited_actions,
                    remaining_actions=current.state.remaining_actions,
                ),
            )
        )

    return updater


# ----------------------------------------------------------------------
# Deterministic schedule-id helpers
# ----------------------------------------------------------------------


def mail_sync_schedule_id(account_id: str) -> str:
    """Deterministic schedule id for an account's inbound-mail poll."""
    return f"mail-sync:{account_id}"


def crawl_schedule_id(owner_id: str, plan_version_id: str) -> str:
    """Deterministic schedule id for a crawl plan version's source interval."""
    return f"crawl:{owner_id}:{plan_version_id}"


def gmail_token_refresh_schedule_id() -> str:
    """Deterministic schedule id for the hourly Gmail token refresh."""
    return "gmail-token-refresh"
