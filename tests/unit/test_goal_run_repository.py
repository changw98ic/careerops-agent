from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from careerops.application.goal_run import (
    GoalRunCancelCommand,
    GoalRunCheckpointCommand,
    GoalRunCheckpointOutcome,
    GoalRunCreateCommand,
    GoalRunCreateLookupQuery,
    GoalRunListOwnerQuery,
    GoalRunPhase,
    GoalRunRepositoryError,
    GoalRunResumeCommand,
    GoalRunReviewDecision,
    GoalRunReviewDecisionCommand,
    GoalRunReviewRequestCommand,
    GoalRunStatus,
    GoalRunStatusQuery,
)
from careerops.infrastructure.database.goal_run import (
    PostgresGoalRunRepository,
    compile_query_for_test,
    goal_run_cancel_statement,
    goal_run_checkpoint_statement,
    goal_run_create_statement,
    goal_run_list_owner_statement,
    goal_run_lookup_create_statement,
    goal_run_record_review_decision_statement,
    goal_run_request_review_statement,
    goal_run_resume_statement,
    goal_run_status_statement,
)

NOW = datetime(2026, 7, 20, 12, 30, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
RUN_ID = UUID("00000000-0000-0000-0000-000000000003")
TOKEN = UUID("00000000-0000-0000-0000-000000000002")
REGISTRY_ID = UUID("00000000-0000-0000-0000-000000000004")
REVIEW_ITEM_ID = UUID("00000000-0000-0000-0000-000000000005")
SNAPSHOT = "a" * 64


def create_command() -> GoalRunCreateCommand:
    return GoalRunCreateCommand(
        actor_id=ACTOR_ID,
        idempotency_key="goal-run:create:trace-1",
        goal_kind="job-search",
        goal="Find backend roles with bounded autopilot review.",
        context={"hosts": ["greenhouse.io"]},
        registry_id=REGISTRY_ID,
        source_id="greenhouse",
        max_records=100,
        temporal_workflow_id="goal-run-workflow-1",
        trace_id="trace-1",
    )


def checkpoint_command(
    *,
    status: GoalRunStatus = GoalRunStatus.RUNNING,
    phase: GoalRunPhase = GoalRunPhase.DISCOVERY,
    outcome: GoalRunCheckpointOutcome = GoalRunCheckpointOutcome.PROGRESS,
) -> GoalRunCheckpointCommand:
    return GoalRunCheckpointCommand(
        actor_id=ACTOR_ID,
        goal_run_id=RUN_ID,
        expected_version=1,
        fencing_token=TOKEN,
        phase=phase,
        status=status,
        outcome=outcome,
        checkpoint={"cursor": "page-2"},
        last_error_code=None,
        idempotency_key="goal-run:checkpoint:trace-2",
        trace_id="trace-2",
    )


def review_request_command() -> GoalRunReviewRequestCommand:
    return GoalRunReviewRequestCommand(
        actor_id=ACTOR_ID,
        goal_run_id=RUN_ID,
        expected_version=2,
        fencing_token=TOKEN,
        review_item_id=REVIEW_ITEM_ID,
        review_snapshot_sha256=SNAPSHOT,
        review_kind="manual-approval",
        review_payload={"reason": "needs-review"},
        idempotency_key="goal-run:review-request:trace-3",
        trace_id="trace-3",
    )


def review_decision_command() -> GoalRunReviewDecisionCommand:
    return GoalRunReviewDecisionCommand(
        actor_id=ACTOR_ID,
        goal_run_id=RUN_ID,
        expected_version=3,
        fencing_token=TOKEN,
        review_item_id=REVIEW_ITEM_ID,
        review_snapshot_sha256=SNAPSHOT,
        decision=GoalRunReviewDecision.APPROVE,
        reason="approved by authenticated owner",
        idempotency_key="goal-run:review-decision:trace-4",
        trace_id="trace-4",
    )


def test_commands_validate_actor_owned_idempotent_fenced_inputs() -> None:
    with pytest.raises(ValueError, match="create lookup idempotency_key"):
        GoalRunCreateLookupQuery(actor_id=ACTOR_ID, idempotency_key="bad key")

    with pytest.raises(ValueError, match="idempotency_key"):
        GoalRunCreateCommand(
            actor_id=ACTOR_ID,
            idempotency_key="bad key",
            goal_kind="job-search",
            goal="goal",
            context={},
            registry_id=None,
            source_id=None,
            max_records=10,
            temporal_workflow_id=None,
            trace_id="trace-1",
        )

    with pytest.raises(ValueError, match="positive"):
        GoalRunResumeCommand(
            actor_id=ACTOR_ID,
            goal_run_id=uuid4(),
            expected_version=0,
            fencing_token=uuid4(),
            idempotency_key="goal-run:resume:trace-1",
            trace_id="trace-1",
        )

    with pytest.raises(ValueError, match="finalize"):
        checkpoint_command(status=GoalRunStatus.COMPLETED, phase=GoalRunPhase.COMPLETED)

    terminal = checkpoint_command(
        status=GoalRunStatus.COMPLETED,
        phase=GoalRunPhase.COMPLETED,
        outcome=GoalRunCheckpointOutcome.FINALIZE,
    )
    assert terminal.status is GoalRunStatus.COMPLETED

    with pytest.raises(ValueError, match="sha256"):
        GoalRunReviewDecisionCommand(
            actor_id=ACTOR_ID,
            goal_run_id=RUN_ID,
            expected_version=1,
            fencing_token=TOKEN,
            review_item_id=REVIEW_ITEM_ID,
            review_snapshot_sha256="not-a-snapshot",
            decision=GoalRunReviewDecision.REJECT,
            reason="reject",
            idempotency_key="goal-run:review-decision:trace-5",
            trace_id="trace-5",
        )


def test_statement_builders_call_only_goal_run_definer_functions() -> None:
    statements = [
        goal_run_lookup_create_statement(
            GoalRunCreateLookupQuery(
                actor_id=ACTOR_ID,
                idempotency_key=create_command().idempotency_key,
            )
        ),
        goal_run_create_statement(create_command()),
        goal_run_status_statement(GoalRunStatusQuery(actor_id=ACTOR_ID, goal_run_id=RUN_ID)),
        goal_run_list_owner_statement(GoalRunListOwnerQuery(actor_id=ACTOR_ID, limit=50)),
        goal_run_checkpoint_statement(checkpoint_command()),
        goal_run_request_review_statement(review_request_command()),
        goal_run_record_review_decision_statement(review_decision_command()),
        goal_run_cancel_statement(
            GoalRunCancelCommand(
                actor_id=ACTOR_ID,
                goal_run_id=RUN_ID,
                expected_version=4,
                fencing_token=TOKEN,
                reason="user requested stop",
                idempotency_key="goal-run:cancel:trace-6",
                trace_id="trace-6",
            )
        ),
        goal_run_resume_statement(
            GoalRunResumeCommand(
                actor_id=ACTOR_ID,
                goal_run_id=RUN_ID,
                expected_version=5,
                fencing_token=TOKEN,
                idempotency_key="goal-run:resume:trace-7",
                trace_id="trace-7",
            )
        ),
    ]

    rendered = "\n".join(
        compile_query_for_test(statement, literal_binds=False) for statement in statements
    )

    for function_name in (
        "careerops.goal_run_create",
        "careerops.goal_run_lookup_create",
        "careerops.goal_run_status",
        "careerops.goal_run_list_owner",
        "careerops.goal_run_checkpoint",
        "careerops.goal_run_request_review",
        "careerops.goal_run_record_review_decision",
        "careerops.goal_run_cancel",
        "careerops.goal_run_resume",
    ):
        assert function_name in rendered
    for parameter in (
        "p_actor_id",
        "p_registry_id",
        "p_source_id",
        "p_max_records",
        "p_temporal_workflow_id",
        "p_expected_version",
        "p_fencing_token",
        "p_outcome",
        "p_last_error_code",
        "p_review_item_id",
        "p_review_snapshot_sha256",
        "p_decision",
        "p_idempotency_key",
    ):
        assert parameter in rendered
    assert "FROM careerops." not in rendered
    assert "INSERT INTO careerops." not in rendered
    assert "UPDATE careerops." not in rendered


class FakeScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class FakeConnection:
    def __init__(self, values: list[object]) -> None:
        self._values = iter(values)
        self.statements: list[str] = []

    def in_transaction(self) -> bool:
        return True

    def execute(self, statement: sa.sql.Executable) -> FakeScalarResult:
        self.statements.append(
            compile_query_for_test(cast("sa.ClauseElement", statement), literal_binds=False)
        )
        return FakeScalarResult(next(self._values))


def row(
    *,
    version: int = 1,
    status: str = "running",
    phase: str = "discovery",
    review_decision: str | None = None,
) -> dict[str, object]:
    return {
        "goal_run_id": RUN_ID,
        "actor_id": ACTOR_ID,
        "goal_kind": "job-search",
        "goal": "Find backend roles with bounded autopilot review.",
        "status": status,
        "phase": phase,
        "version": version,
        "fencing_token": TOKEN,
        "context": {"hosts": ["greenhouse.io"]},
        "checkpoint": {"cursor": "page-2"},
        "idempotency_key": "goal-run:create:trace-1",
        "trace_id": "trace-1",
        "created_at": NOW,
        "updated_at": NOW,
        "registry_id": REGISTRY_ID,
        "source_id": "greenhouse",
        "max_records": 100,
        "temporal_workflow_id": "goal-run-workflow-1",
        "review_item_id": REVIEW_ITEM_ID,
        "review_snapshot_sha256": SNAPSHOT,
        "review_decision": review_decision,
        "review_kind": "manual-approval",
        "review_payload": {"reason": "needs-review"},
        "last_error_code": None,
    }


def test_repository_requires_explicit_transaction() -> None:
    class NonTransactionalConnection:
        def in_transaction(self) -> bool:
            return False

    with pytest.raises(ValueError, match="explicit transaction"):
        PostgresGoalRunRepository(cast("Connection", NonTransactionalConnection()))


def test_repository_projects_json_function_result_to_typed_record_and_owner_list() -> None:
    connection = FakeConnection(
        [
            {
                "record": row(version=2),
                "idempotency_key": "goal-run:create:trace-1",
                "newly_created": True,
            },
            {"records": [row(version=2)], "next_cursor": "cursor-2"},
        ]
    )
    repository = PostgresGoalRunRepository(cast("Connection", connection))

    created = repository.create(create_command())
    page = repository.list_owner(GoalRunListOwnerQuery(actor_id=ACTOR_ID, limit=50))

    assert created.goal_run_id == RUN_ID
    assert created.fencing_token == TOKEN
    assert created.record.registry_id == REGISTRY_ID
    assert created.record.max_records == 100
    assert created.record.status is GoalRunStatus.RUNNING
    assert created.newly_created is True
    assert page.records[0].goal_run_id == RUN_ID
    assert page.next_cursor == "cursor-2"
    assert "careerops.goal_run_create" in connection.statements[0]
    assert "careerops.goal_run_list_owner" in connection.statements[1]


def test_repository_create_lookup_returns_current_record_or_none() -> None:
    found = {
        "record": row(version=2),
        "idempotency_key": "goal-run:create:trace-1",
        "newly_created": False,
    }
    connection = FakeConnection([found, None])
    repository = PostgresGoalRunRepository(cast("Connection", connection))
    query = GoalRunCreateLookupQuery(
        actor_id=ACTOR_ID,
        idempotency_key="goal-run:create:trace-1",
    )

    existing = repository.find_create(query)
    missing = repository.find_create(query)

    assert existing is not None
    assert existing.record.version == 2
    assert existing.newly_created is False
    assert missing is None
    assert all("careerops.goal_run_lookup_create" in item for item in connection.statements)


def test_repository_binds_authenticated_review_decision() -> None:
    connection = FakeConnection(
        [
            {
                "record": row(
                    version=4,
                    status="running",
                    phase="dispatch",
                    review_decision="approve",
                ),
                "idempotency_key": "goal-run:review-decision:trace-4",
                "newly_created": True,
            }
        ]
    )

    result = PostgresGoalRunRepository(cast("Connection", connection)).record_review_decision(
        review_decision_command()
    )

    assert result.record.review_item_id == REVIEW_ITEM_ID
    assert result.record.review_snapshot_sha256 == SNAPSHOT
    assert result.record.review_decision is GoalRunReviewDecision.APPROVE
    assert "careerops.goal_run_record_review_decision" in connection.statements[0]
    assert "p_review_item_id" in connection.statements[0]
    assert "p_review_snapshot_sha256" in connection.statements[0]
    assert "p_decision" in connection.statements[0]


@pytest.mark.parametrize(
    ("sqlstate", "reason_code"),
    [
        ("23503", "GOAL_RUN_NOT_FOUND"),
        ("23505", "GOAL_RUN_IDEMPOTENCY_CONFLICT"),
        ("23514", "GOAL_RUN_STATE_CONFLICT"),
    ],
)
def test_repository_sanitizes_sqlstate_without_database_message(
    sqlstate: str,
    reason_code: str,
) -> None:
    class OriginError(Exception):
        def __init__(self, sqlstate: str) -> None:
            super().__init__("detail")
            self.sqlstate = sqlstate

    class FailingConnection:
        def in_transaction(self) -> bool:
            return True

        def execute(self, statement: sa.sql.Executable) -> FakeScalarResult:
            del statement
            raise DBAPIError("select leaked_secret", {"secret": "value"}, OriginError(sqlstate))

    with pytest.raises(GoalRunRepositoryError) as error:
        PostgresGoalRunRepository(cast("Connection", FailingConnection())).checkpoint(
            checkpoint_command()
        )

    assert error.value.reason_code == reason_code
    assert error.value.sqlstate == sqlstate
    assert "leaked_secret" not in str(error.value)
    assert "secret" not in str(error.value)
