from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from careerops.application.goal_run_crawler_discovery import (
    GoalRunCrawlerDiscoveryRepositoryError,
    GoalRunCrawlerDiscoveryState,
    GoalRunReviewedCrawlerResult,
    GoalRunReviewedCrawlerResultQuery,
    goal_run_reviewed_crawler_result_from_mapping,
)
from careerops.infrastructure.database.goal_run_crawler_discovery import (
    PostgresGoalRunCrawlerDiscoveryRepository,
    compile_query_for_test,
    goal_run_reviewed_crawler_result_statement,
)

ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
RUN_ID = UUID("00000000-0000-0000-0000-000000000002")
REGISTRY_ID = UUID("00000000-0000-0000-0000-000000000003")
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000004")
RESULT_ID = UUID("00000000-0000-0000-0000-000000000005")
OUTBOX_EVENT_ID = UUID("00000000-0000-0000-0000-000000000006")
SOURCE_ROW_ID = UUID("00000000-0000-0000-0000-000000000007")
NOW = datetime(2026, 7, 20, 12, 30, tzinfo=UTC)
SHA = "a" * 64


def query() -> GoalRunReviewedCrawlerResultQuery:
    return GoalRunReviewedCrawlerResultQuery(
        actor_id=ACTOR_ID,
        goal_run_id=RUN_ID,
        registry_id=REGISTRY_ID,
        source_id="greenhouse",
    )


def ready_row() -> dict[str, object]:
    return {
        "state": "ready",
        "registry_id": REGISTRY_ID,
        "source_row_id": SOURCE_ROW_ID,
        "source_id": "greenhouse",
        "request_id": REQUEST_ID,
        "result_id": RESULT_ID,
        "outbox_event_id": OUTBOX_EVENT_ID,
        "reviewed_plan_sha256": SHA,
        "source_sha256": "b" * 64,
        "command_sha256": "c" * 64,
        "output_dir": "datasets/raw/ats/greenhouse",
        "adapter": "recruitment.public_ats_feed",
        "completed_at": NOW,
        "error_code": None,
    }


def test_query_and_mapping_validate_bounded_identities_hashes_and_paths() -> None:
    with pytest.raises(ValueError, match="source_id"):
        GoalRunReviewedCrawlerResultQuery(
            actor_id=ACTOR_ID,
            goal_run_id=RUN_ID,
            registry_id=REGISTRY_ID,
            source_id="../bad",
        )

    result = goal_run_reviewed_crawler_result_from_mapping(ready_row())

    assert result.state is GoalRunCrawlerDiscoveryState.READY
    assert result.registry_id == REGISTRY_ID
    assert result.source_row_id == SOURCE_ROW_ID
    assert result.source_id == "greenhouse"
    assert result.request_id == REQUEST_ID
    assert result.result_id == RESULT_ID
    assert result.outbox_event_id == OUTBOX_EVENT_ID
    assert result.reviewed_plan_sha256 == SHA
    assert result.output_dir == "datasets/raw/ats/greenhouse"
    assert result.adapter == "recruitment.public_ats_feed"
    assert result.completed_at == NOW

    with pytest.raises(ValueError, match="sha256"):
        goal_run_reviewed_crawler_result_from_mapping({**ready_row(), "source_sha256": "bad"})

    with pytest.raises(ValueError, match="safe relative path"):
        goal_run_reviewed_crawler_result_from_mapping(
            {**ready_row(), "output_dir": "../datasets/raw"}
        )

    with pytest.raises(ValueError, match="result_id"):
        goal_run_reviewed_crawler_result_from_mapping({**ready_row(), "result_id": None})

    with pytest.raises(ValueError, match="source_id"):
        goal_run_reviewed_crawler_result_from_mapping({**ready_row(), "source_id": "Bad_Source"})


def test_non_ready_states_allow_pre_request_waiting_and_require_error_evidence() -> None:
    waiting = GoalRunReviewedCrawlerResult(
        state=GoalRunCrawlerDiscoveryState.WAITING_REVIEW,
        request_id=REQUEST_ID,
    )
    waiting_before_request = GoalRunReviewedCrawlerResult(
        state=GoalRunCrawlerDiscoveryState.WAITING_REVIEW,
    )
    failed = GoalRunReviewedCrawlerResult(
        state=GoalRunCrawlerDiscoveryState.FAILED,
        error_code="CRAWLER_EXECUTION_FAILED",
    )
    reconciliation = GoalRunReviewedCrawlerResult(
        state=GoalRunCrawlerDiscoveryState.RECONCILIATION_REQUIRED,
        error_code="CRAWLER_RESULT_MISMATCH",
    )

    assert waiting.state is GoalRunCrawlerDiscoveryState.WAITING_REVIEW
    assert waiting_before_request.request_id is None
    assert failed.error_code == "CRAWLER_EXECUTION_FAILED"
    assert reconciliation.state is GoalRunCrawlerDiscoveryState.RECONCILIATION_REQUIRED
    with pytest.raises(ValueError, match="error_code"):
        GoalRunReviewedCrawlerResult(state=GoalRunCrawlerDiscoveryState.FAILED)


def test_statement_builder_calls_only_goal_run_reviewed_crawler_result_function() -> None:
    rendered = compile_query_for_test(
        goal_run_reviewed_crawler_result_statement(query()),
        literal_binds=False,
    )

    assert "careerops.goal_run_reviewed_crawler_result" in rendered
    assert "p_actor_id" in rendered
    assert "p_goal_run_id" in rendered
    assert "p_registry_id" in rendered
    assert "p_source_id" in rendered
    assert "FROM careerops." not in rendered
    assert "INSERT INTO careerops." not in rendered
    assert "UPDATE careerops." not in rendered


class FakeScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class FakeConnection:
    def __init__(self, value: object) -> None:
        self.value = value
        self.statement: str | None = None

    def in_transaction(self) -> bool:
        return True

    def execute(self, statement: sa.sql.Executable) -> FakeScalarResult:
        self.statement = compile_query_for_test(
            cast("sa.ClauseElement", statement),
            literal_binds=False,
        )
        return FakeScalarResult(self.value)


def test_repository_requires_explicit_transaction() -> None:
    class NonTransactionalConnection:
        def in_transaction(self) -> bool:
            return False

    with pytest.raises(ValueError, match="explicit transaction"):
        PostgresGoalRunCrawlerDiscoveryRepository(cast("Connection", NonTransactionalConnection()))


def test_repository_projects_json_function_result_to_typed_result() -> None:
    connection = FakeConnection(ready_row())
    repository = PostgresGoalRunCrawlerDiscoveryRepository(cast("Connection", connection))

    result = repository.reviewed_result(query())

    assert result.state is GoalRunCrawlerDiscoveryState.READY
    assert result.result_id == RESULT_ID
    assert connection.statement is not None
    assert "careerops.goal_run_reviewed_crawler_result" in connection.statement


def test_repository_sanitizes_sqlstate_without_database_message() -> None:
    class OriginError(Exception):
        sqlstate = "42501"

    class FailingConnection:
        def in_transaction(self) -> bool:
            return True

        def execute(self, statement: sa.sql.Executable) -> FakeScalarResult:
            del statement
            raise DBAPIError("select leaked_secret", {"secret": "value"}, OriginError("detail"))

    with pytest.raises(GoalRunCrawlerDiscoveryRepositoryError) as error:
        PostgresGoalRunCrawlerDiscoveryRepository(
            cast("Connection", FailingConnection())
        ).reviewed_result(query())

    assert error.value.reason_code == "GOAL_RUN_CRAWLER_DISCOVERY_FORBIDDEN"
    assert error.value.sqlstate == "42501"
    assert "leaked_secret" not in str(error.value)
    assert "secret" not in str(error.value)
