# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from time import monotonic, sleep
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from careerops.application.autopilot_commands import RevokeGrantCommand
from careerops.infrastructure.database.autopilot_commands import PostgresAutopilotCommandStore

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)
DISPOSABLE_PREFIX = "careerops_test_"

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for Gmail send DB tests")
    database_name = sa.engine.make_url(value).database or ""
    if not database_name.startswith(DISPOSABLE_PREFIX):
        pytest.skip("Gmail send DB tests require a disposable careerops_test_* database")
    if os.environ.get("CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE") != "1":
        pytest.skip("CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE=1 is required")
    return value


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    database_engine = sa.create_engine(database_url)
    try:
        yield database_engine
    finally:
        database_engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as database_connection:
        transaction = database_connection.begin()
        try:
            yield database_connection
        finally:
            transaction.rollback()


def test_register_account_binds_exact_readonly_without_touching_unrelated_account(
    connection: Connection,
) -> None:
    owner_id, candidate_id = _seed_owner_candidate_pair(connection, "register")
    readonly_id = _register_readonly_account(
        connection,
        owner_id=owner_id,
        candidate_id=candidate_id,
        handle="vault://gmail-readonly-independent",
        subject="same-user@example.com",
    )
    assert _raises_db_error(
        connection,
        """
        SELECT careerops.gmail_send_register_account(
            :owner_id, :candidate_id, :handle, :subject, 'testing',
            :credential_evidence, :release_evidence, 50, :readonly_id, :key, :trace
        )
        """,
        {
            "owner_id": owner_id,
            "candidate_id": candidate_id,
            "handle": "vault://gmail-send-out-of-scope",
            "subject": "same-user-send@example.com",
            "credential_evidence": _sha("credential:gmail-send-out-of-scope"),
            "release_evidence": _sha("release:gmail-send-out-of-scope"),
            "readonly_id": readonly_id,
            "key": "send-register-out-of-scope",
            "trace": "trace-send-register-out-of-scope",
        },
    )

    account = _register_send_account(
        connection,
        owner_id=owner_id,
        candidate_id=candidate_id,
        subject="same-user-send@example.com",
        handle="vault://gmail-send-primary",
        key="send-register-primary",
    )

    assert account["status"] == "active"
    assert account["reconciliation_gmail_account_id"] != readonly_id
    credential = (
        connection.execute(
            sa.text(
                """
            SELECT provider, account_subject, secret_handle, granted_scopes, status
            FROM careerops.oauth_credential_references
            WHERE id = (
                SELECT oauth_credential_reference_id
                FROM careerops.gmail_send_accounts
                WHERE id = :account_id
            )
            """
            ),
            {"account_id": account["account_id"]},
        )
        .mappings()
        .one()
    )
    assert credential == {
        "provider": "gmail_send",
        "account_subject": "same-user-send@example.com",
        "secret_handle": "vault://gmail-send-primary",
        "granted_scopes": [GMAIL_SEND_SCOPE],
        "status": "active",
    }
    assert (
        connection.scalar(
            sa.text("SELECT status FROM careerops.gmail_accounts WHERE id = :readonly_id"),
            {"readonly_id": readonly_id},
        )
        == "active"
    )
    assert (
        connection.scalar(
            sa.text(
                """
                SELECT account_subject
                FROM careerops.gmail_accounts
                WHERE id = :readonly_id
                """
            ),
            {"readonly_id": account["reconciliation_gmail_account_id"]},
        )
        == "same-user-send@example.com"
    )
    cross_owner_rows = connection.execute(
        sa.text("SELECT * FROM careerops.gmail_send_status(:other_owner_id, :account_id)"),
        {"other_owner_id": uuid4(), "account_id": account["account_id"]},
    ).all()
    assert cross_owner_rows == []


def test_register_account_replay_rejects_changed_reconciliation_binding(
    connection: Connection,
) -> None:
    owner_id, candidate_id = _seed_owner_candidate_pair(connection, "register-replay-binding")
    readonly_id = _register_readonly_account(
        connection,
        owner_id=owner_id,
        candidate_id=candidate_id,
        handle="vault://gmail-readonly-replay-binding",
        subject="replay-binding@example.com",
    )
    send_handle = "vault://gmail-send-replay-binding"
    key = "send-register-replay-binding"
    account = _register_send_account(
        connection,
        owner_id=owner_id,
        candidate_id=candidate_id,
        subject="replay-binding@example.com",
        handle=send_handle,
        key=key,
        reconciliation_gmail_account_id=readonly_id,
    )

    replay_id = connection.scalar(
        sa.text(
            """
            SELECT careerops.gmail_send_register_account(
                :owner_id, :candidate_id, :handle, :subject, 'testing',
                :credential_evidence, :release_evidence, 50, :readonly_id, :key, :trace
            )
            """
        ),
        {
            "owner_id": owner_id,
            "candidate_id": candidate_id,
            "handle": send_handle,
            "subject": "replay-binding@example.com",
            "credential_evidence": _sha(f"credential:{send_handle}"),
            "release_evidence": _sha(f"release:{send_handle}"),
            "readonly_id": readonly_id,
            "key": key,
            "trace": "trace-send-register-replay-binding-replay",
        },
    )
    assert replay_id == account["account_id"]

    other_readonly_id = _register_readonly_account(
        connection,
        owner_id=owner_id,
        candidate_id=candidate_id,
        handle="vault://gmail-readonly-replay-binding-other",
        subject="other-replay-binding@example.com",
    )
    connection.execute(
        sa.text(
            """
            UPDATE careerops.gmail_send_accounts
            SET reconciliation_gmail_account_id = :other_readonly_id
            WHERE id = :account_id
            """
        ),
        {"other_readonly_id": other_readonly_id, "account_id": account["account_id"]},
    )

    with pytest.raises(DBAPIError) as replay_error, connection.begin_nested():
        connection.execute(
            sa.text(
                """
                SELECT careerops.gmail_send_register_account(
                    :owner_id, :candidate_id, :handle, :subject, 'testing',
                    :credential_evidence, :release_evidence, 50, :readonly_id, :key, :trace
                )
                """
            ),
            {
                "owner_id": owner_id,
                "candidate_id": candidate_id,
                "handle": send_handle,
                "subject": "replay-binding@example.com",
                "credential_evidence": _sha(f"credential:{send_handle}"),
                "release_evidence": _sha(f"release:{send_handle}"),
                "readonly_id": readonly_id,
                "key": key,
                "trace": "trace-send-register-replay-binding-conflict",
            },
        )
    assert _sqlstate(replay_error.value) == "23505"
    assert (
        connection.scalar(
            sa.text(
                """
                SELECT reconciliation_gmail_account_id
                FROM careerops.gmail_send_accounts
                WHERE id = :account_id
                """
            ),
            {"account_id": account["account_id"]},
        )
        == other_readonly_id
    )


def test_reserve_requires_exact_reviewed_payload_and_replays_without_duplicate_outbox(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection)
    first = _reserve(connection, fixture, key="reserve-exact")
    replay = _reserve(connection, fixture, key="reserve-exact")

    assert first["receipt_state"] == "created"
    assert replay["receipt_state"] == "replayed"
    assert replay["reservation_id"] == first["reservation_id"]
    assert replay["outbox_event_id"] == first["outbox_event_id"]
    assert (
        _count_where(
            connection,
            "careerops.autopilot_cap_reservations",
            "action_intent_id = :action_intent_id",
            {"action_intent_id": fixture.action_intent_id},
        )
        == 1
    )
    assert (
        _count_where(
            connection,
            "careerops.outbox_events",
            "action_intent_id = :action_intent_id",
            {"action_intent_id": fixture.action_intent_id},
        )
        == 1
    )
    assert (
        _count_where(
            connection,
            "careerops.gmail_send_review_evidence",
            "action_intent_id = :action_intent_id",
            {"action_intent_id": fixture.action_intent_id},
        )
        == 1
    )


def test_reserve_reviewed_intent_replay_rejects_changed_immutable_inputs(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="reviewed-intent-replay")
    application_id = uuid4()
    attachment_manifest_sha256 = _sha(f"attachment:{fixture.id_prefix}")
    reservation_id = uuid4()
    key = "reserve-reviewed-intent-replay"
    params = _reserve_reviewed_intent_params(
        fixture,
        application_id=application_id,
        attachment_manifest_sha256=attachment_manifest_sha256,
        key=key,
    )
    _insert_reviewed_intent_reservation(
        connection,
        fixture,
        reservation_id=reservation_id,
        application_id=application_id,
        recipient_email=f"recruiter-{fixture.id_prefix}@example.com",
        attachment_manifest_sha256=attachment_manifest_sha256,
        key=key,
    )

    replay_id = connection.scalar(
        sa.text(_reserve_reviewed_intent_sql()),
        params | {"trace_id": "trace-reserve-reviewed-intent-replay-same"},
    )
    assert replay_id == reservation_id

    changed_inputs: dict[str, object] = {
        "account_id": uuid4(),
        "reviewed_intent_id": uuid4(),
        "application_id": uuid4(),
        "recipient_email": "changed-recipient@example.com",
        "recipient_snapshot_sha256": _sha("changed-recipient"),
        "subject_sha256": _sha("changed-subject"),
        "body_sha256": _sha("changed-body"),
        "payload_sha256": _sha("changed-payload"),
        "approval_snapshot_sha256": _sha("changed-approval"),
        "attachment_manifest_sha256": _sha("changed-attachment"),
    }
    for field, value in changed_inputs.items():
        with pytest.raises(DBAPIError) as replay_error, connection.begin_nested():
            connection.execute(
                sa.text(_reserve_reviewed_intent_sql()),
                params | {field: value, "trace_id": f"trace-reserve-reviewed-intent-{field}"},
            )
        assert _sqlstate(replay_error.value) == "23505"


def test_reserve_rejects_payload_hash_tamper_and_generic_approval(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection)
    assert _raises_db_error(
        connection,
        _reserve_sql(),
        _reserve_params(fixture, key="reserve-tampered") | {"payload_hash": "f" * 64},
    )

    generic_fixture = _seed_unreviewed_draft_fixture(connection, id_prefix="generic-approval")
    connection.execute(
        sa.text(
            """
            UPDATE careerops.approval_requests
            SET requested_for = 'generic_human_approval'
            WHERE id = :approval_request_id
            """
        ),
        {"approval_request_id": generic_fixture.approval_request_id},
    )
    assert _raises_db_error(
        connection,
        _review_draft_sql(),
        _review_params(generic_fixture, key="review-generic"),
    )


def test_reserve_rejects_unqualified_release_qualification(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="forged-release")
    unqualified_release_id, _, _ = _seed_gmail_release_qualification(
        connection,
        owner_id=fixture.owner_id,
        account_id=fixture.account_id,
        id_prefix="forged-release-unqualified",
        qualified=False,
    )

    assert _raises_db_error(
        connection,
        _reserve_sql(),
        _reserve_params(fixture, key="reserve-forged-release")
        | {"release_qualification_id": unqualified_release_id},
    )


def test_reserve_rejects_subject_sha256_mismatch(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="subject-mismatch")

    assert _raises_db_error(
        connection,
        _reserve_sql(),
        _reserve_params(fixture, key="reserve-subject-mismatch")
        | {"subject_sha256": _sha("subject:forged")},
    )


def test_reserve_rejects_grant_with_only_recipient_material(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(
        connection,
        id_prefix="reserve-recipient-only-grant",
        grant_recipient_only=True,
        review=False,
    )

    with pytest.raises(DBAPIError), connection.begin_nested():
        _review_send_fixture(
            connection,
            fixture,
            key=f"review-recipient-only-{uuid4()}",
        )
    assert _raises_db_error(
        connection,
        _reserve_sql(),
        _reserve_params(fixture, key="reserve-recipient-only-grant"),
    )
    with pytest.raises(DBAPIError), connection.begin_nested():
        _insert_bait_reservation_for_same_payload(
            connection,
            fixture,
            reservation_key=f"recipient-only-reservation-{uuid4().hex}",
            reconciliation_key=f"recipient-only/reconcile/{uuid4().hex}",
        )
    assert (
        _count_where(
            connection,
            "careerops.autopilot_cap_reservations",
            "action_intent_id = :action_intent_id",
            {"action_intent_id": fixture.action_intent_id},
        )
        == 0
    )
    assert (
        _count_where(
            connection,
            "careerops.outbox_events",
            "action_intent_id = :action_intent_id",
            {"action_intent_id": fixture.action_intent_id},
        )
        == 0
    )


def test_review_draft_rejects_campaign_grant_tuple_mismatch(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="review-grant-mismatch", review=False)
    other_campaign_id = _seed_campaign(connection, owner_id=fixture.owner_id)

    assert _raises_db_error(
        connection,
        _review_draft_sql(),
        _review_params(fixture, key="review-grant-mismatch") | {"campaign_id": other_campaign_id},
    )


def test_reserve_rejects_reviewer_owner_mismatch(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="reserve-reviewer-mismatch")

    assert _raises_db_error(
        connection,
        _reserve_sql(),
        _reserve_params(fixture, key="reserve-reviewer-mismatch")
        | {"reviewed_by_user_id": uuid4()},
    )


def test_prepare_stops_without_attempt_when_release_is_revoked_after_reserve(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="prepare-revoked-release")
    reserved = _reserve(connection, fixture, key="reserve-revoked-release")
    _revoke_gmail_release_qualification(
        connection,
        fixture,
        reason="test revoke after reservation before provider send",
    )
    claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))

    prepared = _prepare(connection, claim)

    assert prepared["prepare_state"] == "stopped"
    assert prepared["reason_code"] == "GMAIL_SEND_RELEASE_NOT_QUALIFIED"
    assert (
        _count_where(
            connection,
            "careerops.side_effect_attempts",
            "outbox_event_id = :outbox_event_id",
            {"outbox_event_id": reserved["outbox_event_id"]},
        )
        == 0
    )


def test_prepare_stops_without_attempt_when_grant_is_revoked_after_reserve(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="prepare-revoked-grant")
    reserved = _reserve(connection, fixture, key="reserve-prepare-revoked-grant")
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_grant_revocations (
                id, grant_version_id, revoked_by_user_id, reason, created_at
            )
            VALUES (
                :id, :grant_id, :owner_id,
                'test revoke after reservation before provider send', :now
            )
            """
        ),
        {
            "id": uuid4(),
            "grant_id": fixture.grant_id,
            "owner_id": fixture.owner_id,
            "now": NOW,
        },
    )
    claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))

    prepared = _prepare(connection, claim)

    assert prepared["prepare_state"] == "stopped"
    assert prepared["reason_code"] == "GMAIL_SEND_GRANT_NOT_ACTIVE"
    assert (
        _count_where(
            connection,
            "careerops.side_effect_attempts",
            "outbox_event_id = :outbox_event_id",
            {"outbox_event_id": reserved["outbox_event_id"]},
        )
        == 0
    )


def test_concurrent_grant_revocation_linearizes_before_prepare(engine: Engine) -> None:
    with engine.begin() as connection:
        fixture = _seed_send_fixture(connection, id_prefix="prepare-concurrent-grant-revoke")
        reserved = _reserve(connection, fixture, key="reserve-concurrent-grant-revoke")
        claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))

    revocation_inserted = Event()
    allow_revocation_commit = Event()
    prepare_pid_ready = Event()
    prepare_backend_pid: list[int] = []

    def revoke_while_holding_state_lock() -> None:
        with engine.begin() as revocation_connection:
            store = PostgresAutopilotCommandStore(revocation_connection)
            store.revoke_grant(
                RevokeGrantCommand(
                    actor_id=fixture.owner_id,
                    grant_version_id=fixture.grant_id,
                    reason="concurrent revoke before provider prepare",
                    trace_id="trace-concurrent-grant-revoke",
                )
            )
            revocation_inserted.set()
            assert allow_revocation_commit.wait(timeout=10)

    def prepare_while_revocation_is_uncommitted() -> dict[str, object]:
        assert revocation_inserted.wait(timeout=10)
        with engine.begin() as prepare_connection:
            backend_pid = prepare_connection.scalar(sa.text("SELECT pg_backend_pid()"))
            assert isinstance(backend_pid, int)
            prepare_backend_pid.append(backend_pid)
            prepare_pid_ready.set()
            return _prepare(prepare_connection, claim)

    with ThreadPoolExecutor(max_workers=2) as executor:
        revocation_future = executor.submit(revoke_while_holding_state_lock)
        assert revocation_inserted.wait(timeout=10)
        prepare_future = executor.submit(prepare_while_revocation_is_uncommitted)
        assert prepare_pid_ready.wait(timeout=10)

        deadline = monotonic() + 10
        observed_advisory_wait = False
        with engine.connect() as observer_connection:
            while monotonic() < deadline:
                wait_state = (
                    observer_connection.execute(
                        sa.text(
                            """
                            SELECT wait_event_type, wait_event
                            FROM pg_stat_activity
                            WHERE pid = :backend_pid
                            """
                        ),
                        {"backend_pid": prepare_backend_pid[0]},
                    )
                    .mappings()
                    .one_or_none()
                )
                if wait_state is not None and str(wait_state["wait_event"]).lower() == "advisory":
                    observed_advisory_wait = True
                    break
                sleep(0.01)
        assert observed_advisory_wait
        allow_revocation_commit.set()
        revocation_future.result(timeout=10)
        prepared = prepare_future.result(timeout=10)

    assert prepared["prepare_state"] == "stopped"
    assert prepared["reason_code"] == "GMAIL_SEND_GRANT_NOT_ACTIVE"
    with engine.connect() as connection:
        assert (
            _count_where(
                connection,
                "careerops.side_effect_attempts",
                "outbox_event_id = :outbox_event_id",
                {"outbox_event_id": reserved["outbox_event_id"]},
            )
            == 0
        )


def test_prepare_stops_without_attempt_when_provider_kill_switch_is_enabled_after_reserve(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="prepare-provider-kill")
    reserved = _reserve(connection, fixture, key="reserve-provider-kill")
    _set_kill_switch(connection, scope_type="provider", active=True, provider="gmail")
    claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))

    prepared = _prepare(connection, claim)

    assert prepared["prepare_state"] == "stopped"
    assert prepared["reason_code"] == "GMAIL_SEND_KILL_SWITCH_ACTIVE"
    assert (
        _count_where(
            connection,
            "careerops.side_effect_attempts",
            "outbox_event_id = :outbox_event_id",
            {"outbox_event_id": reserved["outbox_event_id"]},
        )
        == 0
    )


def test_prepare_ignores_newer_unreserved_account_review_evidence(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="prepare-cross-owner")
    reserved = _reserve(connection, fixture, key="reserve-prepare-cross-owner")
    other_candidate_id = uuid4()
    connection.execute(
        sa.text("INSERT INTO careerops.candidates (id, display_name) VALUES (:id, 'Other Owner')"),
        {"id": other_candidate_id},
    )
    other_account_id = cast(
        UUID,
        _register_send_account(
            connection,
            owner_id=fixture.owner_id,
            candidate_id=other_candidate_id,
            subject="prepare-cross-owner@example.com",
            handle=f"vault://prepare-cross-owner/{uuid4().hex}",
            key=f"prepare-cross-owner-register-{uuid4()}",
        )["account_id"],
    )
    _insert_review_evidence(
        connection,
        fixture,
        owner_id=fixture.owner_id,
        account_id=other_account_id,
        idempotency_key=f"prepare-cross-owner-evidence-{uuid4()}",
    )
    claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))

    prepared = _prepare(connection, claim)

    assert prepared["prepare_state"] == "ready"
    assert prepared["account_id"] == fixture.account_id


def test_expired_draft_and_expired_review_authorization_are_rejected(
    connection: Connection,
) -> None:
    owner_id, candidate_id = _seed_owner_candidate_pair(connection, "expired-draft")
    campaign_id = _seed_campaign(connection, owner_id=owner_id)
    account_id = cast(
        UUID,
        _register_send_account(
            connection,
            owner_id=owner_id,
            candidate_id=candidate_id,
            subject="expired-draft@example.com",
            handle="vault://expired-draft/send",
            key="expired-draft-register",
        )["account_id"],
    )

    assert _raises_db_error(
        connection,
        _create_draft_sql(),
        _create_draft_params(
            id_prefix="expired-draft",
            owner_id=owner_id,
            candidate_id=candidate_id,
            expires_at=NOW - timedelta(minutes=1),
        ),
    )

    fixture = _seed_send_fixture(
        connection,
        owner_id=owner_id,
        candidate_id=candidate_id,
        campaign_id=campaign_id,
        account_id=account_id,
        id_prefix="expired-review",
        review=False,
    )
    assert _raises_db_error(
        connection,
        _review_draft_sql(),
        _review_params(fixture, key="review-expired")
        | {"authorization_expires_at": NOW - timedelta(minutes=1)},
    )


def test_short_lived_authorization_and_release_expiries_are_not_silently_extended(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="exact-expiry", review=False)
    authorization_expires_at = NOW + timedelta(minutes=20)
    release_expires_at = NOW + timedelta(minutes=15)
    reviewed = (
        connection.execute(
            sa.text(_review_draft_sql()),
            _review_params(fixture, key="review-short-lived")
            | {"authorization_expires_at": authorization_expires_at},
        )
        .mappings()
        .one()
    )
    fixture = replace(fixture, authorization_id=reviewed["authorization_id"])
    release_qualification_id, release_evidence_hash, derived_release_expires_at = (
        _seed_gmail_release_qualification(
            connection,
            owner_id=fixture.owner_id,
            account_id=fixture.account_id,
            id_prefix="exact-expiry",
            expires_at=release_expires_at,
        )
    )
    fixture = replace(
        fixture,
        release_qualification_id=release_qualification_id,
        release_evidence_hash=release_evidence_hash,
        release_evidence_expires_at=derived_release_expires_at,
    )
    _reserve(
        connection,
        fixture,
        key="reserve-short-lived",
    )

    persisted = (
        connection.execute(
            sa.text(
                """
                SELECT authz.expires_at AS authorization_expires_at,
                       reservation.release_evidence_expires_at
                FROM careerops.autopilot_intent_authorizations AS authz
                JOIN careerops.autopilot_cap_reservations AS reservation
                  ON reservation.authorization_id = authz.id
                WHERE authz.id = :authorization_id
                """
            ),
            {"authorization_id": fixture.authorization_id},
        )
        .mappings()
        .one()
    )
    assert persisted["authorization_expires_at"] == authorization_expires_at
    assert persisted["release_evidence_expires_at"] == release_expires_at


def test_final_daily_and_per_recipient_slot_allows_only_one_concurrent_reservation(
    engine: Engine,
) -> None:
    with engine.begin() as connection:
        owner_id, candidate_id = _seed_owner_candidate_pair(connection, "race")
        campaign_id = _seed_campaign(connection, owner_id=owner_id)
        account_id = cast(
            UUID,
            _register_send_account(
                connection,
                owner_id=owner_id,
                candidate_id=candidate_id,
                subject="race-sender@example.com",
                handle="vault://gmail-send-race",
                key="send-register-race",
            )["account_id"],
        )
        unreviewed = [
            _seed_send_fixture(
                connection,
                owner_id=owner_id,
                candidate_id=candidate_id,
                campaign_id=campaign_id,
                grant_id=uuid4(),
                account_id=account_id,
                recipient_email="race-recipient@example.com",
                id_prefix=f"race-{index}",
                review=False,
            )
            for index in range(2)
        ]
        allowed_materials = tuple(
            dict.fromkeys(
                material
                for fixture in unreviewed
                for material in _grant_materials_for_payload_version(
                    connection, fixture.payload_version_id
                )
            )
        )
        grant_id = _seed_grant(
            connection,
            campaign_id=campaign_id,
            allowed_materials=allowed_materials,
            max_total=1,
            max_daily=1,
            max_per_company=1,
        )
        fixtures = [
            _review_send_fixture(
                connection,
                replace(fixture, grant_id=grant_id),
                key=f"review-{fixture.id_prefix}-{uuid4()}",
            )
            for fixture in unreviewed
        ]
    barrier = Barrier(len(fixtures))

    def reserve_after_barrier(fixture: SendFixture) -> tuple[str, str | None]:
        with engine.begin() as thread_connection:
            barrier.wait(timeout=10)
            try:
                _reserve(thread_connection, fixture, key=f"reserve-{fixture.id_prefix}")
            except DBAPIError as error:
                return ("dbapi_error", _sqlstate(error))
            return ("reserved", None)

    with ThreadPoolExecutor(max_workers=len(fixtures)) as executor:
        futures = [executor.submit(reserve_after_barrier, fixture) for fixture in fixtures]
        results = [future.result(timeout=20) for future in as_completed(futures, timeout=20)]

    assert sorted(result[0] for result in results) == ["dbapi_error", "reserved"]
    assert [result[1] for result in results if result[0] == "dbapi_error"] == ["23514"]


def test_kill_switches_and_revoked_grant_block_before_reservation(
    connection: Connection,
) -> None:
    global_fixture = _seed_send_fixture(connection, id_prefix="global-kill")
    _set_kill_switch(connection, scope_type="global", active=True)
    assert _raises_db_error(
        connection, _reserve_sql(), _reserve_params(global_fixture, key="global-kill")
    )
    _set_kill_switch(connection, scope_type="global", active=False)

    campaign_fixture = _seed_send_fixture(connection, id_prefix="campaign-kill")
    _set_kill_switch(
        connection,
        scope_type="campaign",
        active=True,
        campaign_id=campaign_fixture.campaign_id,
    )
    assert _raises_db_error(
        connection, _reserve_sql(), _reserve_params(campaign_fixture, key="campaign-kill")
    )
    _set_kill_switch(
        connection,
        scope_type="campaign",
        active=False,
        campaign_id=campaign_fixture.campaign_id,
    )

    provider_fixture = _seed_send_fixture(connection, id_prefix="provider-kill")
    _set_kill_switch(connection, scope_type="provider", active=True, provider="gmail")
    assert _raises_db_error(
        connection, _reserve_sql(), _reserve_params(provider_fixture, key="provider-kill")
    )
    _set_kill_switch(connection, scope_type="provider", active=False, provider="gmail")

    revoked_fixture = _seed_send_fixture(connection, id_prefix="revoked-grant")
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_grant_revocations (
                id, grant_version_id, revoked_by_user_id, reason, created_at
            )
            VALUES (:id, :grant_id, :owner_id, 'operator revoked', :now)
            """
        ),
        {
            "id": uuid4(),
            "grant_id": revoked_fixture.grant_id,
            "owner_id": revoked_fixture.owner_id,
            "now": NOW,
        },
    )
    assert _raises_db_error(
        connection, _reserve_sql(), _reserve_params(revoked_fixture, key="revoked-grant")
    )


def test_prepare_then_ambiguous_blocks_second_send_attempt(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection)
    reserved = _reserve(connection, fixture, key="reserve-ambiguous")
    claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))
    prepared = _prepare(connection, claim)

    assert prepared["prepare_state"] == "ready"
    connection.execute(
        sa.text(
            """
            SELECT careerops.gmail_send_record_ambiguity(
                :event_id, :lease_owner, :lease_token, 'GMAIL_SEND_PROVIDER_AMBIGUOUS'
            )
            """
        ),
        claim,
    )
    failed_event = (
        connection.execute(
            sa.text(
                """
            SELECT status, last_error_code, lease_token
            FROM careerops.outbox_events
            WHERE id = :event_id
            """
            ),
            claim,
        )
        .mappings()
        .one()
    )
    assert failed_event == {
        "status": "failed",
        "last_error_code": "GMAIL_SEND_PROVIDER_AMBIGUOUS",
        "lease_token": None,
    }
    assert _raises_db_error(
        connection,
        "SELECT * FROM careerops.gmail_send_prepare_outbox_event(:event_id, :lease_owner, :lease_token)",
        claim,
    )


def test_claim_reconciliation_job_uses_event_reserve_receipt_binding(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="claim-reconciliation-binding")
    reserved = _reserve(connection, fixture, key="reserve-claim-reconciliation-binding")
    bait_reservation = _insert_bait_reservation_for_same_payload(
        connection,
        fixture,
        reservation_key=f"bait-reservation-{uuid4().hex}",
        reconciliation_key=f"bait/reconcile/{uuid4().hex}",
    )
    other_candidate_id = uuid4()
    connection.execute(
        sa.text("INSERT INTO careerops.candidates (id, display_name) VALUES (:id, 'Bait Account')"),
        {"id": other_candidate_id},
    )
    bait_account_id = cast(
        UUID,
        _register_send_account(
            connection,
            owner_id=fixture.owner_id,
            candidate_id=other_candidate_id,
            subject="claim-reconciliation-bait@example.com",
            handle=f"vault://claim-reconciliation-bait/{uuid4().hex}",
            key=f"claim-reconciliation-bait-register-{uuid4()}",
        )["account_id"],
    )
    _insert_review_evidence(
        connection,
        fixture,
        owner_id=fixture.owner_id,
        account_id=bait_account_id,
        idempotency_key=f"claim-reconciliation-bait-evidence-{uuid4()}",
    )
    claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))

    prepared = _prepare(connection, claim)

    assert prepared["prepare_state"] == "ready"
    assert prepared["reservation_key"] == fixture.reservation_key
    assert prepared["reconciliation_key"] == fixture.reconciliation_key
    assert prepared["account_id"] == fixture.account_id
    connection.execute(
        sa.text(
            """
            SELECT careerops.record_gmail_send_outbox_ambiguity(
                :event_id, :lease_owner, :lease_token, 'GMAIL_SEND_PROVIDER_AMBIGUOUS'
            )
            """
        ),
        claim,
    )

    reconciled_claim = (
        connection.execute(
            sa.text(
                """
                SELECT *
                FROM careerops.claim_gmail_send_reconciliation_jobs(
                    'gmail-reconciler-test', 60, 10
                )
                """
            )
        )
        .mappings()
        .all()
    )

    assert len(reconciled_claim) == 1
    assert reconciled_claim[0]["event_id"] == reserved["outbox_event_id"]
    assert reconciled_claim[0]["reservation_key"] == fixture.reservation_key
    assert reconciled_claim[0]["reconciliation_key"] == fixture.reconciliation_key
    assert reconciled_claim[0]["send_account_id"] == fixture.account_id
    assert reconciled_claim[0]["reservation_key"] != bait_reservation["reservation_key"]
    assert reconciled_claim[0]["reconciliation_key"] != bait_reservation["reconciliation_key"]
    assert reconciled_claim[0]["send_account_id"] != bait_account_id


def test_reconciliation_readonly_scope_must_be_exact_before_prepare_or_claim(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="claim-reconciliation-wide-readonly")
    reserved = _reserve(connection, fixture, key="reserve-claim-reconciliation-wide-readonly")
    _set_reconciliation_readonly_scopes(
        connection,
        send_account_id=fixture.account_id,
        granted_scopes=(GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE),
    )
    claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))

    prepared = _prepare(connection, claim)

    assert prepared["prepare_state"] == "stopped"
    assert prepared["reason_code"] == "GMAIL_SEND_READONLY_CREDENTIAL_NOT_ACTIVE"
    assert prepared["credential_reference_id"] is None
    assert (
        _count_where(
            connection,
            "careerops.side_effect_attempts",
            "outbox_event_id = :outbox_event_id",
            {"outbox_event_id": reserved["outbox_event_id"]},
        )
        == 0
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.gmail_send_reconciliation_jobs (
                outbox_event_id, status, available_at, last_error_code
            )
            VALUES (
                :event_id, 'queued', CURRENT_TIMESTAMP,
                'GMAIL_SEND_PROVIDER_AMBIGUOUS'
            )
            """
        ),
        {"event_id": reserved["outbox_event_id"]},
    )

    reconciled_claim = (
        connection.execute(
            sa.text(
                """
                SELECT *
                FROM careerops.claim_gmail_send_reconciliation_jobs(
                    'gmail-reconciler-wide-scope-test', 60, 10
                )
                """
            )
        )
        .mappings()
        .all()
    )

    assert reconciled_claim == []


def test_record_receipt_and_reconcile_confirm_sent_message_metadata(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection)
    reserved = _reserve(connection, fixture, key="reserve-receipt")
    claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))
    _prepare(connection, claim)
    receipt_id = uuid4()

    connection.execute(
        sa.text(
            """
            SELECT careerops.gmail_send_record_receipt(
                :event_id, :lease_owner, :lease_token, 'gmail-msg-1',
                :reconciliation_key, :provider_timestamp, :receipt_id
            )
            """
        ),
        claim
        | {
            "reconciliation_key": fixture.reconciliation_key,
            "provider_timestamp": NOW,
            "receipt_id": receipt_id,
        },
    )
    receipt = (
        connection.execute(
            sa.text(
                """
            SELECT receipt.provider, receipt.provider_resource_id, receipt.reconciliation_key,
                   receipt.final_state, event.status AS event_status, intent.status AS intent_status
            FROM careerops.provider_receipts AS receipt
            JOIN careerops.side_effect_attempts AS attempt ON attempt.id = receipt.side_effect_attempt_id
            JOIN careerops.outbox_events AS event ON event.id = attempt.outbox_event_id
            JOIN careerops.action_intents AS intent ON intent.id = attempt.action_intent_id
            WHERE receipt.id = :receipt_id
            """
            ),
            {"receipt_id": receipt_id},
        )
        .mappings()
        .one()
    )
    assert receipt == {
        "provider": "gmail",
        "provider_resource_id": "gmail-msg-1",
        "reconciliation_key": fixture.reconciliation_key,
        "final_state": "confirmed",
        "event_status": "leased",
        "intent_status": "confirmed",
    }
    connection.execute(
        sa.text(
            "SELECT careerops.mark_gmail_send_outbox_published(:event_id, :lease_owner, :lease_token)"
        ),
        claim,
    )
    assert (
        connection.scalar(
            sa.text("SELECT status FROM careerops.outbox_events WHERE id = :event_id"),
            claim,
        )
        == "published"
    )
    reconciled = (
        connection.execute(
            sa.text(
                """
            SELECT *
            FROM careerops.gmail_send_reconcile_receipt(
                :owner_id, :account_id, :reconciliation_key, 'gmail-msg-1', :receipt_id,
                'trace-reconcile'
            )
            """
            ),
            {
                "owner_id": fixture.owner_id,
                "account_id": fixture.account_id,
                "reconciliation_key": fixture.reconciliation_key,
                "receipt_id": receipt_id,
            },
        )
        .mappings()
        .one()
    )
    assert reconciled == {
        "reconciliation_key": fixture.reconciliation_key,
        "provider_message_id": "gmail-msg-1",
        "final_state": "confirmed",
        "receipt_state": "found",
    }


def test_reconcile_outbox_event_replay_must_match_sent_metadata(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="reconcile-outbox-replay")
    reserved = _reserve(connection, fixture, key="reserve-reconcile-outbox-replay")
    claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))
    _prepare(connection, claim)
    connection.execute(
        sa.text(
            """
            UPDATE careerops.side_effect_attempts
            SET response_metadata = jsonb_build_object('prepared_marker', 'keep-me')
            WHERE outbox_event_id = :event_id
            """
        ),
        {"event_id": claim["event_id"]},
    )

    reconcile_sql = """
        SELECT careerops.reconcile_gmail_send_outbox_event(
            :event_id, :lease_owner, :lease_token, :provider_message_id,
            :provider_thread_id, :rfc_message_id, CAST(:sent_metadata AS jsonb),
            :reconciled_at
        )
    """
    params = claim | {
        "provider_message_id": "gmail-msg-replay-1",
        "provider_thread_id": "gmail-thread-replay-1",
        "rfc_message_id": "<rfc-replay-1@example.test>",
        "sent_metadata": _json({"labelIds": ["SENT"], "sizeEstimate": 2048}),
        "reconciled_at": NOW,
    }
    connection.execute(sa.text(reconcile_sql), params)
    connection.execute(sa.text(reconcile_sql), params)

    with pytest.raises(DBAPIError) as provider_error, connection.begin_nested():
        connection.execute(
            sa.text(reconcile_sql),
            params | {"provider_message_id": "gmail-msg-replay-changed"},
        )
    assert _sqlstate(provider_error.value) == "23505"
    with pytest.raises(DBAPIError) as thread_error, connection.begin_nested():
        connection.execute(
            sa.text(reconcile_sql),
            params | {"provider_thread_id": "gmail-thread-replay-changed"},
        )
    assert _sqlstate(thread_error.value) == "23505"
    with pytest.raises(DBAPIError) as rfc_error, connection.begin_nested():
        connection.execute(
            sa.text(reconcile_sql),
            params | {"rfc_message_id": "<rfc-replay-changed@example.test>"},
        )
    assert _sqlstate(rfc_error.value) == "23505"
    with pytest.raises(DBAPIError) as metadata_error, connection.begin_nested():
        connection.execute(
            sa.text(reconcile_sql),
            params | {"sent_metadata": _json({"labelIds": ["SENT"], "sizeEstimate": 4096})},
        )
    assert _sqlstate(metadata_error.value) == "23505"
    with pytest.raises(DBAPIError) as reconciled_at_error, connection.begin_nested():
        connection.execute(
            sa.text(reconcile_sql),
            params | {"reconciled_at": datetime(2026, 7, 20, 12, 1, tzinfo=UTC)},
        )
    assert _sqlstate(reconciled_at_error.value) == "23505"
    response_metadata = connection.scalar(
        sa.text(
            """
            SELECT response_metadata
            FROM careerops.side_effect_attempts
            WHERE outbox_event_id = :event_id
            """
        ),
        {"event_id": claim["event_id"]},
    )
    assert response_metadata["provider"] == "gmail"
    assert response_metadata["provider_message_id"] == "gmail-msg-replay-1"
    assert response_metadata["provider_thread_id"] == "gmail-thread-replay-1"
    assert response_metadata["rfc_message_id"] == "<rfc-replay-1@example.test>"
    assert response_metadata["labelIds"] == ["SENT"]
    assert response_metadata["sizeEstimate"] == 2048
    assert response_metadata["prepared_marker"] == "keep-me"
    assert len(response_metadata["gmail_reconcile_request_sha256"]) == 64


def test_concurrent_reconcile_outbox_event_exact_replay_preserves_one_metadata_binding(
    engine: Engine,
) -> None:
    _clear_engine_test_data(engine)
    release_first_commit = Event()
    try:
        with engine.begin() as connection:
            fixture = _seed_send_fixture(connection, id_prefix="reconcile-outbox-concurrent")
            reserved = _reserve(connection, fixture, key="reserve-reconcile-outbox-concurrent")
            claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))
            _prepare(connection, claim)

        reconcile_sql = """
            SELECT careerops.reconcile_gmail_send_outbox_event(
                :event_id, :lease_owner, :lease_token, :provider_message_id,
                :provider_thread_id, :rfc_message_id, CAST(:sent_metadata AS jsonb),
                :reconciled_at
            )
        """
        params = claim | {
            "provider_message_id": "gmail-msg-concurrent-replay",
            "provider_thread_id": "gmail-thread-concurrent-replay",
            "rfc_message_id": "<rfc-concurrent-replay@example.test>",
            "sent_metadata": _json({"labelIds": ["SENT"], "sizeEstimate": 2048}),
            "reconciled_at": NOW,
        }
        first_call_completed = Event()
        second_call_started = Event()
        first_backend_pid: list[int] = []
        second_backend_pid: list[int] = []

        def reconcile_and_hold_commit() -> None:
            with engine.begin() as thread_connection:
                backend_pid = thread_connection.scalar(sa.text("SELECT pg_backend_pid()"))
                assert isinstance(backend_pid, int)
                first_backend_pid.append(backend_pid)
                thread_connection.execute(sa.text(reconcile_sql), params).one()
                first_call_completed.set()
                assert release_first_commit.wait(timeout=30)

        def reconcile_while_first_is_uncommitted() -> None:
            with engine.begin() as thread_connection:
                backend_pid = thread_connection.scalar(sa.text("SELECT pg_backend_pid()"))
                assert isinstance(backend_pid, int)
                second_backend_pid.append(backend_pid)
                second_call_started.set()
                thread_connection.execute(sa.text(reconcile_sql), params).one()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(reconcile_and_hold_commit)
            try:
                assert first_call_completed.wait(timeout=10)
                second_future = executor.submit(reconcile_while_first_is_uncommitted)
                assert second_call_started.wait(timeout=10)
                _assert_backend_is_blocked_by(
                    engine,
                    blocked_backend_pid=second_backend_pid[0],
                    blocker_backend_pid=first_backend_pid[0],
                )
            finally:
                release_first_commit.set()
            first_future.result(timeout=20)
            second_future.result(timeout=20)

        with engine.connect() as connection:
            attempt_count = connection.scalar(
                sa.text(
                    """
                    SELECT count(*)
                    FROM careerops.side_effect_attempts
                    WHERE outbox_event_id = :event_id
                    """
                ),
                {"event_id": claim["event_id"]},
            )
            assert attempt_count == 1
            response_metadata = connection.scalar(
                sa.text(
                    """
                    SELECT response_metadata
                    FROM careerops.side_effect_attempts
                    WHERE outbox_event_id = :event_id
                    """
                ),
                {"event_id": claim["event_id"]},
            )
        assert response_metadata["provider_message_id"] == "gmail-msg-concurrent-replay"
        assert response_metadata["provider_thread_id"] == "gmail-thread-concurrent-replay"
        assert response_metadata["rfc_message_id"] == "<rfc-concurrent-replay@example.test>"
        assert response_metadata["labelIds"] == ["SENT"]
        assert response_metadata["sizeEstimate"] == 2048
        assert len(response_metadata["gmail_reconcile_request_sha256"]) == 64
    finally:
        release_first_commit.set()
        _clear_engine_test_data(engine)


def test_reconcile_outbox_event_rejects_unmarked_legacy_metadata(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="reconcile-outbox-legacy")
    reserved = _reserve(connection, fixture, key="reserve-reconcile-outbox-legacy")
    claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))
    _prepare(connection, claim)
    legacy_metadata = {
        "provider": "gmail",
        "provider_message_id": "gmail-msg-legacy",
        "provider_thread_id": "gmail-thread-legacy",
        "rfc_message_id": "<rfc-legacy@example.test>",
        "reconciled_at": str(NOW),
        "prepared_marker": "keep-me",
    }
    connection.execute(
        sa.text(
            """
            UPDATE careerops.side_effect_attempts
            SET response_metadata = CAST(:metadata AS jsonb)
            WHERE outbox_event_id = :event_id
            """
        ),
        {"event_id": claim["event_id"], "metadata": _json(legacy_metadata)},
    )

    with pytest.raises(DBAPIError) as replay_error, connection.begin_nested():
        connection.execute(
            sa.text(
                """
                SELECT careerops.reconcile_gmail_send_outbox_event(
                    :event_id, :lease_owner, :lease_token, 'gmail-msg-changed',
                    'gmail-thread-changed', '<rfc-changed@example.test>',
                    CAST(:sent_metadata AS jsonb), :reconciled_at
                )
                """
            ),
            claim
            | {
                "sent_metadata": _json({"labelIds": ["SENT"], "sizeEstimate": 4096}),
                "reconciled_at": NOW,
            },
        )
    assert _sqlstate(replay_error.value) == "23505"
    assert (
        connection.scalar(
            sa.text(
                """
                SELECT response_metadata
                FROM careerops.side_effect_attempts
                WHERE outbox_event_id = :event_id
                """
            ),
            {"event_id": claim["event_id"]},
        )
        == legacy_metadata
    )


def test_legacy_exact_payload_draft_review_replay_must_match_review_binding(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection, id_prefix="exact-payload-review-replay")
    params = {
        "owner_id": fixture.owner_id,
        "account_id": fixture.account_id,
        "reviewed_intent_id": fixture.action_intent_id,
        "application_id": uuid4(),
        "recipient_email": "legacy-review-replay@example.com",
        "recipient_snapshot_sha256": fixture.recipient_sha256,
        "subject_sha256": fixture.subject_sha256,
        "body_sha256": fixture.body_sha256,
        "payload_sha256": fixture.payload_hash,
        "attachment_manifest_sha256": _sha("exact-payload-review-replay-attachments"),
        "requested_for": "gmail_send_exact_payload",
        "create_key": "exact-payload-review-replay-create",
        "review_key": "exact-payload-review-replay-review",
        "trace_id": "trace-exact-payload-review-replay",
        "reason": "approved legacy exact payload draft",
    }
    draft_id = connection.scalar(
        sa.text(
            """
            SELECT careerops.gmail_send_create_exact_payload_draft(
                :owner_id, :account_id, :reviewed_intent_id, :application_id,
                :recipient_email, :recipient_snapshot_sha256, :subject_sha256,
                :body_sha256, :payload_sha256, :attachment_manifest_sha256,
                :requested_for, :create_key, :trace_id
            )
            """
        ),
        params,
    )
    assert isinstance(draft_id, UUID)
    params["draft_id"] = draft_id

    review_sql = """
        SELECT careerops.gmail_send_review_exact_payload_draft(
            :owner_id, :account_id, :draft_id, :decision, :requested_for,
            :recipient_snapshot_sha256, :subject_sha256, :body_sha256,
            :payload_sha256, :attachment_manifest_sha256, :reason,
            :review_key, :trace_id
        )
    """
    approved = connection.scalar(sa.text(review_sql), params | {"decision": "approved"})
    assert approved == draft_id
    assert connection.scalar(sa.text(review_sql), params | {"decision": "approved"}) == draft_id
    with pytest.raises(DBAPIError) as reason_error, connection.begin_nested():
        connection.execute(
            sa.text(review_sql),
            params | {"decision": "approved", "reason": "changed legacy review reason"},
        )
    assert _sqlstate(reason_error.value) == "23505"
    with pytest.raises(DBAPIError) as decision_error, connection.begin_nested():
        connection.execute(sa.text(review_sql), params | {"decision": "rejected"})
    assert _sqlstate(decision_error.value) == "23505"
    with pytest.raises(DBAPIError) as requested_for_error, connection.begin_nested():
        connection.execute(
            sa.text(review_sql),
            params | {"decision": "approved", "requested_for": "changed-request"},
        )
    assert _sqlstate(requested_for_error.value) == "23505"
    with pytest.raises(DBAPIError) as binding_error, connection.begin_nested():
        connection.execute(
            sa.text(review_sql),
            params | {"decision": "approved", "subject_sha256": _sha("changed subject")},
        )
    assert _sqlstate(binding_error.value) == "23505"
    with pytest.raises(DBAPIError) as account_error, connection.begin_nested():
        connection.execute(
            sa.text(review_sql),
            params | {"decision": "approved", "account_id": uuid4()},
        )
    assert _sqlstate(account_error.value) == "23505"
    other_draft_id = connection.scalar(
        sa.text(
            """
            SELECT careerops.gmail_send_create_exact_payload_draft(
                :owner_id, :account_id, :reviewed_intent_id, :other_application_id,
                :recipient_email, :recipient_snapshot_sha256, :subject_sha256,
                :body_sha256, :payload_sha256, :attachment_manifest_sha256,
                :requested_for, :other_create_key, :trace_id
            )
            """
        ),
        params
        | {
            "other_application_id": uuid4(),
            "other_create_key": "exact-payload-review-replay-create-other",
        },
    )
    assert isinstance(other_draft_id, UUID)
    with pytest.raises(DBAPIError) as draft_error, connection.begin_nested():
        connection.execute(
            sa.text(review_sql),
            params | {"decision": "approved", "draft_id": other_draft_id},
        )
    assert _sqlstate(draft_error.value) == "23505"


def test_concurrent_legacy_exact_payload_draft_review_exact_replay_returns_one_draft(
    engine: Engine,
) -> None:
    _clear_engine_test_data(engine)
    release_first_commit = Event()
    try:
        with engine.begin() as connection:
            fixture = _seed_send_fixture(connection, id_prefix="exact-payload-review-concurrent")
            params = {
                "owner_id": fixture.owner_id,
                "account_id": fixture.account_id,
                "reviewed_intent_id": fixture.action_intent_id,
                "application_id": uuid4(),
                "recipient_email": "legacy-review-concurrent@example.com",
                "recipient_snapshot_sha256": fixture.recipient_sha256,
                "subject_sha256": fixture.subject_sha256,
                "body_sha256": fixture.body_sha256,
                "payload_sha256": fixture.payload_hash,
                "attachment_manifest_sha256": _sha("exact-payload-review-concurrent-attachments"),
                "requested_for": "gmail_send_exact_payload",
                "create_key": "exact-payload-review-concurrent-create",
                "review_key": f"exact-payload-review-concurrent-review-{uuid4()}",
                "trace_id": "trace-exact-payload-review-concurrent",
                "reason": "approved legacy exact payload draft concurrently",
            }
            draft_id = connection.scalar(
                sa.text(
                    """
                    SELECT careerops.gmail_send_create_exact_payload_draft(
                        :owner_id, :account_id, :reviewed_intent_id, :application_id,
                        :recipient_email, :recipient_snapshot_sha256, :subject_sha256,
                        :body_sha256, :payload_sha256, :attachment_manifest_sha256,
                        :requested_for, :create_key, :trace_id
                    )
                    """
                ),
                params,
            )
            assert isinstance(draft_id, UUID)
            params["draft_id"] = draft_id

        review_sql = """
            SELECT careerops.gmail_send_review_exact_payload_draft(
                :owner_id, :account_id, :draft_id, 'approved', :requested_for,
                :recipient_snapshot_sha256, :subject_sha256, :body_sha256,
                :payload_sha256, :attachment_manifest_sha256, :reason,
                :review_key, :trace_id
            )
        """
        first_call_completed = Event()
        second_call_started = Event()
        first_backend_pid: list[int] = []
        second_backend_pid: list[int] = []

        def review_and_hold_commit() -> UUID | None:
            with engine.begin() as thread_connection:
                backend_pid = thread_connection.scalar(sa.text("SELECT pg_backend_pid()"))
                assert isinstance(backend_pid, int)
                first_backend_pid.append(backend_pid)
                reviewed_draft_id = thread_connection.scalar(
                    sa.text(review_sql),
                    params | {"trace_id": "trace-exact-payload-review-concurrent-first"},
                )
                first_call_completed.set()
                assert release_first_commit.wait(timeout=30)
                return reviewed_draft_id

        def review_while_first_is_uncommitted() -> UUID | None:
            with engine.begin() as thread_connection:
                backend_pid = thread_connection.scalar(sa.text("SELECT pg_backend_pid()"))
                assert isinstance(backend_pid, int)
                second_backend_pid.append(backend_pid)
                second_call_started.set()
                return thread_connection.scalar(
                    sa.text(review_sql),
                    params | {"trace_id": "trace-exact-payload-review-concurrent-second"},
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(review_and_hold_commit)
            try:
                assert first_call_completed.wait(timeout=10)
                second_future = executor.submit(review_while_first_is_uncommitted)
                assert second_call_started.wait(timeout=10)
                _assert_backend_is_blocked_by(
                    engine,
                    blocked_backend_pid=second_backend_pid[0],
                    blocker_backend_pid=first_backend_pid[0],
                )
            finally:
                release_first_commit.set()
            first_reviewed_draft_id = first_future.result(timeout=20)
            second_reviewed_draft_id = second_future.result(timeout=20)

        assert isinstance(first_reviewed_draft_id, UUID)
        assert isinstance(second_reviewed_draft_id, UUID)
        with engine.connect() as connection:
            draft = (
                connection.execute(
                    sa.text(
                        """
                        SELECT id, status, review_idempotency_key
                        FROM careerops.gmail_send_drafts
                        WHERE id = :draft_id
                        """
                    ),
                    {"draft_id": draft_id},
                )
                .mappings()
                .one()
            )
        assert first_reviewed_draft_id == draft["id"]
        assert second_reviewed_draft_id == draft["id"]
        assert draft["status"] == "approved"
        assert draft["review_idempotency_key"] == params["review_key"]
    finally:
        release_first_commit.set()
        _clear_engine_test_data(engine)


def test_api_role_can_execute_only_current_gmail_send_control_plane_surface(
    connection: Connection,
) -> None:
    connection.execute(sa.text("SET LOCAL ROLE careerops_api"))

    allowed_signatures = (
        "careerops.gmail_send_create_draft(uuid, uuid, uuid, jsonb, jsonb, jsonb, text, text, text, text, text, timestamp with time zone)",
        "careerops.gmail_send_review_draft(uuid, uuid, uuid, uuid, uuid, uuid, text, text, uuid, uuid, text, text, timestamp with time zone, text, text)",
        "careerops.gmail_send_register_account(uuid, uuid, text, text, text, text, text, integer, uuid, text, text)",
        "careerops.gmail_send_reserve_and_enqueue(uuid, uuid, uuid, uuid, uuid, uuid, uuid, text, text, uuid, text, text, uuid, uuid, text, text, text, text, text, text, text)",
        "careerops.gmail_send_status(uuid, uuid)",
        "careerops.gmail_send_list_accounts(uuid, integer)",
    )
    denied_signatures = (
        "careerops.gmail_send_register_account(uuid, uuid, text, text, text, text, text, text, text, integer)",
        "careerops.gmail_send_registration_receipt(uuid, text)",
        "careerops.gmail_send_account_status(uuid, uuid)",
        "careerops.gmail_send_create_exact_payload_draft(uuid, uuid, uuid, uuid, text, text, text, text, text, text, text, text, text)",
        "careerops.gmail_send_review_exact_payload_draft(uuid, uuid, uuid, text, text, text, text, text, text, text, text, text, text)",
        "careerops.gmail_send_list_drafts(uuid, uuid, integer)",
        "careerops.gmail_send_reserve_reviewed_intent(uuid, uuid, uuid, uuid, text, text, text, text, text, text, text, text, text)",
        "careerops.gmail_send_list_reservations(uuid, uuid, integer)",
    )

    assert all(_has_execute_privilege(connection, signature) for signature in allowed_signatures)
    assert not any(_has_execute_privilege(connection, signature) for signature in denied_signatures)
    connection.execute(sa.text("RESET ROLE"))


def test_mail_sender_role_is_limited_to_narrow_send_functions(
    connection: Connection,
) -> None:
    fixture = _seed_send_fixture(connection)
    reserved = _reserve(connection, fixture, key="reserve-role")
    claim = _lease_outbox_event(connection, cast(UUID, reserved["outbox_event_id"]))

    with pytest.raises(DBAPIError) as readonly_error, connection.begin_nested():
        connection.execute(sa.text("SET LOCAL ROLE careerops_mail_sender"))
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.gmail_readonly_claim_sync_runs('gmail-send-test', 300, 1)"
            )
        )
    assert _sqlstate(readonly_error.value) == "42501"
    connection.execute(sa.text("RESET ROLE"))

    with pytest.raises(DBAPIError) as direct_write_error, connection.begin_nested():
        connection.execute(sa.text("SET LOCAL ROLE careerops_mail_sender"))
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.provider_receipts (
                    id, side_effect_attempt_id, provider, provider_resource_id,
                    reconciliation_key, final_state, provider_timestamp
                )
                VALUES (:id, :attempt_id, 'gmail', 'forged', 'forged', 'confirmed', :now)
                """
            ),
            {"id": uuid4(), "attempt_id": uuid4(), "now": NOW},
        )
    assert _sqlstate(direct_write_error.value) == "42501"
    connection.execute(sa.text("RESET ROLE"))

    connection.execute(sa.text("SET LOCAL ROLE careerops_mail_sender"))
    prepared = (
        connection.execute(
            sa.text(
                """
            SELECT *
            FROM careerops.prepare_gmail_send_outbox_event(:event_id, :lease_owner, :lease_token)
            """
            ),
            claim,
        )
        .mappings()
        .one()
    )
    connection.execute(sa.text("RESET ROLE"))
    assert prepared["prepare_state"] == "ready"


def test_mail_sender_role_cannot_execute_gmail_send_control_plane_functions(
    connection: Connection,
) -> None:
    connection.execute(sa.text("SET LOCAL ROLE careerops_mail_sender"))

    denied_signatures = (
        "careerops.gmail_send_create_draft(uuid, uuid, uuid, jsonb, jsonb, jsonb, text, text, text, text, text, timestamp with time zone)",
        "careerops.gmail_send_review_draft(uuid, uuid, uuid, uuid, uuid, uuid, text, text, uuid, uuid, text, text, timestamp with time zone, text, text)",
        "careerops.gmail_send_register_account(uuid, uuid, text, text, text, text, text, text, text, integer)",
        "careerops.gmail_send_register_account(uuid, uuid, text, text, text, text, text, integer, uuid, text, text)",
        "careerops.gmail_send_reserve_and_enqueue(uuid, uuid, uuid, uuid, uuid, uuid, uuid, text, text, uuid, text, text, uuid, uuid, text, text, text, text, text, text, text)",
        "careerops.gmail_send_create_exact_payload_draft(uuid, uuid, uuid, uuid, text, text, text, text, text, text, text, text, text)",
        "careerops.gmail_send_review_exact_payload_draft(uuid, uuid, uuid, text, text, text, text, text, text, text, text, text, text)",
        "careerops.gmail_send_reserve_reviewed_intent(uuid, uuid, uuid, uuid, text, text, text, text, text, text, text, text, text)",
        "careerops.gmail_send_prepare_outbox_event(uuid, text, uuid)",
        "careerops.gmail_send_record_receipt(uuid, text, uuid, text, text, timestamp with time zone, uuid)",
        "careerops.gmail_send_record_ambiguity(uuid, text, uuid, text)",
        "careerops.gmail_send_reconcile_receipt(uuid, uuid, text, text, uuid, text)",
    )

    assert not any(_has_execute_privilege(connection, signature) for signature in denied_signatures)
    connection.execute(sa.text("RESET ROLE"))


@dataclass(frozen=True, slots=True)
class SendFixture:
    id_prefix: str
    owner_id: UUID
    candidate_id: UUID
    account_id: UUID
    campaign_id: UUID
    grant_id: UUID
    authorization_id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    approval_request_id: UUID
    payload_hash: str
    recipient_sha256: str
    subject_sha256: str
    body_sha256: str
    review_evidence_sha256: str
    review_snapshot_sha256: str
    release_qualification_id: UUID
    release_evidence_hash: str
    release_evidence_expires_at: datetime
    reservation_key: str
    reconciliation_key: str


def _seed_send_fixture(
    connection: Connection,
    *,
    owner_id: UUID | None = None,
    candidate_id: UUID | None = None,
    campaign_id: UUID | None = None,
    grant_id: UUID | None = None,
    account_id: UUID | None = None,
    recipient_email: str | None = None,
    id_prefix: str = "gmail-send",
    grant_recipient_only: bool = False,
    review: bool = True,
) -> SendFixture:
    if owner_id is None or candidate_id is None:
        owner_id, candidate_id = _seed_owner_candidate_pair(connection, id_prefix)
    if campaign_id is None:
        campaign_id = _seed_campaign(connection, owner_id=owner_id)
    recipient = recipient_email or f"recruiter-{id_prefix}@example.com"
    recipient_hash = _sha(recipient.lower().strip())
    subject = f"subject:{id_prefix}"
    subject_sha256 = _sha(subject)
    body_sha256 = _sha(f"body:{id_prefix}")
    attachment_hash = _sha(f"attachment:{id_prefix}")
    grant_material_hash = _sha(f"grant-material:{id_prefix}")
    if account_id is None:
        account_id = cast(
            UUID,
            _register_send_account(
                connection,
                owner_id=owner_id,
                candidate_id=candidate_id,
                subject=f"{id_prefix}-{uuid4().hex[:8]}@example.com",
                handle=f"vault://{id_prefix}/send/{uuid4().hex}",
                key=f"{id_prefix}-register-{uuid4()}",
            )["account_id"],
        )
    unreviewed = _create_unreviewed_draft(
        connection,
        id_prefix=id_prefix,
        owner_id=owner_id,
        candidate_id=candidate_id,
        account_id=account_id,
        campaign_id=campaign_id,
        grant_id=grant_id or uuid4(),
        recipient=recipient,
        recipient_hash=recipient_hash,
        subject=subject,
        subject_sha256=subject_sha256,
        body_sha256=body_sha256,
        attachment_hash=attachment_hash,
        grant_material_hash=grant_material_hash,
    )
    if grant_id is None:
        grant_id = _seed_grant(
            connection,
            campaign_id=campaign_id,
            allowed_materials=(recipient_hash,)
            if grant_recipient_only
            else _grant_materials_for_payload_version(connection, unreviewed.payload_version_id),
        )
    unreviewed = replace(unreviewed, grant_id=grant_id)
    if not review:
        return unreviewed
    return _review_send_fixture(
        connection,
        unreviewed,
        key=f"review-{id_prefix}-{uuid4()}",
    )


def _seed_unreviewed_draft_fixture(connection: Connection, *, id_prefix: str) -> SendFixture:
    owner_id, candidate_id = _seed_owner_candidate_pair(connection, id_prefix)
    campaign_id = _seed_campaign(connection, owner_id=owner_id)
    recipient = f"recruiter-{id_prefix}@example.com"
    recipient_hash = _sha(recipient.lower().strip())
    subject = f"subject:{id_prefix}"
    subject_sha256 = _sha(subject)
    body_sha256 = _sha(f"body:{id_prefix}")
    attachment_hash = _sha(f"attachment:{id_prefix}")
    grant_material_hash = _sha(f"grant-material:{id_prefix}")
    account_id = cast(
        UUID,
        _register_send_account(
            connection,
            owner_id=owner_id,
            candidate_id=candidate_id,
            subject=f"{id_prefix}-{uuid4().hex[:8]}@example.com",
            handle=f"vault://{id_prefix}/send/{uuid4().hex}",
            key=f"{id_prefix}-register-{uuid4()}",
        )["account_id"],
    )
    unreviewed = _create_unreviewed_draft(
        connection,
        id_prefix=id_prefix,
        owner_id=owner_id,
        candidate_id=candidate_id,
        account_id=account_id,
        campaign_id=campaign_id,
        grant_id=uuid4(),
        recipient=recipient,
        recipient_hash=recipient_hash,
        subject=subject,
        subject_sha256=subject_sha256,
        body_sha256=body_sha256,
        attachment_hash=attachment_hash,
        grant_material_hash=grant_material_hash,
    )
    grant_id = _seed_grant(
        connection,
        campaign_id=campaign_id,
        allowed_materials=_grant_materials_for_payload_version(
            connection, unreviewed.payload_version_id
        ),
    )
    return replace(unreviewed, grant_id=grant_id)


def _create_unreviewed_draft(
    connection: Connection,
    *,
    id_prefix: str,
    owner_id: UUID,
    candidate_id: UUID,
    account_id: UUID,
    campaign_id: UUID,
    grant_id: UUID,
    recipient: str,
    recipient_hash: str,
    subject: str,
    subject_sha256: str,
    body_sha256: str,
    attachment_hash: str,
    grant_material_hash: str,
) -> SendFixture:
    target = {
        "target_host": "gmail.googleapis.com",
        "channel": "gmail:send",
        "adapter_id": "gmail",
        "fixture_id": "gmail-send.v1",
        "recipient_sha256": recipient_hash,
        "body_sha256": body_sha256,
        "grant_material_hash": grant_material_hash,
    }
    payload = {
        "to": [recipient],
        "recipient": recipient,
        "recipient_sha256": recipient_hash,
        "subject": subject,
        "text_body": f"body:{id_prefix}",
        "subject_sha256": subject_sha256,
        "body_sha256": body_sha256,
    }
    attachment_refs = [
        {
            "object_key": f"resume/{id_prefix}.pdf",
            "filename": "resume.pdf",
            "content_type": "application/pdf",
            "size_bytes": 10,
            "sha256": attachment_hash,
        }
    ]
    draft = (
        connection.execute(
            sa.text(
                """
                SELECT *
                FROM careerops.gmail_send_create_draft(
                    :owner_id, :candidate_id, :candidate_id,
                    CAST(:target AS jsonb), CAST(:payload AS jsonb),
                    CAST(:attachment_refs AS jsonb), NULL, 'gmail-send-test.v1',
                    :idempotency_key, :trace_id, 'exact-review-required',
                    :expires_at
                )
                """
            ),
            {
                "owner_id": owner_id,
                "candidate_id": candidate_id,
                "target": _json(target),
                "payload": _json(payload),
                "attachment_refs": _json(attachment_refs),
                "idempotency_key": f"draft-{id_prefix}-{uuid4()}",
                "trace_id": f"trace-draft-{id_prefix}",
                "expires_at": NOW + timedelta(hours=1),
            },
        )
        .mappings()
        .one()
    )
    return SendFixture(
        id_prefix=id_prefix,
        owner_id=owner_id,
        candidate_id=candidate_id,
        account_id=account_id,
        campaign_id=campaign_id,
        grant_id=grant_id,
        authorization_id=uuid4(),
        action_intent_id=draft["action_intent_id"],
        payload_version_id=draft["payload_version_id"],
        approval_request_id=draft["approval_request_id"],
        payload_hash=draft["payload_hash"],
        recipient_sha256=recipient_hash,
        subject_sha256=subject_sha256,
        body_sha256=body_sha256,
        review_evidence_sha256=_sha(f"review-evidence:{id_prefix}"),
        review_snapshot_sha256=_sha(f"review-snapshot:{id_prefix}"),
        release_qualification_id=uuid4(),
        release_evidence_hash=_sha(f"release-evidence:{id_prefix}"),
        release_evidence_expires_at=NOW + timedelta(hours=1),
        reservation_key=f"reservation-{id_prefix}-{uuid4().hex}",
        reconciliation_key=f"reconcile/{id_prefix}/{uuid4().hex}",
    )


def _review_send_fixture(
    connection: Connection,
    fixture: SendFixture,
    *,
    key: str,
) -> SendFixture:
    reviewed = (
        connection.execute(
            sa.text(_review_draft_sql()),
            _review_params(fixture, key=key),
        )
        .mappings()
        .one()
    )
    assert reviewed["decision"] == "approved"
    assert reviewed["authorization_id"] == fixture.authorization_id
    release_qualification_id, release_evidence_hash, release_expires_at = (
        _seed_gmail_release_qualification(
            connection,
            owner_id=fixture.owner_id,
            account_id=fixture.account_id,
            id_prefix=fixture.id_prefix,
        )
    )
    return replace(
        fixture,
        release_qualification_id=release_qualification_id,
        release_evidence_hash=release_evidence_hash,
        release_evidence_expires_at=release_expires_at,
    )


def _grant_materials_for_payload_version(
    connection: Connection,
    payload_version_id: UUID,
) -> tuple[str, ...]:
    rows = connection.execute(
        sa.text(
            """
            SELECT material_hash
            FROM (
                SELECT payload.payload_hash AS material_hash, 1 AS material_order
                FROM careerops.action_payload_versions AS payload
                WHERE payload.id = :payload_version_id
                UNION ALL
                SELECT payload.target ->> 'body_sha256', 2
                FROM careerops.action_payload_versions AS payload
                WHERE payload.id = :payload_version_id
                UNION ALL
                SELECT payload.target ->> 'grant_material_hash', 3
                FROM careerops.action_payload_versions AS payload
                WHERE payload.id = :payload_version_id
                UNION ALL
                SELECT attachment.value ->> 'sha256', 4 + attachment.ordinality::integer
                FROM careerops.action_payload_versions AS payload
                CROSS JOIN LATERAL jsonb_array_elements(payload.attachment_refs)
                    WITH ORDINALITY AS attachment(value, ordinality)
                WHERE payload.id = :payload_version_id
            ) AS materials
            WHERE material_hash ~ '^[a-f0-9]{64}$'
            ORDER BY material_order
            """
        ),
        {"payload_version_id": payload_version_id},
    ).scalars()
    materials = tuple(dict.fromkeys(rows))
    assert len(materials) >= 3
    return materials


def _seed_gmail_release_qualification(
    connection: Connection,
    *,
    owner_id: UUID,
    account_id: UUID,
    id_prefix: str,
    expires_at: datetime | None = None,
    qualified: bool = True,
) -> tuple[UUID, str, datetime]:
    qualification_id = uuid4()
    evidence_id = uuid4()
    release_expires_at = expires_at or (NOW + timedelta(hours=1))
    release_evidence_hash = _sha(f"qualified-evidence:{id_prefix}")
    credential_ref_hash = connection.scalar(
        sa.text(
            """
            SELECT credential_store_evidence_sha256
            FROM careerops.gmail_send_accounts
            WHERE id = :account_id
              AND owner_user_id = :owner_id
            """
        ),
        {"account_id": account_id, "owner_id": owner_id},
    )
    assert isinstance(credential_ref_hash, str)
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.release_qualifications (
                id, capability, action_name, rollout_mode, provider, adapter_id,
                adapter_version, implementation_hash, config_hash, policy_hash,
                dataset_hash, git_commit, migration_revision, oauth_scope_hash,
                credential_ref_hash, network_policy_hash, reconcile_policy_hash,
                hard_stop_hash, sensitive_field_hash, kill_switch_hash,
                fixture_manifest_sha256, fault_manifest_sha256, status,
                requested_by_user_id, created_by, expires_at
            ) VALUES (
                :id, 'gmail_send', 'send_email', 'review_required',
                'gmail', 'gmail', 'gmail-send.v1',
                :implementation_hash, :config_hash, :policy_hash, :dataset_hash,
                :git_commit, '0013', :oauth_scope_hash, :credential_ref_hash,
                :network_policy_hash, :reconcile_policy_hash, :hard_stop_hash,
                :sensitive_field_hash, :kill_switch_hash, :fixture_manifest_sha256,
                :fault_manifest_sha256, 'draft', :owner_id, :created_by,
                :expires_at
            )
            """
        ),
        {
            "id": qualification_id,
            "implementation_hash": _sha(f"implementation:{id_prefix}"),
            "config_hash": _sha(f"config:{id_prefix}"),
            "policy_hash": _sha(f"policy:{id_prefix}"),
            "dataset_hash": _sha(f"dataset:{id_prefix}"),
            "git_commit": "a" * 40,
            "oauth_scope_hash": _sha(GMAIL_SEND_SCOPE),
            "credential_ref_hash": credential_ref_hash,
            "network_policy_hash": _sha(f"network:{id_prefix}"),
            "reconcile_policy_hash": _sha(f"reconcile-policy:{id_prefix}"),
            "hard_stop_hash": _sha(f"hard-stop:{id_prefix}"),
            "sensitive_field_hash": _sha(f"sensitive:{id_prefix}"),
            "kill_switch_hash": _sha(f"kill-switch:{id_prefix}"),
            "fixture_manifest_sha256": _sha(f"fixture:{id_prefix}"),
            "fault_manifest_sha256": _sha(f"fault:{id_prefix}"),
            "owner_id": owner_id,
            "created_by": f"{id_prefix}-test",
            "expires_at": release_expires_at,
        },
    )
    if not qualified:
        return qualification_id, release_evidence_hash, release_expires_at

    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.release_qualification_decisions (
                id, qualification_id, from_status, to_status, decision_role,
                actor_id, decided_by_user_id, reason, evidence_sha256, evidence_ids
            ) VALUES (
                :id, :qualification_id, 'draft', 'evaluating', 'runner',
                :actor_id, NULL, 'test evaluation opened',
                :evidence_sha256, ARRAY[]::uuid[]
            )
            """
        ),
        {
            "id": uuid4(),
            "qualification_id": qualification_id,
            "actor_id": f"{id_prefix}-runner",
            "evidence_sha256": _sha(f"open-evidence:{id_prefix}"),
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.release_qualification_evidence (
                id, qualification_id, evidence_kind, artifact_uri, artifact_sha256,
                run_id, runner_user_id, runner_actor, metrics, sample_manifest_sha256
            ) VALUES (
                :id, :qualification_id, 'metrics', :artifact_uri, :artifact_sha256,
                :run_id, :owner_id, :runner_actor, CAST(:metrics AS jsonb),
                :sample_manifest_sha256
            )
            """
        ),
        {
            "id": evidence_id,
            "qualification_id": qualification_id,
            "artifact_uri": f"datasets/derived/gmail-send/{id_prefix}/metrics.json",
            "artifact_sha256": _sha(f"artifact:{id_prefix}"),
            "run_id": f"gmail-send-{uuid4()}",
            "owner_id": owner_id,
            "runner_actor": f"{id_prefix}-runner",
            "metrics": _json(
                {
                    "total_observations": 50,
                    "external_provider_calls": 0,
                    "false_negative": 0,
                    "autonomous_provider_write_attempts": 0,
                    "no_autonomous_writes": True,
                }
            ),
            "sample_manifest_sha256": _sha(f"sample:{id_prefix}"),
        },
    )
    for from_status, to_status, role, actor_id in (
        ("evaluating", "pending_independent_review", "reviewer", f"{id_prefix}-reviewer"),
        ("pending_independent_review", "qualified", "operator", f"{id_prefix}-operator"),
    ):
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.release_qualification_decisions (
                    id, qualification_id, from_status, to_status, decision_role,
                    actor_id, decided_by_user_id, reason, evidence_sha256, evidence_ids
                ) VALUES (
                    :id, :qualification_id, :from_status, :to_status, :role,
                    :actor_id, :owner_id, :reason, :evidence_sha256,
                    CAST(:evidence_ids AS uuid[])
                )
                """
            ),
            {
                "id": uuid4(),
                "qualification_id": qualification_id,
                "from_status": from_status,
                "to_status": to_status,
                "role": role,
                "actor_id": actor_id,
                "owner_id": owner_id,
                "reason": f"test {to_status}",
                "evidence_sha256": release_evidence_hash,
                "evidence_ids": [evidence_id],
            },
        )
    return qualification_id, release_evidence_hash, release_expires_at


def _revoke_gmail_release_qualification(
    connection: Connection,
    fixture: SendFixture,
    *,
    reason: str,
) -> None:
    latest = (
        connection.execute(
            sa.text(
                """
                SELECT evidence_sha256, evidence_ids
                FROM careerops.release_qualification_decisions
                WHERE qualification_id = :qualification_id
                ORDER BY sequence DESC
                LIMIT 1
                """
            ),
            {"qualification_id": fixture.release_qualification_id},
        )
        .mappings()
        .one()
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.release_qualification_decisions (
                id, qualification_id, from_status, to_status, decision_role,
                actor_id, decided_by_user_id, reason, evidence_sha256, evidence_ids
            ) VALUES (
                :id, :qualification_id, 'qualified', 'revoked', 'operator',
                :actor_id, :owner_id, :reason, :evidence_sha256,
                CAST(:evidence_ids AS uuid[])
            )
            """
        ),
        {
            "id": uuid4(),
            "qualification_id": fixture.release_qualification_id,
            "actor_id": f"{fixture.id_prefix}-revoker",
            "owner_id": fixture.owner_id,
            "reason": reason,
            "evidence_sha256": latest["evidence_sha256"],
            "evidence_ids": latest["evidence_ids"],
        },
    )


def _insert_bait_reservation_for_same_payload(
    connection: Connection,
    fixture: SendFixture,
    *,
    reservation_key: str,
    reconciliation_key: str,
) -> dict[str, object]:
    policy_decision_id = uuid4()
    authorization_id = uuid4()
    reservation_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.policy_decisions (
                id, action_intent_id, payload_version_id, ruleset_version,
                decision, reason_codes, payload_hash, expires_at
            )
            VALUES (
                :policy_decision_id, :action_intent_id, :payload_version_id,
                'gmail-send-policy.v1', 'allow_autopilot_submission',
                '[]'::jsonb, :payload_hash, CURRENT_TIMESTAMP + interval '1 hour'
            )
            """
        ),
        {
            "policy_decision_id": policy_decision_id,
            "action_intent_id": fixture.action_intent_id,
            "payload_version_id": fixture.payload_version_id,
            "payload_hash": fixture.payload_hash,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_intent_authorizations (
                id, campaign_id, grant_version_id, action_intent_id,
                payload_version_id, payload_hash, policy_decision_id,
                authorization_outcome, reason_codes, authorized_at, expires_at
            )
            VALUES (
                :authorization_id, :campaign_id, :grant_id, :action_intent_id,
                :payload_version_id, :payload_hash, :policy_decision_id,
                'allow_autopilot_submission', '[]'::jsonb, CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP + interval '1 hour'
            )
            """
        ),
        {
            "authorization_id": authorization_id,
            "campaign_id": fixture.campaign_id,
            "grant_id": fixture.grant_id,
            "action_intent_id": fixture.action_intent_id,
            "payload_version_id": fixture.payload_version_id,
            "payload_hash": fixture.payload_hash,
            "policy_decision_id": policy_decision_id,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_cap_reservations (
                id, campaign_id, grant_version_id, authorization_id,
                action_intent_id, payload_version_id, payload_hash, target_host,
                channel, release_version, company_key, adapter_id, fixture_id,
                reservation_key, reconciliation_key, release_evidence_hash,
                release_evidence_expires_at
            )
            VALUES (
                :reservation_id, :campaign_id, :grant_id, :authorization_id,
                :action_intent_id, :payload_version_id, :payload_hash,
                'gmail.googleapis.com', 'gmail:send', 'gmail-send-release.v1',
                :company_key, 'gmail', 'gmail-send.v1', :reservation_key,
                :reconciliation_key, :release_evidence_hash,
                :release_evidence_expires_at
            )
            """
        ),
        {
            "reservation_id": reservation_id,
            "campaign_id": fixture.campaign_id,
            "grant_id": fixture.grant_id,
            "authorization_id": authorization_id,
            "action_intent_id": fixture.action_intent_id,
            "payload_version_id": fixture.payload_version_id,
            "payload_hash": fixture.payload_hash,
            "company_key": fixture.recipient_sha256,
            "reservation_key": reservation_key,
            "reconciliation_key": reconciliation_key,
            "release_evidence_hash": fixture.release_evidence_hash,
            "release_evidence_expires_at": fixture.release_evidence_expires_at,
        },
    )
    return {
        "authorization_id": authorization_id,
        "reservation_id": reservation_id,
        "reservation_key": reservation_key,
        "reconciliation_key": reconciliation_key,
    }


def _seed_owner_candidate_pair(connection: Connection, prefix: str) -> tuple[UUID, UUID]:
    candidate_id = uuid4()
    owner_id = _ensure_console_user(connection, prefix=prefix)
    connection.execute(
        sa.text(
            "INSERT INTO careerops.candidates (id, display_name) VALUES (:id, 'Gmail Send Test')"
        ),
        {"id": candidate_id},
    )
    return owner_id, candidate_id


def _clear_engine_test_data(engine: Engine) -> None:
    # Engine-level concurrency tests commit by design and are permitted only on the
    # disposable, module-reset database guarded by ``database_url`` above.
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                TRUNCATE careerops.console_users,
                         careerops.candidates,
                         careerops.companies,
                         careerops.job_sources,
                         careerops.canonical_jobs,
                         careerops.job_postings
                CASCADE
                """
            )
        )


def _assert_backend_is_blocked_by(
    engine: Engine,
    *,
    blocked_backend_pid: int,
    blocker_backend_pid: int,
    timeout_seconds: float = 10,
) -> None:
    deadline = monotonic() + timeout_seconds
    last_state: dict[str, object] | None = None
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer_connection:
        while monotonic() < deadline:
            wait_state = (
                observer_connection.execute(
                    sa.text(
                        """
                        SELECT state, wait_event_type, wait_event,
                               pg_blocking_pids(pid) AS blocking_pids
                        FROM pg_stat_activity
                        WHERE pid = :blocked_backend_pid
                        """
                    ),
                    {"blocked_backend_pid": blocked_backend_pid},
                )
                .mappings()
                .one_or_none()
            )
            if wait_state is not None:
                last_state = dict(wait_state)
                blocking_pids = tuple(wait_state["blocking_pids"] or ())
                if (
                    wait_state["state"] == "active"
                    and wait_state["wait_event_type"] == "Lock"
                    and blocker_backend_pid in blocking_pids
                ):
                    return
            sleep(0.01)
    raise AssertionError(
        f"backend {blocked_backend_pid} did not wait on backend "
        f"{blocker_backend_pid}; last pg_stat_activity state: {last_state!r}"
    )


def _ensure_console_user(connection: Connection, *, prefix: str) -> UUID:
    existing = connection.scalar(sa.text("SELECT id FROM careerops.console_users LIMIT 1"))
    if isinstance(existing, UUID):
        return existing
    return _create_console_user(connection, prefix=prefix)


def _create_console_user(connection: Connection, *, prefix: str) -> UUID:
    owner_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.console_users (
                id, username, password_hash, password_algorithm,
                password_parameters, password_changed_at
            )
            VALUES (
                :owner_id, :username, '$argon2id$gmail-send-test',
                'argon2id', '{}'::jsonb, :now
            )
            """
        ),
        {"owner_id": owner_id, "username": f"{prefix}-{owner_id.hex}", "now": NOW},
    )
    return owner_id


def _insert_review_evidence(
    connection: Connection,
    fixture: SendFixture,
    *,
    owner_id: UUID,
    account_id: UUID,
    idempotency_key: str,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.gmail_send_review_evidence (
                id, owner_user_id, account_id, release_qualification_id,
                action_intent_id, payload_version_id,
                payload_hash, approval_request_id, reviewed_by_user_id,
                recipient_sha256, subject_sha256, body_sha256, attachment_sha256s,
                review_evidence_sha256, review_snapshot_sha256, expires_at,
                idempotency_key, trace_id
            )
            VALUES (
                :id, :owner_id, :account_id, :release_qualification_id,
                :action_intent_id, :payload_version_id,
                :payload_hash, :approval_request_id, :reviewed_by_user_id,
                :recipient_sha256, :subject_sha256, :body_sha256, '[]'::jsonb,
                :review_evidence_sha256, :review_snapshot_sha256, :expires_at,
                :idempotency_key, :trace_id
            )
            """
        ),
        {
            "id": uuid4(),
            "owner_id": owner_id,
            "account_id": account_id,
            "release_qualification_id": fixture.release_qualification_id,
            "action_intent_id": fixture.action_intent_id,
            "payload_version_id": fixture.payload_version_id,
            "payload_hash": fixture.payload_hash,
            "approval_request_id": fixture.approval_request_id,
            "reviewed_by_user_id": owner_id,
            "recipient_sha256": fixture.recipient_sha256,
            "subject_sha256": fixture.subject_sha256,
            "body_sha256": fixture.body_sha256,
            "review_evidence_sha256": _sha(f"review-evidence:{idempotency_key}"),
            "review_snapshot_sha256": _sha(f"review-snapshot:{idempotency_key}"),
            "expires_at": NOW + timedelta(hours=1),
            "idempotency_key": idempotency_key,
            "trace_id": f"trace-{idempotency_key}",
        },
    )


def _seed_campaign(connection: Connection, *, owner_id: UUID) -> UUID:
    campaign_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_campaigns (
                id, owner_user_id, name, objective, criteria, exclusions, created_by
            )
            VALUES (
                :campaign_id, :owner_id, 'Gmail Send Test', 'send reviewed emails',
                '{}'::jsonb, '[]'::jsonb, :created_by
            )
            """
        ),
        {"campaign_id": campaign_id, "owner_id": owner_id, "created_by": f"user:{owner_id}"},
    )
    return campaign_id


def _seed_grant(
    connection: Connection,
    *,
    campaign_id: UUID,
    allowed_materials: tuple[str, ...],
    max_total: int = 10,
    max_daily: int = 10,
    max_per_company: int = 10,
) -> UUID:
    grant_id = uuid4()
    owner_id = connection.scalar(
        sa.text("SELECT owner_user_id FROM careerops.autopilot_campaigns WHERE id = :campaign_id"),
        {"campaign_id": campaign_id},
    )
    assert isinstance(owner_id, UUID)
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_grant_versions (
                id, campaign_id, version, subject_actor, allowed_action_kinds,
                allowed_channels, allowed_target_hosts, material_hashes,
                max_total_submissions, max_daily_submissions, max_per_company,
                policy_ruleset_version, release_version, expires_at
            )
            VALUES (
                :grant_id, :campaign_id, 1, :subject_actor,
                '["send_email"]'::jsonb, '["gmail:send"]'::jsonb,
                '["gmail.googleapis.com"]'::jsonb, CAST(:material_hashes AS jsonb),
                :max_total, :max_daily, :max_per_company,
                'gmail-send-policy.v1', 'gmail-send-release.v1', :expires_at
            )
            """
        ),
        {
            "grant_id": grant_id,
            "campaign_id": campaign_id,
            "subject_actor": str(owner_id),
            "material_hashes": _json(list(allowed_materials)),
            "max_total": max_total,
            "max_daily": max_daily,
            "max_per_company": max_per_company,
            "expires_at": NOW + timedelta(days=1),
        },
    )
    return grant_id


def _register_send_account(
    connection: Connection,
    *,
    owner_id: UUID,
    candidate_id: UUID,
    subject: str,
    handle: str,
    key: str,
    reconciliation_gmail_account_id: UUID | None = None,
    daily_limit: int = 50,
) -> dict[str, object]:
    readonly_id = reconciliation_gmail_account_id or _register_readonly_account(
        connection,
        owner_id=owner_id,
        candidate_id=candidate_id,
        handle=f"vault://{key}/readonly/{uuid4().hex}",
        subject=subject,
    )
    account_id = connection.scalar(
        sa.text(
            """
            SELECT careerops.gmail_send_register_account(
                :owner_id, :candidate_id, :handle, :subject, 'testing',
                :credential_evidence, :release_evidence, :daily_limit,
                :readonly_id, :key, :trace
            )
            """
        ),
        {
            "owner_id": owner_id,
            "candidate_id": candidate_id,
            "subject": subject,
            "handle": handle,
            "credential_evidence": _sha(f"credential:{handle}"),
            "release_evidence": _sha(f"release:{handle}"),
            "daily_limit": daily_limit,
            "readonly_id": readonly_id,
            "key": key,
            "trace": f"trace-{key}",
        },
    )
    assert isinstance(account_id, UUID)
    status = (
        connection.execute(
            sa.text("SELECT * FROM careerops.gmail_send_status(:owner_id, :account_id)"),
            {"owner_id": owner_id, "account_id": account_id},
        )
        .mappings()
        .one()
    )
    return {
        "account_id": account_id,
        "status": status["status"],
        "receipt_state": "created",
        "reconciliation_gmail_account_id": status["reconciliation_gmail_account_id"],
    }


def _register_readonly_account(
    connection: Connection,
    *,
    owner_id: UUID,
    candidate_id: UUID,
    handle: str,
    subject: str,
) -> UUID:
    account_id = connection.scalar(
        sa.text(
            """
            SELECT careerops.gmail_readonly_register_account(
                :owner_id, :candidate_id, :handle, :subject, 'testing',
                :credential_evidence, :key, :trace
            )
            """
        ),
        {
            "owner_id": owner_id,
            "candidate_id": candidate_id,
            "handle": handle,
            "subject": subject,
            "credential_evidence": _sha(f"readonly:{handle}"),
            "key": f"readonly-{uuid4()}",
            "trace": f"trace-readonly-{uuid4()}",
        },
    )
    assert isinstance(account_id, UUID)
    credential = (
        connection.execute(
            sa.text(
                """
            SELECT provider, granted_scopes
            FROM careerops.oauth_credential_references
            WHERE id = (
                SELECT oauth_credential_reference_id
                FROM careerops.gmail_accounts
                WHERE id = :account_id
            )
            """
            ),
            {"account_id": account_id},
        )
        .mappings()
        .one()
    )
    assert credential == {"provider": "gmail", "granted_scopes": [GMAIL_READONLY_SCOPE]}
    return account_id


def _set_reconciliation_readonly_scopes(
    connection: Connection,
    *,
    send_account_id: UUID,
    granted_scopes: tuple[str, ...],
) -> None:
    updated = connection.scalar(
        sa.text(
            """
            UPDATE careerops.oauth_credential_references AS credential
            SET granted_scopes = CAST(:granted_scopes AS jsonb)
            FROM careerops.gmail_send_accounts AS send_account
            JOIN careerops.gmail_accounts AS readonly_account
              ON readonly_account.id = send_account.reconciliation_gmail_account_id
            WHERE send_account.id = :send_account_id
              AND credential.id = readonly_account.oauth_credential_reference_id
            RETURNING credential.id
            """
        ),
        {
            "send_account_id": send_account_id,
            "granted_scopes": _json(list(granted_scopes)),
        },
    )
    assert isinstance(updated, UUID)


def _reserve(
    connection: Connection,
    fixture: SendFixture,
    *,
    key: str,
) -> dict[str, object]:
    row = (
        connection.execute(
            sa.text(_reserve_sql()),
            _reserve_params(fixture, key=key),
        )
        .mappings()
        .one()
    )
    return dict(row)


def _create_draft_sql() -> str:
    return """
        SELECT *
        FROM careerops.gmail_send_create_draft(
            :owner_id, :candidate_id, :candidate_id,
            CAST(:target AS jsonb), CAST(:payload AS jsonb),
            CAST(:attachment_refs AS jsonb), NULL, 'gmail-send-test.v1',
            :idempotency_key, :trace_id, 'exact-review-required',
            :expires_at
        )
        """


def _create_draft_params(
    *,
    id_prefix: str,
    owner_id: UUID,
    candidate_id: UUID,
    expires_at: datetime,
) -> dict[str, object]:
    recipient = f"recruiter-{id_prefix}@example.com"
    recipient_hash = _sha(recipient.lower().strip())
    subject = f"subject:{id_prefix}"
    body_sha256 = _sha(f"body:{id_prefix}")
    attachment_hash = _sha(f"attachment:{id_prefix}")
    return {
        "owner_id": owner_id,
        "candidate_id": candidate_id,
        "target": _json(
            {
                "target_host": "gmail.googleapis.com",
                "channel": "gmail:send",
                "adapter_id": "gmail",
                "fixture_id": "gmail-send.v1",
                "recipient_sha256": recipient_hash,
                "body_sha256": body_sha256,
                "grant_material_hash": _sha(f"grant-material:{id_prefix}"),
            }
        ),
        "payload": _json(
            {
                "to": [recipient],
                "recipient": recipient,
                "recipient_sha256": recipient_hash,
                "subject": subject,
                "text_body": f"body:{id_prefix}",
                "subject_sha256": _sha(subject),
                "body_sha256": body_sha256,
            }
        ),
        "attachment_refs": _json(
            [
                {
                    "object_key": f"resume/{id_prefix}.pdf",
                    "filename": "resume.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 10,
                    "sha256": attachment_hash,
                }
            ]
        ),
        "idempotency_key": f"draft-{id_prefix}-{uuid4()}",
        "trace_id": f"trace-draft-{id_prefix}",
        "expires_at": expires_at,
    }


def _reserve_sql() -> str:
    return """
        SELECT *
        FROM careerops.gmail_send_reserve_and_enqueue(
            :owner_id, :account_id, :campaign_id, :grant_id, :authorization_id,
            :action_intent_id, :payload_version_id, :payload_hash,
            :recipient_sha256, :approval_request_id, :review_evidence_sha256,
            :review_snapshot_sha256, :reviewed_by_user_id, :release_qualification_id,
            :reservation_key, :reconciliation_key, :event_key,
            :idempotency_key, :trace_id,
            :subject_sha256, :body_sha256
        )
        """


def _review_draft_sql() -> str:
    return """
        SELECT *
        FROM careerops.gmail_send_review_draft(
            :owner_id, :action_intent_id, :payload_version_id,
            :approval_request_id, :campaign_id, :grant_id, :payload_hash,
            :decision, :reviewed_by_user_id, :authorization_id,
            :review_snapshot_sha256, :idempotency_key, :authorization_expires_at,
            :trace_id, :reason
        )
        """


def _review_params(fixture: SendFixture, *, key: str) -> dict[str, object]:
    return {
        "owner_id": fixture.owner_id,
        "action_intent_id": fixture.action_intent_id,
        "payload_version_id": fixture.payload_version_id,
        "approval_request_id": fixture.approval_request_id,
        "campaign_id": fixture.campaign_id,
        "grant_id": fixture.grant_id,
        "payload_hash": fixture.payload_hash,
        "decision": "approved",
        "reviewed_by_user_id": fixture.owner_id,
        "authorization_id": fixture.authorization_id,
        "review_snapshot_sha256": fixture.review_snapshot_sha256,
        "idempotency_key": key,
        "authorization_expires_at": NOW + timedelta(hours=1),
        "trace_id": f"trace-{key}",
        "reason": "exact human review approved immutable Gmail send payload",
    }


def _reserve_params(
    fixture: SendFixture,
    *,
    key: str,
) -> dict[str, object]:
    return {
        "owner_id": fixture.owner_id,
        "account_id": fixture.account_id,
        "campaign_id": fixture.campaign_id,
        "grant_id": fixture.grant_id,
        "authorization_id": fixture.authorization_id,
        "action_intent_id": fixture.action_intent_id,
        "payload_version_id": fixture.payload_version_id,
        "payload_hash": fixture.payload_hash,
        "recipient_sha256": fixture.recipient_sha256,
        "approval_request_id": fixture.approval_request_id,
        "review_evidence_sha256": fixture.review_evidence_sha256,
        "review_snapshot_sha256": fixture.review_snapshot_sha256,
        "reviewed_by_user_id": fixture.owner_id,
        "release_qualification_id": fixture.release_qualification_id,
        "reservation_key": fixture.reservation_key,
        "reconciliation_key": fixture.reconciliation_key,
        "event_key": f"gmail-send:{fixture.reservation_key}",
        "idempotency_key": key,
        "trace_id": f"trace-{key}",
        "subject_sha256": fixture.subject_sha256,
        "body_sha256": fixture.body_sha256,
    }


def _reserve_reviewed_intent_sql() -> str:
    return """
        SELECT careerops.gmail_send_reserve_reviewed_intent(
            :owner_id, :account_id, :reviewed_intent_id, :application_id,
            :recipient_email, :recipient_snapshot_sha256, :subject_sha256,
            :body_sha256, :payload_sha256, :approval_snapshot_sha256,
            :attachment_manifest_sha256, :idempotency_key, :trace_id
        )
        """


def _reserve_reviewed_intent_params(
    fixture: SendFixture,
    *,
    application_id: UUID,
    attachment_manifest_sha256: str,
    key: str,
) -> dict[str, object]:
    return {
        "owner_id": fixture.owner_id,
        "account_id": fixture.account_id,
        "reviewed_intent_id": fixture.action_intent_id,
        "application_id": application_id,
        "recipient_email": f"recruiter-{fixture.id_prefix}@example.com",
        "recipient_snapshot_sha256": fixture.recipient_sha256,
        "subject_sha256": fixture.subject_sha256,
        "body_sha256": fixture.body_sha256,
        "payload_sha256": fixture.payload_hash,
        "approval_snapshot_sha256": fixture.review_snapshot_sha256,
        "attachment_manifest_sha256": attachment_manifest_sha256,
        "idempotency_key": key,
        "trace_id": f"trace-{key}",
    }


def _insert_reviewed_intent_reservation(
    connection: Connection,
    fixture: SendFixture,
    *,
    reservation_id: UUID,
    application_id: UUID,
    recipient_email: str,
    attachment_manifest_sha256: str,
    key: str,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.gmail_send_reservations (
                id, owner_user_id, gmail_send_account_id, reviewed_intent_id,
                application_id, recipient_email,
                recipient_snapshot_sha256, subject_sha256, body_sha256,
                payload_sha256, attachment_manifest_sha256, status,
                approval_snapshot_sha256, idempotency_key, trace_id, created_at
            )
            VALUES (
                :reservation_id, :owner_id, :account_id, :reviewed_intent_id,
                :application_id, :recipient_email,
                :recipient_snapshot_sha256, :subject_sha256, :body_sha256,
                :payload_sha256, :attachment_manifest_sha256, 'reserved',
                :approval_snapshot_sha256, :idempotency_key, :trace_id, :now
            )
            """
        ),
        {
            "reservation_id": reservation_id,
            "owner_id": fixture.owner_id,
            "account_id": fixture.account_id,
            "reviewed_intent_id": fixture.action_intent_id,
            "application_id": application_id,
            "recipient_email": recipient_email,
            "recipient_snapshot_sha256": fixture.recipient_sha256,
            "subject_sha256": fixture.subject_sha256,
            "body_sha256": fixture.body_sha256,
            "payload_sha256": fixture.payload_hash,
            "attachment_manifest_sha256": attachment_manifest_sha256,
            "approval_snapshot_sha256": fixture.review_snapshot_sha256,
            "idempotency_key": key,
            "trace_id": f"trace-{key}",
            "now": NOW,
        },
    )


def _lease_outbox_event(connection: Connection, event_id: UUID) -> dict[str, object]:
    lease_token = uuid4()
    params = {
        "event_id": event_id,
        "lease_owner": f"gmail-send-test-{uuid4().hex[:8]}",
        "lease_token": lease_token,
    }
    updated = connection.scalar(
        sa.text(
            """
            UPDATE careerops.outbox_events
            SET status = 'leased',
                lease_owner = :lease_owner,
                lease_token = :lease_token,
                lease_until = CURRENT_TIMESTAMP + interval '5 minutes',
                attempt_count = attempt_count + 1
            WHERE id = :event_id
              AND status = 'pending'
            RETURNING id
            """
        ),
        params,
    )
    assert updated == event_id
    return params


def _prepare(connection: Connection, claim: dict[str, object]) -> dict[str, object]:
    row = (
        connection.execute(
            sa.text(
                "SELECT * FROM careerops.gmail_send_prepare_outbox_event(:event_id, :lease_owner, :lease_token)"
            ),
            claim,
        )
        .mappings()
        .one()
    )
    return dict(row)


def _set_kill_switch(
    connection: Connection,
    *,
    scope_type: str,
    active: bool,
    campaign_id: UUID | None = None,
    provider: str | None = None,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_kill_switch_events (
                id, scope_type, campaign_id, provider, active, reason,
                actor_user_id, idempotency_key, trace_id
            )
            VALUES (
                :id, :scope_type, :campaign_id, :provider, :active, 'gmail send test',
                :actor_user_id, :idempotency_key, :trace_id
            )
            """
        ),
        {
            "id": uuid4(),
            "scope_type": scope_type,
            "campaign_id": campaign_id,
            "provider": provider,
            "active": active,
            "actor_user_id": _ensure_console_user(connection, prefix="kill-switch"),
            "idempotency_key": f"gmail-send-kill-switch:{uuid4()}",
            "trace_id": f"gmail-send-kill-switch:{uuid4()}",
        },
    )


def _raises_db_error(
    connection: Connection,
    statement: str,
    params: dict[str, object] | None = None,
) -> bool:
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(sa.text(statement), params or {})
    return True


def _count_where(
    connection: Connection,
    table: str,
    predicate: str,
    params: dict[str, object],
) -> int:
    return int(
        connection.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE {predicate}"), params) or 0
    )


def _sqlstate(error: DBAPIError) -> str | None:
    original = getattr(error, "orig", None)
    return getattr(original, "pgcode", None) or getattr(original, "sqlstate", None)


def _has_execute_privilege(connection: Connection, signature: str) -> bool:
    return bool(
        connection.scalar(
            sa.text("SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"),
            {"signature": signature},
        )
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _json(value: object) -> str:

    return json.dumps(value, sort_keys=True, separators=(",", ":"))
