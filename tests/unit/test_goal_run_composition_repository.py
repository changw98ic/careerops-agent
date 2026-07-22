from __future__ import annotations

from typing import cast
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from careerops.infrastructure.database.goal_run_composition import (
    GoalRunCandidateQuery,
    GoalRunDispatchGmailCommand,
    GoalRunGmailCompositionRepositoryError,
    GoalRunInspectGmailQuery,
    GoalRunPrepareGmailCommand,
    GoalRunRecordGmailMatchCommand,
    PostgresGoalRunCompositionRepository,
    PostgresGoalRunGmailCompositionRepository,
    compile_query_for_test,
    goal_run_candidate_from_mapping,
    goal_run_candidate_jobs_statement,
    goal_run_dispatch_gmail_statement,
    goal_run_gmail_composition_record_from_mapping,
    goal_run_inspect_gmail_statement,
    goal_run_prepare_gmail_statement,
    goal_run_record_gmail_match_statement,
)

ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
GOAL_RUN_ID = UUID("00000000-0000-0000-0000-000000000002")
SOURCE_ROW_ID = UUID("00000000-0000-0000-0000-000000000003")
CRAWLER_RUN_ID = UUID("00000000-0000-0000-0000-000000000004")
RUN_EVENT_ID = UUID("00000000-0000-0000-0000-000000000005")
CANONICAL_JOB_ID = UUID("00000000-0000-0000-0000-000000000006")
JOB_POSTING_ID = UUID("00000000-0000-0000-0000-000000000007")
JOB_POSTING_VERSION_ID = UUID("00000000-0000-0000-0000-000000000008")
COMPOSITION_ID = UUID("00000000-0000-0000-0000-000000000009")
ACTION_INTENT_ID = UUID("00000000-0000-0000-0000-00000000000a")
PAYLOAD_VERSION_ID = UUID("00000000-0000-0000-0000-00000000000b")
APPROVAL_REQUEST_ID = UUID("00000000-0000-0000-0000-00000000000c")
AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-00000000000d")
OUTBOX_EVENT_ID = UUID("00000000-0000-0000-0000-00000000000e")
RESERVATION_ID = UUID("00000000-0000-0000-0000-00000000000f")
SHA = "a" * 64


def candidate_query() -> GoalRunCandidateQuery:
    return GoalRunCandidateQuery(
        actor_id=ACTOR_ID,
        goal_run_id=GOAL_RUN_ID,
        source_row_id=SOURCE_ROW_ID,
        crawler_run_id=CRAWLER_RUN_ID,
    )


def match_command() -> GoalRunRecordGmailMatchCommand:
    return GoalRunRecordGmailMatchCommand(
        actor_id=ACTOR_ID,
        goal_run_id=GOAL_RUN_ID,
        source_row_id=SOURCE_ROW_ID,
        crawler_run_id=CRAWLER_RUN_ID,
        canonical_job_id=CANONICAL_JOB_ID,
        job_posting_id=JOB_POSTING_ID,
        job_posting_version_id=JOB_POSTING_VERSION_ID,
        match_score=0.75,
        match_reasons={"matched_keywords": ["python"], "reason_codes": ["MEETS_MIN_SCORE"]},
        match_snapshot_sha256=SHA,
    )


def prepare_command() -> GoalRunPrepareGmailCommand:
    return GoalRunPrepareGmailCommand(
        actor_id=ACTOR_ID,
        goal_run_id=GOAL_RUN_ID,
        target={
            "target_host": "gmail.googleapis.com",
            "channel": "gmail:send",
            "recipient_sha256": "b" * 64,
            "subject_sha256": "c" * 64,
            "body_sha256": "d" * 64,
        },
        payload={"version": "gmail-send-payload.v1", "subject": "Hello"},
        attachment_refs=[{"filename": "resume.pdf", "sha256": "e" * 64}],
    )


def composition_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "composition_id": COMPOSITION_ID,
        "goal_run_id": GOAL_RUN_ID,
        "actor_user_id": ACTOR_ID,
        "source_row_id": SOURCE_ROW_ID,
        "crawler_run_id": CRAWLER_RUN_ID,
        "crawler_run_event_id": RUN_EVENT_ID,
        "job_posting_id": JOB_POSTING_ID,
        "job_posting_version_id": JOB_POSTING_VERSION_ID,
        "canonical_job_id": CANONICAL_JOB_ID,
        "state": "draft_prepared",
        "receipt_state": "created",
        "payload_hash": SHA,
        "action_intent_id": ACTION_INTENT_ID,
        "payload_version_id": PAYLOAD_VERSION_ID,
        "approval_request_id": APPROVAL_REQUEST_ID,
        "authorization_id": AUTHORIZATION_ID,
        "outbox_event_id": OUTBOX_EVENT_ID,
        "reservation_id": RESERVATION_ID,
    }
    row.update(overrides)
    return row


def candidate_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "source_row_id": SOURCE_ROW_ID,
        "crawler_run_id": CRAWLER_RUN_ID,
        "crawler_run_event_id": RUN_EVENT_ID,
        "canonical_job_id": CANONICAL_JOB_ID,
        "job_posting_id": JOB_POSTING_ID,
        "job_posting_version_id": JOB_POSTING_VERSION_ID,
        "source_id": "greenhouse",
        "discovered_job_id": "job-123",
        "company_name": "Example AI",
        "title": "Platform Engineer",
        "canonical_url": "https://example.com/jobs/123",
        "description": "Build Python systems",
        "keywords": ["Python", "Distributed"],
        "location": "Remote - US",
        "locations": ["Remote - US", "New York, NY"],
        "department": "Platform",
        "apply_url": "https://example.com/jobs/123/apply",
        "employment_type": "Full-time",
        "work_mode": "Remote",
        "remote": True,
        "seniority": "Senior",
        "salary": {"salaryRange": {"currency": "USD", "min": 150000}},
        "authorization": {"visaSponsorship": "available"},
        "evidence_sha256": SHA,
        "structured_data": {
            "location": "Remote - US",
            "work_mode": "Remote",
            "salary": {"salaryRange": {"currency": "USD", "min": 150000}},
        },
    }
    row.update(overrides)
    return row


def test_candidate_statement_is_source_and_run_scoped_and_read_only() -> None:
    sql = compile_query_for_test(
        goal_run_candidate_jobs_statement(candidate_query()),
        literal_binds=False,
    )

    assert "crawler_job_ingestion_evidence" in sql
    assert "crawler_source_runs" in sql
    assert "event_kind = %(event_kind_1)s" in sql
    assert "p_actor_id" in sql
    assert "p_goal_run_id" in sql
    assert "p_source_row_id" in sql
    assert "p_crawler_run_id" in sql
    assert "p_limit" in sql
    assert " AS location" in sql
    assert " AS locations" in sql
    assert " AS apply_url" in sql
    assert " AS employment_type" in sql
    assert " AS work_mode" in sql
    assert " AS seniority" in sql
    assert " AS salary" in sql
    assert "authorization" in sql
    assert "row_number() OVER" in sql
    assert "PARTITION BY careerops.crawler_job_ingestion_evidence.canonical_job_id" in sql
    assert "goal_run_ranked_candidate_jobs._canonical_rank =" in sql
    assert sql.index("goal_run_ranked_candidate_jobs._canonical_rank =") < sql.index("LIMIT")
    assert "INSERT INTO careerops" not in sql
    assert "UPDATE careerops" not in sql
    assert "gmail_send_" not in sql


def test_wrapper_statement_builders_call_only_goal_run_gmail_functions() -> None:
    statements = [
        compile_query_for_test(
            goal_run_record_gmail_match_statement(match_command()),
            literal_binds=False,
        ),
        compile_query_for_test(
            goal_run_prepare_gmail_statement(prepare_command()),
            literal_binds=False,
        ),
        compile_query_for_test(
            goal_run_dispatch_gmail_statement(GoalRunDispatchGmailCommand(ACTOR_ID, GOAL_RUN_ID)),
            literal_binds=False,
        ),
        compile_query_for_test(
            goal_run_inspect_gmail_statement(GoalRunInspectGmailQuery(ACTOR_ID, GOAL_RUN_ID)),
            literal_binds=False,
        ),
    ]

    joined = "\n".join(statements)
    assert "careerops.goal_run_record_gmail_match" in joined
    assert "careerops.goal_run_prepare_gmail" in joined
    assert "p_payload_hash" in joined
    assert "careerops.goal_run_dispatch_gmail" in joined
    assert "careerops.goal_run_inspect_gmail" in joined
    assert "careerops.gmail_send_create_draft" not in joined
    assert "careerops.gmail_send_review_draft" not in joined
    assert "careerops.gmail_send_reserve_and_enqueue" not in joined
    assert "INSERT INTO careerops" not in joined
    assert "UPDATE careerops" not in joined


def test_candidate_mapping_normalizes_keywords_and_rejects_invalid_hashes() -> None:
    candidate = goal_run_candidate_from_mapping(candidate_row())

    assert candidate.source_id == "greenhouse"
    assert candidate.discovered_job_id == "job-123"
    assert candidate.keywords == ("distributed", "python")
    assert candidate.location == "Remote - US"
    assert candidate.locations == ("new york, ny", "remote - us")
    assert candidate.department == "Platform"
    assert candidate.apply_url == "https://example.com/jobs/123/apply"
    assert candidate.employment_type == "Full-time"
    assert candidate.work_mode == "Remote"
    assert candidate.remote is True
    assert candidate.seniority == "Senior"
    assert candidate.salary == {"salaryRange": {"currency": "USD", "min": 150000}}
    assert candidate.authorization == {"visaSponsorship": "available"}
    assert candidate.structured_data == {
        "location": "Remote - US",
        "work_mode": "Remote",
        "salary": {"salaryRange": {"currency": "USD", "min": 150000}},
    }

    with pytest.raises(ValueError, match="evidence_sha256"):
        goal_run_candidate_from_mapping(candidate_row(evidence_sha256="bad"))

    with pytest.raises(ValueError, match="keywords items must be strings"):
        goal_run_candidate_from_mapping(candidate_row(keywords=["Python", 123]))


def test_composition_mapping_accepts_string_uuids_from_jsonb_and_validates_state() -> None:
    row = composition_row(
        composition_id=str(COMPOSITION_ID),
        goal_run_id=str(GOAL_RUN_ID),
        actor_user_id=str(ACTOR_ID),
        source_row_id=str(SOURCE_ROW_ID),
        crawler_run_id=str(CRAWLER_RUN_ID),
        crawler_run_event_id=str(RUN_EVENT_ID),
        job_posting_id=str(JOB_POSTING_ID),
        job_posting_version_id=str(JOB_POSTING_VERSION_ID),
        canonical_job_id=str(CANONICAL_JOB_ID),
    )

    record = goal_run_gmail_composition_record_from_mapping(row)

    assert record.composition_id == COMPOSITION_ID
    assert record.payload_hash == SHA
    assert record.outbox_event_id == OUTBOX_EVENT_ID

    with pytest.raises(ValueError, match="composition state"):
        goal_run_gmail_composition_record_from_mapping(composition_row(state="bad"))


class FakeScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class FakeMappingsResult:
    def __init__(self, value: list[dict[str, object]]) -> None:
        self._value = value

    def mappings(self) -> FakeMappingsResult:
        return self

    def all(self) -> list[dict[str, object]]:
        return self._value


class FakeConnection:
    def __init__(self, value: object) -> None:
        self.value = value
        self.statements: list[str] = []

    def in_transaction(self) -> bool:
        return True

    def execute(self, statement: sa.sql.Executable) -> FakeScalarResult | FakeMappingsResult:
        self.statements.append(
            compile_query_for_test(cast("sa.ClauseElement", statement), literal_binds=False)
        )
        if isinstance(self.value, list):
            return FakeMappingsResult(cast("list[dict[str, object]]", self.value))
        return FakeScalarResult(self.value)


def test_repository_requires_explicit_transaction() -> None:
    class NonTransactionalConnection:
        def in_transaction(self) -> bool:
            return False

    with pytest.raises(ValueError, match="explicit transaction"):
        PostgresGoalRunGmailCompositionRepository(cast("Connection", NonTransactionalConnection()))


def test_repository_projects_candidates_and_function_json_results() -> None:
    candidates_connection = FakeConnection([candidate_row()])
    candidates_repository = PostgresGoalRunCompositionRepository(
        cast("Connection", candidates_connection)
    )

    candidates = candidates_repository.list_candidates(candidate_query())

    assert len(candidates) == 1
    assert candidates[0].canonical_job_id == CANONICAL_JOB_ID
    assert "crawler_job_ingestion_evidence" in candidates_connection.statements[0]

    function_connection = FakeConnection(composition_row(state="dispatch_enqueued"))
    function_repository = PostgresGoalRunGmailCompositionRepository(
        cast("Connection", function_connection)
    )

    record = function_repository.dispatch_gmail(GoalRunDispatchGmailCommand(ACTOR_ID, GOAL_RUN_ID))

    assert record.outbox_event_id == OUTBOX_EVENT_ID
    assert "careerops.goal_run_dispatch_gmail" in function_connection.statements[0]


def test_repository_rejects_missing_inspection_result_as_invalid() -> None:
    connection = FakeConnection(None)

    with pytest.raises(GoalRunGmailCompositionRepositoryError) as error:
        PostgresGoalRunGmailCompositionRepository(cast("Connection", connection)).inspect_gmail(
            GoalRunInspectGmailQuery(ACTOR_ID, GOAL_RUN_ID)
        )

    assert error.value.reason_code == "GOAL_RUN_GMAIL_COMPOSITION_RESULT_INVALID"


def test_repository_sanitizes_sqlstate_without_database_message() -> None:
    class OriginError(Exception):
        sqlstate = "23514"

    class FailingConnection:
        def in_transaction(self) -> bool:
            return True

        def execute(self, statement: sa.sql.Executable) -> FakeScalarResult:
            del statement
            raise DBAPIError("select leaked_secret", {"secret": "value"}, OriginError("detail"))

    with pytest.raises(GoalRunGmailCompositionRepositoryError) as error:
        PostgresGoalRunGmailCompositionRepository(
            cast("Connection", FailingConnection())
        ).record_match(
            match_command(),
        )

    assert error.value.reason_code == "GOAL_RUN_GMAIL_COMPOSITION_STATE_CONFLICT"
    assert error.value.sqlstate == "23514"
    assert "leaked_secret" not in str(error.value)
    assert "secret" not in str(error.value)
