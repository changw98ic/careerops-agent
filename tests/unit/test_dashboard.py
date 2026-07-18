from __future__ import annotations

from datetime import UTC, datetime
from typing import Self, cast

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from careerops.application.dashboard import DashboardSystemStatus
from careerops.application.ports.readiness import ReadinessReport, ReadinessState
from careerops.config import Settings
from careerops.infrastructure.dashboard import (
    RuntimeDashboardSnapshotProvider,
    pending_dashboard_counts_statement,
)

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


class FixedReadinessProbe:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            checks={
                "database": ReadinessState.OK,
                "redis": ReadinessState.OK,
                "temporal": ReadinessState.OK,
                "storage": ReadinessState.OK,
            }
        )

    async def close(self) -> None:
        return None


class CountResult:
    def one(self) -> tuple[int, int]:
        return 3, 4


class CountConnection:
    def __init__(self) -> None:
        self.statement: sa.Executable | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: sa.Executable) -> CountResult:
        self.statement = statement
        return CountResult()


class CountEngine:
    def __init__(self) -> None:
        self.connection = CountConnection()

    def connect(self) -> CountConnection:
        return self.connection


class FailingCountEngine:
    def connect(self) -> CountConnection:
        raise SQLAlchemyError("sensitive database failure")


@pytest.mark.asyncio
async def test_runtime_dashboard_combines_live_readiness_integrations_and_counts() -> None:
    engine = CountEngine()
    provider = RuntimeDashboardSnapshotProvider(
        FixedReadinessProbe(),
        cast("Engine", engine),
        Settings(),
        clock=lambda: NOW,
    )

    snapshot = await provider.snapshot()

    assert snapshot.system_status is DashboardSystemStatus.READY
    assert snapshot.dependency_checks == (
        ("database", "ok"),
        ("redis", "ok"),
        ("temporal", "ok"),
        ("storage", "ok"),
    )
    assert snapshot.integration_checks == (
        ("model_provider", "disabled"),
        ("google_oauth", "disabled"),
        ("external_writes", "disabled"),
    )
    assert snapshot.pending_approvals == 3
    assert snapshot.pending_outbox_events == 4
    assert engine.connection.statement is not None


@pytest.mark.asyncio
async def test_runtime_dashboard_marks_counts_unavailable_when_database_count_fails() -> None:
    provider = RuntimeDashboardSnapshotProvider(
        FixedReadinessProbe(),
        cast("Engine", FailingCountEngine()),
        Settings(),
        clock=lambda: NOW,
    )

    snapshot = await provider.snapshot()

    assert snapshot.system_status is DashboardSystemStatus.READY
    assert snapshot.pending_approvals is None
    assert snapshot.pending_outbox_events is None


def test_pending_count_query_filters_actionable_approvals_and_pending_outbox() -> None:
    sql = str(pending_dashboard_counts_statement(now=NOW))

    assert "approval_requests.decision" in sql
    assert "approval_requests.expires_at" in sql
    assert "outbox_events.status" in sql
