from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import pairwise
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from careerops.application.audit import AuditActorType, AuditEventDraft
from careerops.infrastructure.database.audit import PostgresAuditWriter
from careerops.infrastructure.database.schema import audit_events

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    database_url = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for audit integration tests")
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    database_engine = sa.create_engine(database_url)
    yield database_engine
    database_engine.dispose()


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def _draft(resource_id: UUID, index: int) -> AuditEventDraft:
    return AuditEventDraft(
        actor_type=AuditActorType.WORKER,
        actor_id=f"worker-{index}",
        event_type="audit_database_test",
        resource_type="action_intent",
        resource_id=resource_id,
        trace_id=f"audit-integration-{index}",
        occurred_at=datetime(2026, 7, 18, 9, index, tzinfo=UTC),
        event_data={"attempt": index, "reason": "permission_probe"},
    )


def _db_recomputed_hashes(connection: sa.Connection) -> list[str]:
    return list(
        connection.execute(
            sa.text(
                """
                SELECT encode(
                    sha256(
                        convert_to(
                            jsonb_build_object(
                                'actor_id', actor_id,
                                'actor_type', actor_type,
                                'event_data', event_data,
                                'event_id', event_id::text,
                                'event_type', event_type,
                                'occurred_at', to_char(
                                    occurred_at AT TIME ZONE 'UTC',
                                    'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'
                                ),
                                'previous_hash', previous_hash,
                                'resource_id', resource_id::text,
                                'resource_type', resource_type,
                                'trace_id', trace_id
                            )::text,
                            'UTF8'
                        )
                    ),
                    'hex'
                ) AS recomputed_hash
                FROM careerops.audit_events
                ORDER BY sequence
                """
            )
        ).scalars()
    )


def test_api_role_cannot_directly_insert_audit_events(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE careerops.audit_events RESTART IDENTITY"))

    with (
        pytest.raises(DBAPIError) as permission_error,
        engine.begin() as connection,
    ):
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.audit_events (
                    event_id, occurred_at, actor_type, event_type,
                    resource_type, resource_id, trace_id, event_hash
                ) VALUES (
                    :event_id, now(), 'worker', 'direct_insert',
                    'action_intent', :resource_id, 'direct-insert', :event_hash
                )
                """
            ),
            {
                "event_id": uuid4(),
                "resource_id": uuid4(),
                "event_hash": "a" * 64,
            },
        )
    assert _sqlstate(permission_error.value) == "42501"


def test_audit_writer_serializes_a_database_owned_hash_chain(engine: Engine) -> None:
    resource_id = uuid4()
    with engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE careerops.audit_events RESTART IDENTITY"))
        connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
        writer = PostgresAuditWriter(connection)
        first = writer.append(_draft(resource_id, 1))
        second = writer.append(_draft(resource_id, 2))

    assert first.sequence == 1
    assert first.previous_hash is None
    assert second.sequence == 2
    assert second.previous_hash == first.event_hash
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(audit_events.c.previous_hash, audit_events.c.event_hash).order_by(
                audit_events.c.sequence
            )
        ).all()
        recomputed_hashes = _db_recomputed_hashes(connection)
    assert rows == [(None, first.event_hash), (first.event_hash, second.event_hash)]
    assert recomputed_hashes == [first.event_hash, second.event_hash]


def test_concurrent_audit_appends_do_not_fork_the_hash_chain(engine: Engine) -> None:
    resource_id = uuid4()
    event_count = 12
    with engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE careerops.audit_events RESTART IDENTITY"))

    def append_event(index: int) -> int:
        with engine.begin() as connection:
            connection.execute(sa.text("SET LOCAL ROLE careerops_api"))
            appended = PostgresAuditWriter(connection).append(_draft(resource_id, index))
            return appended.sequence

    with ThreadPoolExecutor(max_workers=6) as executor:
        appended_sequences = sorted(executor.map(append_event, range(event_count)))

    assert appended_sequences == list(range(1, event_count + 1))
    with engine.connect() as connection:
        rows = (
            connection.execute(
                sa.text(
                    """
                SELECT sequence, previous_hash, event_hash
                FROM careerops.audit_events
                ORDER BY sequence
                """
                )
            )
            .mappings()
            .all()
        )
        recomputed_hashes = _db_recomputed_hashes(connection)

    assert [row["sequence"] for row in rows] == list(range(1, event_count + 1))
    assert rows[0]["previous_hash"] is None
    for previous, current in pairwise(rows):
        assert current["previous_hash"] == previous["event_hash"]
    assert recomputed_hashes == [row["event_hash"] for row in rows]
