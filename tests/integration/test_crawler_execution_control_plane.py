from __future__ import annotations

import inspect
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine

from careerops.application.crawler_execution import (
    CrawlerExecutionApprovalDraft,
    CrawlerExecutionApprovalOutcome,
    CrawlerExecutionPlanner,
    CrawlerExecutionRequestDraft,
)
from careerops.application.crawler_outbox import (
    CRAWLER_EXECUTION_EVENT_KEY_PREFIX,
)
from careerops.application.outbox import ClaimedOutboxEvent
from careerops.infrastructure.database import crawler_execution as repository_module
from careerops.infrastructure.database.crawler_execution import (
    PostgresCrawlerExecutionRepository,
    insert_request_statement,
    select_pending_requests_statement,
)
from careerops.infrastructure.database.crawler_execution import (
    compile_query_for_test as compile_crawler_query,
)
from careerops.infrastructure.database.crawler_outbox import (
    compile_query_for_test as compile_outbox_query,
)
from careerops.infrastructure.database.crawler_outbox import (
    select_crawler_dispatch_for_outbox_event_statement,
)
from careerops.infrastructure.database.outbox import PostgresOutboxRepository
from careerops.infrastructure.database.schema import (
    action_intents,
    console_users,
    crawler_execution_approvals,
    crawler_execution_dispatches,
    crawler_execution_requests,
    crawler_execution_results,
    outbox_events,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
REQUEST_ID = UUID("00000000-0000-0000-0000-000000000201")


def request_draft(**overrides: object) -> CrawlerExecutionRequestDraft:
    intent_id = UUID("00000000-0000-0000-0000-000000000101")
    payload_id = UUID("00000000-0000-0000-0000-000000000102")
    values: dict[str, object] = {
        "request_id": UUID("00000000-0000-0000-0000-000000000201"),
        "owner_user_id": UUID("00000000-0000-0000-0000-000000000301"),
        "action_intent_id": intent_id,
        "payload_version_id": payload_id,
        "payload_hash": "a" * 64,
        "manifest_path": "datasets/manifests/recruitment-crawler-sources.example.json",
        "request_artifact_path": "datasets/private/crawler-execution-reviews/request.json",
        "manifest_sha256": "b" * 64,
        "request_sha256": "c" * 64,
        "reviewed_plan_sha256": "d" * 64,
        "source_ids": ["greenhouse-sitemap", "workday-pages"],
        "reason": "reviewed crawler request may be queued",
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=2),
    }
    values.update(overrides)
    return CrawlerExecutionRequestDraft(**values)  # type: ignore[arg-type]


def approval_draft(
    request_id: UUID = REQUEST_ID,
    **overrides: object,
) -> CrawlerExecutionApprovalDraft:
    values: dict[str, object] = {
        "request_id": request_id,
        "outcome": CrawlerExecutionApprovalOutcome.APPROVE,
        "decided_by_user_id": UUID("00000000-0000-0000-0000-000000000401"),
        "decision_reason": "server-side authenticated approval",
        "decided_at": NOW + timedelta(minutes=5),
        "approval_artifact_path": "datasets/private/crawler-execution-reviews/approval.json",
        "approval_artifact_sha256": "e" * 64,
    }
    values.update(overrides)
    return CrawlerExecutionApprovalDraft(**values)  # type: ignore[arg-type]


def claimed_crawler_event() -> ClaimedOutboxEvent:
    execution_key = "f" * 64
    return ClaimedOutboxEvent(
        event_id=UUID("00000000-0000-0000-0000-000000000501"),
        event_key=f"{CRAWLER_EXECUTION_EVENT_KEY_PREFIX}{execution_key}",
        action_intent_id=UUID("00000000-0000-0000-0000-000000000101"),
        payload_version_id=UUID("00000000-0000-0000-0000-000000000102"),
        event_type="workflow_signal",
        available_at=NOW,
        attempt_count=1,
        lease_token=UUID("00000000-0000-0000-0000-000000000601"),
        lease_until=NOW + timedelta(minutes=1),
    )


def test_crawler_repository_does_not_mutate_canonical_schema_with_legacy_columns() -> None:
    """Repository code must use the migration/schema tables, not redefine a second model."""

    assert repository_module.__name__.endswith(".crawler_execution")

    assert _columns(crawler_execution_requests) >= {
        "owner_user_id",
        "action_intent_id",
        "payload_version_id",
        "payload_hash",
        "manifest_sha256",
        "request_sha256",
        "reviewed_plan_sha256",
    }
    assert _columns(crawler_execution_requests).isdisjoint(
        {
            "request_key",
            "requested_by_actor_id",
            "request_artifact_sha256",
            "hash_bindings",
            "requested_at",
            "status",
        }
    )
    assert _columns(crawler_execution_approvals) >= {
        "decided_by_user_id",
        "decision",
        "decision_reason",
    }
    assert _columns(crawler_execution_approvals).isdisjoint(
        {"approval_key", "actor_id", "outcome", "reason"}
    )
    assert _columns(crawler_execution_dispatches) >= {
        "action_intent_id",
        "outbox_event_id",
        "execution_key",
    }
    assert _columns(crawler_execution_dispatches).isdisjoint(
        {"approval_id", "dispatch_key", "workflow_payload_id", "status"}
    )


def test_create_request_insert_binds_to_action_intent_payload_identity() -> None:
    sql = compile_crawler_query(
        insert_request_statement(request_draft()),
        literal_binds=False,
    )

    assert "INSERT INTO careerops.crawler_execution_requests" in sql
    assert "owner_user_id" in sql
    assert "action_intent_id" in sql
    assert "payload_version_id" in sql
    assert "payload_hash" in sql
    assert "manifest_sha256" in sql
    assert "request_sha256" in sql
    assert "reviewed_plan_sha256" in sql
    assert "request_key" not in sql
    assert "hash_bindings" not in sql


def test_pending_request_query_is_owner_scoped_for_authenticated_console() -> None:
    signature = inspect.signature(select_pending_requests_statement)
    assert "owner_user_id" in signature.parameters

    sql = compile_crawler_query(
        select_pending_requests_statement(
            owner_user_id=UUID("00000000-0000-0000-0000-000000000301"),
            limit=25,
        )
    )

    assert "crawler_execution_requests.owner_user_id" in sql
    assert "WHERE" in sql
    assert "LIMIT 25" in sql


def test_dispatch_reader_selects_only_claim_bound_crawler_metadata() -> None:
    sql = compile_outbox_query(
        select_crawler_dispatch_for_outbox_event_statement(claimed_crawler_event())
    )

    assert "crawler_execution_dispatches.outbox_event_id" in sql
    assert "outbox_events.event_key" in sql
    assert "outbox_events.action_intent_id" in sql
    assert "outbox_events.payload_version_id" in sql
    assert "crawler_execution_requests.request_sha256" in sql
    assert "crawler_execution_approvals.approval_artifact_sha256" in sql
    assert "crawler_execution_approvals.decision" in sql
    assert "hash_bindings" not in sql
    assert "source_ids" not in sql


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")

    database_engine = sa.create_engine(database_url)
    yield database_engine
    database_engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as database_connection:
        transaction = database_connection.begin()
        try:
            yield database_connection
        finally:
            transaction.rollback()


def test_approved_crawler_request_replay_creates_one_dispatch_and_one_outbox_event(
    connection: Connection,
) -> None:
    now = datetime.now(UTC)
    request = request_draft(
        request_id=uuid4(),
        owner_user_id=_singleton_console_user_id(connection),
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        created_at=now,
        expires_at=now + timedelta(hours=2),
    )
    approval = approval_draft(
        request.request_id,
        decided_by_user_id=request.owner_user_id,
        decided_at=now + timedelta(minutes=1),
    )
    repository = PostgresCrawlerExecutionRepository(connection)
    decision = CrawlerExecutionPlanner().plan(
        request,
        approval,
        now=approval.decided_at + timedelta(seconds=1),
    )

    repository.create_request(request)
    first_dispatch = repository.approve_and_enqueue(request, approval, decision)
    second_dispatch = repository.approve_and_enqueue(request, approval, decision)

    assert second_dispatch == first_dispatch
    dispatch_rows = connection.execute(sa.select(crawler_execution_dispatches)).all()
    outbox_rows = connection.execute(sa.select(outbox_events)).all()
    assert len(dispatch_rows) == 1
    assert len(outbox_rows) == 1
    assert outbox_rows[0]._mapping["event_type"] == "workflow_signal"
    assert outbox_rows[0]._mapping["event_key"].startswith(CRAWLER_EXECUTION_EVENT_KEY_PREFIX)
    assert outbox_rows[0]._mapping["event_key"] == dispatch_rows[0]._mapping["execution_key"]


def _create_leased_approved_crawler_execution(
    connection: Connection,
) -> tuple[CrawlerExecutionRequestDraft, UUID, str, UUID]:
    now = datetime.now(UTC)
    request = request_draft(
        request_id=uuid4(),
        owner_user_id=_singleton_console_user_id(connection),
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        created_at=now,
        expires_at=now + timedelta(hours=2),
    )
    approval = approval_draft(
        request.request_id,
        decided_by_user_id=request.owner_user_id,
        decided_at=now + timedelta(minutes=1),
    )
    repository = PostgresCrawlerExecutionRepository(connection)
    decision = CrawlerExecutionPlanner().plan(
        request,
        approval,
        now=approval.decided_at + timedelta(seconds=1),
    )
    repository.create_request(request)
    repository.approve_and_enqueue(request, approval, decision)

    lease_owner = "crawler-outbox"
    claimed = PostgresOutboxRepository(connection).claim(
        owner=lease_owner,
        now=approval.decided_at + timedelta(seconds=1),
        lease_for=timedelta(minutes=5),
        limit=1,
        event_key_prefix=CRAWLER_EXECUTION_EVENT_KEY_PREFIX,
    )
    assert len(claimed) == 1
    return request, claimed[0].event_id, lease_owner, claimed[0].lease_token


def _singleton_console_user_id(connection: Connection) -> UUID:
    user_id = connection.scalar(sa.select(console_users.c.id))
    assert isinstance(user_id, UUID)
    return user_id


@pytest.mark.parametrize(
    ("outcome", "error_code", "expected_intent_status", "expected_outbox_status"),
    [
        ("succeeded", None, "confirmed", "published"),
        ("failed", "CRAWLER_EXECUTION_FAILED", "failed", "failed"),
        (
            "reconciliation_required",
            "CRAWLER_EXECUTION_RECONCILIATION_REQUIRED",
            "reconciliation_required",
            "failed",
        ),
    ],
)
def test_database_owned_completion_transitions_intent_and_writes_one_result(
    connection: Connection,
    outcome: str,
    error_code: str | None,
    expected_intent_status: str,
    expected_outbox_status: str,
) -> None:
    request, event_id, lease_owner, lease_token = _create_leased_approved_crawler_execution(
        connection
    )
    result_id = uuid4()

    connection.execute(sa.text("SET LOCAL ROLE careerops_outbox"))
    connection.execute(
        sa.text(
            "SELECT careerops.complete_crawler_execution_outbox_event("
            ":event_id, :lease_owner, :lease_token, :outcome, :error_code, :result_id)"
        ),
        {
            "event_id": event_id,
            "lease_owner": lease_owner,
            "lease_token": lease_token,
            "outcome": outcome,
            "error_code": error_code,
            "result_id": result_id,
        },
    )
    connection.execute(sa.text("RESET ROLE"))

    intent_status = connection.scalar(
        sa.select(action_intents.c.status).where(action_intents.c.id == request.action_intent_id)
    )
    event = (
        connection.execute(sa.select(outbox_events).where(outbox_events.c.id == event_id))
        .mappings()
        .one()
    )
    results = (
        connection.execute(
            sa.select(crawler_execution_results).where(
                crawler_execution_results.c.outbox_event_id == event_id
            )
        )
        .mappings()
        .all()
    )

    assert intent_status == expected_intent_status
    assert event["status"] == expected_outbox_status
    assert event["lease_owner"] is None
    assert event["lease_token"] is None
    assert event["lease_until"] is None
    assert event["last_error_code"] == error_code
    assert (event["published_at"] is not None) is (outcome == "succeeded")
    assert len(results) == 1
    assert results[0]["id"] == result_id
    assert results[0]["request_id"] == request.request_id
    assert results[0]["action_intent_id"] == request.action_intent_id
    assert results[0]["outbox_event_id"] == event_id
    assert results[0]["outcome"] == outcome
    assert results[0]["error_code"] == error_code


def _columns(table: sa.Table) -> set[str]:
    return set(table.c.keys())
