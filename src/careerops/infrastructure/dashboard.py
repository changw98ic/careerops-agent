from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from careerops.application.dashboard import (
    DashboardSnapshot,
    DashboardSystemStatus,
)
from careerops.application.ports.readiness import ReadinessProbe
from careerops.config import Settings
from careerops.infrastructure.database.schema import approval_requests, outbox_events


def pending_dashboard_counts_statement(*, now: datetime) -> sa.Select[tuple[int, int]]:
    pending_approvals = (
        sa.select(sa.func.count())
        .select_from(approval_requests)
        .where(
            approval_requests.c.decision == "pending",
            approval_requests.c.expires_at > now,
        )
        .scalar_subquery()
    )
    pending_outbox = (
        sa.select(sa.func.count())
        .select_from(outbox_events)
        .where(outbox_events.c.status == "pending")
        .scalar_subquery()
    )
    return sa.select(pending_approvals, pending_outbox)


class RuntimeDashboardSnapshotProvider:
    """Build the authenticated M0 dashboard from live, read-only runtime state."""

    def __init__(
        self,
        readiness: ReadinessProbe,
        engine: Engine,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._readiness = readiness
        self._engine = engine
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))

    async def snapshot(self) -> DashboardSnapshot:
        report = await self._readiness.check()
        try:
            pending_approvals, pending_outbox = await asyncio.to_thread(self._pending_counts)
        except SQLAlchemyError:
            pending_approvals = None
            pending_outbox = None
        return DashboardSnapshot(
            system_status=(
                DashboardSystemStatus.READY if report.ready else DashboardSystemStatus.NOT_READY
            ),
            dependency_checks=tuple(
                (component, state.value) for component, state in report.checks.items()
            ),
            integration_checks=(
                ("model_provider", self._settings.model_provider),
                (
                    "google_oauth",
                    "enabled" if self._settings.google_oauth_enabled else "disabled",
                ),
                (
                    "external_writes",
                    "enabled" if self._settings.external_writes_enabled else "disabled",
                ),
            ),
            pending_approvals=pending_approvals,
            pending_outbox_events=pending_outbox,
        )

    def _pending_counts(self) -> tuple[int, int]:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("dashboard clock must return a timezone-aware timestamp")
        with self._engine.connect() as connection:
            row = connection.execute(pending_dashboard_counts_statement(now=now)).one()
        return cast("int", row[0]), cast("int", row[1])


__all__ = ["RuntimeDashboardSnapshotProvider", "pending_dashboard_counts_statement"]
