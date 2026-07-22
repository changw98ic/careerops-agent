from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from careerops.api.gmail_send import GmailSendUnavailable
from careerops.infrastructure import gmail_send_operator

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000003001")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000003002")
COMMAND_ID = UUID("00000000-0000-0000-0000-000000003003")
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000003004")
READONLY_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000003005")
RELEASE_QUALIFICATION_ID = UUID("00000000-0000-0000-0000-000000003006")
CAMPAIGN_ID = UUID("00000000-0000-0000-0000-000000003007")
GRANT_VERSION_ID = UUID("00000000-0000-0000-0000-000000003008")
AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-000000003009")
ACTION_INTENT_ID = UUID("00000000-0000-0000-0000-00000000300a")
PAYLOAD_VERSION_ID = UUID("00000000-0000-0000-0000-00000000300b")
APPROVAL_REQUEST_ID = UUID("00000000-0000-0000-0000-00000000300c")
RESERVATION_ID = UUID("00000000-0000-0000-0000-00000000300d")
OUTBOX_EVENT_ID = UUID("00000000-0000-0000-0000-00000000300e")
CREDENTIAL_STORE_EVIDENCE_SHA256 = "a" * 64
RELEASE_EVIDENCE_SHA256 = "b" * 64
PAYLOAD_SHA256 = "c" * 64
RECIPIENT_SHA256 = "d" * 64
REVIEW_EVIDENCE_SHA256 = "e" * 64
REVIEW_SNAPSHOT_SHA256 = "f" * 64
SUBJECT_SHA256 = "1" * 64
BODY_SHA256 = "2" * 64


def test_gmail_send_registration_uses_security_definer_readonly_binding_wrapper() -> None:
    statement = str(gmail_send_operator._REGISTER_ACCOUNT)

    assert "careerops.gmail_send_register_account" in statement
    assert ":reconciliation_gmail_account_id" in statement
    assert ":publishing_status" in statement
    assert ":credential_store_evidence_sha256" in statement
    assert ":release_evidence_sha256" in statement
    assert ":daily_send_limit" in statement
    assert "UPDATE careerops.gmail_send_accounts" not in statement


@pytest.mark.asyncio
async def test_runtime_provider_sends_registration_release_hash_and_daily_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, Mapping[str, object]]] = []

    def fake_one_row(
        _connection: Connection,
        statement: object,
        parameters: Mapping[str, object],
    ) -> Mapping[str, object]:
        statement_text = str(statement)
        captured.append((statement_text, dict(parameters)))
        if "gmail_send_register_account" in statement_text:
            return {"account_id": ACCOUNT_ID}
        if "gmail_send_status" in statement_text:
            return {
                "account_id": ACCOUNT_ID,
                "reconciliation_gmail_account_id": READONLY_ACCOUNT_ID,
                "account_subject": "candidate@example.com",
                "status": "active",
                "daily_send_limit": 123,
                "credential_status": "active",
                "updated_at": NOW,
            }
        raise AssertionError(f"unexpected SQL statement: {statement_text}")

    monkeypatch.setattr(gmail_send_operator, "_one_row", fake_one_row)
    provider = gmail_send_operator.RuntimeGmailSendOperatorProvider(
        transaction_factory=lambda: nullcontext(cast("Connection", object()))
    )

    response = await provider.register_account(
        actor_id=ACTOR_ID,
        command_id=COMMAND_ID,
        candidate_id=CANDIDATE_ID,
        reconciliation_gmail_account_id=READONLY_ACCOUNT_ID,
        credential_handle="vault:gmail-send/candidate@example.com",
        account_subject="candidate@example.com",
        credential_store_evidence_sha256=CREDENTIAL_STORE_EVIDENCE_SHA256,
        release_evidence_sha256=RELEASE_EVIDENCE_SHA256,
        requested_status="active",
        daily_send_limit=123,
        dedicated=True,
        oauth_client_mode="byo",
        now=NOW,
    )

    register_parameters = next(
        parameters
        for statement, parameters in captured
        if "gmail_send_register_account" in statement
    )
    assert response.account.account_id == ACCOUNT_ID
    assert response.account.reconciliation_gmail_account_id == READONLY_ACCOUNT_ID
    assert response.account.daily_send_limit == 123
    assert register_parameters["credential_store_evidence_sha256"] == (
        CREDENTIAL_STORE_EVIDENCE_SHA256
    )
    assert register_parameters["release_evidence_sha256"] == RELEASE_EVIDENCE_SHA256
    assert (
        register_parameters["release_evidence_sha256"]
        != register_parameters["credential_store_evidence_sha256"]
    )
    assert register_parameters["daily_send_limit"] == 123
    assert register_parameters["reconciliation_gmail_account_id"] == READONLY_ACCOUNT_ID


@pytest.mark.asyncio
async def test_runtime_provider_reserves_with_release_qualification_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, Mapping[str, object]]] = []

    def fake_one_row(
        _connection: Connection,
        statement: object,
        parameters: Mapping[str, object],
    ) -> Mapping[str, object]:
        statement_text = str(statement)
        captured.append((statement_text, dict(parameters)))
        if "gmail_send_reserve_and_enqueue" in statement_text:
            return {
                "account_id": ACCOUNT_ID,
                "reservation_id": RESERVATION_ID,
                "outbox_event_id": OUTBOX_EVENT_ID,
                "receipt_state": "created",
            }
        raise AssertionError(f"unexpected SQL statement: {statement_text}")

    monkeypatch.setattr(gmail_send_operator, "_one_row", fake_one_row)
    provider = gmail_send_operator.RuntimeGmailSendOperatorProvider(
        transaction_factory=lambda: nullcontext(cast("Connection", object()))
    )

    response = await provider.reserve_intent(
        actor_id=ACTOR_ID,
        account_id=ACCOUNT_ID,
        command_id=COMMAND_ID,
        campaign_id=CAMPAIGN_ID,
        grant_version_id=GRANT_VERSION_ID,
        authorization_id=AUTHORIZATION_ID,
        action_intent_id=ACTION_INTENT_ID,
        payload_version_id=PAYLOAD_VERSION_ID,
        payload_hash=PAYLOAD_SHA256,
        recipient_sha256=RECIPIENT_SHA256,
        approval_request_id=APPROVAL_REQUEST_ID,
        review_evidence_sha256=REVIEW_EVIDENCE_SHA256,
        review_snapshot_sha256=REVIEW_SNAPSHOT_SHA256,
        reviewed_by_user_id=ACTOR_ID,
        release_qualification_id=RELEASE_QUALIFICATION_ID,
        reservation_key="reservation-1",
        reconciliation_key="reconciliation-1",
        subject_sha256=SUBJECT_SHA256,
        body_sha256=BODY_SHA256,
        now=NOW,
    )

    reserve_statement, reserve_parameters = captured[0]
    assert response.reservation_id == RESERVATION_ID
    assert response.event_key == "gmail-send:reservation-1"
    assert ":release_qualification_id" in reserve_statement
    assert ":release_evidence_hash" not in reserve_statement
    assert ":release_evidence_expires_at" not in reserve_statement
    assert reserve_parameters["release_qualification_id"] == RELEASE_QUALIFICATION_ID
    assert "release_evidence_hash" not in reserve_parameters
    assert "release_evidence_expires_at" not in reserve_parameters


def test_runtime_provider_does_not_mask_programming_or_row_shape_errors() -> None:
    provider = gmail_send_operator.RuntimeGmailSendOperatorProvider(
        transaction_factory=lambda: nullcontext(cast("Connection", object()))
    )

    def invalid_row_shape(_connection: Connection) -> None:
        raise KeyError("account_subject")

    with pytest.raises(KeyError, match="account_subject"):
        provider._run_sync(invalid_row_shape)


def test_runtime_provider_does_not_mask_unknown_database_programming_errors() -> None:
    provider = gmail_send_operator.RuntimeGmailSendOperatorProvider(
        transaction_factory=lambda: nullcontext(cast("Connection", object()))
    )

    class UndefinedColumnError(Exception):
        sqlstate = "42703"

    error = DBAPIError("SELECT broken_column", {}, UndefinedColumnError(), False)

    def programming_error(_connection: Connection) -> None:
        raise error

    with pytest.raises(DBAPIError) as caught:
        provider._run_sync(programming_error)

    assert caught.value is error


def test_runtime_provider_translates_invalidated_database_connections() -> None:
    provider = gmail_send_operator.RuntimeGmailSendOperatorProvider(
        transaction_factory=lambda: nullcontext(cast("Connection", object()))
    )
    error = DBAPIError(
        "SELECT 1",
        {},
        ConnectionError("closed"),
        connection_invalidated=True,
    )

    def unavailable_database(_connection: Connection) -> None:
        raise error

    with pytest.raises(GmailSendUnavailable):
        provider._run_sync(unavailable_database)
