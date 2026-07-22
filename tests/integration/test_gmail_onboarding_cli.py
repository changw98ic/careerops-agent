from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from careerops.cli import gmail_onboarding
from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.database.engine import create_database_engine

pytestmark = pytest.mark.integration

DISPOSABLE_PREFIX = "careerops_test_"
ACCOUNT_SUBJECT = "onboarding.integration@example.com"
READONLY_HANDLE = "keychain://gmail/readonly/onboarding-local"
SEND_HANDLE = "keychain://gmail/send/onboarding-local"
SEND_QUALIFICATION_SHA256 = "9" * 64


@pytest.fixture(scope="module")
def database_url() -> str:
    value = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if value is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for Gmail onboarding DB tests")
    database_name = sa.engine.make_url(value).database or ""
    if not database_name.startswith(DISPOSABLE_PREFIX):
        pytest.skip("Gmail onboarding DB tests require a disposable careerops_test_* database")
    if os.environ.get("CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE") != "1":
        pytest.skip("CAREEROPS_ALLOW_DESTRUCTIVE_TEST_DATABASE=1 is required")
    return value


@pytest.fixture(scope="module")
def owner_engine(database_url: str) -> Iterator[Engine]:
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
def api_runtime(database_url: str, owner_engine: Engine) -> Iterator[ApiRuntime]:
    login = f"co_api_onboarding_{uuid4().hex}"
    password = f"careerops-test-{uuid4().hex}"
    runtime_url = sa.engine.make_url(database_url).set(username=login, password=password)
    with _without_careerops_environment():
        settings = Settings.model_validate(
            {
                "database_url": runtime_url.render_as_string(hide_password=False),
                "database_role": DatabaseCapabilityRole.API,
            }
        )
    with owner_engine.begin() as connection:
        connection.execute(
            sa.text(
                f"CREATE ROLE {login} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                f"NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{password}'"
            )
        )
        connection.execute(sa.text(f"GRANT careerops_api TO {login}"))
    try:
        yield ApiRuntime(settings=settings)
    finally:
        with owner_engine.begin() as connection:
            connection.execute(sa.text(f"REVOKE careerops_api FROM {login}"))
            connection.execute(sa.text(f"DROP ROLE {login}"))


@dataclass(frozen=True)
class ApiRuntime:
    settings: Settings

    def engine_factory(self, settings: Settings) -> Engine:
        assert settings.database_role is DatabaseCapabilityRole.API
        return create_database_engine(self.settings)


class FakeQualifiedGmailHost:
    def local_onboarding_status(self) -> dict[str, object]:
        return {
            "clients": {"readonly": True, "send": True},
            "credentials": {
                "readonly": {
                    "active": True,
                    "account_subject": ACCOUNT_SUBJECT,
                    "handle": READONLY_HANDLE,
                    "live_qualified": True,
                    "qualification_evidence_sha256": None,
                    "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                },
                "send": {
                    "active": True,
                    "account_subject": ACCOUNT_SUBJECT,
                    "handle": SEND_HANDLE,
                    "live_qualified": True,
                    "qualification_evidence_sha256": SEND_QUALIFICATION_SHA256,
                    "scopes": ["https://www.googleapis.com/auth/gmail.send"],
                },
            },
        }


def test_register_local_uses_real_api_role_providers_and_replays_without_duplicates(
    owner_engine: Engine,
    api_runtime: ApiRuntime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    owner_id = uuid4()
    _seed_console_owner(owner_engine, owner_id=owner_id)
    before_gate_rows = _gate_row_counts(owner_engine)

    first_payload = _run_register_local(
        api_runtime,
        capsys,
        "--candidate-display-name",
        "Gmail Onboarding Integration Candidate",
    )

    candidate_id = UUID(first_payload["candidate_id"])
    readonly_account_id = UUID(first_payload["readonly"]["account_id"])
    send_account_id = UUID(first_payload["send"]["account_id"])
    assert UUID(first_payload["owner_user_id"]) == owner_id
    assert first_payload["account_subject"] == ACCOUNT_SUBJECT
    assert first_payload["status"] == "registered_local"
    assert first_payload["readonly"]["publishing_status"] == "testing"
    assert first_payload["send"]["requested_status"] == "disabled"
    assert first_payload["daily_send_limit"] == 25

    with owner_engine.begin() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT display_name FROM careerops.candidates WHERE id = :candidate_id"),
                {"candidate_id": candidate_id},
            )
            == "Gmail Onboarding Integration Candidate"
        )
        rows = _registered_rows(connection, readonly_account_id, send_account_id)
        assert rows["readonly_account"] == {
            "id": readonly_account_id,
            "owner_user_id": owner_id,
            "candidate_id": candidate_id,
            "account_subject": ACCOUNT_SUBJECT,
            "publishing_status": "testing",
            "status": "active",
            "credential_store_evidence_sha256": first_payload["readonly"][
                "credential_store_evidence_sha256"
            ],
            "in_production_attested": False,
        }
        assert rows["readonly_credential"] == {
            "provider": "gmail",
            "account_subject": ACCOUNT_SUBJECT,
            "secret_handle": READONLY_HANDLE,
            "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
            "status": "active",
        }
        assert rows["send_account"] == {
            "id": send_account_id,
            "owner_user_id": owner_id,
            "candidate_id": candidate_id,
            "reconciliation_gmail_account_id": readonly_account_id,
            "account_subject": ACCOUNT_SUBJECT,
            "status": "disabled",
            "daily_send_limit": 25,
            "credential_store_evidence_sha256": first_payload["send"][
                "credential_store_evidence_sha256"
            ],
            "release_evidence_sha256": first_payload["send"]["release_evidence_sha256"],
        }
        assert rows["send_credential"] == {
            "provider": "gmail_send",
            "account_subject": ACCOUNT_SUBJECT,
            "secret_handle": SEND_HANDLE,
            "granted_scopes": ["https://www.googleapis.com/auth/gmail.send"],
            "status": "active",
        }
        assert _count(connection, "careerops.gmail_accounts") == 1
        assert _count(connection, "careerops.gmail_send_accounts") == 1
        assert _count(connection, "careerops.oauth_credential_references") == 2

    second_payload = _run_register_local(
        api_runtime,
        capsys,
        "--candidate-id",
        str(candidate_id),
    )

    assert second_payload == first_payload
    with owner_engine.begin() as connection:
        assert _count(connection, "careerops.candidates") == 1
        assert _count(connection, "careerops.gmail_accounts") == 1
        assert _count(connection, "careerops.gmail_send_accounts") == 1
        assert _count(connection, "careerops.oauth_credential_references") == 2
        assert _count(connection, "careerops.gmail_command_receipts") == 1
        assert _count(connection, "careerops.gmail_send_command_receipts") == 1
    assert _gate_row_counts(owner_engine) == before_gate_rows


def _run_register_local(
    api_runtime: ApiRuntime,
    capsys: pytest.CaptureFixture[str],
    *candidate_args: str,
) -> dict[str, Any]:
    status = gmail_onboarding.main(
        (
            "register-local",
            "--account-subject",
            ACCOUNT_SUBJECT,
            "--json",
            *candidate_args,
        ),
        host=FakeQualifiedGmailHost(),
        settings=api_runtime.settings,
        engine_factory=api_runtime.engine_factory,
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert status == 0, captured.out
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    return payload


def _seed_console_owner(engine: Engine, *, owner_id: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO careerops.console_users (
                    id, username, password_hash, password_algorithm,
                    password_parameters, password_changed_at
                )
                VALUES (
                    :owner_id, :username, '$argon2id$gmail-onboarding-test',
                    'argon2id', '{}'::jsonb, CURRENT_TIMESTAMP
                )
                """
            ),
            {"owner_id": owner_id, "username": f"gmail-onboarding-{owner_id.hex}"},
        )


def _registered_rows(
    connection: sa.Connection,
    readonly_account_id: UUID,
    send_account_id: UUID,
) -> dict[str, dict[str, Any]]:
    readonly_account = (
        connection.execute(
            sa.text(
                """
                SELECT id, owner_user_id, candidate_id, account_subject,
                       publishing_status, status, credential_store_evidence_sha256,
                       in_production_attested
                FROM careerops.gmail_accounts
                WHERE id = :account_id
                """
            ),
            {"account_id": readonly_account_id},
        )
        .mappings()
        .one()
    )
    readonly_credential = (
        connection.execute(
            sa.text(
                """
                SELECT provider, account_subject, secret_handle, granted_scopes, status
                FROM careerops.oauth_credential_references
                WHERE id = (
                    SELECT oauth_credential_reference_id
                    FROM careerops.gmail_accounts
                    WHERE id = :account_id
                )
                """
            ),
            {"account_id": readonly_account_id},
        )
        .mappings()
        .one()
    )
    send_account = (
        connection.execute(
            sa.text(
                """
                SELECT id, owner_user_id, candidate_id, reconciliation_gmail_account_id,
                       account_subject, status, daily_send_limit,
                       credential_store_evidence_sha256, release_evidence_sha256
                FROM careerops.gmail_send_accounts
                WHERE id = :account_id
                """
            ),
            {"account_id": send_account_id},
        )
        .mappings()
        .one()
    )
    send_credential = (
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
            {"account_id": send_account_id},
        )
        .mappings()
        .one()
    )
    return {
        "readonly_account": dict(readonly_account),
        "readonly_credential": dict(readonly_credential),
        "send_account": dict(send_account),
        "send_credential": dict(send_credential),
    }


def _gate_row_counts(engine: Engine) -> dict[str, int]:
    with engine.begin() as connection:
        return {
            "autopilot_kill_switch_events": _count(
                connection, "careerops.autopilot_kill_switch_events"
            ),
            "release_qualifications": _count(connection, "careerops.release_qualifications"),
            "outbox_events": _count(connection, "careerops.outbox_events"),
        }


def _count(connection: sa.Connection, table_name: str) -> int:
    count = connection.scalar(sa.text(f"SELECT count(*) FROM {table_name}"))
    assert isinstance(count, int)
    return count


@contextmanager
def _without_careerops_environment() -> Iterator[None]:
    saved = {key: value for key, value in os.environ.items() if key.startswith("CAREEROPS_")}
    for key in saved:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.update(saved)
