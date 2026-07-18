from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from careerops.application.outbox import OutboxEventType, PendingOutboxEvent
from careerops.application.ports.storage import StorageDeleteResult
from careerops.infrastructure.database.content_retention import (
    PostgresContentRetentionRepository,
    RetentionStateError,
)
from careerops.infrastructure.database.outbox import (
    OutboxLeaseLostError,
    PostgresOutboxRepository,
)
from careerops.infrastructure.database.schema import (
    action_intents,
    action_payload_versions,
    companies,
    content_blobs,
    content_objects,
    evidence_records,
    job_posting_versions,
    job_postings,
    job_sources,
    outbox_events,
)

pytestmark = pytest.mark.integration

BASE_TIME = datetime(2000, 1, 1, tzinfo=UTC)


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
    """Rollback even append-only fixtures without truncating a shared test DB."""

    with engine.connect() as database_connection:
        transaction = database_connection.begin()
        try:
            yield database_connection
        finally:
            transaction.rollback()


def _digest() -> str:
    return uuid4().hex * 2


def _blob_values(*, blob_id: UUID, digest: str) -> dict[str, object]:
    return {
        "id": blob_id,
        "sha256": digest,
        "object_key": f"sha256/{digest[:2]}/{digest[2:4]}/{digest}",
        "byte_size": 128,
    }


def _content_values(
    *,
    object_id: UUID,
    blob_id: UUID,
    retention_until: datetime = BASE_TIME - timedelta(days=1),
    expired_at: datetime | None = None,
    retired_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "id": object_id,
        "blob_id": blob_id,
        "media_type": "text/plain",
        "classification": "raw_webpage",
        "owner_resource_type": "integration_test",
        "owner_resource_id": uuid4(),
        "retention_until": retention_until,
        "expired_at": expired_at,
        "retired_at": retired_at,
        "created_at": BASE_TIME - timedelta(days=10),
    }


def _insert_blob_object(
    connection: Connection,
    *,
    retention_until: datetime = BASE_TIME - timedelta(days=1),
    expired_at: datetime | None = None,
    retired_at: datetime | None = None,
) -> tuple[UUID, UUID, str]:
    blob_id = uuid4()
    object_id = uuid4()
    digest = _digest()
    connection.execute(
        sa.insert(content_blobs).values(**_blob_values(blob_id=blob_id, digest=digest))
    )
    connection.execute(
        sa.insert(content_objects).values(
            **_content_values(
                object_id=object_id,
                blob_id=blob_id,
                retention_until=retention_until,
                expired_at=expired_at,
                retired_at=retired_at,
            )
        )
    )
    return blob_id, object_id, digest


def _insert_intent_payload(connection: Connection) -> tuple[UUID, UUID]:
    intent_id = uuid4()
    payload_id = uuid4()
    connection.execute(
        sa.insert(action_intents).values(
            id=intent_id,
            action_kind="integration_test",
            resource_type="integration_test",
            resource_id=uuid4(),
            idempotency_key=f"integration:{uuid4()}",
            created_by="integration-test",
        )
    )
    connection.execute(
        sa.insert(action_payload_versions).values(
            id=payload_id,
            action_intent_id=intent_id,
            version=1,
            target={},
            payload={},
            payload_hash=_digest(),
        )
    )
    return intent_id, payload_id


def _pending_event(
    *,
    event_id: UUID,
    intent_id: UUID,
    payload_id: UUID,
    event_key: str | None = None,
    event_type: OutboxEventType = OutboxEventType.WORKFLOW_SIGNAL,
) -> PendingOutboxEvent:
    return PendingOutboxEvent(
        event_id=event_id,
        event_key=event_key or f"integration/{event_id}",
        action_intent_id=intent_id,
        payload_version_id=payload_id,
        event_type=event_type,
        available_at=BASE_TIME,
    )


def test_physical_blob_is_unique_while_logical_owners_and_ttls_are_independent(
    connection: Connection,
) -> None:
    blob_id = uuid4()
    digest = _digest()
    connection.execute(
        sa.insert(content_blobs).values(**_blob_values(blob_id=blob_id, digest=digest))
    )

    first_object = uuid4()
    second_object = uuid4()
    first = _content_values(object_id=first_object, blob_id=blob_id)
    second = _content_values(
        object_id=second_object,
        blob_id=blob_id,
        retention_until=BASE_TIME + timedelta(days=30),
    )
    second["owner_resource_id"] = uuid4()
    connection.execute(sa.insert(content_objects), [first, second])

    logical_rows = connection.execute(
        sa.select(
            content_objects.c.id,
            content_objects.c.blob_id,
            content_objects.c.owner_resource_id,
            content_objects.c.retention_until,
        ).where(content_objects.c.blob_id == blob_id)
    ).all()
    assert {row.id for row in logical_rows} == {first_object, second_object}
    assert {row.blob_id for row in logical_rows} == {blob_id}
    assert len({row.owner_resource_id for row in logical_rows}) == 2
    assert len({row.retention_until for row in logical_rows}) == 2

    duplicate_blob = _blob_values(blob_id=uuid4(), digest=digest)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(content_blobs).values(**duplicate_blob))

    traversal_blob = _blob_values(blob_id=uuid4(), digest=_digest())
    traversal_blob["object_key"] = "../../outside"
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(content_blobs).values(**traversal_blob))

    blank_owner = _content_values(object_id=uuid4(), blob_id=blob_id)
    blank_owner["owner_resource_type"] = "   "
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(content_objects).values(**blank_owner))

    model_debug = _content_values(object_id=uuid4(), blob_id=blob_id)
    model_debug["classification"] = "model_debug"
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(content_objects).values(**model_debug))

    overlong_quarantine = _content_values(object_id=uuid4(), blob_id=blob_id)
    overlong_quarantine.update(
        classification="quarantined_attachment",
        created_at=BASE_TIME,
        retention_until=BASE_TIME + timedelta(hours=25),
    )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(content_objects).values(**overlong_quarantine))


def test_retention_requires_every_logical_ref_expired_and_replay_safe(
    connection: Connection,
) -> None:
    unsafe_evidence_blob, unsafe_evidence_object, unsafe_evidence_digest = _insert_blob_object(
        connection
    )
    unsafe_job_blob, unsafe_job_object, unsafe_job_digest = _insert_blob_object(connection)
    safe_evidence_blob, safe_evidence_object, safe_evidence_digest = _insert_blob_object(connection)
    safe_job_blob, safe_job_object, safe_job_digest = _insert_blob_object(connection)

    shared_blob, shared_short_object, _ = _insert_blob_object(connection)
    shared_long_object = uuid4()
    connection.execute(
        sa.insert(content_objects).values(
            **_content_values(
                object_id=shared_long_object,
                blob_id=shared_blob,
                retention_until=BASE_TIME + timedelta(days=30),
            )
        )
    )

    connection.execute(
        sa.insert(evidence_records),
        [
            {
                "id": uuid4(),
                "resource_type": "integration_test",
                "resource_id": uuid4(),
                "content_object_id": unsafe_evidence_object,
                "source_url": "",
                "provider_id": None,
                "captured_at": BASE_TIME - timedelta(days=2),
                "content_hash": _digest(),
                "sanitized_span": "",
                "span_hash": _digest(),
                "extractor_version": "",
            },
            {
                "id": uuid4(),
                "resource_type": "integration_test",
                "resource_id": uuid4(),
                "content_object_id": safe_evidence_object,
                "source_url": "https://example.invalid/evidence",
                "provider_id": None,
                "captured_at": BASE_TIME - timedelta(days=2),
                "content_hash": safe_evidence_digest,
                "sanitized_span": "replay-safe excerpt",
                "span_hash": _digest(),
                "extractor_version": "integration-v1",
            },
        ],
    )

    company_id = uuid4()
    source_id = uuid4()
    connection.execute(
        sa.insert(companies).values(
            id=company_id,
            name="Integration Test Company",
            normalized_name=f"integration-{uuid4()}",
        )
    )
    connection.execute(
        sa.insert(job_sources).values(
            id=source_id,
            company_id=company_id,
            source_type="career_page",
            source_identifier=f"integration-{uuid4()}",
            base_url="https://example.invalid/jobs",
        )
    )
    unsafe_posting_id = uuid4()
    safe_posting_id = uuid4()
    connection.execute(
        sa.insert(job_postings),
        [
            {
                "id": posting_id,
                "source_id": source_id,
                "external_id": f"integration-{posting_id}",
                "canonical_url": f"https://example.invalid/jobs/{posting_id}",
                "first_seen_at": BASE_TIME - timedelta(days=3),
                "last_seen_at": BASE_TIME - timedelta(days=2),
            }
            for posting_id in (unsafe_posting_id, safe_posting_id)
        ],
    )
    connection.execute(
        sa.insert(job_posting_versions),
        [
            {
                "id": uuid4(),
                "job_posting_id": unsafe_posting_id,
                "raw_snapshot_id": unsafe_job_object,
                "content_hash": _digest(),
                "source_url": "",
                "parser_version": "",
                "captured_at": BASE_TIME - timedelta(days=2),
            },
            {
                "id": uuid4(),
                "job_posting_id": safe_posting_id,
                "raw_snapshot_id": safe_job_object,
                "content_hash": safe_job_digest,
                "source_url": f"https://example.invalid/jobs/{safe_posting_id}",
                "parser_version": "integration-v1",
                "captured_at": BASE_TIME - timedelta(days=2),
            },
        ],
    )

    repository = PostgresContentRetentionRepository(connection)
    assert repository.mark_due_expired(now=BASE_TIME) == 5
    assert (
        repository.claim_deletable(
            owner="retention-before-retirement",
            now=BASE_TIME,
            lease_for=timedelta(minutes=1),
        )
        is None
    )
    connection.execute(
        sa.update(content_objects)
        .where(
            content_objects.c.id.in_(
                (
                    unsafe_evidence_object,
                    unsafe_job_object,
                    safe_evidence_object,
                    safe_job_object,
                    shared_short_object,
                )
            )
        )
        .values(retired_at=BASE_TIME)
    )

    claimed_blobs: set[UUID] = set()
    for ordinal in range(2):
        candidate = repository.claim_deletable(
            owner=f"retention-{ordinal}",
            now=BASE_TIME,
            lease_for=timedelta(minutes=1),
        )
        assert candidate is not None
        claimed_blobs.add(candidate.blob_id)
        repository.finalize_deletion(
            candidate.blob_id,
            lease_token=candidate.lease_token,
            now=BASE_TIME + timedelta(seconds=1),
            delete_result=StorageDeleteResult.DELETED,
        )

    assert claimed_blobs == {safe_evidence_blob, safe_job_blob}
    assert (
        repository.claim_deletable(
            owner="retention-blocked",
            now=BASE_TIME,
            lease_for=timedelta(minutes=1),
        )
        is None
    )

    later = BASE_TIME + timedelta(days=31)
    assert repository.mark_due_expired(now=later) == 1
    connection.execute(
        sa.update(content_objects)
        .where(content_objects.c.id == shared_long_object)
        .values(retired_at=later)
    )
    shared_candidate = repository.claim_deletable(
        owner="retention-later",
        now=later,
        lease_for=timedelta(minutes=1),
    )
    assert shared_candidate is not None
    assert shared_candidate.blob_id == shared_blob

    blocked_states = {
        row.id: row.deletion_state
        for row in connection.execute(
            sa.select(content_blobs.c.id, content_blobs.c.deletion_state).where(
                content_blobs.c.id.in_((unsafe_evidence_blob, unsafe_job_blob))
            )
        )
    }
    assert blocked_states == {
        unsafe_evidence_blob: "active",
        unsafe_job_blob: "active",
    }
    assert unsafe_evidence_digest != safe_evidence_digest
    assert unsafe_job_digest != safe_job_digest
    assert shared_short_object != shared_long_object


def test_stale_blob_lease_recovery_fences_old_token_and_records_missing_bytes(
    connection: Connection,
) -> None:
    blob_id, _, _ = _insert_blob_object(
        connection,
        expired_at=BASE_TIME,
        retired_at=BASE_TIME,
    )
    repository = PostgresContentRetentionRepository(connection)

    first = repository.claim_deletable(
        owner="retention-a",
        now=BASE_TIME,
        lease_for=timedelta(seconds=5),
    )
    assert first is not None

    new_reference = _content_values(object_id=uuid4(), blob_id=blob_id)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(content_objects).values(**new_reference))

    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(
            sa.update(content_blobs)
            .where(content_blobs.c.id == blob_id)
            .values(
                deletion_state="deleted",
                delete_lease_owner=None,
                delete_lease_token=None,
                delete_lease_until=None,
                deleted_at=BASE_TIME,
                delete_result="deleted",
            )
        )

    assert (
        repository.claim_deletable(
            owner="retention-b",
            now=BASE_TIME + timedelta(seconds=4),
            lease_for=timedelta(seconds=5),
        )
        is None
    )
    reclaimed = repository.claim_deletable(
        owner="retention-b",
        now=BASE_TIME + timedelta(seconds=5),
        lease_for=timedelta(seconds=5),
    )
    assert reclaimed is not None
    assert reclaimed.blob_id == blob_id
    assert reclaimed.lease_token != first.lease_token

    with pytest.raises(RetentionStateError):
        repository.finalize_deletion(
            blob_id,
            lease_token=first.lease_token,
            now=BASE_TIME + timedelta(seconds=5),
            delete_result=StorageDeleteResult.DELETED,
        )
    repository.finalize_deletion(
        blob_id,
        lease_token=reclaimed.lease_token,
        now=BASE_TIME + timedelta(seconds=5),
        delete_result=StorageDeleteResult.ALREADY_MISSING,
    )

    state = connection.execute(
        sa.select(
            content_blobs.c.deletion_state,
            content_blobs.c.delete_lease_token,
            content_blobs.c.deleted_at,
            content_blobs.c.delete_result,
        ).where(content_blobs.c.id == blob_id)
    ).one()
    assert state.deletion_state == "deleted"
    assert state.delete_lease_token is None
    assert state.deleted_at == BASE_TIME + timedelta(seconds=5)
    assert state.delete_result == "already_missing"


def test_outbox_idempotency_and_m0_event_types_are_database_enforced(
    connection: Connection,
) -> None:
    intent_id, payload_id = _insert_intent_payload(connection)
    repository = PostgresOutboxRepository(connection)
    event_key = f"integration/{uuid4()}"
    repository.enqueue(
        _pending_event(
            event_id=uuid4(),
            intent_id=intent_id,
            payload_id=payload_id,
            event_key=event_key,
        )
    )

    with pytest.raises(IntegrityError), connection.begin_nested():
        repository.enqueue(
            _pending_event(
                event_id=uuid4(),
                intent_id=intent_id,
                payload_id=payload_id,
                event_key=event_key,
            )
        )

    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(
            sa.insert(outbox_events).values(
                id=uuid4(),
                event_key=f"integration/{uuid4()}",
                action_intent_id=intent_id,
                payload_version_id=payload_id,
                event_type="workflow_signal",
                status="published",
                available_at=BASE_TIME,
                published_at=BASE_TIME,
            )
        )

    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            sa.insert(outbox_events).values(
                id=uuid4(),
                event_key=f"integration/{uuid4()}",
                action_intent_id=intent_id,
                payload_version_id=payload_id,
                event_type="side_effect_requested",
                status="pending",
                available_at=BASE_TIME,
            )
        )


def test_outbox_owners_do_not_duplicate_claims_and_transitions_are_fenced(
    connection: Connection,
) -> None:
    intent_id, payload_id = _insert_intent_payload(connection)
    repository = PostgresOutboxRepository(connection)
    event_ids = {uuid4(), uuid4()}
    for event_id in event_ids:
        repository.enqueue(
            _pending_event(event_id=event_id, intent_id=intent_id, payload_id=payload_id)
        )

    owner_a_claim = repository.claim(
        owner="publisher-a",
        now=BASE_TIME,
        lease_for=timedelta(seconds=30),
        limit=1,
    )
    owner_b_claim = repository.claim(
        owner="publisher-b",
        now=BASE_TIME,
        lease_for=timedelta(seconds=30),
        limit=10,
    )
    assert len(owner_a_claim) == 1
    assert len(owner_b_claim) == 1
    assert owner_a_claim[0].event_id != owner_b_claim[0].event_id
    assert {owner_a_claim[0].event_id, owner_b_claim[0].event_id} == event_ids

    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(
            sa.update(outbox_events)
            .where(outbox_events.c.id == owner_a_claim[0].event_id)
            .values(
                status="published",
                lease_owner=None,
                lease_token=None,
                lease_until=None,
                published_at=BASE_TIME,
                last_error_code=None,
            )
        )

    with pytest.raises(OutboxLeaseLostError):
        repository.mark_published(
            owner_a_claim[0].event_id,
            owner="publisher-b",
            lease_token=owner_a_claim[0].lease_token,
            now=BASE_TIME,
        )
    with pytest.raises(OutboxLeaseLostError):
        repository.mark_published(
            owner_a_claim[0].event_id,
            owner="publisher-a",
            lease_token=uuid4(),
            now=BASE_TIME,
        )

    retry_at = BASE_TIME + timedelta(minutes=1)
    repository.release(
        owner_a_claim[0].event_id,
        owner="publisher-a",
        lease_token=owner_a_claim[0].lease_token,
        now=BASE_TIME,
        retry_at=retry_at,
        error_code="INTERNAL_SINK_UNAVAILABLE",
        terminal=False,
    )
    repository.mark_published(
        owner_b_claim[0].event_id,
        owner="publisher-b",
        lease_token=owner_b_claim[0].lease_token,
        now=BASE_TIME,
    )

    retry_claim = repository.claim(
        owner="publisher-c",
        now=retry_at,
        lease_for=timedelta(seconds=30),
        limit=10,
    )
    assert [event.event_id for event in retry_claim] == [owner_a_claim[0].event_id]
    assert retry_claim[0].attempt_count == 2
    repository.release(
        retry_claim[0].event_id,
        owner="publisher-c",
        lease_token=retry_claim[0].lease_token,
        now=retry_at,
        retry_at=retry_at,
        error_code="PERMANENT_INTERNAL_FAILURE",
        terminal=True,
    )

    states = {
        row.id: row
        for row in connection.execute(
            sa.select(
                outbox_events.c.id,
                outbox_events.c.status,
                outbox_events.c.lease_owner,
                outbox_events.c.lease_token,
                outbox_events.c.published_at,
                outbox_events.c.last_error_code,
            ).where(outbox_events.c.id.in_(event_ids))
        )
    }
    published = states[owner_b_claim[0].event_id]
    failed = states[owner_a_claim[0].event_id]
    assert published.status == "published"
    assert published.published_at == BASE_TIME
    assert published.lease_owner is None
    assert published.lease_token is None
    assert failed.status == "failed"
    assert failed.last_error_code == "PERMANENT_INTERNAL_FAILURE"
    assert failed.lease_owner is None
    assert failed.lease_token is None


def test_expired_outbox_lease_reclaim_fences_stale_token(connection: Connection) -> None:
    intent_id, payload_id = _insert_intent_payload(connection)
    repository = PostgresOutboxRepository(connection)
    event_id = uuid4()
    repository.enqueue(
        _pending_event(
            event_id=event_id,
            intent_id=intent_id,
            payload_id=payload_id,
            event_type=OutboxEventType.INTERNAL_NOTIFICATION,
        )
    )

    first = repository.claim(
        owner="publisher-a",
        now=BASE_TIME,
        lease_for=timedelta(seconds=5),
        limit=1,
    )[0]
    reclaim_time = BASE_TIME + timedelta(seconds=5)
    reclaimed = repository.claim(
        owner="publisher-b",
        now=reclaim_time,
        lease_for=timedelta(seconds=5),
        limit=1,
    )[0]
    assert reclaimed.event_id == event_id
    assert reclaimed.attempt_count == 2
    assert reclaimed.lease_token != first.lease_token

    with pytest.raises(OutboxLeaseLostError):
        repository.mark_published(
            event_id,
            owner="publisher-a",
            lease_token=first.lease_token,
            now=reclaim_time,
        )
    repository.mark_published(
        event_id,
        owner="publisher-b",
        lease_token=reclaimed.lease_token,
        now=reclaim_time,
    )

    final_state = connection.execute(
        sa.select(
            outbox_events.c.status,
            outbox_events.c.attempt_count,
            outbox_events.c.lease_token,
            outbox_events.c.published_at,
        ).where(outbox_events.c.id == event_id)
    ).one()
    assert final_state.status == "published"
    assert final_state.attempt_count == 2
    assert final_state.lease_token is None
    assert final_state.published_at == reclaim_time
