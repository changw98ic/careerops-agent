from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import socket
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from types import ModuleType
from typing import Any, cast
from uuid import UUID

import pytest

from careerops.application.greenhouse_submit import (
    GREENHOUSE_BOARD_API_HOST,
    GREENHOUSE_BOARD_API_ORIGIN,
    GreenhouseAttachmentRef,
    GreenhouseHardStopCategory,
    GreenhouseJobSchemaSnapshot,
    GreenhouseSchemaError,
    GreenhouseSubmissionPayload,
    GreenhouseTarget,
    qualify_greenhouse_submission,
)
from careerops.infrastructure.greenhouse import (
    GREENHOUSE_CREDENTIAL_KIND,
    GreenhouseResolvedAttachment,
    GreenhouseSubmissionOutcome,
    UnixGreenhouseSubmissionBroker,
)

NOW = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
OWNER_ID = UUID("00000000-0000-0000-0000-000000000111")
BASE_HASH = "a" * 64
SECOND_HASH = "b" * 64
THIRD_HASH = "c" * 64
GREENHOUSE_SUBMIT_OPERATION = "submit_application"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CREDENTIALS_MODULE = "_careerops_greenhouse_credentials_under_test"


def load_credentials_module() -> ModuleType:
    path = _REPO_ROOT / "src/careerops/infrastructure/greenhouse/credentials.py"
    spec = importlib.util.spec_from_file_location(_CREDENTIALS_MODULE, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


greenhouse_credentials = load_credentials_module()
GreenhouseCredentialHandle = greenhouse_credentials.GreenhouseCredentialHandle


def schema_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": 123456,
        "internal_job_id": 987654,
        "title": "Senior Agent Engineer",
        "company_name": "Example AI",
        "updated_at": "2026-07-21T08:00:00Z",
        "application_deadline": None,
        "absolute_url": "https://job-boards.greenhouse.io/example/jobs/123456",
        "questions": [
            question("First name", True, "first_name"),
            question("Last name", True, "last_name"),
            question("Email", True, "email"),
            question("Resume", True, "resume", kind="input_file"),
        ],
    }
    payload.update(overrides)
    return payload


def question(
    label: str,
    required: bool,
    name: str,
    *,
    kind: str = "input_text",
    values: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    field: dict[str, object] = {"name": name, "type": kind}
    if values is not None:
        field["values"] = values
    return {"label": label, "required": required, "fields": [field]}


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def contains_key(value: object, key_name: str) -> bool:
    if isinstance(value, Mapping):
        return key_name in value or any(contains_key(item, key_name) for item in value.values())
    if isinstance(value, list):
        return any(contains_key(item, key_name) for item in value)
    return False


def target(*, board_token: str = "example", job_id: int = 123456) -> GreenhouseTarget:
    return GreenhouseTarget(board_token=board_token, job_id=job_id)


def schema(
    *,
    payload: Mapping[str, object] | None = None,
) -> GreenhouseJobSchemaSnapshot:
    raw = canonical_json_bytes(payload or schema_payload())
    return GreenhouseJobSchemaSnapshot.from_json_bytes(
        target=target(),
        raw_response=raw,
    )


def attachment(*, sha256: str = SECOND_HASH, size_bytes: int = 1024) -> GreenhouseAttachmentRef:
    return GreenhouseAttachmentRef(
        field_name="resume",
        object_key=f"materials/resume/{sha256}.pdf",
        filename="resume.pdf",
        content_type="application/pdf",
        size_bytes=size_bytes,
        sha256=sha256,
    )


def submission_payload(
    *,
    item_schema: GreenhouseJobSchemaSnapshot | None = None,
    fields: Mapping[str, object] | None = None,
    attachment_sha256: str = SECOND_HASH,
    attachment_size_bytes: int = 1024,
    approved_material_hashes: tuple[str, ...] = (BASE_HASH,),
) -> GreenhouseSubmissionPayload:
    item_schema = item_schema or schema()
    return GreenhouseSubmissionPayload(
        target=item_schema.target,
        schema_hash=item_schema.schema_hash,
        fields=fields
        or {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
        },
        source_draft_payload_hash=THIRD_HASH,
        approved_material_hashes=approved_material_hashes,
        attachment_refs=(attachment(sha256=attachment_sha256, size_bytes=attachment_size_bytes),),
    )


def handle(**overrides: object) -> Any:
    values: dict[str, object] = {
        "opaque_handle": "vault://greenhouse/example/profile-v1",
        "owner_user_id": OWNER_ID,
        "employer_key": "example-ai",
        "board_token": "example",
        "credential_profile_version": "profile-v1",
        "credential_profile_hash": BASE_HASH,
        "profile_status": "active",
        "profile_expires_at": NOW + timedelta(days=1),
        "allowed_operations": (GREENHOUSE_SUBMIT_OPERATION,),
    }
    values.update(overrides)
    return cast("Any", GreenhouseCredentialHandle)(**values)


def test_target_uses_fixed_job_board_origin_without_redirect_or_proxy_surface() -> None:
    item = target(board_token="acme_jobs", job_id=998877)

    assert item.endpoint == "https://boards-api.greenhouse.io/v1/boards/acme_jobs/jobs/998877"
    assert item.schema_endpoint == f"{item.endpoint}?questions=true"
    assert item.canonical() == {
        "host": GREENHOUSE_BOARD_API_HOST,
        "board_token": "acme_jobs",
        "job_id": 998877,
    }
    assert GREENHOUSE_BOARD_API_ORIGIN == "https://boards-api.greenhouse.io"
    assert "redirect" not in item.canonical()
    assert "proxy" not in item.canonical()


def test_schema_hash_binds_raw_full_schema_internal_job_id_and_updated_at() -> None:
    baseline_payload = schema_payload()
    baseline = schema(payload=baseline_payload)
    changed_updated_at = schema(payload=schema_payload(updated_at="2026-07-21T08:00:01Z"))
    changed_internal_job = schema(payload=schema_payload(internal_job_id=987655))

    assert (
        baseline.raw_response_sha256
        == hashlib.sha256(canonical_json_bytes(baseline_payload)).hexdigest()
    )
    assert baseline.internal_job_id == 987654
    assert baseline.updated_at == "2026-07-21T08:00:00Z"
    assert baseline.schema_hash != changed_updated_at.schema_hash
    assert baseline.schema_hash != changed_internal_job.schema_hash


def test_schema_rejects_target_job_mismatch_before_review_binding() -> None:
    raw = canonical_json_bytes(schema_payload(id=654321))

    with pytest.raises(GreenhouseSchemaError, match="job id"):
        GreenhouseJobSchemaSnapshot.from_json_bytes(target=target(), raw_response=raw)


@pytest.mark.parametrize(
    ("payload", "expected_stop"),
    [
        (
            schema_payload(
                questions=[
                    question("First name", True, "first_name"),
                    question("Last name", True, "last_name"),
                    question("Email", True, "email"),
                    question("Resume", True, "resume", kind="input_file"),
                    question("LinkedIn website", False, "website"),
                ]
            ),
            GreenhouseHardStopCategory.UNKNOWN_FIELD,
        ),
        (
            schema_payload(
                questions=[
                    question("First name", True, "first_name"),
                    question("Last name", True, "last_name"),
                    question("Email", True, "email"),
                    question("Resume", True, "resume", kind="input_file"),
                    question("Custom question", False, "question_123"),
                ]
            ),
            GreenhouseHardStopCategory.UNKNOWN_FIELD,
        ),
        (
            schema_payload(
                location_questions=[question("Location", True, "location")],
            ),
            GreenhouseHardStopCategory.LOCATION,
        ),
    ],
)
def test_question_location_and_website_fields_default_to_hard_stop(
    payload: Mapping[str, object],
    expected_stop: GreenhouseHardStopCategory,
) -> None:
    result = schema(payload=payload)

    assert expected_stop in result.hard_stop_categories
    assert result.can_submit is False


def test_custom_question_allowlisting_is_not_available_in_greenhouse_v1() -> None:
    item = schema(
        payload=schema_payload(
            questions=[
                question("First name", True, "first_name"),
                question("Last name", True, "last_name"),
                question("Email", True, "email"),
                question("Resume", True, "resume", kind="input_file"),
                question("Portfolio question", False, "question_123"),
            ]
        )
    )

    assert item.can_submit is False
    assert GreenhouseHardStopCategory.UNKNOWN_FIELD in item.hard_stop_categories
    assert "question_123" not in item.allowed_field_names
    with pytest.raises(TypeError, match="allowed_custom_fields"):
        GreenhouseJobSchemaSnapshot.from_json_bytes(
            target=target(),
            raw_response=canonical_json_bytes(schema_payload()),
            allowed_custom_fields=("question_123",),  # type: ignore[call-arg]
        )


def test_payload_rehashes_attachments_and_each_material_sha_into_exact_identity() -> None:
    baseline = submission_payload(
        attachment_sha256=SECOND_HASH,
        approved_material_hashes=(BASE_HASH, THIRD_HASH),
    )
    changed_attachment = submission_payload(
        attachment_sha256="d" * 64,
        approved_material_hashes=(BASE_HASH, THIRD_HASH),
    )
    changed_material = submission_payload(
        attachment_sha256=SECOND_HASH,
        approved_material_hashes=(BASE_HASH,),
    )

    assert baseline.material_hash != changed_attachment.material_hash
    assert baseline.payload_hash != changed_attachment.payload_hash
    assert baseline.submission_identity != changed_attachment.submission_identity
    assert baseline.material_hash != changed_material.material_hash
    assert baseline.approved_material_hashes == (BASE_HASH, THIRD_HASH)


def test_payload_rejects_duplicate_material_authorization_hashes() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        submission_payload(approved_material_hashes=(BASE_HASH, BASE_HASH))


def test_qualification_stops_on_schema_target_payload_or_required_field_mismatch() -> None:
    item_schema = schema()
    target_mismatch = GreenhouseSubmissionPayload(
        target=target(board_token="other"),
        schema_hash=item_schema.schema_hash,
        fields={"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"},
        source_draft_payload_hash=THIRD_HASH,
        approved_material_hashes=(BASE_HASH,),
        attachment_refs=(attachment(),),
    )
    schema_mismatch = GreenhouseSubmissionPayload(
        target=item_schema.target,
        schema_hash="d" * 64,
        fields={"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"},
        source_draft_payload_hash=THIRD_HASH,
        approved_material_hashes=(BASE_HASH,),
        attachment_refs=(attachment(),),
    )
    missing_required = submission_payload(
        item_schema=item_schema,
        fields={"first_name": "Ada", "email": "ada@example.com"},
    )

    assert (
        "GREENHOUSE_TARGET_MISMATCH"
        in qualify_greenhouse_submission(schema=item_schema, payload=target_mismatch).reason_codes
    )
    assert (
        "GREENHOUSE_SCHEMA_HASH_MISMATCH"
        in qualify_greenhouse_submission(schema=item_schema, payload=schema_mismatch).reason_codes
    )
    assert (
        "GREENHOUSE_REQUIRED_FIELD_MISSING"
        in qualify_greenhouse_submission(schema=item_schema, payload=missing_required).reason_codes
    )


def test_credential_handle_requires_opaque_broker_submit_scope_only() -> None:
    item = handle(allowed_operations=(" submit_application ", "submit_application"))

    assert item.opaque_handle == "vault://greenhouse/example/profile-v1"
    assert item.allowed_operations == (GREENHOUSE_SUBMIT_OPERATION,)
    assert item.canonical_binding() == {
        "credential_kind": GREENHOUSE_CREDENTIAL_KIND,
        "opaque_handle": "vault://greenhouse/example/profile-v1",
        "owner_user_id": str(OWNER_ID),
        "employer_key": "example-ai",
        "board_token": "example",
        "credential_profile_version": "profile-v1",
        "credential_profile_hash": BASE_HASH,
        "profile_status": "active",
        "profile_expires_at": (NOW + timedelta(days=1)).isoformat(),
        "allowed_operations": [GREENHOUSE_SUBMIT_OPERATION],
    }
    with pytest.raises(ValueError, match="exactly submit_application"):
        handle(allowed_operations=("submit_application", "read_applications"))
    with pytest.raises(ValueError, match="opaque_handle"):
        handle(opaque_handle="raw api key with spaces")


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"profile_status": "revoked"}, "credential profile must be active"),
        ({"profile_status": "disabled"}, "credential profile must be active"),
        ({"profile_expires_at": datetime(2026, 7, 21, 9, 0)}, "timezone-aware"),
        ({"credential_profile_hash": "not-a-hash"}, "credential_profile_hash"),
        ({"board_token": "internal"}, "board_token"),
        ({"credential_profile_version": "bad profile version"}, "credential_profile_version"),
    ],
)
def test_credential_handle_rejects_status_expiry_board_profile_or_hash_mismatch(
    override: Mapping[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        handle(**dict(override))


def test_credential_handle_tracks_expiry_without_containing_provider_secret() -> None:
    expired = handle(profile_expires_at=NOW - timedelta(seconds=1))
    current = handle(profile_expires_at=NOW + timedelta(seconds=1))

    assert expired.is_expired(now=NOW)
    assert not current.is_expired(now=NOW)
    assert "api_key" not in expired.canonical_binding()
    assert "secret" not in repr(expired).casefold()
    assert "opaque_handle=<redacted>" in repr(expired)


def test_job_board_post_client_contract_exists_for_unverified_result_semantics() -> None:
    greenhouse_infrastructure = importlib.import_module("careerops.infrastructure.greenhouse")
    missing = [
        name
        for name in (
            "GreenhouseJobBoardHttpClient",
            "GreenhouseSubmissionEvidence",
            "GreenhouseSubmissionOutcome",
        )
        if not hasattr(greenhouse_infrastructure, name)
    ]

    assert not missing, (
        "Greenhouse infrastructure must expose a real Job Board POST client contract "
        "with accepted_unverified, reconciliation_required, ambiguous, and no-retry "
        f"result semantics; missing: {missing}"
    )


def resolved_attachment() -> GreenhouseResolvedAttachment:
    data = b"resume bytes"
    return GreenhouseResolvedAttachment(
        ref=attachment(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)),
        data=data,
    )


def run_one_shot_broker(
    tmp_path: Path,
    response_factory: Any,
) -> tuple[Path, list[dict[str, object]], Thread]:
    socket_path = Path(f"/tmp/gh-{os.getpid()}-{len(str(tmp_path))}.sock")
    socket_path.unlink(missing_ok=True)
    captured: list[dict[str, object]] = []
    ready = Event()

    def serve() -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(socket_path))
                server.listen(1)
                ready.set()
                connection, _ = server.accept()
                with connection:
                    request_body = b""
                    while True:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        request_body += chunk
                    request = json.loads(request_body.decode("utf-8"))
                    assert isinstance(request, dict)
                    captured.append(cast("dict[str, object]", request))
                    response = response_factory(request, request_body)
                    connection.sendall(response)
        finally:
            socket_path.unlink(missing_ok=True)

    thread = Thread(target=serve)
    thread.start()
    assert ready.wait(timeout=5)
    return socket_path, captured, thread


def submit_via_broker(socket_path: Path) -> Any:
    item_schema = schema()
    item_attachment = resolved_attachment()
    item_payload = submission_payload(
        item_schema=item_schema,
        attachment_sha256=item_attachment.ref.sha256,
        attachment_size_bytes=item_attachment.ref.size_bytes,
    )
    return UnixGreenhouseSubmissionBroker(
        socket_path=socket_path,
        now=lambda: NOW,
    ).submit(
        handle(),
        schema=item_schema,
        payload=item_payload,
        attachments=(item_attachment,),
        reconciliation_key="greenhouse-reconcile-123",
    )


def test_submission_broker_rpc_sends_secret_free_exact_binding(tmp_path: Path) -> None:
    def response_factory(request: Mapping[str, object], request_body: bytes) -> bytes:
        submission = cast("Mapping[str, object]", request["submission"])
        expected_schema = cast("Mapping[str, object]", request["expected_schema"])
        return canonical_json_bytes(
            {
                "outcome": "accepted_unverified",
                "reason_code": "GREENHOUSE_SUBMISSION_ACCEPTED_UNVERIFIED",
                "parser_version": "greenhouse-job-board-response.v1",
                "submission_identity": submission["submission_identity"],
                "reconciliation_key": submission["reconciliation_key"],
                "broker_request_sha256": hashlib.sha256(request_body).hexdigest(),
                "expected_schema_hash": expected_schema["schema_hash"],
                "payload_hash": submission["payload_hash"],
                "material_hash": submission["material_hash"],
                "journal_committed": True,
                "journal_state": "response_observed",
                "journal_sequence": 1,
                "next_attempt_allowed": False,
                "reconciliation_required": True,
                "status_code": 200,
                "journal_receipt_hash": "d" * 64,
                "provider_request_sha256": "e" * 64,
                "provider_response_sha256": "f" * 64,
                "observed_schema_hash": expected_schema["schema_hash"],
                "observed_schema_raw_response_sha256": expected_schema["raw_response_sha256"],
            }
        )

    socket_path, captured, thread = run_one_shot_broker(tmp_path, response_factory)

    evidence = submit_via_broker(socket_path)
    thread.join(timeout=5)

    assert evidence.outcome is GreenhouseSubmissionOutcome.ACCEPTED_UNVERIFIED
    assert evidence.reconciliation_required
    assert evidence.next_attempt_allowed is False
    assert evidence.journal_committed
    request = captured[0]
    assert not contains_key(request, "api_key")
    assert not contains_key(request, "Authorization")
    assert not contains_key(request, "authorization")
    assert request["operation"] == "submit_application_once"
    assert request["destination"] == target().canonical()
    credential = cast("Mapping[str, object]", request["credential"])
    assert credential["opaque_handle"] == "vault://greenhouse/example/profile-v1"
    assert credential["owner_user_id"] == str(OWNER_ID)
    assert credential["employer_key"] == "example-ai"
    assert credential["board_token"] == "example"
    assert credential["credential_profile_hash"] == BASE_HASH
    submission = cast("Mapping[str, object]", request["submission"])
    assert submission["approved_material_hashes"] == [BASE_HASH]


def test_submission_broker_malformed_post_connect_response_is_ambiguous_no_retry(
    tmp_path: Path,
) -> None:
    socket_path, _captured, thread = run_one_shot_broker(
        tmp_path,
        lambda _request, _request_body: b"not-json",
    )

    evidence = submit_via_broker(socket_path)
    thread.join(timeout=5)

    assert evidence.outcome is GreenhouseSubmissionOutcome.AMBIGUOUS
    assert evidence.reason_code == "GREENHOUSE_BROKER_RESPONSE_INVALID_AMBIGUOUS"
    assert evidence.reconciliation_required
    assert evidence.next_attempt_allowed is False
    assert not evidence.journal_committed
