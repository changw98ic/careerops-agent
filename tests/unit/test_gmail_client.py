from __future__ import annotations

import base64
import hashlib
import json
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.message import EmailMessage, Message
from io import BytesIO
from typing import cast
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import pytest

from careerops.infrastructure.gmail.client import (
    GmailApiError,
    GmailHistoryCursorExpired,
    GmailMetadataMessage,
    GmailReadOnlyHttpClient,
)
from careerops.infrastructure.gmail.credentials import GMAIL_READONLY_SCOPE, GmailAccessToken

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
SMOKE_NONCE = "0123456789abcdef"
SMOKE_SUBJECT = f"CareerOps Gmail qualification {SMOKE_NONCE}"
SMOKE_RECIPIENT = f"candidate+careerops-smoke-{SMOKE_NONCE}@example.com"


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


def token() -> GmailAccessToken:
    return GmailAccessToken(
        access_token="ya29.readonly",
        account_subject="candidate@example.com",
        granted_scopes=(GMAIL_READONLY_SCOPE,),
        expires_at=datetime(2100, 1, 1, tzinfo=UTC),
    )


def _raw_smoke_api_message(
    *,
    message_id: str = "msg-smoke-1",
    thread_id: str = "thread-smoke-1",
    subject: str = SMOKE_SUBJECT,
    sender: str = "candidate@example.com",
    recipient: str = SMOKE_RECIPIENT,
    body: str = "CareerOps controlled Gmail qualification body",
    rfc_message_id: str = "<gmail-rewritten-message-id@mail.gmail.com>",
    label_ids: tuple[str, ...] = ("SENT",),
) -> dict[str, object]:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Message-ID"] = rfc_message_id
    message.set_content(body)
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
    return {
        "id": message_id,
        "threadId": thread_id,
        "historyId": "12345",
        "labelIds": list(label_ids),
        "internalDate": str(int(NOW.timestamp() * 1000)),
        "raw": encoded,
    }


def test_messages_list_uses_fixed_endpoint_max_500_and_returns_only_message_refs(
    monkeypatch,
) -> None:
    calls: list[object] = []

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del timeout
        calls.append(request)
        return FakeResponse(
            {
                "messages": [
                    {"id": "msg-1", "threadId": "thread-1"},
                    {"id": "msg-2", "threadId": "thread-2", "snippet": "ignored"},
                ],
                "nextPageToken": "next-page",
            }
        )

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)

    page = GmailReadOnlyHttpClient(timeout_seconds=3).list_messages(
        token(),
        max_results=500,
        page_token="prev-page",
    )

    request = cast("Request", calls[0])
    parsed = urlparse(request.full_url)
    assert f"{parsed.scheme}://{parsed.netloc}" == "https://gmail.googleapis.com"
    assert parsed.path == "/gmail/v1/users/me/messages"
    query = parse_qs(parsed.query)
    assert query["maxResults"] == ["500"]
    assert query["pageToken"] == ["prev-page"]
    assert "q" not in query
    assert "labelIds" not in query
    assert request.headers["Authorization"] == "Bearer ya29.readonly"
    assert page.messages[0].id == "msg-1"
    assert page.messages[0].thread_id == "thread-1"
    assert page.next_page_token == "next-page"

    with pytest.raises(ValueError, match="between 1 and 500"):
        GmailReadOnlyHttpClient().list_messages(token(), max_results=501)

    assert "query" not in GmailReadOnlyHttpClient.list_messages.__annotations__
    assert "label_ids" not in GmailReadOnlyHttpClient.list_messages.__annotations__


def test_message_get_uses_metadata_format_and_requested_headers_only(monkeypatch) -> None:
    calls: list[object] = []

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del timeout
        calls.append(request)
        return FakeResponse(
            {
                "id": "msg-1",
                "threadId": "thread-1",
                "historyId": "12345",
                "labelIds": ["INBOX", "IMPORTANT"],
                "snippet": "Interview invitation for 30 minutes",
                "internalDate": "1784568000000",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Interview with ExampleCorp"},
                        {"name": "From", "value": "Recruiter <recruiter@example.com>"},
                        {"name": "To", "value": "candidate@example.com"},
                    ],
                    "body": {"data": "must-not-be-read"},
                },
            }
        )

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)

    message = GmailReadOnlyHttpClient().get_message(
        token(),
        message_id="msg-1",
        metadata_headers=("Subject", "From", "Date"),
    )

    request = cast("Request", calls[0])
    parsed = urlparse(request.full_url)
    assert parsed.path == "/gmail/v1/users/me/messages/msg-1"
    query = parse_qs(parsed.query)
    assert query["format"] == ["METADATA"]
    assert query["metadataHeaders"] == ["Subject", "From", "Date"]
    assert message.headers == {
        "from": "Recruiter <recruiter@example.com>",
        "subject": "Interview with ExampleCorp",
        "to": "candidate@example.com",
    }
    assert message.snippet == "Interview invitation for 30 minutes"
    assert not hasattr(GmailReadOnlyHttpClient, "send")
    assert not hasattr(GmailReadOnlyHttpClient, "compose")
    assert not hasattr(GmailReadOnlyHttpClient, "modify")
    assert not hasattr(GmailReadOnlyHttpClient, "watch")


def test_metadata_message_repr_hides_header_values_and_snippet() -> None:
    message = GmailMetadataMessage(
        id="msg-safe-1",
        thread_id="thread-safe-1",
        history_id="12345",
        headers={
            "from": "Recruiter <recruiter-pii-hidden@example.com>",
            "subject": "Confidential interview with SecretCorp",
        },
        label_ids=("INBOX", "IMPORTANT"),
        snippet="PII snippet: candidate salary expectation is 123456",
        internal_date=NOW,
    )

    rendered = repr(message)

    for private_value in (
        "recruiter-pii-hidden@example.com",
        "Confidential interview with SecretCorp",
        "candidate salary expectation",
        "123456",
    ):
        assert private_value not in rendered
    for safe_value in (
        "msg-safe-1",
        "thread-safe-1",
        "12345",
        "from",
        "subject",
        "INBOX",
        "datetime.datetime(2026",
    ):
        assert safe_value in rendered


def test_find_sent_message_by_rfc_message_id_uses_fixed_sent_search(monkeypatch) -> None:
    calls: list[object] = []
    responses = iter(
        [
            FakeResponse({"messages": [{"id": "msg-1", "threadId": "thread-1"}]}),
            FakeResponse(
                {
                    "id": "msg-1",
                    "threadId": "thread-1",
                    "historyId": "12345",
                    "labelIds": ["SENT"],
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "candidate@example.com"},
                            {"name": "To", "value": "hr@example.com"},
                            {"name": "Subject", "value": "Application"},
                            {
                                "name": "Message-ID",
                                "value": "<careerops.abc123@gmail-send.careerops.local>",
                            },
                        ]
                    },
                }
            ),
        ]
    )

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del timeout
        calls.append(request)
        return next(responses)

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)

    message = GmailReadOnlyHttpClient().find_sent_message_by_rfc_message_id(
        token(),
        rfc_message_id="<careerops.abc123@gmail-send.careerops.local>",
        metadata_headers=("From", "To", "Subject", "Message-ID"),
    )

    assert message is not None
    search_request = cast("Request", calls[0])
    parsed = urlparse(search_request.full_url)
    assert f"{parsed.scheme}://{parsed.netloc}" == "https://gmail.googleapis.com"
    assert parsed.path == "/gmail/v1/users/me/messages"
    query = parse_qs(parsed.query)
    assert query["q"] == ["rfc822msgid:<careerops.abc123@gmail-send.careerops.local>"]
    assert query["labelIds"] == ["SENT"]
    assert query["maxResults"] == ["10"]

    metadata_request = cast("Request", calls[1])
    metadata_query = parse_qs(urlparse(metadata_request.full_url).query)
    assert metadata_query["format"] == ["METADATA"]
    assert metadata_query["metadataHeaders"] == ["From", "To", "Subject", "Message-ID"]

    with pytest.raises(ValueError, match="RFC Message-ID"):
        GmailReadOnlyHttpClient().find_sent_message_by_rfc_message_id(
            token(),
            rfc_message_id="not-a-message-id",
            metadata_headers=("Message-ID",),
        )


def test_find_sent_message_by_rfc_message_id_rejects_multiple_exact_sent_matches(
    monkeypatch,
) -> None:
    responses = iter(
        [
            FakeResponse(
                {
                    "messages": [
                        {"id": "msg-1", "threadId": "thread-1"},
                        {"id": "msg-2", "threadId": "thread-2"},
                    ]
                }
            ),
            FakeResponse(
                {
                    "id": "msg-1",
                    "threadId": "thread-1",
                    "historyId": "12345",
                    "labelIds": ["SENT"],
                    "payload": {
                        "headers": [
                            {
                                "name": "Message-ID",
                                "value": "<careerops.dupe@gmail-send.careerops.local>",
                            }
                        ]
                    },
                }
            ),
            FakeResponse(
                {
                    "id": "msg-2",
                    "threadId": "thread-2",
                    "historyId": "12346",
                    "labelIds": ["SENT"],
                    "payload": {
                        "headers": [
                            {
                                "name": "Message-ID",
                                "value": "<careerops.dupe@gmail-send.careerops.local>",
                            }
                        ]
                    },
                }
            ),
        ]
    )

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del request, timeout
        return next(responses)

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)

    with pytest.raises(GmailApiError, match="GMAIL_SENT_RFC_MESSAGE_ID_AMBIGUOUS"):
        GmailReadOnlyHttpClient().find_sent_message_by_rfc_message_id(
            token(),
            rfc_message_id="<careerops.dupe@gmail-send.careerops.local>",
            metadata_headers=("Message-ID",),
        )


def test_find_recent_sent_smoke_messages_accepts_exact_raw_proof_with_rewritten_message_id(
    monkeypatch,
) -> None:
    body = "CareerOps controlled Gmail qualification body"
    responses = iter(
        [
            FakeResponse({"messages": [{"id": "msg-smoke-1", "threadId": "thread-smoke-1"}]}),
            FakeResponse(
                _raw_smoke_api_message(
                    rfc_message_id="<gmail-rewritten-value@mail.gmail.com>",
                    body=body,
                )
            ),
        ]
    )
    calls: list[object] = []

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del timeout
        calls.append(request)
        return next(responses)

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)

    proofs = GmailReadOnlyHttpClient().find_recent_sent_smoke_messages(
        token(),
        account_subject="candidate@example.com",
        not_before=NOW,
        expected_body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )

    assert len(proofs) == 1
    assert proofs[0].message.id == "msg-smoke-1"
    assert proofs[0].message.thread_id == "thread-smoke-1"
    assert proofs[0].message.headers["message-id"] == ("<gmail-rewritten-value@mail.gmail.com>")
    assert proofs[0].body_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()

    search_request = cast("Request", calls[0])
    search_query = parse_qs(urlparse(search_request.full_url).query)
    assert search_query == {
        "maxResults": ["10"],
        "q": ['in:sent subject:"CareerOps Gmail qualification" newer_than:1d'],
        "labelIds": ["SENT"],
    }
    raw_request = cast("Request", calls[1])
    raw_query = parse_qs(urlparse(raw_request.full_url).query)
    assert raw_query == {"format": ["RAW"]}


@pytest.mark.parametrize(
    "invalid_field",
    ["subject", "from", "to", "sent_label", "body"],
)
def test_find_recent_sent_smoke_messages_rejects_inexact_raw_candidates(
    monkeypatch,
    invalid_field: str,
) -> None:
    expected_body = "CareerOps controlled Gmail qualification body"
    subject = SMOKE_SUBJECT
    sender = "candidate@example.com"
    recipient = SMOKE_RECIPIENT
    body = expected_body
    label_ids = ("SENT",)
    if invalid_field == "subject":
        subject = "CareerOps Gmail qualification not-a-valid-nonce"
    elif invalid_field == "from":
        sender = "attacker@example.com"
    elif invalid_field == "to":
        recipient = "candidate+careerops-smoke-wrong@example.com"
    elif invalid_field == "sent_label":
        label_ids = ("INBOX",)
    else:
        body = "tampered qualification body"
    responses = iter(
        [
            FakeResponse({"messages": [{"id": "msg-smoke-1", "threadId": "thread-smoke-1"}]}),
            FakeResponse(
                _raw_smoke_api_message(
                    subject=subject,
                    sender=sender,
                    recipient=recipient,
                    body=body,
                    label_ids=label_ids,
                )
            ),
        ]
    )

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del request, timeout
        return next(responses)

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)

    assert (
        GmailReadOnlyHttpClient().find_recent_sent_smoke_messages(
            token(),
            account_subject="candidate@example.com",
            not_before=NOW,
            expected_body_sha256=hashlib.sha256(expected_body.encode("utf-8")).hexdigest(),
        )
        == ()
    )


@pytest.mark.parametrize("invalid_field", ["subject", "from", "to", "sent_label"])
def test_empty_body_digest_cannot_qualify_an_invalid_raw_candidate(
    monkeypatch,
    invalid_field: str,
) -> None:
    subject = SMOKE_SUBJECT
    sender = "candidate@example.com"
    recipient = SMOKE_RECIPIENT
    label_ids = ("SENT",)
    if invalid_field == "subject":
        subject = "not a qualification subject"
    elif invalid_field == "from":
        sender = "attacker@example.com"
    elif invalid_field == "to":
        recipient = "candidate@example.com"
    else:
        label_ids = ("INBOX",)
    responses = iter(
        [
            FakeResponse({"messages": [{"id": "msg-smoke-1", "threadId": "thread-smoke-1"}]}),
            FakeResponse(
                _raw_smoke_api_message(
                    subject=subject,
                    sender=sender,
                    recipient=recipient,
                    body="",
                    label_ids=label_ids,
                )
            ),
        ]
    )

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del request, timeout
        return next(responses)

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)

    assert (
        GmailReadOnlyHttpClient().find_recent_sent_smoke_messages(
            token(),
            account_subject="candidate@example.com",
            not_before=NOW,
            expected_body_sha256=hashlib.sha256(b"").hexdigest(),
        )
        == ()
    )


def test_find_recent_sent_smoke_messages_returns_all_exact_matches_for_broker_ambiguity(
    monkeypatch,
) -> None:
    body = "CareerOps controlled Gmail qualification body"
    responses = iter(
        [
            FakeResponse(
                {
                    "messages": [
                        {"id": "msg-smoke-1", "threadId": "thread-smoke-1"},
                        {"id": "msg-smoke-2", "threadId": "thread-smoke-2"},
                    ]
                }
            ),
            FakeResponse(
                _raw_smoke_api_message(
                    message_id="msg-smoke-1",
                    thread_id="thread-smoke-1",
                    body=body,
                    rfc_message_id="<gmail-rewritten-one@mail.gmail.com>",
                )
            ),
            FakeResponse(
                _raw_smoke_api_message(
                    message_id="msg-smoke-2",
                    thread_id="thread-smoke-2",
                    body=body,
                    rfc_message_id="<gmail-rewritten-two@mail.gmail.com>",
                )
            ),
        ]
    )

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del request, timeout
        return next(responses)

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)

    proofs = GmailReadOnlyHttpClient().find_recent_sent_smoke_messages(
        token(),
        account_subject="candidate@example.com",
        not_before=NOW,
        expected_body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )

    assert tuple(proof.message.id for proof in proofs) == ("msg-smoke-1", "msg-smoke-2")


def test_find_sent_smoke_message_reconciles_provider_ids_with_rewritten_message_id(
    monkeypatch,
) -> None:
    responses = iter(
        [
            FakeResponse({"messages": [{"id": "msg-smoke-1", "threadId": "thread-smoke-1"}]}),
            FakeResponse(
                {
                    "id": "msg-smoke-1",
                    "threadId": "thread-smoke-1",
                    "historyId": "12345",
                    "labelIds": ["SENT"],
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "candidate@example.com"},
                            {"name": "To", "value": SMOKE_RECIPIENT},
                            {"name": "Subject", "value": SMOKE_SUBJECT},
                            {
                                "name": "Message-ID",
                                "value": "<gmail-rewritten-value@mail.gmail.com>",
                            },
                        ]
                    },
                }
            ),
        ]
    )

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del request, timeout
        return next(responses)

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)

    message = GmailReadOnlyHttpClient().find_sent_smoke_message(
        token(),
        provider_message_id="msg-smoke-1",
        provider_thread_id="thread-smoke-1",
        sender="candidate@example.com",
        recipient=SMOKE_RECIPIENT,
        subject=SMOKE_SUBJECT,
        metadata_headers=("From", "To", "Subject", "Message-ID"),
    )

    assert message is not None
    assert message.id == "msg-smoke-1"
    assert message.thread_id == "thread-smoke-1"
    assert message.headers["message-id"] == "<gmail-rewritten-value@mail.gmail.com>"


def test_history_list_404_requires_full_sync_and_checkpoint_only_on_last_page(monkeypatch) -> None:
    calls: list[object] = []
    responses = iter(
        [
            FakeResponse(
                {
                    "history": [{"id": "200", "messagesAdded": [{"message": {"id": "msg-1"}}]}],
                    "historyId": "201",
                    "nextPageToken": "next",
                }
            ),
            FakeResponse({"history": [], "historyId": "203"}),
        ]
    )

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del timeout
        calls.append(request)
        return next(responses)

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)
    client = GmailReadOnlyHttpClient()

    first = client.list_history(token(), start_history_id="100", max_results=500)
    second = client.list_history(
        token(),
        start_history_id="100",
        page_token=first.next_page_token,
        max_results=500,
    )

    request = cast("Request", calls[0])
    first_query = parse_qs(urlparse(request.full_url).query)
    assert first_query["startHistoryId"] == ["100"]
    assert first_query["maxResults"] == ["500"]
    assert first.checkpoint_history_id is None
    assert first.next_page_token == "next"
    assert second.checkpoint_history_id == "203"

    @contextmanager
    def raising_urlopen(*args: object, **kwargs: object) -> Iterator[FakeResponse]:
        del args, kwargs
        raise GmailApiError("GMAIL_HTTP_404", status_code=404)
        yield FakeResponse({})

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", raising_urlopen)
    with pytest.raises(GmailHistoryCursorExpired):
        client.list_history(token(), start_history_id="999999", max_results=1)


def test_response_size_limit_blocks_oversized_payload(monkeypatch) -> None:
    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del request, timeout
        return FakeResponse({"snippet": "x" * 200})

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)

    with pytest.raises(GmailApiError, match="GMAIL_RESPONSE_TOO_LARGE"):
        GmailReadOnlyHttpClient(max_response_bytes=20).get_profile(token())


@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
def test_http_errors_are_bounded_and_do_not_retain_sensitive_provider_data(
    monkeypatch,
    status_code: int,
) -> None:
    secret_body = b'{"error":"super-secret-response-body"}'
    sensitive_headers = Message()
    sensitive_headers["Authorization"] = "Bearer ya29.readonly"
    sensitive_headers["X-Provider-Secret"] = "secret-header-value"
    provider_errors: list[HTTPError] = []

    def raising_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del timeout
        provider_error = HTTPError(
            cast("Request", request).full_url,
            status_code,
            "provider reason contains secret-reason-value",
            sensitive_headers,
            BytesIO(secret_body),
        )
        provider_errors.append(provider_error)
        raise provider_error

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", raising_urlopen)

    with pytest.raises(GmailApiError) as raised:
        GmailReadOnlyHttpClient().get_profile(token())

    error = raised.value
    assert error.error_code == f"GMAIL_HTTP_{status_code}"
    assert error.status_code == status_code
    assert str(error) == f"GMAIL_HTTP_{status_code}"
    assert len(str(error)) <= 32
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    for secret in (
        "ya29.readonly",
        "Authorization",
        "super-secret-response-body",
        "secret-header-value",
        "secret-reason-value",
    ):
        assert secret not in rendered
    # The client must not consume a Gmail error response body while mapping it.
    assert provider_errors[0].file is not None
    assert provider_errors[0].file.tell() == 0


@pytest.mark.parametrize(
    ("body", "error_code"),
    [
        (b'{"emailAddress":"private@example.com",', "GMAIL_INVALID_JSON"),
        (b'["private@example.com"]', "GMAIL_SCHEMA_MISMATCH"),
        (b"\xffprivate@example.com", "GMAIL_INVALID_JSON"),
    ],
)
def test_invalid_json_and_top_level_schema_errors_do_not_retain_response_body(
    monkeypatch,
    body: bytes,
    error_code: str,
) -> None:
    def fake_urlopen(request: object, *, timeout: float) -> RawResponse:
        del request, timeout
        return RawResponse(body)

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)

    with pytest.raises(GmailApiError) as raised:
        GmailReadOnlyHttpClient().get_profile(token())

    error = raised.value
    assert error.error_code == error_code
    assert error.status_code is None
    assert str(error) == error_code
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    assert "private@example.com" not in rendered
    assert repr(body) not in rendered


def test_required_profile_field_schema_error_is_machine_readable(monkeypatch) -> None:
    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        del request, timeout
        return FakeResponse(
            {
                "emailAddress": "candidate@example.com",
                "messagesTotal": "not-an-integer",
                "threadsTotal": 3,
                "historyId": "123",
            }
        )

    monkeypatch.setattr("careerops.infrastructure.gmail.client.urlopen", fake_urlopen)

    with pytest.raises(GmailApiError) as raised:
        GmailReadOnlyHttpClient().get_profile(token())

    assert raised.value.error_code == "GMAIL_SCHEMA_MISMATCH"
    assert raised.value.status_code is None
    assert str(raised.value) == "GMAIL_SCHEMA_MISMATCH"
