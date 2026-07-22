from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn, cast

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.sql import Executable

from careerops.application.goal_run import (
    GoalRunCancelCommand,
    GoalRunCheckpointCommand,
    GoalRunCreateCommand,
    GoalRunCreateLookupQuery,
    GoalRunListOwnerQuery,
    GoalRunListPage,
    GoalRunMutationResult,
    GoalRunRecord,
    GoalRunRepositoryError,
    GoalRunResumeCommand,
    GoalRunReviewDecisionCommand,
    GoalRunReviewRequestCommand,
    GoalRunStatusQuery,
    goal_run_list_page_from_mapping,
    goal_run_mutation_result_from_mapping,
    goal_run_record_from_mapping,
)

_SQLSTATE_REASON_CODES = {
    "23503": "GOAL_RUN_NOT_FOUND",
    "23514": "GOAL_RUN_STATE_CONFLICT",
    "23505": "GOAL_RUN_IDEMPOTENCY_CONFLICT",
    "40001": "GOAL_RUN_RETRY_SERIALIZATION",
    "40P01": "GOAL_RUN_RETRY_DEADLOCK",
    "P0001": "GOAL_RUN_REJECTED",
}


class PostgresGoalRunRepository:
    """Goal-run repository restricted to migration-provided SECURITY DEFINER functions."""

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError("goal-run repository requires an explicit transaction")
        self._connection = connection

    def find_create(self, query: GoalRunCreateLookupQuery) -> GoalRunMutationResult | None:
        value = self._execute_optional_json(goal_run_lookup_create_statement(query))
        if value is None:
            return None
        return goal_run_mutation_result_from_mapping(value)

    def create(self, command: GoalRunCreateCommand) -> GoalRunMutationResult:
        value = self._execute_json(goal_run_create_statement(command))
        return goal_run_mutation_result_from_mapping(value)

    def status(self, query: GoalRunStatusQuery) -> GoalRunRecord:
        value = self._execute_json(goal_run_status_statement(query))
        return goal_run_record_from_mapping(value)

    def list_owner(self, query: GoalRunListOwnerQuery) -> GoalRunListPage:
        value = self._execute_json(goal_run_list_owner_statement(query))
        return goal_run_list_page_from_mapping(value)

    def checkpoint(self, command: GoalRunCheckpointCommand) -> GoalRunMutationResult:
        value = self._execute_json(goal_run_checkpoint_statement(command))
        return goal_run_mutation_result_from_mapping(value)

    def request_review(self, command: GoalRunReviewRequestCommand) -> GoalRunMutationResult:
        value = self._execute_json(goal_run_request_review_statement(command))
        return goal_run_mutation_result_from_mapping(value)

    def record_review_decision(
        self,
        command: GoalRunReviewDecisionCommand,
    ) -> GoalRunMutationResult:
        value = self._execute_json(goal_run_record_review_decision_statement(command))
        return goal_run_mutation_result_from_mapping(value)

    def cancel(self, command: GoalRunCancelCommand) -> GoalRunMutationResult:
        value = self._execute_json(goal_run_cancel_statement(command))
        return goal_run_mutation_result_from_mapping(value)

    def resume(self, command: GoalRunResumeCommand) -> GoalRunMutationResult:
        value = self._execute_json(goal_run_resume_statement(command))
        return goal_run_mutation_result_from_mapping(value)

    def _execute_json(self, statement: sa.Select[tuple[Any]]) -> Mapping[str, object]:
        value = self._execute_optional_json(statement)
        if value is None:
            raise GoalRunRepositoryError("GOAL_RUN_DATABASE_RESULT_INVALID")
        return value

    def _execute_optional_json(
        self,
        statement: sa.Select[tuple[Any]],
    ) -> Mapping[str, object] | None:
        try:
            value = self._connection.execute(statement).scalar_one()
        except DBAPIError as exc:
            _raise_sanitized_database_error(exc)
        except SQLAlchemyError:
            raise GoalRunRepositoryError("GOAL_RUN_DATABASE_ERROR") from None
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise GoalRunRepositoryError("GOAL_RUN_DATABASE_RESULT_INVALID")
        return cast("Mapping[str, object]", value)


def goal_run_create_statement(command: GoalRunCreateCommand) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_create(
            _uuid_param("p_actor_id", command.actor_id),
            _text_param("p_idempotency_key", command.idempotency_key),
            _text_param("p_goal_kind", command.goal_kind),
            _text_param("p_goal", command.goal),
            _jsonb_param("p_context", dict(command.context)),
            _optional_uuid_param("p_registry_id", command.registry_id),
            _optional_text_param("p_source_id", command.source_id),
            _integer_param("p_max_records", command.max_records),
            _optional_text_param("p_temporal_workflow_id", command.temporal_workflow_id),
            _text_param("p_trace_id", command.trace_id),
        ).label("goal_run")
    )


def goal_run_lookup_create_statement(
    query: GoalRunCreateLookupQuery,
) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_lookup_create(
            _uuid_param("p_actor_id", query.actor_id),
            _text_param("p_idempotency_key", query.idempotency_key),
        ).label("goal_run")
    )


def goal_run_status_statement(query: GoalRunStatusQuery) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_status(
            _uuid_param("p_actor_id", query.actor_id),
            _uuid_param("p_goal_run_id", query.goal_run_id),
        ).label("goal_run")
    )


def goal_run_list_owner_statement(query: GoalRunListOwnerQuery) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_list_owner(
            _uuid_param("p_actor_id", query.actor_id),
            _integer_param("p_limit", query.limit),
            _optional_text_param("p_cursor", query.cursor),
        ).label("goal_runs")
    )


def goal_run_checkpoint_statement(command: GoalRunCheckpointCommand) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_checkpoint(
            _uuid_param("p_actor_id", command.actor_id),
            _uuid_param("p_goal_run_id", command.goal_run_id),
            _bigint_param("p_expected_version", command.expected_version),
            _uuid_param("p_fencing_token", command.fencing_token),
            _text_param("p_phase", command.phase.value),
            _text_param("p_status", command.status.value),
            _text_param("p_outcome", command.outcome.value),
            _jsonb_param("p_checkpoint", dict(command.checkpoint)),
            _optional_text_param("p_last_error_code", command.last_error_code),
            _text_param("p_idempotency_key", command.idempotency_key),
            _text_param("p_trace_id", command.trace_id),
        ).label("goal_run")
    )


def goal_run_request_review_statement(
    command: GoalRunReviewRequestCommand,
) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_request_review(
            _uuid_param("p_actor_id", command.actor_id),
            _uuid_param("p_goal_run_id", command.goal_run_id),
            _bigint_param("p_expected_version", command.expected_version),
            _uuid_param("p_fencing_token", command.fencing_token),
            _uuid_param("p_review_item_id", command.review_item_id),
            _text_param("p_review_snapshot_sha256", command.review_snapshot_sha256),
            _text_param("p_review_kind", command.review_kind),
            _jsonb_param("p_review_payload", dict(command.review_payload)),
            _text_param("p_idempotency_key", command.idempotency_key),
            _text_param("p_trace_id", command.trace_id),
        ).label("goal_run")
    )


def goal_run_record_review_decision_statement(
    command: GoalRunReviewDecisionCommand,
) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_record_review_decision(
            _uuid_param("p_actor_id", command.actor_id),
            _uuid_param("p_goal_run_id", command.goal_run_id),
            _bigint_param("p_expected_version", command.expected_version),
            _uuid_param("p_fencing_token", command.fencing_token),
            _uuid_param("p_review_item_id", command.review_item_id),
            _text_param("p_review_snapshot_sha256", command.review_snapshot_sha256),
            _text_param("p_decision", command.decision.value),
            _text_param("p_reason", command.reason),
            _text_param("p_idempotency_key", command.idempotency_key),
            _text_param("p_trace_id", command.trace_id),
        ).label("goal_run")
    )


def goal_run_cancel_statement(command: GoalRunCancelCommand) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_cancel(
            _uuid_param("p_actor_id", command.actor_id),
            _uuid_param("p_goal_run_id", command.goal_run_id),
            _bigint_param("p_expected_version", command.expected_version),
            _uuid_param("p_fencing_token", command.fencing_token),
            _text_param("p_reason", command.reason),
            _text_param("p_idempotency_key", command.idempotency_key),
            _text_param("p_trace_id", command.trace_id),
        ).label("goal_run")
    )


def goal_run_resume_statement(command: GoalRunResumeCommand) -> sa.Select[tuple[Any]]:
    return sa.select(
        sa.func.careerops.goal_run_resume(
            _uuid_param("p_actor_id", command.actor_id),
            _uuid_param("p_goal_run_id", command.goal_run_id),
            _bigint_param("p_expected_version", command.expected_version),
            _uuid_param("p_fencing_token", command.fencing_token),
            _text_param("p_idempotency_key", command.idempotency_key),
            _text_param("p_trace_id", command.trace_id),
        ).label("goal_run")
    )


def _uuid_param(key: str, value: object) -> sa.BindParameter[Any]:
    return sa.bindparam(key, value, type_=postgresql.UUID(as_uuid=True))


def _optional_uuid_param(key: str, value: object | None) -> sa.BindParameter[Any]:
    return sa.bindparam(key, value, type_=postgresql.UUID(as_uuid=True))


def _text_param(key: str, value: str) -> sa.BindParameter[str]:
    return sa.bindparam(key, value, type_=sa.Text())


def _optional_text_param(key: str, value: str | None) -> sa.BindParameter[Any]:
    return sa.bindparam(key, value, type_=sa.Text())


def _integer_param(key: str, value: int) -> sa.BindParameter[int]:
    return sa.bindparam(key, value, type_=sa.Integer())


def _bigint_param(key: str, value: int) -> sa.BindParameter[int]:
    return sa.bindparam(key, value, type_=sa.BigInteger())


def _jsonb_param(key: str, value: Mapping[str, object]) -> sa.BindParameter[Mapping[str, object]]:
    return sa.bindparam(key, dict(value), type_=postgresql.JSONB())


def _raise_sanitized_database_error(exc: DBAPIError) -> NoReturn:
    sqlstate = _sqlstate(exc)
    reason_code = _SQLSTATE_REASON_CODES.get(sqlstate or "", "GOAL_RUN_DATABASE_ERROR")
    raise GoalRunRepositoryError(reason_code, sqlstate=sqlstate) from None


def _sqlstate(exc: DBAPIError) -> str | None:
    origin = exc.orig
    value = getattr(origin, "sqlstate", None) or getattr(origin, "pgcode", None)
    if isinstance(value, str) and len(value) == 5:
        return value
    return None


def compile_query_for_test(
    statement: sa.ClauseElement | Executable,
    *,
    literal_binds: bool = True,
) -> str:
    return str(
        cast("sa.ClauseElement", statement).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": literal_binds},
        )
    )


__all__ = [
    "PostgresGoalRunRepository",
    "compile_query_for_test",
    "goal_run_cancel_statement",
    "goal_run_checkpoint_statement",
    "goal_run_create_statement",
    "goal_run_list_owner_statement",
    "goal_run_lookup_create_statement",
    "goal_run_record_review_decision_statement",
    "goal_run_request_review_statement",
    "goal_run_resume_statement",
    "goal_run_status_statement",
]
