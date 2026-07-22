from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import ClauseElement

from careerops.application.autopilot_control import (
    AutopilotCommandState,
    AutopilotControlCapabilityState,
    CampaignGrantStatus,
    ReviewQueueTab,
    ReviewResolutionMode,
)
from careerops.infrastructure.database import autopilot_control
from careerops.infrastructure.database.autopilot_control import RuntimeAutopilotControlPlane

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")


class FakeResult:
    def __init__(
        self,
        *,
        mapping_rows: Iterable[Mapping[str, object]] = (),
        one_row: tuple[int, int, int] | None = None,
    ) -> None:
        self._mapping_rows = tuple(mapping_rows)
        self._one_row = one_row

    def mappings(self) -> tuple[Mapping[str, object], ...]:
        return self._mapping_rows

    def one(self) -> tuple[int, int, int]:
        if self._one_row is None:
            raise AssertionError("one() was not expected")
        return self._one_row


class FakeConnection:
    def __init__(self, results: Iterable[FakeResult]) -> None:
        self._results = iter(results)
        self.statements: list[ClauseElement] = []

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, statement: sa.sql.Executable) -> FakeResult:
        self.statements.append(cast("ClauseElement", statement))
        return next(self._results)


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> FakeConnection:
        return self.connection


class FailingConnection:
    def __enter__(self) -> FailingConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, _statement: sa.sql.Executable) -> FakeResult:
        raise SQLAlchemyError("database unavailable")


class FailingEngine:
    def connect(self) -> FailingConnection:
        return FailingConnection()


class InvalidProjectionConnection:
    def __enter__(self) -> InvalidProjectionConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, _statement: sa.sql.Executable) -> FakeResult:
        raise ValueError("invalid database projection")


class InvalidProjectionEngine:
    def connect(self) -> InvalidProjectionConnection:
        return InvalidProjectionConnection()


def test_runtime_control_plane_projects_actor_owned_live_grants_read_only() -> None:
    connection = FakeConnection(
        (
            FakeResult(
                mapping_rows=(
                    {
                        "grant_id": UUID("00000000-0000-0000-0000-000000000101"),
                        "campaign_id": UUID("00000000-0000-0000-0000-000000000201"),
                        "campaign_name": "平台工程授权",
                        "scope_summary": "只处理匹配度高的岗位",
                        "version": 2,
                        "allowed_action_kinds": ["prepare_application", "submit_application"],
                        "allowed_channels": ["greenhouse"],
                        "allowed_target_hosts": ["greenhouse.io"],
                        "material_hashes": ["a" * 64],
                        "max_total_submissions": 5,
                        "remaining_submissions": 3,
                        "expires_at": NOW + timedelta(days=7),
                        "created_at": NOW,
                    },
                )
            ),
        )
    )
    control_plane = RuntimeAutopilotControlPlane(cast("Engine", FakeEngine(connection)))

    snapshot = asyncio.run(control_plane.campaign_grants(actor_id=ACTOR_ID, now=NOW))

    assert snapshot.capability.state is AutopilotControlCapabilityState.READ_ONLY
    assert len(snapshot.grants) == 1
    grant = snapshot.grants[0]
    assert grant.status is CampaignGrantStatus.ACTIVE
    assert grant.remaining_submissions == 3
    assert grant.channels == ("greenhouse",)
    rendered_statement = str(connection.statements[0])
    assert "owner_user_id" in rendered_statement
    assert "expires_at" in rendered_statement
    assert "autopilot_grant_revocations" in rendered_statement


def test_runtime_control_plane_keeps_manual_only_out_of_agent_approvable_queue() -> None:
    connection = FakeConnection(
        (
            FakeResult(one_row=(1, 1, 1)),
            FakeResult(
                mapping_rows=(
                    {
                        "item_id": UUID("00000000-0000-0000-0000-000000000301"),
                        "campaign_id": UUID("00000000-0000-0000-0000-000000000201"),
                        "grant_id": UUID("00000000-0000-0000-0000-000000000101"),
                        "action_intent_id": UUID("00000000-0000-0000-0000-000000000401"),
                        "payload_hash": "b" * 64,
                        "review_kind": "exceptions",
                        "resolution_mode": "manual_only",
                        "reason_codes": ["SITE_POLICY_PROHIBITED"],
                        "snapshot": {
                            "title_zh": "站点禁止自动化",
                            "summary_zh": "必须人工处理",
                            "target_host": "greenhouse.io",
                        },
                        "created_at": NOW,
                    },
                )
            ),
        )
    )
    control_plane = RuntimeAutopilotControlPlane(cast("Engine", FakeEngine(connection)))

    snapshot = asyncio.run(
        control_plane.review_queue(actor_id=ACTOR_ID, tab=ReviewQueueTab.EXCEPTIONS, now=NOW)
    )

    assert snapshot.counts[0].tab is ReviewQueueTab.PENDING
    assert snapshot.counts[0].count == 1
    assert len(snapshot.items) == 1
    item = snapshot.items[0]
    assert item.tab is ReviewQueueTab.EXCEPTIONS
    assert item.resolution_mode is ReviewResolutionMode.MANUAL_ONLY
    assert not item.can_agent_continue_after_approval
    compiled_items_statement = connection.statements[1].compile()
    rendered_items_statement = str(compiled_items_statement)
    assert "owner_user_id" in rendered_items_statement
    assert "manual_only" in (compiled_items_statement.params or {}).values()


def test_runtime_control_plane_fails_closed_when_database_projection_unavailable() -> None:
    control_plane = RuntimeAutopilotControlPlane(cast("Engine", FailingEngine()))

    grants = asyncio.run(control_plane.campaign_grants(actor_id=ACTOR_ID, now=NOW))
    review = asyncio.run(
        control_plane.review_queue(actor_id=ACTOR_ID, tab=ReviewQueueTab.PENDING, now=NOW)
    )
    command = asyncio.run(
        control_plane.request_grant_revocation(
            actor_id=ACTOR_ID,
            grant_id=UUID("00000000-0000-0000-0000-000000000101"),
        )
    )

    assert grants.capability.state is AutopilotControlCapabilityState.DISABLED
    assert grants.grants == ()
    assert review.capability.state is AutopilotControlCapabilityState.DISABLED
    assert review.items == ()
    assert command.state is AutopilotCommandState.REJECTED


def test_runtime_control_plane_fails_closed_when_database_projection_is_invalid() -> None:
    control_plane = RuntimeAutopilotControlPlane(cast("Engine", InvalidProjectionEngine()))

    grants = asyncio.run(control_plane.campaign_grants(actor_id=ACTOR_ID, now=NOW))
    review = asyncio.run(
        control_plane.review_queue(actor_id=ACTOR_ID, tab=ReviewQueueTab.PENDING, now=NOW)
    )

    assert grants.capability.state is AutopilotControlCapabilityState.DISABLED
    assert grants.grants == ()
    assert review.capability.state is AutopilotControlCapabilityState.DISABLED
    assert review.items == ()


def test_runtime_control_plane_does_not_import_execution_surfaces() -> None:
    tree = ast.parse(inspect.getsource(autopilot_control))
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any("outbox" in module for module in imported_modules)
    assert not any("browser" in module for module in imported_modules)
    assert not any("credential" in module for module in imported_modules)
    assert not any("provider" in module for module in imported_modules)
    assert not any("side_effect" in module for module in imported_modules)
