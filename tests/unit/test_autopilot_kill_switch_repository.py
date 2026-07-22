from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from careerops.application.autopilot_kill_switches import (
    AutopilotKillSwitchScope,
    AutopilotKillSwitchState,
    SetAutopilotKillSwitchCommand,
)
from careerops.infrastructure.database import autopilot_kill_switches as db_kill_switches
from careerops.infrastructure.database.autopilot_kill_switches import (
    PostgresAutopilotKillSwitchRepository,
    compile_query_for_test,
    current_kill_switch_state_statement,
    insert_kill_switch_event_statement,
    select_kill_switch_event_by_key_statement,
)

NOW = datetime(2026, 7, 20, 11, 0, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")


def command(
    *,
    scope: AutopilotKillSwitchScope | None = None,
    state: AutopilotKillSwitchState = AutopilotKillSwitchState.ACTIVE,
    reason: str = "emergency stop",
) -> SetAutopilotKillSwitchCommand:
    return SetAutopilotKillSwitchCommand(
        actor_id=ACTOR_ID,
        scope=scope or AutopilotKillSwitchScope.global_scope(),
        state=state,
        reason=reason,
        trace_id="trace-1",
    )


def test_repository_requires_explicit_transaction() -> None:
    class NonTransactionalConnection:
        def in_transaction(self) -> bool:
            return False

    with pytest.raises(ValueError, match="explicit transaction"):
        PostgresAutopilotKillSwitchRepository(cast("Connection", NonTransactionalConnection()))


def test_insert_statement_is_append_only_idempotent_and_server_timestamped() -> None:
    item = command(scope=AutopilotKillSwitchScope.provider_scope("greenhouse"))
    event_id = uuid4()

    sql = compile_query_for_test(insert_kill_switch_event_statement(item, event_id))

    assert "INSERT INTO careerops.autopilot_kill_switch_events" in sql
    assert str(event_id) in sql
    assert str(ACTOR_ID) in sql
    assert "provider outage" not in sql
    assert "greenhouse" in sql
    assert "emergency stop" in sql
    assert "trace-1" in sql
    assert "idempotency_key" in sql
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in sql
    assert "RETURNING careerops.autopilot_kill_switch_events.sequence" in sql
    assert "created_at" not in sql.split("VALUES", maxsplit=1)[0]
    assert "sequence" not in sql.split("VALUES", maxsplit=1)[0]


def test_current_state_statement_selects_latest_matching_scope() -> None:
    campaign_id = uuid4()

    sql = compile_query_for_test(
        current_kill_switch_state_statement(AutopilotKillSwitchScope.campaign(campaign_id))
    )

    assert "FROM careerops.autopilot_kill_switch_events" in sql
    assert "scope_type = 'campaign'" in sql
    assert str(campaign_id) in sql
    assert "provider IS NULL" in sql
    assert "ORDER BY" in sql
    assert "sequence DESC" in sql
    assert "LIMIT 1" in sql


def test_current_state_uses_sequence_when_server_timestamps_match() -> None:
    sql = compile_query_for_test(
        current_kill_switch_state_statement(AutopilotKillSwitchScope.global_scope())
    )

    order_by = sql.split("ORDER BY", maxsplit=1)[1].split("LIMIT", maxsplit=1)[0]
    assert "sequence DESC" in order_by
    assert "created_at" not in order_by
    assert ".id" not in order_by


def test_idempotency_lookup_is_read_only_for_api_role() -> None:
    sql = compile_query_for_test(select_kill_switch_event_by_key_statement("key-1"))

    assert "WHERE careerops.autopilot_kill_switch_events.idempotency_key = 'key-1'" in sql
    assert "FOR UPDATE" not in sql


class FakeMappingResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def first(self) -> dict[str, object] | None:
        return self._row


class FakeExecuteResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> FakeMappingResult:
        return FakeMappingResult(self._row)


class FakeConnection:
    def __init__(self, rows: list[dict[str, object] | None]) -> None:
        self._rows = iter(rows)
        self.statements: list[str] = []

    def in_transaction(self) -> bool:
        return True

    def execute(self, statement: sa.sql.Executable) -> FakeExecuteResult:
        self.statements.append(compile_query_for_test(cast("sa.ClauseElement", statement)))
        return FakeExecuteResult(next(self._rows))


def test_append_event_returns_inserted_event_reference() -> None:
    event_id = uuid4()
    connection = FakeConnection(
        [
            {
                "id": event_id,
                "active": True,
                "reason": "emergency stop",
                "actor_user_id": ACTOR_ID,
                "created_at": NOW,
            }
        ]
    )
    store = PostgresAutopilotKillSwitchRepository(cast("Connection", connection))

    result = store.append_event(command())

    assert result.event_id == event_id
    assert result.state is AutopilotKillSwitchState.ACTIVE
    assert result.newly_created is True
    assert len(connection.statements) == 1


def test_append_event_conflict_reads_existing_idempotency_row() -> None:
    event_id = uuid4()
    connection = FakeConnection(
        [
            None,
            {
                "id": event_id,
                "active": False,
                "reason": "resume",
                "actor_user_id": ACTOR_ID,
                "created_at": NOW,
            },
        ]
    )
    store = PostgresAutopilotKillSwitchRepository(cast("Connection", connection))

    result = store.append_event(command(state=AutopilotKillSwitchState.INACTIVE, reason="resume"))

    assert result.event_id == event_id
    assert result.state is AutopilotKillSwitchState.INACTIVE
    assert result.newly_created is False
    assert all("FOR UPDATE" not in sql for sql in connection.statements)


def test_current_state_defaults_inactive_without_event_and_projects_latest_row() -> None:
    scope = AutopilotKillSwitchScope.global_scope()
    active_event_id = uuid4()
    empty_connection = FakeConnection([None])
    populated_connection = FakeConnection(
        [
            {
                "id": active_event_id,
                "active": True,
                "reason": "emergency stop",
                "actor_user_id": ACTOR_ID,
                "created_at": NOW,
            }
        ]
    )

    empty = PostgresAutopilotKillSwitchRepository(
        cast("Connection", empty_connection)
    ).current_state(scope)
    populated = PostgresAutopilotKillSwitchRepository(
        cast("Connection", populated_connection)
    ).current_state(scope)

    assert empty.active is False
    assert empty.event_id is None
    assert populated.active is True
    assert populated.event_id == active_event_id
    assert populated.actor_id == ACTOR_ID
    assert populated.changed_at == NOW


def test_repository_module_does_not_import_execution_capabilities() -> None:
    tree = ast.parse(inspect.getsource(db_kill_switches))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    forbidden_fragments = (
        "outbox",
        "browser",
        "credential",
        "provider.",
        "requests",
        "httpx",
        "playwright",
        "side_effect",
        "openai",
    )
    assert not any(
        fragment in module for module in imported_modules for fragment in forbidden_fragments
    )
