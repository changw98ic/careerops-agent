from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from careerops.api.greenhouse_submit import GreenhouseSubmitConflict, GreenhouseSubmitUnavailable
from careerops.application.greenhouse_submit import GreenhouseJobSchemaSnapshot, GreenhouseTarget
from careerops.infrastructure import greenhouse_submit_operator

NOW = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
EXPIRES_AT = NOW + timedelta(hours=1)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000004001")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000004002")
COMMAND_ID = UUID("00000000-0000-0000-0000-000000004003")
RESOURCE_ID = UUID("00000000-0000-0000-0000-000000004004")
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000004005")
ACTION_INTENT_ID = UUID("00000000-0000-0000-0000-000000004006")
PAYLOAD_VERSION_ID = UUID("00000000-0000-0000-0000-000000004007")
POLICY_DECISION_ID = UUID("00000000-0000-0000-0000-000000004008")
APPROVAL_REQUEST_ID = UUID("00000000-0000-0000-0000-000000004009")
CAMPAIGN_ID = UUID("00000000-0000-0000-0000-00000000400a")
GRANT_VERSION_ID = UUID("00000000-0000-0000-0000-00000000400b")
AUTHORIZATION_ID = UUID("00000000-0000-0000-0000-00000000400c")
RESERVATION_ID = UUID("00000000-0000-0000-0000-00000000400d")
OUTBOX_EVENT_ID = UUID("00000000-0000-0000-0000-00000000400e")
RELEASE_QUALIFICATION_ID = UUID("00000000-0000-0000-0000-00000000400f")
SHA256_A = "a" * 64
SHA256_B = "b" * 64
SHA256_C = "c" * 64
SHA256_D = "d" * 64
SHA256_E = "e" * 64


class RecordingJobBoardClient:
    def __init__(self, schema: GreenhouseJobSchemaSnapshot) -> None:
        self.schema = schema
        self.targets: list[GreenhouseTarget] = []

    def fetch_job_schema(self, target: GreenhouseTarget) -> GreenhouseJobSchemaSnapshot:
        self.targets.append(target)
        return self.schema


@pytest.mark.asyncio
async def test_registration_uses_security_definer_wrapper_and_never_returns_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    def fake_one_row(_connection: Connection, statement: object) -> Mapping[str, object]:
        captured.append(statement)
        statement_text = str(statement)
        if "greenhouse_submit_register_account" in statement_text:
            return {"account_id": ACCOUNT_ID, "receipt_state": "created"}
        if "get_greenhouse_submit_account" in statement_text:
            return _account_row()
        raise AssertionError(f"unexpected SQL statement: {statement_text}")

    monkeypatch.setattr(greenhouse_submit_operator, "_one_row", fake_one_row)
    provider = _provider()

    response = await provider.register_account(
        actor_id=ACTOR_ID,
        command_id=COMMAND_ID,
        candidate_id=CANDIDATE_ID,
        employer_id="example-ai",
        board_token="example",
        account_subject="candidate@example.com",
        opaque_credential_handle="vault://greenhouse/example/profile-v1",
        credential_profile_id="profile-v1",
        credential_profile_version=1,
        credential_fingerprint_sha256=SHA256_A,
        credential_profile_status="active",
        credential_profile_expires_at=EXPIRES_AT,
        employer_authorization_evidence_sha256=SHA256_B,
        credential_store_evidence_sha256=SHA256_C,
        release_evidence_sha256=SHA256_D,
        requested_status="active",
        daily_submit_limit=25,
        now=NOW,
    )

    statement_text = str(captured[0])
    register_statement = cast("sa.TextClause", captured[0])
    params = register_statement.compile().params
    assert "careerops.greenhouse_submit_register_account" in statement_text
    assert "UPDATE careerops.greenhouse_submit_accounts" not in statement_text
    assert params["authorized_integration_source"] == "employer_api_profile"
    assert params["operator_user_id"] == ACTOR_ID
    assert "opaque_broker_handle" in params
    assert "opaque_credential_handle" not in response.model_dump_json()
    assert "vault://greenhouse/example/profile-v1" not in response.model_dump_json()


def test_account_projection_fails_loudly_when_database_contract_omits_required_field() -> None:
    row = dict(_account_row())
    row.pop("daily_submit_limit")

    with pytest.raises(ValueError, match="daily_submit_limit"):
        greenhouse_submit_operator._account_from_row(row)


@pytest.mark.asyncio
async def test_create_draft_fetches_public_schema_and_binds_canonical_exact_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []
    client = RecordingJobBoardClient(_schema())

    def fake_one_row(_connection: Connection, statement: object) -> Mapping[str, object]:
        captured.append(statement)
        assert "greenhouse_submit_create_draft" in str(statement)
        params = cast("sa.TextClause", statement).compile().params
        target_json = json.loads(cast("str", params["target_json"]))
        payload_json = json.loads(cast("str", params["payload_json"]))
        attachment_refs = json.loads(cast("str", params["attachment_refs_json"]))
        assert target_json["target_host"] == "boards-api.greenhouse.io"
        assert target_json["channel"] == "greenhouse:job-board"
        assert target_json["adapter_id"] == "greenhouse-job-board"
        assert target_json["fixture_id"] == "greenhouse-submit.v1"
        assert target_json["board_token"] == "example"
        assert target_json["job_id"] == 123456
        assert target_json["schema_snapshot"] == {
            "id": 123456,
            "internal_job_id": 987654,
            "title": "Senior Agent Engineer",
            "company_name": "Example AI",
            "updated_at": "2026-07-21T08:00:00Z",
            "application_deadline": None,
            "absolute_url": "https://job-boards.greenhouse.io/example/jobs/123456",
            "questions": [
                _question("First name", True, "first_name"),
                _question("Last name", True, "last_name"),
                _question("Email", True, "email"),
                _question("Resume", False, "resume", kind="input_file"),
            ],
        }
        assert len(target_json["board_token_sha256"]) == 64
        assert len(target_json["job_id_sha256"]) == 64
        assert payload_json["fields"] == {
            "email": "ada@example.com",
            "first_name": "Ada",
            "last_name": "Lovelace",
        }
        assert payload_json["schema_sha256"] == target_json["schema_sha256"]
        assert attachment_refs[0]["object_key"] == "materials/resume.pdf"
        assert params["payload_hash"] == payload_json["payload_hash"]
        return {
            "action_intent_id": ACTION_INTENT_ID,
            "payload_version_id": PAYLOAD_VERSION_ID,
            "payload_hash": params["payload_hash"],
            "policy_decision_id": POLICY_DECISION_ID,
            "approval_request_id": APPROVAL_REQUEST_ID,
            "receipt_state": "created",
        }

    monkeypatch.setattr(greenhouse_submit_operator, "_one_row", fake_one_row)
    provider = _provider(client=client)

    response = await provider.create_draft(
        actor_id=ACTOR_ID,
        command_id=COMMAND_ID,
        candidate_id=CANDIDATE_ID,
        resource_id=RESOURCE_ID,
        target={"host": "boards-api.greenhouse.io", "board_token": "example", "job_id": 123456},
        core_fields={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
        },
        attachment_refs=(_attachment(),),
        source_draft_payload_hash=SHA256_A,
        approved_material_hashes=(SHA256_B,),
        ruleset_version="greenhouse-submit-rules.v1",
        expires_at=EXPIRES_AT,
        requested_for="greenhouse_submit_exact_payload",
        now=NOW,
    )

    assert client.targets == [GreenhouseTarget(board_token="example", job_id=123456)]
    assert response.material_hash
    assert response.submission_identity_sha256
    create_params = cast("sa.TextClause", captured[0]).compile().params
    assert response.review_snapshot_sha256 == create_params["decision_rule_reference"]
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_create_draft_fails_closed_on_hard_stop_before_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("database must not be called after schema hard stop")

    monkeypatch.setattr(greenhouse_submit_operator, "_one_row", fail_if_called)
    provider = _provider(client=RecordingJobBoardClient(_schema(include_website=True)))

    with pytest.raises(GreenhouseSubmitConflict):
        await provider.create_draft(
            actor_id=ACTOR_ID,
            command_id=COMMAND_ID,
            candidate_id=CANDIDATE_ID,
            resource_id=RESOURCE_ID,
            target={"board_token": "example", "job_id": 123456},
            core_fields={
                "first_name": "Ada",
                "last_name": "Lovelace",
                "email": "ada@example.com",
                "website": "https://example.com",
            },
            attachment_refs=(),
            source_draft_payload_hash=SHA256_A,
            approved_material_hashes=(SHA256_B,),
            ruleset_version="greenhouse-submit-rules.v1",
            expires_at=EXPIRES_AT,
            requested_for="greenhouse_submit_exact_payload",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_reserve_creates_greenhouse_submit_event_key_and_passes_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    def fake_one_row(_connection: Connection, statement: object) -> Mapping[str, object]:
        captured.append(statement)
        params = cast("sa.TextClause", statement).compile().params
        assert params["event_key"] == "greenhouse-submit:reservation-1"
        assert params["payload_hash"] == SHA256_A
        assert params["material_hash"] == SHA256_E
        assert params["submission_identity_sha256"] == SHA256_A
        assert params["board_token_sha256"] == SHA256_B
        assert params["job_id_sha256"] == SHA256_C
        assert params["schema_sha256"] == SHA256_D
        assert params["release_qualification_id"] == RELEASE_QUALIFICATION_ID
        return {
            "account_id": ACCOUNT_ID,
            "reservation_id": RESERVATION_ID,
            "outbox_event_id": OUTBOX_EVENT_ID,
            "receipt_state": "created",
        }

    monkeypatch.setattr(greenhouse_submit_operator, "_one_row", fake_one_row)
    monkeypatch.setattr(
        greenhouse_submit_operator,
        "greenhouse_submit_reserve_and_enqueue_statement",
        lambda **kwargs: sa.text(
            """
            SELECT *
            FROM careerops.greenhouse_submit_reserve_and_enqueue(
                :owner_user_id, :account_id, :campaign_id, :grant_version_id,
                :authorization_id, :action_intent_id, :payload_version_id,
                :payload_hash, :material_hash, :submission_identity_sha256,
                :board_token_sha256, :job_id_sha256, :schema_sha256,
                :approval_request_id, :review_evidence_sha256,
                :review_snapshot_sha256, :reviewed_by_user_id,
                :release_qualification_id, :reservation_key,
                :reconciliation_key, :event_key, :idempotency_key, :trace_id
            )
            """
        ).bindparams(**kwargs),
    )
    provider = _provider()

    response = await provider.reserve_intent(
        actor_id=ACTOR_ID,
        account_id=ACCOUNT_ID,
        command_id=COMMAND_ID,
        campaign_id=CAMPAIGN_ID,
        grant_version_id=GRANT_VERSION_ID,
        authorization_id=AUTHORIZATION_ID,
        action_intent_id=ACTION_INTENT_ID,
        payload_version_id=PAYLOAD_VERSION_ID,
        payload_hash=SHA256_A,
        material_hash=SHA256_E,
        submission_identity_sha256=SHA256_A,
        board_token_sha256=SHA256_B,
        job_id_sha256=SHA256_C,
        schema_sha256=SHA256_D,
        approval_request_id=APPROVAL_REQUEST_ID,
        review_evidence_sha256=SHA256_E,
        review_snapshot_sha256=SHA256_A,
        reviewed_by_user_id=ACTOR_ID,
        release_qualification_id=RELEASE_QUALIFICATION_ID,
        reservation_key="reservation-1",
        reconciliation_key="reconciliation-1",
        now=NOW,
    )

    assert response.event_key == "greenhouse-submit:reservation-1"
    assert "greenhouse_submit_reserve_and_enqueue" in str(captured[0])


def test_reconciliation_review_uses_two_stage_review_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    def fake_one_row(_connection: Connection, statement: object) -> Mapping[str, object]:
        captured.append(statement)
        return {"receipt_state": "created"}

    monkeypatch.setattr(greenhouse_submit_operator, "_one_row", fake_one_row)
    monkeypatch.setattr(
        greenhouse_submit_operator,
        "review_greenhouse_submit_reconciliation_evidence_statement",
        lambda **kwargs: sa.text(
            """
            SELECT *
            FROM careerops.review_greenhouse_submit_reconciliation_evidence(
                :owner_user_id, :event_id, :evidence_review_id,
                :evidence_sha256, :reviewed_evidence_source,
                :reviewed_observed_status, :decision,
                :reviewed_employer_authorization_evidence_sha256,
                :reviewed_by_user_id, :review_snapshot_sha256, :reason,
                :idempotency_key, :trace_id
            )
            """
        ).bindparams(**kwargs),
    )
    provider = _provider()

    provider._run_sync(
        lambda connection: fake_one_row(
            connection,
            cast(
                "Callable[..., sa.TextClause]",
                greenhouse_submit_operator.review_greenhouse_submit_reconciliation_evidence_statement,
            )(
                owner_user_id=ACTOR_ID,
                event_id=OUTBOX_EVENT_ID,
                evidence_review_id=AUTHORIZATION_ID,
                evidence_sha256=SHA256_A,
                reviewed_evidence_source="employer_admin",
                reviewed_observed_status="accepted_unverified",
                decision="confirmed",
                reviewed_employer_authorization_evidence_sha256=SHA256_C,
                reviewed_by_user_id=ACTOR_ID,
                review_snapshot_sha256=SHA256_B,
                reason="confirmed in employer system",
                idempotency_key=str(COMMAND_ID),
                trace_id=f"greenhouse-submit-command:{COMMAND_ID}",
            ),
        )
    )

    assert "review_greenhouse_submit_reconciliation_evidence" in str(captured[0])
    params = cast("sa.TextClause", captured[0]).compile().params
    assert params["decision"] == "confirmed"
    assert params["event_id"] == OUTBOX_EVENT_ID
    assert params["reviewed_evidence_source"] == "employer_admin"
    assert params["reviewed_observed_status"] == "accepted_unverified"


def test_runtime_provider_does_not_mask_programming_or_row_shape_errors() -> None:
    provider = _provider()

    def invalid_row_shape(_connection: Connection) -> None:
        raise KeyError("account_subject")

    with pytest.raises(KeyError, match="account_subject"):
        provider._run_sync(invalid_row_shape)


def test_runtime_provider_does_not_mask_unknown_database_programming_errors() -> None:
    provider = _provider()

    class UndefinedColumnError(Exception):
        sqlstate = "42703"

    error = DBAPIError("SELECT broken_column", {}, UndefinedColumnError(), False)

    def programming_error(_connection: Connection) -> None:
        raise error

    with pytest.raises(DBAPIError) as caught:
        provider._run_sync(programming_error)

    assert caught.value is error


def test_runtime_provider_translates_invalidated_database_connections() -> None:
    provider = _provider()
    error = DBAPIError(
        "SELECT 1",
        {},
        ConnectionError("closed"),
        connection_invalidated=True,
    )

    def unavailable_database(_connection: Connection) -> None:
        raise error

    with pytest.raises(GreenhouseSubmitUnavailable):
        provider._run_sync(unavailable_database)


def _provider(
    *,
    client: RecordingJobBoardClient | None = None,
) -> greenhouse_submit_operator.RuntimeGreenhouseSubmitOperatorProvider:
    return greenhouse_submit_operator.RuntimeGreenhouseSubmitOperatorProvider(
        transaction_factory=lambda: nullcontext(cast("Connection", object())),
        job_board_client=client or RecordingJobBoardClient(_schema()),
    )


def _schema(*, include_website: bool = False) -> GreenhouseJobSchemaSnapshot:
    questions: list[dict[str, object]] = [
        _question("First name", True, "first_name"),
        _question("Last name", True, "last_name"),
        _question("Email", True, "email"),
        _question("Resume", False, "resume", kind="input_file"),
    ]
    if include_website:
        questions.append(_question("Website", False, "website"))
    raw = json.dumps(
        {
            "id": 123456,
            "internal_job_id": 987654,
            "title": "Senior Agent Engineer",
            "company_name": "Example AI",
            "updated_at": "2026-07-21T08:00:00Z",
            "application_deadline": None,
            "absolute_url": "https://job-boards.greenhouse.io/example/jobs/123456",
            "questions": questions,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return GreenhouseJobSchemaSnapshot.from_json_bytes(
        target=GreenhouseTarget(board_token="example", job_id=123456),
        raw_response=raw,
    )


def _question(
    label: str,
    required: bool,
    name: str,
    *,
    kind: str = "input_text",
) -> dict[str, object]:
    return {"label": label, "required": required, "fields": [{"name": name, "type": kind}]}


def _attachment() -> Mapping[str, object]:
    return {
        "field_name": "resume",
        "object_key": "materials/resume.pdf",
        "filename": "resume.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1024,
        "sha256": SHA256_C,
    }


def _account_row() -> Mapping[str, object]:
    return {
        "account_id": ACCOUNT_ID,
        "candidate_id": CANDIDATE_ID,
        "employer_id": "example-ai",
        "board_token_sha256": SHA256_B,
        "account_subject": "candidate@example.com",
        "status": "active",
        "credential_profile_id": "profile-v1",
        "credential_profile_version": 1,
        "credential_fingerprint_sha256": SHA256_A,
        "credential_profile_status": "active",
        "credential_profile_expires_at": EXPIRES_AT,
        "employer_authorization_evidence_sha256": SHA256_B,
        "daily_submit_limit": 25,
        "updated_at": NOW,
    }
