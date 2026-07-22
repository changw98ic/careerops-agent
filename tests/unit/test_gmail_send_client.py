from __future__ import annotations

import base64
import hashlib
import json
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import Message
from io import BytesIO
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from careerops.application.gmail_send import GmailSendAttachmentRef, GmailSendPayload
from careerops.infrastructure.gmail import send_client
from careerops.infrastructure.gmail.send_client import (
    GMAIL_SEND_API_URL,
    GmailSendApiError,
    GmailSendHttpClient,
    GmailSendResolvedAttachment,
)
from careerops.infrastructure.gmail.send_credentials import GMAIL_SEND_SCOPE, GmailSendAccessToken


class FakeResponse:
    def __init__(self, payload: dict[str, object], *, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            body = self._body
            self._body = b""
            return body
        body = self._body[:size]
        self._body = self._body[size:]
        return body

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class RawResponse(FakeResponse):
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.status = status
        self._body = body


def token() -> GmailSendAccessToken:
    return GmailSendAccessToken(
        access_token="ya29.send-only",
        account_subject="candidate@example.com",
        granted_scopes=(GMAIL_SEND_SCOPE,),
        expires_at=datetime(2100, 1, 1, tzinfo=UTC),
    )


def payload() -> GmailSendPayload:
    data = b"resume bytes"
    ref = GmailSendAttachmentRef(
        object_key="resume:approved:v1",
        filename="resume.txt",
        content_type="text/plain",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    return GmailSendPayload(
        sender="candidate@example.com",
        recipient="recruiter@example.com",
        subject="Reviewed follow-up",
        text_body="Hello from the reviewed payload.",
        attachment_refs=(ref,),
        thread_id="thread-123",
        in_reply_to_message_id="<previous@example.com>",
    )


def test_send_uses_fixed_gmail_endpoint_post_and_returns_only_provider_ids(monkeypatch) -> None:
    calls: list[Request] = []
    send_payload = payload()
    attachment = GmailSendResolvedAttachment(
        ref=send_payload.attachment_refs[0],
        data=b"resume bytes",
    )

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del timeout
        calls.append(cast("Request", request))
        return FakeResponse({"id": "gmail-msg-1", "threadId": "gmail-thread-1"})

    monkeypatch.setattr("careerops.infrastructure.gmail.send_client.urlopen", fake_urlopen)

    receipt = GmailSendHttpClient(timeout_seconds=3).send_message(
        token(),
        payload=send_payload,
        attachments=(attachment,),
    )

    request = calls[0]
    assert request.full_url == GMAIL_SEND_API_URL
    assert request.get_method() == "POST"
    assert request.headers["Authorization"] == "Bearer ya29.send-only"
    assert request.headers["Content-type"] == "application/json"
    body = json.loads(cast("bytes", request.data).decode("utf-8"))
    assert body["threadId"] == "thread-123"
    assert set(body) == {"raw", "threadId"}
    raw = str(body["raw"])
    padded = raw + ("=" * (-len(raw) % 4))
    parsed = message_from_bytes(base64.urlsafe_b64decode(padded))
    assert parsed["From"] == "candidate@example.com"
    assert parsed["To"] == "recruiter@example.com"
    assert parsed["Subject"] == "Reviewed follow-up"
    assert parsed["Message-ID"] == send_payload.message_id_header
    assert parsed["In-Reply-To"] == "<previous@example.com>"
    assert parsed["References"] == "<previous@example.com>"
    assert "Cc" not in parsed
    assert "Bcc" not in parsed
    assert receipt.provider_message_id == "gmail-msg-1"
    assert receipt.provider_thread_id == "gmail-thread-1"


def test_client_rechecks_resolved_attachment_bytes_before_send() -> None:
    send_payload = payload()

    with pytest.raises(ValueError, match="sha256"):
        GmailSendResolvedAttachment(ref=send_payload.attachment_refs[0], data=b"mutatedbytes")

    with pytest.raises(ValueError, match="exactly match"):
        GmailSendHttpClient().send_message(token(), payload=send_payload, attachments=())


def test_resolved_attachment_repr_hides_data_bytes() -> None:
    secret_data = b"PRIVATE-RESUME-BYTES-secret-data-2026"
    ref = GmailSendAttachmentRef(
        object_key="candidate-jane-doe-private-resume:v2",
        filename="Jane_Doe_candidate_private_resume.txt",
        content_type="text/plain",
        size_bytes=len(secret_data),
        sha256=hashlib.sha256(secret_data).hexdigest(),
    )
    attachment = GmailSendResolvedAttachment(ref=ref, data=secret_data)

    rendered = repr(attachment)

    assert "PRIVATE-RESUME-BYTES-secret-data-2026" not in rendered
    assert repr(secret_data) not in rendered
    assert "candidate-jane-doe-private-resume:v2" not in rendered
    assert "Jane_Doe_candidate_private_resume.txt" not in rendered
    assert "text/plain" in rendered
    assert f"size_bytes={len(secret_data)!r}" in rendered
    assert f"data_len={len(secret_data)!r}" in rendered
    assert ref.sha256 in rendered


def test_client_rejects_sender_token_mismatch_before_post(monkeypatch) -> None:
    def forbidden_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del request, timeout
        raise AssertionError("Gmail must not be called for a mismatched sender account")

    monkeypatch.setattr("careerops.infrastructure.gmail.send_client.urlopen", forbidden_urlopen)
    mismatched_token = GmailSendAccessToken(
        access_token="ya29.other-account",
        account_subject="other@example.com",
        granted_scopes=(GMAIL_SEND_SCOPE,),
        expires_at=datetime(2100, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(GmailSendApiError) as raised:
        GmailSendHttpClient().send_message(mismatched_token, payload=payload())

    assert raised.value.error_code == "GMAIL_SEND_ACCOUNT_SENDER_MISMATCH"
    assert raised.value.status_code == 403


def test_client_rejects_reordered_attachments_before_post(monkeypatch) -> None:
    first_data = b"first attachment"
    second_data = b"second attachment"
    refs = (
        GmailSendAttachmentRef(
            object_key="resume:approved:v1",
            filename="resume.txt",
            content_type="text/plain",
            size_bytes=len(first_data),
            sha256=hashlib.sha256(first_data).hexdigest(),
        ),
        GmailSendAttachmentRef(
            object_key="cover-letter:approved:v1",
            filename="cover-letter.txt",
            content_type="text/plain",
            size_bytes=len(second_data),
            sha256=hashlib.sha256(second_data).hexdigest(),
        ),
    )
    reviewed_payload = GmailSendPayload(
        sender="candidate@example.com",
        recipient="recruiter@example.com",
        subject="Reviewed application",
        text_body="Reviewed body",
        attachment_refs=refs,
    )
    resolved = (
        GmailSendResolvedAttachment(ref=refs[1], data=second_data),
        GmailSendResolvedAttachment(ref=refs[0], data=first_data),
    )

    def forbidden_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del request, timeout
        raise AssertionError("Gmail must not be called for reordered attachments")

    monkeypatch.setattr("careerops.infrastructure.gmail.send_client.urlopen", forbidden_urlopen)

    with pytest.raises(ValueError, match="exactly match"):
        GmailSendHttpClient().send_message(
            token(),
            payload=reviewed_payload,
            attachments=resolved,
        )


def test_client_fails_closed_before_post_when_message_or_attachment_boundary_exceeded(
    monkeypatch,
) -> None:
    send_payload = payload()
    attachment = GmailSendResolvedAttachment(
        ref=send_payload.attachment_refs[0],
        data=b"resume bytes",
    )

    def forbidden_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del request, timeout
        raise AssertionError("Gmail must not be called after size boundary failure")

    monkeypatch.setattr("careerops.infrastructure.gmail.send_client.urlopen", forbidden_urlopen)
    monkeypatch.setattr(send_client, "_MAX_RAW_ATTACHMENTS_BYTES", 1)
    with pytest.raises(GmailSendApiError, match="GMAIL_SEND_ATTACHMENTS_TOO_LARGE"):
        GmailSendHttpClient().send_message(
            token(),
            payload=send_payload,
            attachments=(attachment,),
        )

    monkeypatch.setattr(send_client, "_MAX_RAW_ATTACHMENTS_BYTES", 20 * 1024 * 1024)
    monkeypatch.setattr(send_client, "_MAX_MIME_BYTES", 10)
    with pytest.raises(GmailSendApiError, match="GMAIL_SEND_MESSAGE_TOO_LARGE"):
        GmailSendHttpClient().send_message(
            token(),
            payload=send_payload,
            attachments=(attachment,),
        )


@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
def test_http_errors_are_bounded_and_do_not_retain_sensitive_provider_data(
    monkeypatch,
    status_code: int,
) -> None:
    secret_body = b'{"error":"super-secret-send-body"}'
    sensitive_headers = Message()
    sensitive_headers["Authorization"] = "Bearer ya29.send-only"
    provider_errors: list[HTTPError] = []

    def raising_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del timeout
        provider_error = HTTPError(
            cast("Request", request).full_url,
            status_code,
            "provider reason secret",
            sensitive_headers,
            BytesIO(secret_body),
        )
        provider_errors.append(provider_error)
        raise provider_error

    monkeypatch.setattr("careerops.infrastructure.gmail.send_client.urlopen", raising_urlopen)

    with pytest.raises(GmailSendApiError) as raised:
        GmailSendHttpClient().send_message(
            token(),
            payload=GmailSendPayload(
                sender="candidate@example.com",
                recipient="recruiter@example.com",
                subject="Hi",
                text_body="Body",
            ),
        )

    error = raised.value
    assert error.error_code == f"GMAIL_SEND_HTTP_{status_code}"
    assert error.status_code == status_code
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    for secret in ("ya29.send-only", "super-secret-send-body", "provider reason secret"):
        assert secret not in rendered
    assert provider_errors[0].file is not None
    assert provider_errors[0].file.tell() == 0


def test_invalid_response_json_and_schema_are_machine_readable(monkeypatch) -> None:
    @contextmanager
    def invalid_json_urlopen(*args: object, **kwargs: object) -> Iterator[RawResponse]:
        del args, kwargs
        yield RawResponse(b'{"id":"private",')

    monkeypatch.setattr(
        "careerops.infrastructure.gmail.send_client.urlopen",
        invalid_json_urlopen,
    )

    with pytest.raises(GmailSendApiError) as raised:
        GmailSendHttpClient().send_message(
            token(),
            payload=GmailSendPayload(
                sender="candidate@example.com",
                recipient="recruiter@example.com",
                subject="Hi",
                text_body="Body",
            ),
        )

    assert raised.value.error_code == "GMAIL_SEND_INVALID_JSON"
    assert "private" not in "".join(traceback.format_exception(raised.value))

    def schema_urlopen(request: object, *, timeout: float) -> RawResponse:
        del request, timeout
        return RawResponse(b'{"id":"gmail-msg-1"}')

    monkeypatch.setattr("careerops.infrastructure.gmail.send_client.urlopen", schema_urlopen)
    with pytest.raises(GmailSendApiError, match="GMAIL_SEND_SCHEMA_MISMATCH"):
        GmailSendHttpClient().send_message(
            token(),
            payload=GmailSendPayload(
                sender="candidate@example.com",
                recipient="recruiter@example.com",
                subject="Hi",
                text_body="Body",
            ),
        )
