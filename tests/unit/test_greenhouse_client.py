from __future__ import annotations

import hashlib
import json
import socket
import threading
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import cast
from urllib.request import ProxyHandler, Request
from uuid import UUID, uuid4

import pytest

from careerops.application.greenhouse_submit import (
    GreenhouseAttachmentRef,
    GreenhouseJobSchemaSnapshot,
    GreenhouseSubmissionPayload,
    GreenhouseTarget,
)
from careerops.infrastructure.greenhouse import client as client_module
from careerops.infrastructure.greenhouse.client import (
    GreenhouseBrokerJournalState,
    GreenhouseBrokerPrePostUnavailable,
    GreenhouseJobBoardHttpClient,
    GreenhouseResolvedAttachment,
    GreenhouseSubmissionOutcome,
    UnixGreenhouseSubmissionBroker,
)
from careerops.infrastructure.greenhouse.credentials import (
    GREENHOUSE_SUBMIT_OPERATION,
    GreenhouseCredentialHandle,
)

NOW = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)
TARGET = GreenhouseTarget(board_token="acme", job_id=12345)
OWNER_ID = UUID("00000000-0000-0000-0000-000000000123")
RESUME_BYTES = b"reviewed resume bytes"


@pytest.fixture
def socket_path_factory() -> Iterator[Callable[[str], Path]]:
    paths: list[Path] = []

    def create(label: str) -> Path:
        path = Path("/tmp") / f"gh-{label}-{uuid4().hex}.sock"
        paths.append(path)
        return path

    yield create
    for path in paths:
        path.unlink(missing_ok=True)


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.status = status
        self._body = body

    def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            body = self._body
            self._body = b""
            return body
        body = self._body[:amount]
        self._body = self._body[amount:]
        return body

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        return None


def schema_bytes() -> bytes:
    return json.dumps(
        {
            "id": TARGET.job_id,
            "internal_job_id": 777,
            "title": "Senior Engineer",
            "company_name": "Acme",
            "updated_at": "2026-07-20T12:00:00Z",
            "application_deadline": "2026-08-20T12:00:00Z",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
            "questions": [
                {
                    "label": "First Name",
                    "required": True,
                    "fields": [{"name": "first_name", "type": "input_text"}],
                },
                {
                    "label": "Last Name",
                    "required": True,
                    "fields": [{"name": "last_name", "type": "input_text"}],
                },
                {
                    "label": "Email",
                    "required": True,
                    "fields": [{"name": "email", "type": "input_text"}],
                },
                {
                    "label": "Resume",
                    "required": True,
                    "fields": [
                        {"name": "resume", "type": "input_file"},
                        {"name": "resume_text", "type": "textarea"},
                    ],
                },
            ],
            "location_questions": [],
            "compliance": [],
            "data_compliance": [],
        },
        sort_keys=True,
    ).encode("utf-8")


def schema() -> GreenhouseJobSchemaSnapshot:
    return GreenhouseJobSchemaSnapshot.from_json_bytes(
        target=TARGET,
        raw_response=schema_bytes(),
    )


def attachment() -> GreenhouseResolvedAttachment:
    ref = GreenhouseAttachmentRef(
        field_name="resume",
        object_key="approved:resume:v1",
        filename="resume.pdf",
        content_type="application/pdf",
        size_bytes=len(RESUME_BYTES),
        sha256=hashlib.sha256(RESUME_BYTES).hexdigest(),
    )
    return GreenhouseResolvedAttachment(ref=ref, data=RESUME_BYTES)


def payload(item_schema: GreenhouseJobSchemaSnapshot) -> GreenhouseSubmissionPayload:
    resolved = attachment()
    return GreenhouseSubmissionPayload(
        target=TARGET,
        schema_hash=item_schema.schema_hash,
        fields={
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@example.com",
        },
        source_draft_payload_hash="a" * 64,
        approved_material_hashes=(hashlib.sha256(RESUME_BYTES).hexdigest(), "b" * 64),
        attachment_refs=(resolved.ref,),
    )


def credential_handle() -> GreenhouseCredentialHandle:
    return GreenhouseCredentialHandle(
        opaque_handle="greenhouse:acme:job-board:v3",
        owner_user_id=OWNER_ID,
        employer_key="acme",
        board_token="acme",
        credential_profile_version="profile-v3",
        credential_profile_hash="c" * 64,
        profile_status="active",
        profile_expires_at=NOW + timedelta(minutes=10),
        allowed_operations=(GREENHOUSE_SUBMIT_OPERATION,),
    )


def valid_broker_response(
    request: Mapping[str, object],
    raw_request: bytes,
    *,
    outcome: str = "accepted_unverified",
    status_code: int = 200,
    journal_state: str = "response_observed",
) -> dict[str, object]:
    expected_schema = request["expected_schema"]
    submission = request["submission"]
    assert isinstance(expected_schema, dict)
    assert isinstance(submission, dict)
    reason_code = "GREENHOUSE_PROVIDER_RESPONSE_RECORDED"
    if outcome == "rejected":
        reason_code = {
            400: "GREENHOUSE_PROVIDER_VALIDATION_REJECTED",
            401: "GREENHOUSE_PROVIDER_AUTH_REJECTED",
            403: "GREENHOUSE_PROVIDER_AUTH_REJECTED",
            404: "GREENHOUSE_PROVIDER_JOB_NOT_FOUND",
            413: "GREENHOUSE_PROVIDER_REQUEST_TOO_LARGE",
            415: "GREENHOUSE_PROVIDER_MEDIA_TYPE_REJECTED",
            422: "GREENHOUSE_PROVIDER_VALIDATION_REJECTED",
        }.get(status_code, "GREENHOUSE_PROVIDER_REJECTED")
    return {
        "parser_version": "greenhouse-job-board-response.v1",
        "outcome": outcome,
        "reason_code": reason_code,
        "submission_identity": submission["submission_identity"],
        "reconciliation_key": submission["reconciliation_key"],
        "broker_request_sha256": hashlib.sha256(raw_request).hexdigest(),
        "expected_schema_hash": expected_schema["schema_hash"],
        "observed_schema_hash": expected_schema["schema_hash"],
        "observed_schema_raw_response_sha256": expected_schema["raw_response_sha256"],
        "payload_hash": submission["payload_hash"],
        "material_hash": submission["material_hash"],
        "journal_committed": True,
        "journal_receipt_hash": "d" * 64,
        "journal_state": journal_state,
        "journal_sequence": 4,
        "next_attempt_allowed": False,
        "reconciliation_required": outcome != "rejected",
        "status_code": status_code,
        "provider_request_sha256": "e" * 64,
        "provider_response_sha256": "f" * 64,
    }


def run_broker_server(
    socket_path: Path,
    response_factory: Callable[[Mapping[str, object], bytes], Mapping[str, object] | bytes],
    captured: dict[str, object],
) -> tuple[threading.Thread, threading.Event]:
    ready = threading.Event()

    def serve() -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)
        ready.set()
        connection, _ = server.accept()
        chunks: list[bytes] = []
        with connection, server:
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            raw_request = b"".join(chunks)
            decoded = json.loads(raw_request.decode("utf-8"))
            assert isinstance(decoded, dict)
            request = cast("Mapping[str, object]", decoded)
            captured["raw_request"] = raw_request
            captured["request"] = request
            response = response_factory(request, raw_request)
            response_bytes = (
                response if isinstance(response, bytes) else json.dumps(response).encode("utf-8")
            )
            connection.sendall(response_bytes)

    thread = threading.Thread(target=serve)
    thread.start()
    return thread, ready


def test_public_get_uses_exact_questions_endpoint_and_never_sends_auth() -> None:
    calls: list[Request] = []

    def fake_open(request: Request, timeout: float) -> FakeResponse:
        assert timeout == 3
        calls.append(request)
        return FakeResponse(schema_bytes())

    item_schema = GreenhouseJobBoardHttpClient(
        timeout_seconds=3,
        open_request=fake_open,
    ).fetch_job_schema(TARGET)

    request = calls[0]
    assert request.full_url == TARGET.schema_endpoint
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") is None
    assert item_schema.raw_response_sha256 == hashlib.sha256(schema_bytes()).hexdigest()


def test_default_public_transport_disables_proxies_and_redirects(monkeypatch) -> None:
    handlers: list[object] = []

    class FakeOpener:
        def open(self, request: Request, *, timeout: float) -> FakeResponse:
            assert request.full_url == TARGET.schema_endpoint
            assert timeout == 2
            return FakeResponse(schema_bytes())

    def fake_build_opener(*items: object) -> FakeOpener:
        handlers.extend(items)
        return FakeOpener()

    monkeypatch.setattr(client_module, "build_opener", fake_build_opener)

    response = client_module._open_fixed_origin_without_redirects(
        Request(TARGET.schema_endpoint, method="GET"),
        2,
    )

    assert response.status == 200
    proxy_handlers = [item for item in handlers if isinstance(item, ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert vars(proxy_handlers[0]).get("proxies") == {}
    assert any(type(item).__name__ == "_DenyRedirectHandler" for item in handlers)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/v1/boards/acme/jobs/12345?questions=true",
        "http://boards-api.greenhouse.io/v1/boards/acme/jobs/12345?questions=true",
        "https://boards-api.greenhouse.io:444/v1/boards/acme/jobs/12345?questions=true",
        "https://boards-api.greenhouse.io/v1/boards/acme%252fjobs/12345?questions=true",
    ],
)
def test_public_transport_rejects_dynamic_origin_port_scheme_or_encoding(url: str) -> None:
    with pytest.raises(Exception, match="GREENHOUSE_DYNAMIC_ORIGIN_REJECTED"):
        client_module._open_fixed_origin_without_redirects(Request(url, method="GET"), 2)


def test_submission_rpc_is_secret_free_and_returns_only_accepted_unverified(
    socket_path_factory: Callable[[str], Path],
) -> None:
    socket_path = socket_path_factory("accepted")
    captured: dict[str, object] = {}
    thread, ready = run_broker_server(socket_path, valid_broker_response, captured)
    assert ready.wait(timeout=2)
    item_schema = schema()
    item_payload = payload(item_schema)
    resolved = attachment()

    evidence = UnixGreenhouseSubmissionBroker(
        socket_path=socket_path,
        now=lambda: NOW,
    ).submit(
        credential_handle(),
        schema=item_schema,
        payload=item_payload,
        attachments=(resolved,),
        reconciliation_key="greenhouse-reconcile-123",
    )
    thread.join(timeout=2)

    assert not thread.is_alive()
    raw_request = cast("bytes", captured["raw_request"])
    rendered = raw_request.decode("utf-8").casefold()
    assert "api_key" not in rendered
    assert "authorization" not in rendered
    assert "basic " not in rendered
    request = cast("Mapping[str, object]", captured["request"])
    assert request["operation"] == "submit_application_once"
    assert request["destination"] == TARGET.canonical()
    assert evidence.outcome is GreenhouseSubmissionOutcome.ACCEPTED_UNVERIFIED
    assert evidence.reconciliation_required is True
    assert evidence.next_attempt_allowed is False
    assert evidence.journal_committed is True
    assert evidence.journal_state is GreenhouseBrokerJournalState.RESPONSE_OBSERVED
    assert evidence.journal_sequence == 4
    assert evidence.observed_schema_hash == item_schema.schema_hash


@pytest.mark.parametrize(
    "mutate",
    [
        lambda response: response.update(outcome="confirmed"),
        lambda response: response.update(observed_schema_hash="0" * 64),
        lambda response: response.update(journal_sequence=0),
        lambda response: response.update(next_attempt_allowed=True),
        lambda response: response.update(provider_response_sha256=None),
        lambda response: response.update(parser_version="unknown-parser"),
    ],
)
def test_malformed_or_unbound_broker_response_is_ambiguous_and_never_retryable(
    socket_path_factory: Callable[[str], Path],
    mutate: Callable[[dict[str, object]], None],
) -> None:
    socket_path = socket_path_factory("malformed")
    captured: dict[str, object] = {}

    def response_factory(request: Mapping[str, object], raw: bytes) -> Mapping[str, object]:
        response = valid_broker_response(request, raw)
        mutate(response)
        return response

    thread, ready = run_broker_server(socket_path, response_factory, captured)
    assert ready.wait(timeout=2)
    item_schema = schema()

    evidence = UnixGreenhouseSubmissionBroker(
        socket_path=socket_path,
        now=lambda: NOW,
    ).submit(
        credential_handle(),
        schema=item_schema,
        payload=payload(item_schema),
        attachments=(attachment(),),
        reconciliation_key="greenhouse-reconcile-123",
    )
    thread.join(timeout=2)

    assert evidence.outcome is GreenhouseSubmissionOutcome.AMBIGUOUS
    assert evidence.next_attempt_allowed is False
    assert evidence.journal_committed is False
    assert evidence.reason_code == "GREENHOUSE_BROKER_RESPONSE_INVALID_AMBIGUOUS"


def test_only_deterministic_bounded_4xx_can_be_rejected(
    socket_path_factory: Callable[[str], Path],
) -> None:
    def submit_with_status(status_code: int) -> GreenhouseSubmissionOutcome:
        socket_path = socket_path_factory(str(status_code))
        captured: dict[str, object] = {}

        def response_factory(request: Mapping[str, object], raw: bytes) -> Mapping[str, object]:
            return valid_broker_response(
                request,
                raw,
                outcome="rejected",
                status_code=status_code,
            )

        thread, ready = run_broker_server(socket_path, response_factory, captured)
        assert ready.wait(timeout=2)
        item_schema = schema()
        evidence = UnixGreenhouseSubmissionBroker(
            socket_path=socket_path,
            now=lambda: NOW,
        ).submit(
            credential_handle(),
            schema=item_schema,
            payload=payload(item_schema),
            attachments=(attachment(),),
            reconciliation_key=f"greenhouse-reconcile-{status_code}",
        )
        thread.join(timeout=2)
        return evidence.outcome

    assert submit_with_status(422) is GreenhouseSubmissionOutcome.REJECTED
    assert submit_with_status(429) is GreenhouseSubmissionOutcome.AMBIGUOUS
    assert submit_with_status(500) is GreenhouseSubmissionOutcome.AMBIGUOUS


def test_connect_failure_before_any_socket_byte_is_typed_retryable(tmp_path: Path) -> None:
    item_schema = schema()
    broker = UnixGreenhouseSubmissionBroker(
        socket_path=tmp_path / "missing.sock",
        now=lambda: NOW,
    )

    with pytest.raises(GreenhouseBrokerPrePostUnavailable) as raised:
        broker.submit(
            credential_handle(),
            schema=item_schema,
            payload=payload(item_schema),
            attachments=(attachment(),),
            reconciliation_key="greenhouse-reconcile-123",
        )

    assert raised.value.retryable is True
    assert raised.value.request_may_have_reached_broker is False


def test_post_connect_send_or_receive_failure_is_ambiguous(monkeypatch, tmp_path: Path) -> None:
    class FailingSocket:
        def settimeout(self, timeout: float) -> None:
            assert timeout == 30

        def connect(self, path: str) -> None:
            assert path.endswith("broker.sock")

        def sendall(self, data: bytes) -> None:
            assert data
            raise ConnectionResetError("post-connect failure")

        def close(self) -> None:
            return None

        def __enter__(self) -> FailingSocket:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_socket(*args: object, **kwargs: object) -> FailingSocket:
        del args, kwargs
        return FailingSocket()

    monkeypatch.setattr(client_module.socket, "socket", fake_socket)
    item_schema = schema()

    evidence = UnixGreenhouseSubmissionBroker(
        socket_path=tmp_path / "broker.sock",
        now=lambda: NOW,
    ).submit(
        credential_handle(),
        schema=item_schema,
        payload=payload(item_schema),
        attachments=(attachment(),),
        reconciliation_key="greenhouse-reconcile-123",
    )

    assert evidence.outcome is GreenhouseSubmissionOutcome.AMBIGUOUS
    assert evidence.post_may_have_started is True
    assert evidence.next_attempt_allowed is False


def test_resolved_attachment_rechecks_bytes_before_broker_rpc() -> None:
    ref = attachment().ref

    with pytest.raises(ValueError, match="sha256"):
        GreenhouseResolvedAttachment(ref=ref, data=b"tampered resume bytes")
