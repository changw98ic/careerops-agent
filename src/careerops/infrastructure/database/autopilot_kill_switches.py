from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql import Executable

from careerops.application.autopilot_kill_switches import (
    AutopilotKillSwitchEventRef,
    AutopilotKillSwitchScope,
    AutopilotKillSwitchScopeType,
    AutopilotKillSwitchState,
    CurrentAutopilotKillSwitchState,
    SetAutopilotKillSwitchCommand,
)
from careerops.infrastructure.database.schema import autopilot_kill_switch_events


class PostgresAutopilotKillSwitchRepository:
    """Append-only kill-switch events scoped to an explicit caller transaction."""

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError("autopilot kill-switch repository requires an explicit transaction")
        self._connection = connection

    def append_event(
        self,
        command: SetAutopilotKillSwitchCommand,
    ) -> AutopilotKillSwitchEventRef:
        event_id = uuid4()
        row = (
            self._connection.execute(insert_kill_switch_event_statement(command, event_id))
            .mappings()
            .first()
        )
        if row is None:
            existing = self._select_by_idempotency_key(command.idempotency_key)
            if existing is None:
                raise RuntimeError("kill-switch idempotency conflict did not return a row")
            return _event_ref_from_row(existing, command.scope, newly_created=False)
        return _event_ref_from_row(row, command.scope, newly_created=True)

    def current_state(
        self,
        scope: AutopilotKillSwitchScope,
    ) -> CurrentAutopilotKillSwitchState:
        row = (
            self._connection.execute(current_kill_switch_state_statement(scope)).mappings().first()
        )
        if row is None:
            return CurrentAutopilotKillSwitchState(
                scope=scope,
                state=AutopilotKillSwitchState.INACTIVE,
            )
        return _current_state_from_row(row, scope)

    def _select_by_idempotency_key(self, idempotency_key: str) -> RowMapping | None:
        return (
            self._connection.execute(select_kill_switch_event_by_key_statement(idempotency_key))
            .mappings()
            .first()
        )


def insert_kill_switch_event_statement(
    command: SetAutopilotKillSwitchCommand,
    event_id: UUID,
) -> Executable:
    return (
        postgresql.insert(autopilot_kill_switch_events)
        .values(
            id=event_id,
            scope_type=command.scope.scope_type.value,
            campaign_id=command.scope.campaign_id,
            provider=command.scope.provider,
            active=command.active,
            reason=command.reason,
            actor_user_id=command.actor_id,
            idempotency_key=command.idempotency_key,
            trace_id=command.trace_id,
        )
        .on_conflict_do_nothing(index_elements=[autopilot_kill_switch_events.c.idempotency_key])
        .returning(
            autopilot_kill_switch_events.c.sequence,
            autopilot_kill_switch_events.c.id,
            autopilot_kill_switch_events.c.active,
            autopilot_kill_switch_events.c.reason,
            autopilot_kill_switch_events.c.actor_user_id,
            autopilot_kill_switch_events.c.created_at,
        )
    )


def current_kill_switch_state_statement(
    scope: AutopilotKillSwitchScope,
) -> sa.Select[tuple[Any, ...]]:
    return (
        sa.select(
            autopilot_kill_switch_events.c.sequence,
            autopilot_kill_switch_events.c.id,
            autopilot_kill_switch_events.c.active,
            autopilot_kill_switch_events.c.reason,
            autopilot_kill_switch_events.c.actor_user_id,
            autopilot_kill_switch_events.c.created_at,
        )
        .where(*_scope_predicates(scope))
        .order_by(autopilot_kill_switch_events.c.sequence.desc())
        .limit(1)
    )


def select_kill_switch_event_by_key_statement(
    idempotency_key: str,
) -> sa.Select[tuple[Any, ...]]:
    return sa.select(
        autopilot_kill_switch_events.c.sequence,
        autopilot_kill_switch_events.c.id,
        autopilot_kill_switch_events.c.active,
        autopilot_kill_switch_events.c.reason,
        autopilot_kill_switch_events.c.actor_user_id,
        autopilot_kill_switch_events.c.created_at,
    ).where(autopilot_kill_switch_events.c.idempotency_key == idempotency_key)


def _scope_predicates(scope: AutopilotKillSwitchScope) -> tuple[sa.ColumnElement[bool], ...]:
    predicates: list[sa.ColumnElement[bool]] = [
        autopilot_kill_switch_events.c.scope_type == scope.scope_type.value
    ]
    if scope.scope_type is AutopilotKillSwitchScopeType.CAMPAIGN:
        predicates.append(autopilot_kill_switch_events.c.campaign_id == scope.campaign_id)
    else:
        predicates.append(autopilot_kill_switch_events.c.campaign_id.is_(None))
    if scope.scope_type is AutopilotKillSwitchScopeType.PROVIDER:
        predicates.append(autopilot_kill_switch_events.c.provider == scope.provider)
    else:
        predicates.append(autopilot_kill_switch_events.c.provider.is_(None))
    return tuple(predicates)


def _event_ref_from_row(
    row: RowMapping,
    scope: AutopilotKillSwitchScope,
    *,
    newly_created: bool,
) -> AutopilotKillSwitchEventRef:
    return AutopilotKillSwitchEventRef(
        event_id=_row_event_id(row),
        scope=scope,
        state=_row_state(row),
        newly_created=newly_created,
    )


def _current_state_from_row(
    row: RowMapping,
    scope: AutopilotKillSwitchScope,
) -> CurrentAutopilotKillSwitchState:
    return CurrentAutopilotKillSwitchState(
        scope=scope,
        state=_row_state(row),
        reason=cast("str | None", row.get("reason")),
        actor_id=cast("UUID | None", row.get("actor_user_id")),
        event_id=_row_event_id(row),
        changed_at=cast("Any", row.get("created_at")),
    )


def _row_state(row: RowMapping) -> AutopilotKillSwitchState:
    return AutopilotKillSwitchState.ACTIVE if row["active"] else AutopilotKillSwitchState.INACTIVE


def _row_event_id(row: RowMapping) -> UUID | int | None:
    return cast("UUID | int | None", row.get("id"))


def compile_query_for_test(statement: sa.ClauseElement | Executable) -> str:
    return str(
        cast("sa.ClauseElement", statement).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


__all__ = [
    "PostgresAutopilotKillSwitchRepository",
    "autopilot_kill_switch_events",
    "compile_query_for_test",
    "current_kill_switch_state_statement",
    "insert_kill_switch_event_statement",
    "select_kill_switch_event_by_key_statement",
]
