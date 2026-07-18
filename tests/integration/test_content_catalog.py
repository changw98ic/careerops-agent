from __future__ import annotations

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from careerops.application.content_ingestion import OrphanClaimStatus
from careerops.application.ports.storage import (
    ContentClassification,
    ContentOwner,
    StorageDeleteResult,
    StoredContent,
)
from careerops.infrastructure.database.content_catalog import (
    BlobLifecycleConflict,
    PostgresContentCatalogRepository,
    PostgresContentCatalogStore,
)
from careerops.infrastructure.database.schema import content_blobs, content_objects

pytestmark = pytest.mark.integration


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


def _stored_content() -> StoredContent:
    digest = uuid4().hex * 2
    return StoredContent(
        object_key=f"sha256/{digest[:2]}/{digest[2:4]}/{digest}",
        sha256=digest,
        byte_size=128,
        media_type="text/plain",
        classification=ContentClassification.RAW_WEBPAGE,
        owner=ContentOwner(resource_type="integration_test", resource_id=uuid4()),
        retention_until=datetime.now(UTC) + timedelta(days=1),
    )


def _set_role(connection: Connection, role: str) -> None:
    if role not in {"careerops_api", "careerops_retention"}:
        raise ValueError("unsupported integration-test role")
    connection.execute(sa.text(f"SET LOCAL ROLE {role}"))


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


def _register_as_api(
    engine: Engine,
    stored: StoredContent,
    *,
    object_id: UUID | None = None,
) -> UUID:
    with engine.begin() as connection:
        _set_role(connection, "careerops_api")
        registration = PostgresContentCatalogRepository(connection).register(
            stored,
            object_id or uuid4(),
        )
    return registration.object_id


def test_api_role_registers_active_blob_and_logical_object(engine: Engine) -> None:
    stored = _stored_content()
    object_id = uuid4()

    registered_object_id = _register_as_api(engine, stored, object_id=object_id)

    assert registered_object_id == object_id
    with engine.connect() as connection:
        row = connection.execute(
            sa.select(
                content_blobs.c.deletion_state,
                content_objects.c.id,
            )
            .select_from(
                content_blobs.join(
                    content_objects,
                    content_objects.c.blob_id == content_blobs.c.id,
                )
            )
            .where(content_blobs.c.sha256 == stored.sha256)
        ).one()
    assert row.deletion_state == "active"
    assert row.id == object_id


def test_retention_role_recovers_claim_commit_crash_and_finalizes(engine: Engine) -> None:
    stored = _stored_content()
    claim_now = datetime.now(UTC) - timedelta(minutes=10)
    with engine.begin() as connection:
        _set_role(connection, "careerops_retention")
        decision = PostgresContentCatalogRepository(connection).claim_orphan(
            stored,
            now=claim_now,
            lease_until=claim_now + timedelta(minutes=5),
        )
    assert decision.status is OrphanClaimStatus.CLAIMED
    assert decision.claim is not None
    original_claim = decision.claim

    # The committed deleting row with untouched bytes represents a crash after claim commit.
    recovery_now = datetime.now(UTC)
    with engine.begin() as connection:
        _set_role(connection, "careerops_retention")
        recovered = PostgresContentCatalogRepository(connection).recover_orphan_claims(
            now=recovery_now,
            lease_until=recovery_now + timedelta(minutes=5),
            limit=100,
        )
    renewed_claim = next(claim for claim in recovered if claim.blob_id == original_claim.blob_id)
    assert renewed_claim.lease_token != original_claim.lease_token

    with engine.begin() as connection:
        _set_role(connection, "careerops_retention")
        PostgresContentCatalogRepository(connection).finalize_orphan_deletion(
            renewed_claim,
            now=recovery_now,
            result=StorageDeleteResult.ALREADY_MISSING,
        )

    with engine.connect() as connection:
        state, delete_result = connection.execute(
            sa.select(
                content_blobs.c.deletion_state,
                content_blobs.c.delete_result,
            ).where(content_blobs.c.id == original_claim.blob_id)
        ).one()
    assert state == "deleted"
    assert delete_result == "already_missing"
    assert stored.sha256 not in PostgresContentCatalogStore(engine).known_digests((stored.sha256,))


def test_roles_cannot_cross_registration_and_cleanup_boundaries(engine: Engine) -> None:
    orphan = _stored_content()
    now = datetime.now(UTC)
    with pytest.raises(DBAPIError) as api_claim_error, engine.begin() as connection:
        _set_role(connection, "careerops_api")
        PostgresContentCatalogRepository(connection).claim_orphan(
            orphan,
            now=now,
            lease_until=now + timedelta(minutes=5),
        )
    assert _sqlstate(api_claim_error.value) == "42501"

    registered = _stored_content()
    _register_as_api(engine, registered)
    forbidden_object_id = uuid4()
    with pytest.raises(DBAPIError) as retention_register_error, engine.begin() as connection:
        _set_role(connection, "careerops_retention")
        PostgresContentCatalogRepository(connection).register(
            registered,
            forbidden_object_id,
        )
    assert _sqlstate(retention_register_error.value) == "42501"
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(content_objects)
                .where(content_objects.c.id == forbidden_object_id)
            )
            == 0
        )


def test_digest_advisory_lock_serializes_claim_before_registration(engine: Engine) -> None:
    stored = _stored_content()
    register_started = Event()
    worker_pid: list[int] = []

    def concurrent_register() -> str:
        with engine.begin() as connection:
            _set_role(connection, "careerops_api")
            worker_pid.append(connection.scalar(sa.text("SELECT pg_backend_pid()")))
            register_started.set()
            try:
                PostgresContentCatalogRepository(connection).register(stored, uuid4())
            except BlobLifecycleConflict:
                return "tombstone_won"
        return "registration_won"

    with engine.connect() as claim_connection:
        claim_transaction = claim_connection.begin()
        _set_role(claim_connection, "careerops_retention")
        now = datetime.now(UTC)
        decision = PostgresContentCatalogRepository(claim_connection).claim_orphan(
            stored,
            now=now,
            lease_until=now + timedelta(minutes=5),
        )
        assert decision.claim is not None

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(concurrent_register)
            assert register_started.wait(timeout=5)
            deadline = time.monotonic() + 5
            waiting_on_advisory_lock = False
            with engine.connect() as observer:
                while time.monotonic() < deadline:
                    waiting_on_advisory_lock = bool(
                        observer.scalar(
                            sa.text(
                                "SELECT EXISTS ("
                                "SELECT 1 FROM pg_locks "
                                "WHERE pid = :pid AND locktype = 'advisory' AND NOT granted)"
                            ),
                            {"pid": worker_pid[0]},
                        )
                    )
                    if waiting_on_advisory_lock:
                        break
                    time.sleep(0.02)
            assert waiting_on_advisory_lock
            claim_transaction.commit()
            assert future.result(timeout=5) == "tombstone_won"

    assert decision.claim is not None
    with engine.begin() as connection:
        _set_role(connection, "careerops_retention")
        PostgresContentCatalogRepository(connection).finalize_orphan_deletion(
            decision.claim,
            now=datetime.now(UTC),
            result=StorageDeleteResult.ALREADY_MISSING,
        )
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(content_objects)
                .where(content_objects.c.blob_id == decision.claim.blob_id)
            )
            == 0
        )
