from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class DashboardSystemStatus(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    system_status: DashboardSystemStatus
    dependency_checks: tuple[tuple[str, str], ...]
    integration_checks: tuple[tuple[str, str], ...]
    pending_approvals: int | None
    pending_outbox_events: int | None

    def __post_init__(self) -> None:
        for count in (self.pending_approvals, self.pending_outbox_events):
            if count is not None and count < 0:
                raise ValueError("dashboard pending counts must not be negative")


class DashboardSnapshotProvider(Protocol):
    async def snapshot(self) -> DashboardSnapshot: ...


__all__ = [
    "DashboardSnapshot",
    "DashboardSnapshotProvider",
    "DashboardSystemStatus",
]
