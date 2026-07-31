"""PostgreSQL acceptance tests for crawl-permission attention and audit."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa

from careerops.agent_console.contracts import ActionState
from careerops.application.crawl_permission_service import (
    PermissionAuditEvent,
    PermissionDecision,
)
from careerops.domain.crawl_attempts import (
    CrawlPermissionState,
    CrawlSourcePermission,
)
from careerops.infrastructure.database.agent_console_repo import (
    AgentActionRepository,
)
from careerops.infrastructure.database.audit import PostgresAuditWriterEngine
from careerops.infrastructure.database.permission_attention import (
    PostgresPermissionAttentionSink,
    PostgresPermissionAuditSink,
)
from careerops.infrastructure.database.postgres_notification_repo import (
    PostgresNotificationRepository,
)
from careerops.infrastructure.database.schema import audit_events


@pytest.fixture()
def database_engine() -> sa.Engine:
    url = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required")
    engine = sa.create_engine(url)
    yield engine
    engine.dispose()


def test_pending_permission_survives_repository_restart_and_resolves(
    database_engine: sa.Engine,
) -> None:
    owner_id = uuid4()
    source_id = uuid4()
    permission_id = uuid4()
    now = datetime.now(UTC)
    with database_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO careerops.candidates (id, display_name) "
                "VALUES (:id, 'permission acceptance')"
            ),
            {"id": owner_id},
        )

    permission = CrawlSourcePermission(
        id=permission_id,
        source_id=source_id,
        owner_id=owner_id,
        state=CrawlPermissionState.PENDING,
        requested_at=now,
        created_at=now,
        updated_at=now,
    )
    actions = AgentActionRepository(database_engine)
    notifications = PostgresNotificationRepository(database_engine)
    sink = PostgresPermissionAttentionSink(actions, notifications)
    try:
        sink.pending(permission, source_name="Example careers")

        restarted_actions = AgentActionRepository(database_engine)
        restarted_notifications = PostgresNotificationRepository(database_engine)
        action = restarted_actions.find_by_key(
            owner_id,
            f"crawl-permission:{permission_id}",
        )
        assert action is not None
        assert action.state is ActionState.PROPOSED
        pending = restarted_notifications.list_pending(str(owner_id))
        assert len(pending) == 1
        assert pending[0].payload["permission_id"] == str(permission_id)

        sink.resolved(permission, decision=PermissionDecision.DENIED)
        resolved = restarted_actions.get(owner_id, permission_id)
        assert resolved is not None
        assert resolved.state is ActionState.DISMISSED
    finally:
        with database_engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM careerops.candidates WHERE id = :id"),
                {"id": owner_id},
            )


def test_permission_decision_is_appended_to_hash_chain(
    database_engine: sa.Engine,
) -> None:
    owner_id = uuid4()
    permission_id = uuid4()
    source_id = uuid4()
    sink = PostgresPermissionAuditSink(PostgresAuditWriterEngine(database_engine))
    sink.record(
        PermissionAuditEvent(
            permission_id=permission_id,
            source_id=source_id,
            owner_id=owner_id,
            decision=PermissionDecision.GRANTED,
            prior_state=CrawlPermissionState.PENDING,
            new_state=CrawlPermissionState.GRANTED,
            occurred_at=datetime.now(UTC),
        )
    )
    with database_engine.connect() as conn:
        row = conn.execute(
            sa.select(audit_events).where(
                audit_events.c.resource_type == "crawl_permission",
                audit_events.c.resource_id == permission_id,
            )
        ).mappings().one()
    assert row["event_type"] == "crawl_permission.granted"
    assert len(row["event_hash"]) == 64
