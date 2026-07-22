from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Barrier
from time import monotonic, sleep
from types import MappingProxyType
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql.dml import Insert

from careerops.application.application_adapters import (
    BrowserIsolationMode,
    FormFieldSpec,
    SandboxApplicationPayload,
    SandboxBrowserSession,
    SyntheticApplicationFixture,
)
from careerops.application.autopilot_kill_switches import (
    AutopilotKillSwitchScope,
    AutopilotKillSwitchState,
    SetAutopilotKillSwitchCommand,
)
from careerops.application.outbox import OutboxPublisher, PublishBatchResult
from careerops.application.release_qualification import (
    AutopilotReleaseStage,
    SyntheticReleaseQualification,
)
from careerops.application.submission_dispatch import (
    DispatchAuthority,
    DispatchDecision,
    QualifiedSyntheticDispatchRequest,
    SyntheticProviderState,
    SyntheticSubmissionDispatchPlanner,
    SyntheticSubmissionReceipt,
)
from careerops.application.submission_outbox import (
    SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX,
    PreparedSyntheticSubmission,
    SyntheticSubmissionOutboxSink,
)
from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.autopilot_kill_switches import (
    PostgresAutopilotKillSwitchRepository,
)
from careerops.infrastructure.database.engine import create_database_engine
from careerops.infrastructure.database.outbox import PostgresOutboxStore
from careerops.infrastructure.database.schema import (
    action_intents,
    action_payload_versions,
    audit_events,
    autopilot_campaigns,
    autopilot_cap_reservations,
    autopilot_grant_revocations,
    autopilot_grant_versions,
    autopilot_intent_authorizations,
    autopilot_kill_switch_events,
    console_users,
    outbox_events,
    policy_decisions,
    provider_receipts,
    side_effect_attempts,
)
from careerops.infrastructure.database.submission_dispatch import (
    PostgresSyntheticDispatchReservationStore,
    insert_cap_reservation_statement,
)
from careerops.infrastructure.database.submission_outbox import (
    PostgresSyntheticSubmissionCoordinator,
)
from careerops.policy.autopilot import AutopilotOutcome

pytestmark = pytest.mark.integration

OUTBOX_OWNER = "synthetic-submission-outbox"
DISPOSABLE_DATABASE_PREFIX = "careerops_test_"
SOURCE_DRAFT_HASH = "a" * 64
APPROVED_MATERIAL_HASH = "b" * 64


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    database_name = sa.engine.make_url(value).database or ""
    if not database_name.startswith(DISPOSABLE_DATABASE_PREFIX):
        pytest.skip(
            "submission outbox integration tests require a disposable database named "
            f"{DISPOSABLE_DATABASE_PREFIX}*"
        )
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")

    database_engine = sa.create_engine(database_url)
    with database_engine.begin() as connection:
        _ensure_console_user(connection)
        _record_kill_switch(connection, scope_type="global", active=False)
        _record_kill_switch(connection, scope_type="provider", active=False)
    try:
        yield database_engine
    finally:
        with database_engine.begin() as connection:
            _record_kill_switch(connection, scope_type="global", active=False)
            _record_kill_switch(connection, scope_type="provider", active=False)
        database_engine.dispose()


@pytest.fixture(scope="module")
def outbox_engine(database_url: str, engine: Engine) -> Iterator[Engine]:
    with _capability_engine(
        database_url,
        engine,
        database_role=DatabaseCapabilityRole.OUTBOX,
    ) as runtime_engine:
        yield runtime_engine


@pytest.fixture(scope="module")
def api_engine(database_url: str, engine: Engine) -> Iterator[Engine]:
    with _capability_engine(
        database_url,
        engine,
        database_role=DatabaseCapabilityRole.API,
    ) as runtime_engine:
        yield runtime_engine


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as database_connection:
        transaction = database_connection.begin()
        try:
            yield database_connection
        finally:
            transaction.rollback()


def test_qualified_reservation_claim_receipt_and_publish_are_idempotent(
    engine: Engine,
    api_engine: Engine,
    outbox_engine: Engine,
) -> None:
    request, decision, reservation_id = _seed_and_reserve(engine, api_engine)
    with api_engine.begin() as connection:
        replayed_reservation_id = PostgresSyntheticDispatchReservationStore(
            connection
        ).reserve_and_enqueue(decision, request)
    provider = RecordingSyntheticProvider(SyntheticProviderState.CONFIRMED)
    publisher = _publisher(outbox_engine, provider, _event_key(decision))

    first = _publish_one(publisher, outbox_engine)
    replay = _publish_one(publisher, outbox_engine)

    assert replayed_reservation_id == reservation_id
    assert first.claimed == 1
    assert first.published == 1
    assert replay.claimed == 0
    assert provider.calls == 1
    intent_id = request.authority.action_intent_id
    with engine.connect() as connection:
        assert (
            _count(
                connection,
                autopilot_cap_reservations,
                autopilot_cap_reservations.c.action_intent_id == intent_id,
            )
            == 1
        )
        assert (
            _count(
                connection,
                outbox_events,
                outbox_events.c.action_intent_id == intent_id,
            )
            == 1
        )
        assert (
            _count(
                connection,
                side_effect_attempts,
                side_effect_attempts.c.action_intent_id == intent_id,
            )
            == 1
        )
        assert _receipt_count_for_intent(connection, intent_id) == 1
        assert _audit_count(connection, intent_id, "synthetic_dispatch_reserved") == 1
        assert _audit_count(connection, intent_id, "synthetic_submission_receipt_recorded") == 1
        assert (
            connection.scalar(
                sa.select(action_intents.c.status).where(action_intents.c.id == intent_id)
            )
            == "confirmed"
        )
        event = _event_for_intent(connection, intent_id)
        assert event["status"] == "published"
        assert event["attempt_count"] == 1


def test_distinct_intents_racing_for_final_grant_slot_create_one_reservation(
    engine: Engine,
    api_engine: Engine,
) -> None:
    requests = _seed_requests_sharing_grant(engine, request_count=2, max_total_submissions=1)
    decisions = tuple(SyntheticSubmissionDispatchPlanner().plan(request) for request in requests)
    assert all(decision.can_reserve for decision in decisions)
    barrier = Barrier(len(requests))

    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        futures = [
            executor.submit(_reserve_after_barrier, api_engine, barrier, request, decision)
            for request, decision in zip(requests, decisions, strict=True)
        ]
        results = [future.result(timeout=15) for future in as_completed(futures, timeout=15)]

    successes = [result for result in results if result[0] == "reserved"]
    failures = [result for result in results if result[0] == "dbapi_error"]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0][1] == "23514"
    assert "total cap exhausted" in failures[0][2]
    intent_ids = [request.authority.action_intent_id for request in requests]
    with engine.connect() as connection:
        assert (
            _count(
                connection,
                autopilot_cap_reservations,
                autopilot_cap_reservations.c.grant_version_id
                == requests[0].authority.grant_version_id,
            )
            == 1
        )
        assert (
            _count(
                connection,
                outbox_events,
                outbox_events.c.action_intent_id.in_(intent_ids),
            )
            == 1
        )
        assert (
            _count(
                connection,
                audit_events,
                audit_events.c.resource_id.in_(intent_ids),
                audit_events.c.event_type == "synthetic_dispatch_reserved",
            )
            == 1
        )


def test_concurrent_same_intent_reservation_replays_one_reservation_and_outbox_event(
    engine: Engine,
    api_engine: Engine,
) -> None:
    request = _seed_request(engine)
    decision = SyntheticSubmissionDispatchPlanner().plan(request)
    assert decision.can_reserve
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_reserve_after_barrier, api_engine, barrier, request, decision)
            for _ in range(2)
        ]
        results = [future.result(timeout=15) for future in as_completed(futures, timeout=15)]

    assert all(result[0] == "reserved" for result in results)
    reservation_ids = {result[1] for result in results}
    assert len(reservation_ids) == 1
    intent_id = request.authority.action_intent_id
    with engine.connect() as connection:
        assert (
            _count(
                connection,
                autopilot_cap_reservations,
                autopilot_cap_reservations.c.action_intent_id == intent_id,
            )
            == 1
        )
        assert (
            _count(
                connection,
                outbox_events,
                outbox_events.c.action_intent_id == intent_id,
            )
            == 1
        )
        assert _audit_count(connection, intent_id, "synthetic_dispatch_reserved") == 1


def test_prepared_dispatch_replayed_without_receipt_requires_reconciliation(
    engine: Engine,
    api_engine: Engine,
    outbox_engine: Engine,
) -> None:
    request, decision, _ = _seed_and_reserve(engine, api_engine)
    store = PostgresOutboxStore(outbox_engine)
    claimed = store.claim(
        owner=OUTBOX_OWNER,
        now=_database_now(outbox_engine),
        lease_for=timedelta(seconds=1),
        limit=1,
        event_key_prefix=_event_key(decision),
    )
    assert len(claimed) == 1
    coordinator = PostgresSyntheticSubmissionCoordinator(outbox_engine, owner=OUTBOX_OWNER)
    prepared = coordinator.prepare(claimed[0])
    assert prepared.reconciliation_key == decision.reconciliation_key
    _wait_until_database_time(outbox_engine, claimed[0].lease_until)
    provider = RecordingSyntheticProvider(SyntheticProviderState.CONFIRMED)

    result = _publish_one(
        _publisher(outbox_engine, provider, _event_key(decision)),
        outbox_engine,
    )

    assert result.failed == 1
    assert provider.calls == 0
    intent_id = request.authority.action_intent_id
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(action_intents.c.status).where(action_intents.c.id == intent_id)
            )
            == "reconciliation_required"
        )
        event = _event_for_intent(connection, intent_id)
        assert event["status"] == "failed"
        assert event["attempt_count"] == 2
        assert event["last_error_code"] == "SYNTHETIC_SUBMISSION_RECONCILIATION_REQUIRED"
        assert _receipt_count_for_intent(connection, intent_id) == 0
        attempt = (
            connection.execute(
                sa.select(side_effect_attempts).where(
                    side_effect_attempts.c.action_intent_id == intent_id
                )
            )
            .mappings()
            .one()
        )
        assert attempt["state"] == "failed"
        assert attempt["error_code"] == "SYNTHETIC_SUBMISSION_RECONCILIATION_REQUIRED"
        assert (
            _audit_count(
                connection,
                intent_id,
                "synthetic_submission_reconciliation_required",
            )
            == 1
        )


def test_ambiguous_provider_receipt_requires_reconciliation(
    engine: Engine,
    api_engine: Engine,
    outbox_engine: Engine,
) -> None:
    request, decision, _ = _seed_and_reserve(engine, api_engine)
    provider = RecordingSyntheticProvider(SyntheticProviderState.AMBIGUOUS)

    result = _publish_one(
        _publisher(outbox_engine, provider, _event_key(decision)),
        outbox_engine,
    )

    assert result.failed == 1
    assert provider.calls == 1
    intent_id = request.authority.action_intent_id
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(action_intents.c.status).where(action_intents.c.id == intent_id)
            )
            == "reconciliation_required"
        )
        event = _event_for_intent(connection, intent_id)
        assert event["status"] == "failed"
        assert event["last_error_code"] == "SYNTHETIC_PROVIDER_STATE_AMBIGUOUS"
        attempt = (
            connection.execute(
                sa.select(side_effect_attempts).where(
                    side_effect_attempts.c.action_intent_id == intent_id
                )
            )
            .mappings()
            .one()
        )
        assert attempt["state"] == "reconciliation_required"
        assert attempt["error_code"] == "SYNTHETIC_PROVIDER_STATE_AMBIGUOUS"
        assert _receipt_count_for_intent(connection, intent_id) == 1
        assert _audit_count(connection, intent_id, "synthetic_submission_receipt_recorded") == 1
        assert (
            _audit_count(
                connection,
                intent_id,
                "synthetic_submission_reconciliation_required",
            )
            == 1
        )


def test_grant_revocation_after_reservation_blocks_before_provider_attempt(
    engine: Engine,
    api_engine: Engine,
    outbox_engine: Engine,
) -> None:
    request, decision, _ = _seed_and_reserve(engine, api_engine)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(autopilot_grant_revocations).values(
                id=uuid4(),
                grant_version_id=request.authority.grant_version_id,
                revoked_by_user_id=_singleton_console_user_id(connection),
                reason="operator stopped autonomous dispatch before provider attempt",
            )
        )
    provider = RecordingSyntheticProvider(SyntheticProviderState.CONFIRMED)

    result = _publish_one(
        _publisher(outbox_engine, provider, _event_key(decision)),
        outbox_engine,
    )

    assert result.failed == 1
    assert provider.calls == 0
    intent_id = request.authority.action_intent_id
    with engine.connect() as connection:
        assert (
            _count(
                connection,
                side_effect_attempts,
                side_effect_attempts.c.action_intent_id == intent_id,
            )
            == 0
        )
        assert _receipt_count_for_intent(connection, intent_id) == 0
        assert _event_for_intent(connection, intent_id)["last_error_code"] == (
            "SYNTHETIC_GRANT_NOT_CURRENT"
        )


@pytest.mark.parametrize(
    ("scope_type", "reason_code"),
    [
        ("global", "GLOBAL_KILL_SWITCH_ACTIVE"),
        ("campaign", "CAMPAIGN_KILL_SWITCH_ACTIVE"),
        ("provider", "PROVIDER_KILL_SWITCH_ACTIVE"),
    ],
)
def test_durable_kill_switches_block_after_reservation_before_provider_attempt(
    engine: Engine,
    api_engine: Engine,
    outbox_engine: Engine,
    scope_type: str,
    reason_code: str,
) -> None:
    request, decision, _ = _seed_and_reserve(engine, api_engine)
    provider = RecordingSyntheticProvider(SyntheticProviderState.CONFIRMED)
    campaign_id = request.authority.campaign_id if scope_type == "campaign" else None
    with engine.begin() as connection:
        _record_kill_switch(
            connection,
            scope_type=scope_type,
            active=True,
            campaign_id=campaign_id,
        )

    try:
        result = _publish_one(
            _publisher(outbox_engine, provider, _event_key(decision)),
            outbox_engine,
        )
    finally:
        with engine.begin() as connection:
            _record_kill_switch(
                connection,
                scope_type=scope_type,
                active=False,
                campaign_id=campaign_id,
            )

    assert result.failed == 1
    assert provider.calls == 0
    intent_id = request.authority.action_intent_id
    with engine.connect() as connection:
        assert (
            _count(
                connection,
                side_effect_attempts,
                side_effect_attempts.c.action_intent_id == intent_id,
            )
            == 0
        )
        assert _receipt_count_for_intent(connection, intent_id) == 0
        assert _event_for_intent(connection, intent_id)["last_error_code"] == reason_code


def test_api_role_can_append_and_idempotently_replay_a_kill_switch_event(
    engine: Engine,
    api_engine: Engine,
) -> None:
    with engine.connect() as connection:
        actor_id = _singleton_console_user_id(connection)
    command = SetAutopilotKillSwitchCommand(
        actor_id=actor_id,
        scope=AutopilotKillSwitchScope.global_scope(),
        state=AutopilotKillSwitchState.INACTIVE,
        reason="restricted API integration replay",
        trace_id=f"kill-switch-{uuid4().hex}",
    )

    with api_engine.begin() as connection:
        repository = PostgresAutopilotKillSwitchRepository(connection)
        created = repository.append_event(command)
        replayed = repository.append_event(command)

    assert created.newly_created is True
    assert replayed.newly_created is False
    assert replayed.event_id == created.event_id
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(autopilot_kill_switch_events)
                .where(autopilot_kill_switch_events.c.idempotency_key == command.idempotency_key)
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.select(autopilot_kill_switch_events.c.sequence).where(
                    autopilot_kill_switch_events.c.id == created.event_id
                )
            )
            is not None
        )
        assert not connection.scalar(
            sa.text(
                "SELECT has_table_privilege("
                "'careerops_api', 'careerops.autopilot_kill_switch_events', 'UPDATE')"
            )
        )


@pytest.mark.parametrize(
    ("request_update", "expected_reason"),
    [
        (
            lambda item: replace(
                item,
                release_qualification=replace(
                    item.release_qualification,
                    release_version="stale-release",
                ),
            ),
            "STALE_RELEASE_QUALIFICATION",
        ),
        (
            lambda item: replace(
                item,
                release_qualification=replace(
                    item.release_qualification,
                    expires_at=item.now - timedelta(seconds=1),
                ),
            ),
            "RELEASE_QUALIFICATION_EXPIRED",
        ),
    ],
)
def test_stale_or_expired_release_qualification_is_not_runnable(
    connection: Connection,
    request_update: Callable[
        [QualifiedSyntheticDispatchRequest], QualifiedSyntheticDispatchRequest
    ],
    expected_reason: str,
) -> None:
    request = request_update(seed_qualified_request(connection))
    decision = SyntheticSubmissionDispatchPlanner().plan(request)

    assert decision.can_reserve is False
    assert expected_reason in decision.reason_codes
    assert (
        _count(
            connection,
            autopilot_cap_reservations,
            autopilot_cap_reservations.c.action_intent_id == request.authority.action_intent_id,
        )
        == 0
    )
    assert (
        _count(
            connection,
            outbox_events,
            outbox_events.c.action_intent_id == request.authority.action_intent_id,
        )
        == 0
    )


def test_expired_persisted_release_evidence_is_rechecked_before_provider_attempt(
    engine: Engine,
    api_engine: Engine,
    outbox_engine: Engine,
) -> None:
    request, decision, _ = _seed_and_reserve(
        engine,
        api_engine,
        release_evidence_ttl=timedelta(seconds=2),
    )
    provider = RecordingSyntheticProvider(SyntheticProviderState.CONFIRMED)
    _wait_until_database_time(outbox_engine, request.release_qualification.expires_at)

    result = _publish_one(
        _publisher(outbox_engine, provider, _event_key(decision)),
        outbox_engine,
    )

    assert result.failed == 1
    assert provider.calls == 0
    intent_id = request.authority.action_intent_id
    with engine.connect() as connection:
        assert (
            _count(
                connection,
                side_effect_attempts,
                side_effect_attempts.c.action_intent_id == intent_id,
            )
            == 0
        )
        assert _receipt_count_for_intent(connection, intent_id) == 0
        assert _event_for_intent(connection, intent_id)["last_error_code"] == (
            "SYNTHETIC_RELEASE_EVIDENCE_EXPIRED"
        )


def test_reservation_trigger_rejects_missing_release_evidence(
    engine: Engine,
    api_engine: Engine,
) -> None:
    request = _seed_request(engine)
    decision = SyntheticSubmissionDispatchPlanner().plan(request)
    assert decision.can_reserve
    statement = cast(
        Insert,
        insert_cap_reservation_statement(
            reservation_id=uuid4(),
            decision=decision,
            request=request,
        ),
    ).values(
        release_evidence_hash=None,
        release_evidence_expires_at=None,
    )

    with (
        pytest.raises(DBAPIError) as missing_evidence,
        api_engine.begin() as connection,
    ):
        connection.execute(statement)

    assert sqlstate(missing_evidence.value) == "23514"
    with engine.connect() as connection:
        assert (
            _count(
                connection,
                autopilot_cap_reservations,
                autopilot_cap_reservations.c.action_intent_id == request.authority.action_intent_id,
            )
            == 0
        )


def test_confirmed_receipt_replay_after_evidence_expiry_skips_provider_and_republishes(
    engine: Engine,
    api_engine: Engine,
    outbox_engine: Engine,
) -> None:
    request, decision, _ = _seed_and_reserve(
        engine,
        api_engine,
        release_evidence_ttl=timedelta(seconds=4),
    )
    provider = RecordingSyntheticProvider(SyntheticProviderState.CONFIRMED)
    store = PostgresOutboxStore(outbox_engine)
    claimed = store.claim(
        owner=OUTBOX_OWNER,
        now=_database_now(outbox_engine),
        lease_for=timedelta(seconds=5),
        limit=1,
        event_key_prefix=_event_key(decision),
    )
    assert len(claimed) == 1
    coordinator = PostgresSyntheticSubmissionCoordinator(outbox_engine, owner=OUTBOX_OWNER)
    dispatch = coordinator.prepare(claimed[0])
    receipt = provider.submit(dispatch, now=_database_now(outbox_engine))
    coordinator.record_receipt(dispatch, receipt)

    intent_id = request.authority.action_intent_id
    with engine.connect() as connection:
        event_before_replay = _event_for_intent(connection, intent_id)
        assert event_before_replay["status"] == "leased"
        assert event_before_replay["published_at"] is None
        assert _receipt_count_for_intent(connection, intent_id) == 1
    _wait_until_database_time(
        outbox_engine,
        max(claimed[0].lease_until, request.release_qualification.expires_at),
    )
    result = _publish_one(
        _publisher(outbox_engine, provider, _event_key(decision)),
        outbox_engine,
    )

    assert result.claimed == 1
    assert result.published == 1
    assert provider.calls == 1
    with engine.connect() as connection:
        event = _event_for_intent(connection, intent_id)
        assert event["status"] == "published"
        assert event["attempt_count"] == 2
        assert (
            _count(
                connection,
                side_effect_attempts,
                side_effect_attempts.c.action_intent_id == intent_id,
            )
            == 1
        )
        assert _receipt_count_for_intent(connection, intent_id) == 1
        assert _audit_count(connection, intent_id, "synthetic_submission_receipt_recorded") == 1


def test_outbox_role_has_execute_only_for_synthetic_submission_functions(
    connection: Connection,
) -> None:
    functions = {
        row[0]: (row[1], row[2])
        for row in connection.execute(
            sa.text(
                """
                SELECT
                    pg_proc.proname,
                    pg_proc.prosecdef,
                    has_function_privilege(
                        'careerops_outbox', pg_proc.oid, 'EXECUTE'
                    )
                FROM pg_proc
                JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
                WHERE pg_namespace.nspname = 'careerops'
                  AND proname IN (
                    'prepare_synthetic_submission_outbox_event',
                    'record_synthetic_submission_outbox_receipt',
                    'record_synthetic_submission_outbox_ambiguity'
                  )
                """
            )
        )
    }

    assert set(functions) == {
        "prepare_synthetic_submission_outbox_event",
        "record_synthetic_submission_outbox_receipt",
        "record_synthetic_submission_outbox_ambiguity",
    }
    assert all(
        security_definer and can_execute for security_definer, can_execute in functions.values()
    )
    for table_name in ("side_effect_attempts", "provider_receipts"):
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            assert not connection.scalar(
                sa.text("SELECT has_table_privilege('careerops_outbox', :table_name, :privilege)"),
                {
                    "table_name": f"careerops.{table_name}",
                    "privilege": privilege,
                },
            )


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            sa.text("INSERT INTO careerops.side_effect_attempts DEFAULT VALUES"),
            id="attempt",
        ),
        pytest.param(
            sa.text("INSERT INTO careerops.provider_receipts DEFAULT VALUES"),
            id="receipt",
        ),
    ],
)
def test_outbox_role_cannot_directly_write_attempts_or_receipts(
    outbox_engine: Engine,
    statement: sa.TextClause,
) -> None:
    with (
        pytest.raises(DBAPIError) as permission_error,
        outbox_engine.begin() as connection,
    ):
        connection.execute(statement)
    assert sqlstate(permission_error.value) == "42501"


@contextmanager
def _capability_engine(
    database_url: str,
    owner_engine: Engine,
    *,
    database_role: DatabaseCapabilityRole,
) -> Iterator[Engine]:
    capability_role = f"careerops_{database_role.value}"
    login = f"co_{database_role.value}_{uuid4().hex}"
    password = f"careerops-test-{uuid4().hex}"
    runtime_url = sa.engine.make_url(database_url).set(username=login, password=password)
    settings = Settings.model_validate(
        {
            "database_url": runtime_url.render_as_string(hide_password=False),
            "database_role": database_role,
        }
    )
    with owner_engine.begin() as connection:
        connection.execute(
            sa.text(
                f"CREATE ROLE {login} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                f"NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{password}'"
            )
        )
        connection.execute(sa.text(f"GRANT {capability_role} TO {login}"))

    runtime_engine = create_database_engine(settings)
    try:
        yield runtime_engine
    finally:
        runtime_engine.dispose()
        with owner_engine.begin() as connection:
            connection.execute(sa.text(f"REVOKE {capability_role} FROM {login}"))
            connection.execute(sa.text(f"DROP ROLE {login}"))


def _seed_request(
    engine: Engine,
    *,
    release_evidence_ttl: timedelta = timedelta(hours=1),
) -> QualifiedSyntheticDispatchRequest:
    with engine.begin() as connection:
        return seed_qualified_request(
            connection,
            release_evidence_ttl=release_evidence_ttl,
        )


def _seed_and_reserve(
    engine: Engine,
    api_engine: Engine,
    *,
    release_evidence_ttl: timedelta = timedelta(hours=1),
) -> tuple[QualifiedSyntheticDispatchRequest, DispatchDecision, UUID]:
    request = _seed_request(engine, release_evidence_ttl=release_evidence_ttl)
    decision = SyntheticSubmissionDispatchPlanner().plan(request)
    assert decision.can_reserve
    with api_engine.begin() as connection:
        reservation_id = PostgresSyntheticDispatchReservationStore(connection).reserve_and_enqueue(
            decision, request
        )
    return request, decision, reservation_id


def _seed_requests_sharing_grant(
    engine: Engine,
    *,
    request_count: int,
    max_total_submissions: int,
) -> tuple[QualifiedSyntheticDispatchRequest, ...]:
    if request_count < 1:
        raise ValueError("request_count must be positive")
    with engine.begin() as connection:
        first = seed_qualified_request(
            connection,
            max_total_submissions=max_total_submissions,
        )
        requests = [first]
        for _ in range(request_count - 1):
            requests.append(
                seed_qualified_request(
                    connection,
                    shared_campaign_id=first.authority.campaign_id,
                    shared_grant_version_id=first.authority.grant_version_id,
                )
            )
    return tuple(requests)


def _reserve_after_barrier(
    api_engine: Engine,
    barrier: Barrier,
    request: QualifiedSyntheticDispatchRequest,
    decision: DispatchDecision,
) -> tuple[str, UUID | str | None, str]:
    barrier.wait(timeout=10)
    try:
        with api_engine.begin() as connection:
            reservation_id = PostgresSyntheticDispatchReservationStore(
                connection
            ).reserve_and_enqueue(decision, request)
    except DBAPIError as error:
        return ("dbapi_error", sqlstate(error), str(error.orig))
    return ("reserved", reservation_id, "")


def _publisher(
    engine: Engine,
    provider: RecordingSyntheticProvider,
    event_key: str,
) -> OutboxPublisher:
    return OutboxPublisher(
        PostgresOutboxStore(engine),
        SyntheticSubmissionOutboxSink(
            PostgresSyntheticSubmissionCoordinator(engine, owner=OUTBOX_OWNER),
            provider,
            clock=lambda: _database_now(engine),
        ),
        event_key_prefix=event_key,
        clock=lambda: _database_now(engine),
    )


def _publish_one(publisher: OutboxPublisher, engine: Engine) -> PublishBatchResult:
    return publisher.publish_batch(
        owner=OUTBOX_OWNER,
        now=_database_now(engine),
        limit=1,
    )


def _event_key(decision: DispatchDecision) -> str:
    event_key = decision.outbox_event_key
    assert isinstance(event_key, str)
    assert event_key.startswith(SYNTHETIC_DISPATCH_EVENT_KEY_PREFIX)
    return event_key


def _database_now(engine: Engine) -> datetime:
    with engine.connect() as connection:
        return _connection_now(connection)


def _connection_now(connection: Connection) -> datetime:
    now = connection.scalar(sa.select(sa.func.clock_timestamp()))
    assert isinstance(now, datetime)
    assert now.tzinfo is not None and now.utcoffset() is not None
    return now


def _transaction_now(connection: Connection) -> datetime:
    now = connection.scalar(sa.select(sa.func.current_timestamp()))
    assert isinstance(now, datetime)
    assert now.tzinfo is not None and now.utcoffset() is not None
    return now


def _wait_until_database_time(engine: Engine, target: datetime) -> None:
    deadline = monotonic() + 15
    while _database_now(engine) <= target:
        if monotonic() >= deadline:
            pytest.fail(f"database clock did not pass {target.isoformat()}")
        sleep(0.05)


def _ensure_console_user(connection: Connection) -> UUID:
    existing = connection.scalar(sa.select(console_users.c.id))
    if isinstance(existing, UUID):
        return existing
    user_id = uuid4()
    connection.execute(
        sa.insert(console_users).values(
            id=user_id,
            username="submission.outbox.integration",
            password_hash="$argon2id$submission-outbox-integration-placeholder",
            password_parameters={},
            password_changed_at=_connection_now(connection),
        )
    )
    return user_id


def _record_kill_switch(
    connection: Connection,
    *,
    scope_type: str,
    active: bool,
    campaign_id: UUID | None = None,
) -> None:
    if scope_type not in {"global", "campaign", "provider"}:
        raise ValueError("unsupported integration-test kill-switch scope")
    connection.execute(
        sa.insert(autopilot_kill_switch_events).values(
            id=uuid4(),
            scope_type=scope_type,
            campaign_id=campaign_id if scope_type == "campaign" else None,
            provider="synthetic" if scope_type == "provider" else None,
            active=active,
            reason="submission outbox integration test",
            actor_user_id=_singleton_console_user_id(connection),
            idempotency_key=f"submission-outbox-integration:{uuid4()}",
            trace_id=f"submission-outbox-integration:{uuid4()}",
        )
    )


def _event_for_intent(connection: Connection, intent_id: UUID) -> RowMapping:
    return (
        connection.execute(
            sa.select(outbox_events).where(outbox_events.c.action_intent_id == intent_id)
        )
        .mappings()
        .one()
    )


def _receipt_count_for_intent(connection: Connection, intent_id: UUID) -> int:
    count = connection.scalar(
        sa.select(sa.func.count())
        .select_from(
            provider_receipts.join(
                side_effect_attempts,
                provider_receipts.c.side_effect_attempt_id == side_effect_attempts.c.id,
            )
        )
        .where(side_effect_attempts.c.action_intent_id == intent_id)
    )
    assert isinstance(count, int)
    return count


def _audit_count(connection: Connection, intent_id: UUID, event_type: str) -> int:
    return _count(
        connection,
        audit_events,
        audit_events.c.resource_id == intent_id,
        audit_events.c.event_type == event_type,
    )


class RecordingSyntheticProvider:
    def __init__(self, provider_state: SyntheticProviderState) -> None:
        self._provider_state = provider_state
        self.calls = 0

    def submit(
        self,
        dispatch: PreparedSyntheticSubmission,
        *,
        now: datetime,
    ) -> SyntheticSubmissionReceipt:
        self.calls += 1
        return SyntheticSubmissionReceipt(
            provider="synthetic",
            provider_resource_id=f"receipt-{dispatch.reservation_key[:32]}",
            reconciliation_key=dispatch.reconciliation_key,
            provider_state=self._provider_state,
            received_at=now,
        )


def seed_qualified_request(
    connection: Connection,
    *,
    release_evidence_ttl: timedelta = timedelta(hours=1),
    max_total_submissions: int = 10,
    shared_campaign_id: UUID | None = None,
    shared_grant_version_id: UUID | None = None,
    **overrides: object,
) -> QualifiedSyntheticDispatchRequest:
    user_id = _singleton_console_user_id(connection)
    now = _transaction_now(connection)
    intent_id = uuid4()
    payload = sandbox_payload(intent_id)
    if (shared_campaign_id is None) is not (shared_grant_version_id is None):
        raise ValueError("shared campaign and grant ids must be supplied together")
    campaign_id = shared_campaign_id or uuid4()
    grant_version_id = shared_grant_version_id or uuid4()
    policy_decision_id = uuid4()
    authorization_id = uuid4()
    payload_version_id = uuid4()
    connection.execute(
        sa.insert(action_intents).values(
            id=intent_id,
            action_kind="submit_application",
            resource_type="application_event",
            resource_id=uuid4(),
            idempotency_key=f"synthetic-dispatch-test:{intent_id}",
            status="eligible",
            created_by="integration-test",
        )
    )
    connection.execute(
        sa.insert(action_payload_versions).values(
            id=payload_version_id,
            action_intent_id=intent_id,
            version=1,
            target={
                "target_host": payload.target_host,
                "channel": payload.channel,
            },
            payload=dict(payload.fields),
            attachment_refs=[{"sha256": APPROVED_MATERIAL_HASH}],
            payload_hash=payload.payload_hash,
        )
    )
    connection.execute(
        sa.update(action_intents)
        .where(action_intents.c.id == intent_id)
        .values(current_payload_version_id=payload_version_id)
    )
    if shared_campaign_id is None:
        connection.execute(
            sa.insert(autopilot_campaigns).values(
                id=campaign_id,
                owner_user_id=user_id,
                name="Synthetic submission integration",
                objective="Verify synthetic outbox control plane",
                criteria={},
                exclusions=[],
                created_by="integration-test",
            )
        )
        connection.execute(
            sa.insert(autopilot_grant_versions).values(
                id=grant_version_id,
                campaign_id=campaign_id,
                version=1,
                subject_actor=str(user_id),
                allowed_action_kinds=["submit_application"],
                allowed_channels=[payload.channel],
                allowed_target_hosts=[payload.target_host],
                material_hashes=[APPROVED_MATERIAL_HASH],
                max_total_submissions=max_total_submissions,
                max_daily_submissions=10,
                max_per_company=10,
                policy_ruleset_version="ruleset-v1",
                release_version="release-v1",
                expires_at=now + timedelta(hours=1),
            )
        )
    connection.execute(
        sa.insert(policy_decisions).values(
            id=policy_decision_id,
            action_intent_id=intent_id,
            payload_version_id=payload_version_id,
            ruleset_version="ruleset-v1",
            decision="allow_autopilot_submission",
            reason_codes=["SYNTHETIC_SANDBOX_ALLOWED"],
            payload_hash=payload.payload_hash,
            expires_at=now + timedelta(hours=1),
        )
    )
    connection.execute(
        sa.insert(autopilot_intent_authorizations).values(
            id=authorization_id,
            campaign_id=campaign_id,
            grant_version_id=grant_version_id,
            action_intent_id=intent_id,
            payload_version_id=payload_version_id,
            payload_hash=payload.payload_hash,
            policy_decision_id=policy_decision_id,
            authorization_outcome="allow_autopilot_submission",
            reason_codes=["SYNTHETIC_SANDBOX_ALLOWED"],
            authorized_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
    request = QualifiedSyntheticDispatchRequest(
        authority=DispatchAuthority(
            campaign_id=campaign_id,
            grant_version_id=grant_version_id,
            authorization_id=authorization_id,
            action_intent_id=intent_id,
            payload_version_id=payload_version_id,
            policy_decision_id=policy_decision_id,
            action_kind="submit_application",
            channel=payload.channel,
            release_version="release-v1",
            payload_hash=payload.payload_hash,
            target_host=payload.target_host,
            company_key="example-inc",
            policy_outcome=AutopilotOutcome.ALLOW_AUTOPILOT_SUBMISSION,
            authorized_at=now,
            expires_at=now + timedelta(hours=1),
        ),
        fixture=SyntheticApplicationFixture(
            fixture_id="fixture-1",
            adapter_id="greenhouse-sandbox",
            allowed_host=payload.target_host,
            site_policy_text="Automated submissions are allowed for this synthetic sandbox.",
            allowed_fields=(
                FormFieldSpec("first_name"),
                FormFieldSpec("last_name"),
                FormFieldSpec("resume_sha256"),
            ),
        ),
        session=SandboxBrowserSession(
            session_id=uuid4(),
            action_intent_id=intent_id,
            isolation_mode=BrowserIsolationMode.PER_INTENT_CONTEXT,
            host=payload.target_host,
        ),
        payload=payload,
        release_qualification=SyntheticReleaseQualification(
            adapter_id="greenhouse-sandbox",
            fixture_id="fixture-1",
            release_version="release-v1",
            stage=AutopilotReleaseStage.SYNTHETIC_SANDBOX,
            evidence_hash="c" * 64,
            expires_at=now + release_evidence_ttl,
        ),
        now=now,
    )
    if overrides:
        request = replace(request, **overrides)
    return request


def sandbox_payload(intent_id: UUID) -> SandboxApplicationPayload:
    return SandboxApplicationPayload(
        action_intent_id=intent_id,
        target_host="sandbox.greenhouse.test",
        channel="synthetic:greenhouse-sandbox",
        fields=MappingProxyType(
            {
                "first_name": "Ada",
                "last_name": "Lovelace",
                "resume_sha256": APPROVED_MATERIAL_HASH,
            }
        ),
        source_draft_payload_hash=SOURCE_DRAFT_HASH,
        approved_material_hashes=(APPROVED_MATERIAL_HASH,),
    )


def _singleton_console_user_id(connection: Connection) -> UUID:
    user_id = connection.scalar(sa.select(console_users.c.id))
    assert isinstance(user_id, UUID)
    return user_id


def _count(
    connection: Connection,
    table: sa.Table,
    *predicates: sa.ColumnElement[bool],
) -> int:
    count = connection.scalar(sa.select(sa.func.count()).select_from(table).where(*predicates))
    assert isinstance(count, int)
    return count


def sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)
