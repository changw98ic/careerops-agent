# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
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
GREENHOUSE_SCOPE = "greenhouse:applications.create"


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for Greenhouse DB tests")
    database_name = sa.engine.make_url(value).database or ""
    if not database_name.startswith(DISPOSABLE_PREFIX):
        pytest.skip("Greenhouse DB tests require a disposable careerops_test_* database")
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


def test_register_rejects_public_visibility_without_authorized_profile(
    connection: Connection,
) -> None:
    owner_id, candidate_id = _seed_owner_candidate_pair(connection, "greenhouse-public")

    assert _raises_db_error(
        connection,
        _register_sql(),
        _register_params(owner_id, candidate_id, key="public-visibility")
        | {"authorized_integration_source": "public_job_board"},
    )


def test_register_rejects_internal_board_token(
    connection: Connection,
) -> None:
    owner_id, candidate_id = _seed_owner_candidate_pair(connection, "greenhouse-internal")

    assert _raises_db_error(
        connection,
        _register_sql(),
        _register_params(owner_id, candidate_id, key="internal-board")
        | {"board_token": "internal_job_board_token"},
    )


def test_create_draft_rejects_missing_internal_job_id(
    connection: Connection,
) -> None:
    owner_id, candidate_id = _seed_owner_candidate_pair(connection, "greenhouse-missing-internal")
    target, payload, attachment_refs = _greenhouse_payload(id_prefix="missing-internal")
    target.pop("internal_job_id")
    payload.pop("internal_job_id")

    assert _raises_db_error(
        connection,
        _create_draft_sql(),
        _create_draft_params(owner_id, candidate_id, dict(target), payload, attachment_refs),
    )


def test_record_receipt_keeps_accepted_submit_unverified_and_requires_reconciliation(
    connection: Connection,
) -> None:
    fixture = _seed_reserved_greenhouse_fixture(connection, id_prefix="accepted-unverified")
    claim = _lease_outbox_event(connection, cast(UUID, fixture["outbox_event_id"]))
    prepared = _prepare(connection, claim)

    assert prepared["prepare_state"] == "ready"
    receipt_id = uuid4()
    connection.execute(
        sa.text(
            """
            SELECT careerops.greenhouse_submit_record_accepted_unverified(
                :event_id, :lease_owner, :lease_token,
                :reconciliation_key, :provider_timestamp, :receipt_id,
                :http_status, :broker_request_sha256, :broker_response_sha256,
                :provider_request_sha256, :provider_response_sha256,
                :observed_raw_response_sha256,
                :observed_normalized_schema_sha256,
                :journal_receipt_sha256, :journal_state, :journal_sequence,
                :evidence_sha256
            )
            """
        ),
        claim
        | {
            "reconciliation_key": fixture["reconciliation_key"],
            "provider_timestamp": NOW,
            "receipt_id": receipt_id,
            "http_status": 200,
            "broker_request_sha256": _sha("broker-request:accepted-unverified"),
            "broker_response_sha256": _sha("broker-response:accepted-unverified"),
            "provider_request_sha256": _sha("provider-request:accepted-unverified"),
            "provider_response_sha256": _sha("provider-response:accepted-unverified"),
            "observed_raw_response_sha256": fixture["raw_response_sha256"],
            "observed_normalized_schema_sha256": fixture["normalized_schema_sha256"],
            "journal_receipt_sha256": _sha("journal:accepted-unverified"),
            "journal_state": "response_observed",
            "journal_sequence": 1,
            "evidence_sha256": _sha("submit-evidence:accepted-unverified"),
        },
    )

    state = (
        connection.execute(
            sa.text(
                """
                SELECT receipt.final_state, attempt.state AS attempt_state,
                       attempt.error_code, event.status AS event_status,
                       event.last_error_code, intent.status AS intent_status,
                       job.status AS reconciliation_status
                FROM careerops.provider_receipts AS receipt
                JOIN careerops.side_effect_attempts AS attempt
                  ON attempt.id = receipt.side_effect_attempt_id
                JOIN careerops.outbox_events AS event
                  ON event.id = attempt.outbox_event_id
                JOIN careerops.action_intents AS intent
                  ON intent.id = attempt.action_intent_id
                JOIN careerops.greenhouse_submit_reconciliation_jobs AS job
                  ON job.outbox_event_id = event.id
                WHERE receipt.id = :receipt_id
                """
            ),
            {"receipt_id": receipt_id},
        )
        .mappings()
        .one()
    )
    assert state == {
        "final_state": "accepted_unverified",
        "attempt_state": "reconciliation_required",
        "error_code": "GREENHOUSE_SUBMIT_ACCEPTED_UNVERIFIED",
        "event_status": "leased",
        "last_error_code": None,
        "intent_status": "reconciliation_required",
        "reconciliation_status": "required",
    }

    connection.execute(
        sa.text(
            """
            SELECT careerops.mark_greenhouse_submit_outbox_published(
                :event_id, :lease_owner, :lease_token
            )
            """
        ),
        claim,
    )
    submitted = _submit_reconciliation_evidence(
        connection,
        fixture,
        evidence_source="employer_admin",
        observed_status="accepted_unverified",
        key=f"accepted-unverified-evidence-{uuid4()}",
    )
    evidence_sha256 = cast(str, submitted["evidence_sha256"])
    connection.execute(
        sa.text(_review_reconciliation_sql()),
        _review_reconciliation_params(
            fixture,
            submitted,
            decision="confirmed",
            key=f"accepted-unverified-review-{uuid4()}",
        ),
    )
    confirmed = (
        connection.execute(
            sa.text(
                """
                SELECT job.status, job.resolution_source,
                       job.resolution_evidence_sha256, intent.status AS intent_status
                FROM careerops.greenhouse_submit_reconciliation_jobs AS job
                JOIN careerops.outbox_events AS event ON event.id = job.outbox_event_id
                JOIN careerops.action_intents AS intent ON intent.id = event.action_intent_id
                WHERE job.outbox_event_id = :event_id
                """
            ),
            {"event_id": fixture["outbox_event_id"]},
        )
        .mappings()
        .one()
    )
    assert confirmed == {
        "status": "confirmed",
        "resolution_source": "employer_admin",
        "resolution_evidence_sha256": evidence_sha256,
        "intent_status": "confirmed",
    }


def test_duplicate_candidate_board_job_is_rejected_across_variants(
    connection: Connection,
) -> None:
    first = _seed_reserved_greenhouse_fixture(
        connection,
        id_prefix="duplicate-a",
        job_post_id="job-duplicate",
        internal_job_id="internal-duplicate-a",
    )
    duplicate = _seed_reviewed_greenhouse_fixture(
        connection,
        id_prefix="duplicate-b",
        owner_id=cast(UUID, first["owner_id"]),
        candidate_id=cast(UUID, first["candidate_id"]),
        account_id=cast(UUID, first["account_id"]),
        board_token="publicboard",
        job_post_id="job-duplicate",
        internal_job_id="internal-duplicate-b",
    )

    assert _raises_db_error(
        connection,
        _reserve_sql(),
        _reserve_params(duplicate, key="reserve-duplicate-job"),
    )


def test_duplicate_candidate_board_internal_job_is_rejected_across_variants(
    connection: Connection,
) -> None:
    first = _seed_reserved_greenhouse_fixture(
        connection,
        id_prefix="duplicate-internal-a",
        job_post_id="job-duplicate-internal-a",
        internal_job_id="internal-duplicate",
    )
    duplicate = _seed_reviewed_greenhouse_fixture(
        connection,
        id_prefix="duplicate-internal-b",
        owner_id=cast(UUID, first["owner_id"]),
        candidate_id=cast(UUID, first["candidate_id"]),
        account_id=cast(UUID, first["account_id"]),
        board_token="publicboard",
        job_post_id="job-duplicate-internal-b",
        internal_job_id="internal-duplicate",
    )

    assert _raises_db_error(
        connection,
        _reserve_sql(),
        _reserve_params(duplicate, key="reserve-duplicate-internal"),
    )


def test_reserve_rejects_reviewed_payload_when_material_hash_does_not_match(
    connection: Connection,
) -> None:
    fixture = _seed_reviewed_greenhouse_fixture(connection, id_prefix="reserve-material-drift")

    assert _raises_db_error(
        connection,
        _reserve_sql(),
        _reserve_params(fixture, key="reserve-material-drift")
        | {"material_hash": _sha("tampered-material")},
    )


def test_reserve_rejects_reviewed_payload_when_submission_identity_does_not_match(
    connection: Connection,
) -> None:
    fixture = _seed_reviewed_greenhouse_fixture(connection, id_prefix="reserve-submission-drift")

    assert _raises_db_error(
        connection,
        _reserve_sql(),
        _reserve_params(fixture, key="reserve-submission-drift")
        | {"submission_identity_sha256": _sha("tampered-submission")},
    )


def test_per_company_cap_counts_same_employer_across_different_jobs(
    connection: Connection,
) -> None:
    second_target, _, second_attachments = _greenhouse_payload(
        id_prefix="company-cap-b",
        job_post_id="company-cap-job-b",
        internal_job_id="company-cap-internal-b",
    )
    first = _seed_reviewed_greenhouse_fixture(
        connection,
        id_prefix="company-cap-a",
        job_post_id="company-cap-job-a",
        max_per_company=1,
        additional_grant_materials=(
            cast(str, second_target["schema_sha256"]),
            cast(str, second_target["candidate_material_sha256"]),
            cast(str, second_attachments[0]["sha256"]),
        ),
    )
    connection.execute(sa.text(_reserve_sql()), _reserve_params(first, key="reserve-company-cap-a"))

    second = _seed_reviewed_greenhouse_fixture(
        connection,
        id_prefix="company-cap-b",
        owner_id=cast(UUID, first["owner_id"]),
        candidate_id=cast(UUID, first["candidate_id"]),
        account_id=cast(UUID, first["account_id"]),
        campaign_id=cast(UUID, first["campaign_id"]),
        grant_id=cast(UUID, first["grant_id"]),
        job_post_id="company-cap-job-b",
        internal_job_id="company-cap-internal-b",
    )
    assert _raises_db_error(
        connection,
        _reserve_sql(),
        _reserve_params(second, key="reserve-company-cap-b"),
    )


@pytest.mark.parametrize(
    ("scope_type", "campaign_source", "provider"),
    (
        ("global", None, None),
        ("campaign", "fixture", None),
        ("provider", None, "greenhouse"),
    ),
)
def test_reserve_rejects_when_kill_switch_is_active_before_outbox_enqueue(
    connection: Connection,
    scope_type: str,
    campaign_source: str | None,
    provider: str | None,
) -> None:
    fixture = _seed_reviewed_greenhouse_fixture(
        connection,
        id_prefix=f"kill-{scope_type}",
    )
    _append_kill_switch(
        connection,
        scope_type=scope_type,
        campaign_id=cast(UUID, fixture["campaign_id"]) if campaign_source == "fixture" else None,
        provider=provider,
    )

    assert _raises_db_error(
        connection,
        _reserve_sql(),
        _reserve_params(fixture, key=f"reserve-kill-{scope_type}"),
    )


def test_reconciliation_review_binds_exact_submitted_evidence_source_and_status(
    connection: Connection,
) -> None:
    fixture = _seed_required_reconciliation_case(connection, id_prefix="reconcile-bind")
    evidence = _submit_reconciliation_evidence(
        connection,
        fixture,
        evidence_source="employer_admin",
        observed_status="accepted_unverified",
        key="reconcile-bind-evidence",
    )

    assert _raises_db_error(
        connection,
        _review_reconciliation_sql(),
        _review_reconciliation_params(
            fixture,
            evidence,
            decision="confirmed",
            key="reconcile-bind-review",
        )
        | {
            "reviewed_evidence_source": "manual_employer_system",
            "reviewed_observed_status": "ambiguous",
        },
    )


def test_reconciliation_evidence_idempotency_replays_exact_request_and_conflicts_on_change(
    connection: Connection,
) -> None:
    fixture = _seed_required_reconciliation_case(connection, id_prefix="reconcile-idempotency")
    params = _submit_reconciliation_params(
        fixture,
        evidence_source="employer_admin",
        observed_status="accepted_unverified",
        key="same-evidence-key",
    )

    created = connection.execute(sa.text(_submit_reconciliation_sql()), params).mappings().one()
    replayed = connection.execute(sa.text(_submit_reconciliation_sql()), params).mappings().one()

    assert replayed["evidence_review_id"] == created["evidence_review_id"]
    assert replayed["receipt_state"] == "replayed"
    assert _raises_db_error(
        connection,
        _submit_reconciliation_sql(),
        params | {"observed_status": "ambiguous"},
    )


@pytest.mark.parametrize("decision", ("resolved_absent", "blocked"))
def test_reconciliation_absent_or_blocked_is_terminal_without_retry(
    connection: Connection,
    decision: str,
) -> None:
    fixture = _seed_required_reconciliation_case(connection, id_prefix=f"reconcile-{decision}")
    evidence = _submit_reconciliation_evidence(
        connection,
        fixture,
        evidence_source="manual_employer_system",
        observed_status="ambiguous",
        key=f"reconcile-evidence-{decision}",
    )

    reviewed = (
        connection.execute(
            sa.text(_review_reconciliation_sql()),
            _review_reconciliation_params(
                fixture,
                evidence,
                decision=decision,
                key=f"reconcile-review-{decision}",
            ),
        )
        .mappings()
        .one()
    )

    state = _event_intent_case_state(connection, cast(UUID, fixture["outbox_event_id"]))
    assert reviewed["reconciliation_status"] == decision
    assert state == {
        "event_status": "published",
        "intent_status": "failed",
        "reconciliation_status": decision,
    }


def test_account_daily_submit_limit_serializes_competing_reservations(engine: Engine) -> None:
    with engine.begin() as connection:
        second_target, _, second_attachments = _greenhouse_payload(
            id_prefix="daily-cap-b",
            job_post_id="daily-cap-job-b",
            internal_job_id="daily-cap-internal-b",
        )
        first = _seed_reviewed_greenhouse_fixture(
            connection,
            id_prefix="daily-cap-a",
            job_post_id="daily-cap-job-a",
            additional_grant_materials=(
                cast(str, second_target["schema_sha256"]),
                cast(str, second_target["candidate_material_sha256"]),
                cast(str, second_attachments[0]["sha256"]),
            ),
        )
        connection.execute(
            sa.text(
                """
                UPDATE careerops.greenhouse_submit_accounts
                SET daily_submit_limit = 1
                WHERE id = :account_id
                """
            ),
            {"account_id": first["account_id"]},
        )
        second = _seed_reviewed_greenhouse_fixture(
            connection,
            id_prefix="daily-cap-b",
            owner_id=cast(UUID, first["owner_id"]),
            candidate_id=cast(UUID, first["candidate_id"]),
            account_id=cast(UUID, first["account_id"]),
            campaign_id=cast(UUID, first["campaign_id"]),
            grant_id=cast(UUID, first["grant_id"]),
            job_post_id="daily-cap-job-b",
            internal_job_id="daily-cap-internal-b",
        )
        first_params = _reserve_params(first, key="reserve-daily-cap-a")
        second_params = _reserve_params(second, key="reserve-daily-cap-b")

    first_connection = engine.connect()
    first_tx = first_connection.begin()
    try:
        first_connection.execute(sa.text(_reserve_sql()), first_params).mappings().one()
        with engine.connect() as second_connection:
            second_tx = second_connection.begin()
            try:
                second_connection.execute(sa.text("SET LOCAL lock_timeout = '250ms'"))
                with pytest.raises(DBAPIError):
                    second_connection.execute(
                        sa.text(_reserve_sql()), second_params
                    ).mappings().one()
            finally:
                second_tx.rollback()
        first_tx.commit()
    finally:
        if first_tx.is_active:
            first_tx.rollback()
        first_connection.close()

    with engine.begin() as connection:
        assert _raises_db_error(connection, _reserve_sql(), second_params)


def test_api_role_has_only_greenhouse_api_function_privileges(connection: Connection) -> None:
    if not _role_exists(connection, "careerops_api"):
        pytest.skip("careerops_api role is not installed")
    _set_local_role(connection, "careerops_api")

    for signature in _api_function_signatures():
        assert _has_execute_privilege(connection, signature), signature
    for signature in _sender_function_signatures():
        assert not _has_execute_privilege(connection, signature), signature
    assert not _has_table_select_privilege(connection, "careerops.greenhouse_submit_accounts")
    assert not _has_table_select_privilege(
        connection,
        "careerops.greenhouse_submit_review_evidence",
    )


def test_greenhouse_sender_role_cannot_execute_api_or_reconciliation_functions(
    connection: Connection,
) -> None:
    if not _role_exists(connection, "careerops_greenhouse_sender"):
        pytest.skip("careerops_greenhouse_sender role is not installed")
    _set_local_role(connection, "careerops_greenhouse_sender")

    for signature in _sender_function_signatures():
        assert _has_execute_privilege(connection, signature), signature
    for signature in _api_only_function_signatures():
        assert not _has_execute_privilege(connection, signature), signature
    assert not _has_table_select_privilege(connection, "careerops.greenhouse_submit_accounts")
    assert not _has_table_select_privilege(
        connection,
        "careerops.greenhouse_submit_command_receipts",
    )


def test_readonly_role_does_not_expose_broker_handles_or_raw_credentials(
    connection: Connection,
) -> None:
    owner_id, candidate_id = _seed_owner_candidate_pair(connection, "readonly-redaction")
    _register_account(connection, owner_id, candidate_id, key="readonly-redaction")
    if not _role_exists(connection, "careerops_readonly"):
        pytest.skip("careerops_readonly role is not installed")
    _set_local_role(connection, "careerops_readonly")

    assert not _has_table_select_privilege(connection, "careerops.greenhouse_submit_accounts")
    assert not _has_table_select_privilege(
        connection,
        "careerops.greenhouse_submit_review_evidence",
    )
    rows = (
        connection.execute(
            sa.text(
                """
                SELECT response_json::text AS response_json
                FROM careerops.greenhouse_submit_command_receipts
                WHERE command_kind = 'register_account'
                """
            )
        )
        .mappings()
        .all()
    )
    joined = "\n".join(str(row["response_json"]) for row in rows)
    assert "opaque_broker_handle" not in joined
    assert "broker://" not in joined
    assert "credential_fingerprint" not in joined
    assert "credential_profile_id" not in joined


def test_prepare_linearizes_behind_uncommitted_grant_revocation(engine: Engine) -> None:
    with engine.begin() as connection:
        fixture = _seed_reserved_greenhouse_fixture(
            connection,
            id_prefix="prepare-concurrent-grant-revoke",
        )
        claim = _lease_outbox_event(connection, cast(UUID, fixture["outbox_event_id"]))

    revocation_inserted = Event()
    allow_revocation_commit = Event()
    prepare_pid_ready = Event()
    prepare_backend_pid: list[int] = []

    def revoke_while_holding_state_lock() -> None:
        with engine.begin() as revocation_connection:
            store = PostgresAutopilotCommandStore(revocation_connection)
            store.revoke_grant(
                RevokeGrantCommand(
                    actor_id=cast(UUID, fixture["owner_id"]),
                    grant_version_id=cast(UUID, fixture["grant_id"]),
                    reason="concurrent revoke before Greenhouse provider prepare",
                    trace_id="trace-greenhouse-concurrent-grant-revoke",
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

    assert prepared["prepare_state"] in {"stopped", "already_terminal"}
    assert prepared["reason_code"] == "GREENHOUSE_SUBMIT_GRANT_NOT_CURRENT"
    with engine.connect() as connection:
        assert (
            _count_where(
                connection,
                "careerops.side_effect_attempts",
                "outbox_event_id = :outbox_event_id",
                {"outbox_event_id": fixture["outbox_event_id"]},
            )
            == 0
        )


def _seed_reserved_greenhouse_fixture(
    connection: Connection,
    *,
    id_prefix: str,
    job_post_id: str | None = None,
    internal_job_id: str | None = None,
    max_per_company: int = 10,
    additional_grant_materials: tuple[str, ...] = (),
) -> dict[str, object]:
    fixture = _seed_reviewed_greenhouse_fixture(
        connection,
        id_prefix=id_prefix,
        job_post_id=job_post_id,
        internal_job_id=internal_job_id,
        max_per_company=max_per_company,
        additional_grant_materials=additional_grant_materials,
    )
    reserved = (
        connection.execute(
            sa.text(_reserve_sql()), _reserve_params(fixture, key=f"reserve-{id_prefix}")
        )
        .mappings()
        .one()
    )
    return fixture | dict(reserved)


def _seed_reviewed_greenhouse_fixture(
    connection: Connection,
    *,
    id_prefix: str,
    owner_id: UUID | None = None,
    candidate_id: UUID | None = None,
    account_id: UUID | None = None,
    campaign_id: UUID | None = None,
    grant_id: UUID | None = None,
    board_token: str = "publicboard",
    job_post_id: str | None = None,
    internal_job_id: str | None = None,
    max_per_company: int = 10,
    additional_grant_materials: tuple[str, ...] = (),
) -> dict[str, object]:
    if owner_id is None or candidate_id is None:
        owner_id, candidate_id = _seed_owner_candidate_pair(connection, id_prefix)
    if account_id is None:
        account_id = cast(
            UUID,
            _register_account(connection, owner_id, candidate_id, key=id_prefix)["account_id"],
        )
    if campaign_id is None:
        campaign_id = _seed_campaign(connection, owner_id)
    target, payload, attachment_refs = _greenhouse_payload(
        id_prefix=id_prefix,
        board_token=board_token,
        job_post_id=job_post_id,
        internal_job_id=internal_job_id,
    )
    review_snapshot_sha256 = _review_snapshot_sha256(target, payload)
    draft = (
        connection.execute(
            sa.text(_create_draft_sql()),
            _create_draft_params(owner_id, candidate_id, dict(target), payload, attachment_refs),
        )
        .mappings()
        .one()
    )
    if grant_id is None:
        grant_id = _seed_grant(
            connection,
            campaign_id=campaign_id,
            allowed_materials=(
                cast(str, target["schema_sha256"]),
                cast(str, target["candidate_material_sha256"]),
                cast(str, attachment_refs[0]["sha256"]),
                *additional_grant_materials,
            ),
            max_per_company=max_per_company,
        )
    authorization_id = uuid4()
    reviewed = (
        connection.execute(
            sa.text(_review_sql()),
            {
                "owner_id": owner_id,
                "action_intent_id": draft["action_intent_id"],
                "payload_version_id": draft["payload_version_id"],
                "approval_request_id": draft["approval_request_id"],
                "campaign_id": campaign_id,
                "grant_id": grant_id,
                "payload_hash": draft["payload_hash"],
                "material_hash": payload["material_hash"],
                "submission_identity_sha256": payload["submission_identity_sha256"],
                "decision": "approved",
                "reviewed_by_user_id": owner_id,
                "authorization_id": authorization_id,
                "review_snapshot_sha256": review_snapshot_sha256,
                "idempotency_key": f"review-{id_prefix}-{uuid4()}",
                "authorization_expires_at": NOW + timedelta(hours=1),
                "trace_id": f"trace-review-{id_prefix}",
                "reason": "exact reviewed Greenhouse payload",
            },
        )
        .mappings()
        .one()
    )
    release_qualification_id, release_evidence_hash, release_expires_at = (
        _seed_release_qualification(connection, owner_id, account_id, id_prefix=id_prefix)
    )
    return {
        "owner_id": owner_id,
        "candidate_id": candidate_id,
        "account_id": account_id,
        "account_subject": f"greenhouse-{id_prefix}",
        "campaign_id": campaign_id,
        "grant_id": grant_id,
        "authorization_id": reviewed["authorization_id"],
        "action_intent_id": draft["action_intent_id"],
        "payload_version_id": draft["payload_version_id"],
        "payload_hash": draft["payload_hash"],
        "board_token_sha256": target["board_token_sha256"],
        "job_id_sha256": target["job_id_sha256"],
        "schema_sha256": target["schema_sha256"],
        "raw_response_sha256": target["raw_response_sha256"],
        "normalized_schema_sha256": target["normalized_schema_sha256"],
        "material_hash": payload["material_hash"],
        "submission_identity_sha256": payload["submission_identity_sha256"],
        "attachment_sha256": attachment_refs[0]["sha256"],
        "approval_request_id": draft["approval_request_id"],
        "review_evidence_sha256": _sha(f"review-evidence:{id_prefix}"),
        "review_snapshot_sha256": review_snapshot_sha256,
        "reviewed_by_user_id": owner_id,
        "release_qualification_id": release_qualification_id,
        "release_evidence_hash": release_evidence_hash,
        "release_expires_at": release_expires_at,
        "reservation_key": f"greenhouse-reservation-{id_prefix}-{uuid4().hex}",
        "reconciliation_key": f"greenhouse/reconcile/{id_prefix}/{uuid4().hex}",
    }


def _register_account(
    connection: Connection,
    owner_id: UUID,
    candidate_id: UUID,
    *,
    key: str,
) -> dict[str, object]:
    return dict(
        connection.execute(
            sa.text(_register_sql()),
            _register_params(owner_id, candidate_id, key=key),
        )
        .mappings()
        .one()
    )


def _register_sql() -> str:
    return """
        SELECT *
        FROM careerops.greenhouse_submit_register_account(
            :owner_id, :candidate_id, :account_subject, :opaque_broker_handle,
            :authorized_integration_source, :employer_id, :board_token,
            :operator_user_id, :credential_profile_id,
            :credential_profile_version, :credential_fingerprint_sha256,
            :credential_profile_status, :credential_profile_expires_at,
            :credential_profile_revoked_at, :employer_authorization_evidence_sha256,
            :credential_store_evidence_sha256, :release_evidence_sha256, :status,
            :daily_submit_limit, :idempotency_key, :trace_id
        )
    """


def _register_params(owner_id: UUID, candidate_id: UUID, *, key: str) -> dict[str, object]:
    return {
        "owner_id": owner_id,
        "candidate_id": candidate_id,
        "account_subject": f"greenhouse-{key}",
        "opaque_broker_handle": f"broker://greenhouse/{key}/{uuid4().hex}",
        "authorized_integration_source": "employer_api_profile",
        "employer_id": f"employer-{key}",
        "board_token": "publicboard",
        "operator_user_id": owner_id,
        "credential_profile_id": f"profile-{key}",
        "credential_profile_version": 1,
        "credential_fingerprint_sha256": _sha(f"fingerprint:{key}"),
        "credential_profile_status": "active",
        "credential_profile_expires_at": NOW + timedelta(hours=1),
        "credential_profile_revoked_at": None,
        "employer_authorization_evidence_sha256": _sha(f"employer-auth:{key}"),
        "credential_store_evidence_sha256": _sha(f"credential:{key}"),
        "release_evidence_sha256": _sha(f"release:{key}"),
        "status": "active",
        "daily_submit_limit": 20,
        "idempotency_key": f"register-{key}-{uuid4()}",
        "trace_id": f"trace-register-{key}",
    }


def _greenhouse_payload(
    *,
    id_prefix: str,
    board_token: str = "publicboard",
    job_post_id: str | None = None,
    internal_job_id: str | None = None,
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    job_post = job_post_id or f"job-{id_prefix}"
    internal_job = internal_job_id or f"internal-{id_prefix}"
    target = {
        "target_host": "boards-api.greenhouse.io",
        "channel": "greenhouse:job-board",
        "adapter_id": "greenhouse-job-board",
        "fixture_id": "greenhouse-submit.v1",
        "board_token_sha256": _sha(board_token),
        "job_post_id": job_post,
        "internal_job_id": internal_job,
        "job_id_sha256": _sha(job_post),
        "schema_sha256": _sha(f"schema:{id_prefix}"),
        "raw_response_sha256": _sha(f"raw-response:{id_prefix}"),
        "normalized_schema_sha256": _sha(f"normalized-schema:{id_prefix}"),
        "job_updated_at": "2026-07-21T00:00:00Z",
        "application_deadline": None,
        "job_identity_sha256": _sha(f"job-identity:{id_prefix}"),
        "candidate_material_sha256": _sha(f"candidate:{id_prefix}"),
        "schema_snapshot": {
            "id": 123456,
            "internal_job_id": 987654,
            "title": "Integration Test Engineer",
            "company_name": "Example AI",
            "updated_at": "2026-07-21T00:00:00Z",
            "application_deadline": None,
            "absolute_url": "https://job-boards.greenhouse.io/publicboard/jobs/123456",
            "questions": [
                _schema_question("First name", "first_name"),
                _schema_question("Last name", "last_name"),
                _schema_question("Email", "email"),
                _schema_question("Resume", "resume", kind="input_file"),
            ],
        },
    }
    payload = {
        "board_token_sha256": target["board_token_sha256"],
        "job_post_id": job_post,
        "internal_job_id": internal_job,
        "job_id_sha256": target["job_id_sha256"],
        "schema_sha256": target["schema_sha256"],
        "raw_response_sha256": target["raw_response_sha256"],
        "normalized_schema_sha256": target["normalized_schema_sha256"],
        "job_updated_at": target["job_updated_at"],
        "application_deadline": target["application_deadline"],
        "job_identity_sha256": target["job_identity_sha256"],
        "answer_sha256": _sha(f"answers:{id_prefix}"),
        "payload_hash": _sha(f"payload:{id_prefix}"),
        "material_hash": _sha(f"material:{id_prefix}"),
        "submission_identity_sha256": _sha(f"submission:{id_prefix}"),
        "answers": [{"field": "first_name", "value_sha256": _sha(f"name:{id_prefix}")}],
    }
    target["candidate_material_sha256"] = cast(str, payload["material_hash"])
    attachment_refs = [
        {
            "object_key": f"resume/{id_prefix}.pdf",
            "filename": "resume.pdf",
            "content_type": "application/pdf",
            "size_bytes": 10,
            "sha256": _sha(f"attachment:{id_prefix}"),
        }
    ]
    return target, payload, attachment_refs


def _create_draft_sql() -> str:
    return """
        SELECT *
        FROM careerops.greenhouse_submit_create_draft(
            :owner_id, :candidate_id, :candidate_id,
            CAST(:target AS jsonb), CAST(:payload AS jsonb),
            CAST(:attachment_refs AS jsonb), NULL,
            'greenhouse-submit-test.v1', :idempotency_key,
            :trace_id, :review_snapshot_sha256, :expires_at
        )
    """


def _create_draft_params(
    owner_id: UUID,
    candidate_id: UUID,
    target: dict[str, object],
    payload: dict[str, object],
    attachment_refs: list[dict[str, object]],
) -> dict[str, object]:
    key = uuid4()
    return {
        "owner_id": owner_id,
        "candidate_id": candidate_id,
        "target": _json(target),
        "payload": _json(payload),
        "attachment_refs": _json(attachment_refs),
        "review_snapshot_sha256": _review_snapshot_sha256(target, payload),
        "idempotency_key": f"draft-{key}",
        "trace_id": f"trace-draft-{key}",
        "expires_at": NOW + timedelta(hours=1),
    }


def _review_sql() -> str:
    return """
        SELECT *
        FROM careerops.greenhouse_submit_review_draft(
            :owner_id, :action_intent_id, :payload_version_id,
            :approval_request_id, :campaign_id, :grant_id, :payload_hash,
            :material_hash, :submission_identity_sha256,
            :decision, :reviewed_by_user_id, :authorization_id,
            :review_snapshot_sha256, :idempotency_key,
            :authorization_expires_at, :trace_id, :reason
        )
    """


def _reserve_sql() -> str:
    return """
        SELECT *
        FROM careerops.greenhouse_submit_reserve_and_enqueue(
            :owner_id, :account_id, :campaign_id, :grant_id,
            :authorization_id, :action_intent_id, :payload_version_id,
            :payload_hash, :material_hash, :submission_identity_sha256,
            :board_token_sha256, :job_id_sha256, :schema_sha256,
            :approval_request_id,
            :review_evidence_sha256, :review_snapshot_sha256,
            :reviewed_by_user_id, :release_qualification_id,
            :reservation_key, :reconciliation_key, :event_key,
            :idempotency_key, :trace_id
        )
    """


def _reserve_params(fixture: dict[str, object], *, key: str) -> dict[str, object]:
    return {
        **fixture,
        "grant_id": fixture["grant_id"],
        "event_key": f"greenhouse-submit:{fixture['reservation_key']}",
        "idempotency_key": key,
        "trace_id": f"trace-{key}",
    }


def _seed_owner_candidate_pair(connection: Connection, prefix: str) -> tuple[UUID, UUID]:
    existing = connection.scalar(sa.text("SELECT id FROM careerops.console_users LIMIT 1"))
    owner_id = existing if isinstance(existing, UUID) else uuid4()
    if not isinstance(existing, UUID):
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.console_users (
                    id, username, password_hash, password_algorithm,
                    password_parameters, password_changed_at
                )
                VALUES (
                    :owner_id, :username, '$argon2id$greenhouse-submit-test',
                    'argon2id', '{}'::jsonb, :now
                )
                """
            ),
            {"owner_id": owner_id, "username": f"{prefix}-{owner_id.hex}", "now": NOW},
        )
    candidate_id = uuid4()
    connection.execute(
        sa.text(
            "INSERT INTO careerops.candidates (id, display_name) VALUES (:id, 'Greenhouse Test')"
        ),
        {"id": candidate_id},
    )
    return owner_id, candidate_id


def _seed_campaign(connection: Connection, owner_id: UUID) -> UUID:
    campaign_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_campaigns (
                id, owner_user_id, name, objective, criteria, exclusions, created_by
            )
            VALUES (
                :campaign_id, :owner_id, 'Greenhouse Test', 'submit applications',
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
                '["submit_application"]'::jsonb, '["greenhouse:job-board"]'::jsonb,
                '["boards-api.greenhouse.io"]'::jsonb, CAST(:material_hashes AS jsonb),
                10, 10, :max_per_company, 'greenhouse-submit-policy.v1',
                'greenhouse-submit-release.v1', :expires_at
            )
            """
        ),
        {
            "grant_id": grant_id,
            "campaign_id": campaign_id,
            "subject_actor": str(owner_id),
            "material_hashes": _json(list(allowed_materials)),
            "max_per_company": max_per_company,
            "expires_at": NOW + timedelta(hours=1),
        },
    )
    return grant_id


def _seed_release_qualification(
    connection: Connection,
    owner_id: UUID,
    account_id: UUID,
    *,
    id_prefix: str,
) -> tuple[UUID, str, datetime]:
    qualification_id = uuid4()
    evidence_id = uuid4()
    evidence_sha256 = _sha(f"qualified:{id_prefix}")
    expires_at = NOW + timedelta(hours=1)
    credential_ref_hash = connection.scalar(
        sa.text(
            """
            SELECT credential_store_evidence_sha256
            FROM careerops.greenhouse_submit_accounts
            WHERE id = :account_id AND owner_user_id = :owner_id
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
                :id, 'greenhouse_submit', 'submit_application', 'review_required',
                'greenhouse', 'greenhouse-job-board', 'greenhouse-submit.v1',
                :implementation_hash, :config_hash, :policy_hash, :dataset_hash,
                'a000000000000000000000000000000000000000', '0014',
                :oauth_scope_hash, :credential_ref_hash, :network_policy_hash,
                :reconcile_policy_hash, :hard_stop_hash, :sensitive_field_hash,
                :kill_switch_hash, :fixture_manifest_sha256,
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
            "oauth_scope_hash": _sha(GREENHOUSE_SCOPE),
            "credential_ref_hash": credential_ref_hash,
            "network_policy_hash": _sha(f"network:{id_prefix}"),
            "reconcile_policy_hash": _sha(f"reconcile:{id_prefix}"),
            "hard_stop_hash": _sha(f"hard-stop:{id_prefix}"),
            "sensitive_field_hash": _sha(f"sensitive:{id_prefix}"),
            "kill_switch_hash": _sha(f"kill-switch:{id_prefix}"),
            "fixture_manifest_sha256": _sha(f"fixture:{id_prefix}"),
            "fault_manifest_sha256": _sha(f"fault:{id_prefix}"),
            "owner_id": owner_id,
            "created_by": f"{id_prefix}-test",
            "expires_at": expires_at,
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
                :run_id, :owner_id, :runner_actor,
                CAST(:metrics AS jsonb),
                :sample_manifest_sha256
            )
            """
        ),
        {
            "id": evidence_id,
            "qualification_id": qualification_id,
            "artifact_uri": f"greenhouse/{id_prefix}/metrics.json",
            "artifact_sha256": _sha(f"artifact:{id_prefix}"),
            "run_id": f"greenhouse-{uuid4()}",
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
    for from_status, to_status, decision_role, actor_id, decided_by_user_id in (
        ("draft", "evaluating", "runner", f"{id_prefix}-runner", None),
        (
            "evaluating",
            "pending_independent_review",
            "reviewer",
            f"{id_prefix}-reviewer",
            None,
        ),
        (
            "pending_independent_review",
            "qualified",
            "operator",
            f"operator:{owner_id}",
            owner_id,
        ),
    ):
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.release_qualification_decisions (
                    id, qualification_id, from_status, to_status, decision_role,
                    actor_id, decided_by_user_id, reason, evidence_sha256, evidence_ids
                ) VALUES (
                    :id, :qualification_id, :from_status, :to_status, :decision_role,
                    :actor_id, :decided_by_user_id, :reason, :evidence_sha256,
                    CAST(:evidence_ids AS uuid[])
                )
                """
            ),
            {
                "id": uuid4(),
                "qualification_id": qualification_id,
                "from_status": from_status,
                "to_status": to_status,
                "decision_role": decision_role,
                "actor_id": actor_id,
                "decided_by_user_id": decided_by_user_id,
                "reason": f"test {to_status}",
                "evidence_sha256": evidence_sha256,
                "evidence_ids": [] if to_status == "evaluating" else [evidence_id],
            },
        )
    return qualification_id, evidence_sha256, expires_at


def _lease_outbox_event(connection: Connection, event_id: UUID) -> dict[str, object]:
    lease_token = uuid4()
    params = {
        "event_id": event_id,
        "lease_owner": f"greenhouse-submit-test-{uuid4().hex[:8]}",
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
    return dict(
        connection.execute(
            sa.text(
                """
                SELECT *
                FROM careerops.greenhouse_submit_prepare_outbox_event(
                    :event_id, :lease_owner, :lease_token
                )
                """
            ),
            claim,
        )
        .mappings()
        .one()
    )


def _seed_required_reconciliation_case(
    connection: Connection,
    *,
    id_prefix: str,
) -> dict[str, object]:
    fixture = _seed_reserved_greenhouse_fixture(connection, id_prefix=id_prefix)
    claim = _lease_outbox_event(connection, cast(UUID, fixture["outbox_event_id"]))
    _prepare(connection, claim)
    connection.execute(
        sa.text(
            """
            SELECT careerops.greenhouse_submit_record_accepted_unverified(
                :event_id, :lease_owner, :lease_token,
                :reconciliation_key, :provider_timestamp, :receipt_id,
                :http_status, :broker_request_sha256, :broker_response_sha256,
                :provider_request_sha256, :provider_response_sha256,
                :observed_raw_response_sha256,
                :observed_normalized_schema_sha256,
                :journal_receipt_sha256, :journal_state, :journal_sequence,
                :evidence_sha256
            )
            """
        ),
        claim
        | {
            "reconciliation_key": fixture["reconciliation_key"],
            "provider_timestamp": NOW,
            "receipt_id": uuid4(),
            "http_status": 202,
            "broker_request_sha256": _sha(f"broker-request:{id_prefix}"),
            "broker_response_sha256": _sha(f"broker-response:{id_prefix}"),
            "provider_request_sha256": _sha(f"provider-request:{id_prefix}"),
            "provider_response_sha256": _sha(f"provider-response:{id_prefix}"),
            "observed_raw_response_sha256": fixture["raw_response_sha256"],
            "observed_normalized_schema_sha256": fixture["normalized_schema_sha256"],
            "journal_receipt_sha256": _sha(f"journal:{id_prefix}"),
            "journal_state": "response_observed",
            "journal_sequence": 1,
            "evidence_sha256": _sha(f"provider-evidence:{id_prefix}"),
        },
    )
    connection.execute(
        sa.text(
            """
            SELECT careerops.mark_greenhouse_submit_outbox_published(
                :event_id, :lease_owner, :lease_token
            )
            """
        ),
        claim,
    )
    return fixture


def _submit_reconciliation_sql() -> str:
    return """
        SELECT *
        FROM careerops.submit_greenhouse_submit_reconciliation_evidence(
            :owner_id, :outbox_event_id, :evidence_source, :evidence_sha256,
            :observed_status, :observed_at, :reason_code,
            :idempotency_key, :trace_id
        )
    """


def _submit_reconciliation_params(
    fixture: dict[str, object],
    *,
    evidence_source: str,
    observed_status: str,
    key: str,
) -> dict[str, object]:
    return {
        "owner_id": fixture["owner_id"],
        "outbox_event_id": fixture["outbox_event_id"],
        "evidence_source": evidence_source,
        "evidence_sha256": _sha(f"employer-evidence:{key}"),
        "observed_status": observed_status,
        "observed_at": NOW,
        "reason_code": "GREENHOUSE_EMPLOYER_RECONCILIATION_EVIDENCE",
        "idempotency_key": key,
        "trace_id": f"trace-{key}",
    }


def _submit_reconciliation_evidence(
    connection: Connection,
    fixture: dict[str, object],
    *,
    evidence_source: str,
    observed_status: str,
    key: str,
) -> dict[str, object]:
    params = _submit_reconciliation_params(
        fixture,
        evidence_source=evidence_source,
        observed_status=observed_status,
        key=key,
    )
    row = connection.execute(sa.text(_submit_reconciliation_sql()), params).mappings().one()
    return dict(row) | {
        "evidence_source": evidence_source,
        "observed_status": observed_status,
        "evidence_sha256": params["evidence_sha256"],
    }


def _review_reconciliation_sql() -> str:
    return """
        SELECT *
        FROM careerops.review_greenhouse_submit_reconciliation_evidence(
            :owner_id, :outbox_event_id, :evidence_review_id, :evidence_sha256,
            :reviewed_evidence_source, :reviewed_observed_status,
            :decision, :reviewed_employer_authorization_evidence_sha256,
            :reviewed_by_user_id, :review_snapshot_sha256, :reason,
            :idempotency_key, :trace_id
        )
    """


def _review_reconciliation_params(
    fixture: dict[str, object],
    evidence: dict[str, object],
    *,
    decision: str,
    key: str,
) -> dict[str, object]:
    evidence_source = cast(str, evidence.get("evidence_source", "employer_admin"))
    observed_status = cast(str, evidence.get("observed_status", "accepted_unverified"))
    account_subject = cast(str, fixture["account_subject"])
    return {
        "owner_id": fixture["owner_id"],
        "outbox_event_id": fixture["outbox_event_id"],
        "evidence_review_id": evidence["evidence_review_id"],
        "evidence_sha256": evidence["evidence_sha256"],
        "reviewed_evidence_source": evidence_source,
        "reviewed_observed_status": observed_status,
        "decision": decision,
        "reviewed_employer_authorization_evidence_sha256": _sha(
            f"employer-auth:{account_subject.removeprefix('greenhouse-')}"
        ),
        "reviewed_by_user_id": fixture["owner_id"],
        "review_snapshot_sha256": _sha(f"reconciliation-review:{key}"),
        "reason": f"reviewed employer-side evidence for {decision}",
        "idempotency_key": key,
        "trace_id": f"trace-{key}",
    }


def _event_intent_case_state(connection: Connection, event_id: UUID) -> dict[str, object]:
    return dict(
        connection.execute(
            sa.text(
                """
                SELECT event.status AS event_status,
                       intent.status AS intent_status,
                       job.status AS reconciliation_status
                FROM careerops.outbox_events AS event
                JOIN careerops.action_intents AS intent ON intent.id = event.action_intent_id
                JOIN careerops.greenhouse_submit_reconciliation_jobs AS job
                  ON job.outbox_event_id = event.id
                WHERE event.id = :event_id
                """
            ),
            {"event_id": event_id},
        )
        .mappings()
        .one()
    )


def _append_kill_switch(
    connection: Connection,
    *,
    scope_type: str,
    campaign_id: UUID | None,
    provider: str | None,
) -> None:
    owner_id = _ensure_console_user(connection, prefix="greenhouse-kill-switch")
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.autopilot_kill_switch_events (
                id, scope_type, campaign_id, provider, active, reason,
                actor_user_id, idempotency_key, trace_id
            )
            VALUES (
                :id, :scope_type, :campaign_id, :provider, true,
                'greenhouse submit integration test',
                :actor_user_id, :idempotency_key, :trace_id
            )
            """
        ),
        {
            "id": uuid4(),
            "scope_type": scope_type,
            "campaign_id": campaign_id,
            "provider": provider,
            "actor_user_id": owner_id,
            "idempotency_key": f"greenhouse-submit-kill-switch:{uuid4()}",
            "trace_id": f"greenhouse-submit-kill-switch:{uuid4()}",
        },
    )


def _role_exists(connection: Connection, role_name: str) -> bool:
    return bool(
        connection.scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": role_name},
        )
    )


def _set_local_role(connection: Connection, role_name: str) -> None:
    if role_name not in {"careerops_api", "careerops_greenhouse_sender", "careerops_readonly"}:
        raise ValueError("unexpected role")
    connection.execute(sa.text(f"SET LOCAL ROLE {role_name}"))


def _has_execute_privilege(connection: Connection, signature: str) -> bool:
    return bool(
        connection.scalar(
            sa.text("SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"),
            {"signature": signature},
        )
    )


def _has_table_select_privilege(connection: Connection, table_name: str) -> bool:
    return bool(
        connection.scalar(
            sa.text("SELECT has_table_privilege(current_user, :table_name, 'SELECT')"),
            {"table_name": table_name},
        )
    )


def _api_function_signatures() -> tuple[str, ...]:
    return (
        "careerops.greenhouse_submit_register_account(uuid, uuid, text, text, text, text, text, uuid, text, integer, text, text, timestamp with time zone, timestamp with time zone, text, text, text, text, integer, text, text)",
        "careerops.greenhouse_submit_create_draft(uuid, uuid, uuid, jsonb, jsonb, jsonb, text, text, text, text, text, timestamp with time zone)",
        "careerops.greenhouse_submit_review_draft(uuid, uuid, uuid, uuid, uuid, uuid, text, text, text, text, uuid, uuid, text, text, timestamp with time zone, text, text)",
        "careerops.greenhouse_submit_reserve_and_enqueue(uuid, uuid, uuid, uuid, uuid, uuid, uuid, text, text, text, text, text, text, uuid, text, text, uuid, uuid, text, text, text, text, text)",
        "careerops.list_greenhouse_submit_accounts(uuid)",
        "careerops.get_greenhouse_submit_account(uuid, uuid)",
        "careerops.list_greenhouse_submit_reconciliation_cases(uuid)",
        "careerops.get_greenhouse_submit_reconciliation_case(uuid, uuid)",
        "careerops.submit_greenhouse_submit_reconciliation_evidence(uuid, uuid, text, text, text, timestamp with time zone, text, text, text)",
        "careerops.review_greenhouse_submit_reconciliation_evidence(uuid, uuid, uuid, text, text, text, text, text, uuid, text, text, text, text)",
    )


def _api_only_function_signatures() -> tuple[str, ...]:
    return _api_function_signatures()


def _sender_function_signatures() -> tuple[str, ...]:
    return (
        "careerops.claim_greenhouse_submit_outbox_events(text, integer, integer)",
        "careerops.mark_greenhouse_submit_outbox_published(uuid, text, uuid)",
        "careerops.release_greenhouse_submit_outbox_event(uuid, text, uuid, timestamp with time zone, text, boolean)",
        "careerops.greenhouse_submit_prepare_outbox_event(uuid, text, uuid)",
        "careerops.greenhouse_submit_record_accepted_unverified(uuid, text, uuid, text, timestamp with time zone, uuid, integer, text, text, text, text, text, text, text, text, bigint, text)",
        "careerops.greenhouse_submit_record_rejected(uuid, text, uuid, text, text[], text, integer, text, text, text, text, text, text, text, text, bigint, text, text, text, text)",
        "careerops.greenhouse_submit_record_ambiguity(uuid, text, uuid, text, text, text, text, text, text, text, text, text, text, bigint, text, text, text, text)",
        "careerops.greenhouse_submit_record_prepost_failure(uuid, text, uuid, text)",
    )


def _count_where(
    connection: Connection,
    table: str,
    predicate: str,
    params: dict[str, object],
) -> int:
    return int(
        connection.scalar(sa.text(f"SELECT count(*) FROM {table} WHERE {predicate}"), params) or 0
    )


def _ensure_console_user(connection: Connection, *, prefix: str) -> UUID:
    existing = connection.scalar(sa.text("SELECT id FROM careerops.console_users LIMIT 1"))
    if isinstance(existing, UUID):
        return existing
    owner_id = uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO careerops.console_users (
                id, username, password_hash, password_algorithm,
                password_parameters, password_changed_at
            )
            VALUES (
                :owner_id, :username, '$argon2id$greenhouse-submit-test',
                'argon2id', '{}'::jsonb, :now
            )
            """
        ),
        {"owner_id": owner_id, "username": f"{prefix}-{owner_id.hex}", "now": NOW},
    )
    return owner_id


def _raises_db_error(
    connection: Connection,
    statement: str,
    params: dict[str, object] | None = None,
) -> bool:
    with pytest.raises(DBAPIError), connection.begin_nested():
        connection.execute(sa.text(statement), params or {})
    return True


def _schema_question(
    label: str,
    name: str,
    *,
    kind: str = "input_text",
) -> dict[str, object]:
    return {
        "label": label,
        "required": True,
        "fields": [{"name": name, "type": kind}],
    }


def _review_snapshot_sha256(
    target: dict[str, object],
    payload: dict[str, object],
) -> str:
    return _sha(
        "\n".join(
            (
                "greenhouse-submit-review-snapshot.v1",
                cast(str, payload["payload_hash"]),
                cast(str, payload["material_hash"]),
                cast(str, payload["submission_identity_sha256"]),
                cast(str, target["schema_sha256"]),
                cast(str, target["job_identity_sha256"]),
            )
        )
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
