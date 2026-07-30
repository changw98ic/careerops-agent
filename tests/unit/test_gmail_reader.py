"""Unit tests for GmailReader (Phase 1.3 incremental history.list fetch).

Drives the reader with a fake HTTP transport so the Gmail history/messages
flow and the run_sync_step message-dict assembly are verified without network
access. The assembled dicts must satisfy the MailSyncService.run_sync_step
contract (provider_message_id, provider_thread_id, subject, snippet,
body_text, sender_email, received_at, history_id).
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from careerops.integrations.gmail_reader import (
    FetchResult,
    GmailReadError,
    GmailReader,
    _internal_date_to_datetime,
    _message_to_dict,
    _parse_from,
)
from careerops.integrations.gmail_token_store import GmailTokenStore


def _b64url(data: str) -> str:
    return base64.urlsafe_b64encode(data.encode("utf-8")).rstrip(b"=").decode("ascii")


def _token_store() -> GmailTokenStore:
    return GmailTokenStore(
        refresh_token="rt", client_id="cid", client_secret="sec", refresher=lambda **k: "tok"
    )


def _msg_payload(
    *,
    message_id: str = "msg-1",
    thread_id: str = "thread-1",
    subject: str = "Interview invite",
    from_header: str = "Recruiter <recruiter@company.com>",
    body: str = "We'd love to interview you.",
    snippet: str = "We'd love to...",
    internal_date: str = "1753526400000",  # 2025-07-26T00:00:00Z
    history_id: str = "500",
) -> dict:
    return {
        "id": message_id,
        "threadId": thread_id,
        "historyId": history_id,
        "snippet": snippet,
        "internalDate": internal_date,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": from_header},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64url(body)},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64url("<p>html</p>")},
                },
            ],
        },
    }


class _FakeHttp:
    """Routes /history and /messages/{id} and /profile to canned responses."""

    def __init__(
        self,
        *,
        history_pages: list[dict],
        messages: dict[str, dict],
        profile: dict | None = None,
        raise_on_history: str | None = None,
    ) -> None:
        self._history_pages = history_pages
        self._messages = messages
        self._profile = profile or {"historyId": "seeded-1"}
        self._raise_on_history = raise_on_history
        self._history_call = 0
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, access_token: str) -> dict:
        self.calls.append((url, access_token))
        parsed = urlparse(url)
        path = parsed.path
        qs = parse_qs(parsed.query)
        assert access_token == "tok"
        if path.endswith("/profile"):
            return self._profile
        if path.endswith("/history"):
            if self._raise_on_history and self._history_call == 0:
                self._history_call += 1
                raise GmailReadError(self._raise_on_history)
            idx = min(self._history_call, len(self._history_pages) - 1)
            page = self._history_pages[idx]
            self._history_call += 1
            return page
        if "/messages/" in path:
            msg_id = path.rsplit("/", 1)[-1]
            if msg_id in self._messages:
                return self._messages[msg_id]
            raise GmailReadError(f"unknown message {msg_id}")
        raise GmailReadError(f"unrouted url {url}")


class TestFetchIncremental:
    def test_fetches_messages_and_assembles_dicts(self) -> None:
        history_pages = [
            {
                "history": [
                    {
                        "id": "600",
                        "messages": [
                            {"id": "msg-1", "threadId": "thread-1"},
                            {"id": "msg-2", "threadId": "thread-2"},
                        ],
                    }
                ]
            }
        ]
        messages = {
            "msg-1": _msg_payload(message_id="msg-1"),
            "msg-2": _msg_payload(
                message_id="msg-2",
                thread_id="thread-2",
                subject="Phone screen",
                from_header="sourcer@company.com",
            ),
        }
        http = _FakeHttp(history_pages=history_pages, messages=messages)
        reader = GmailReader(_token_store(), http_get=http)
        result = reader.fetch_incremental("100")
        assert isinstance(result, FetchResult)
        assert result.history_expired is False
        assert result.last_history_id == "600"
        assert len(result.messages) == 2
        ids = {m["provider_message_id"] for m in result.messages}
        assert ids == {"msg-1", "msg-2"}

    def test_message_dict_satisfies_run_sync_step_contract(self) -> None:
        history_pages = [
            {
                "history": [
                    {
                        "id": "600",
                        "messages": [{"id": "msg-1", "threadId": "thread-1"}],
                    }
                ]
            }
        ]
        messages = {"msg-1": _msg_payload()}
        http = _FakeHttp(history_pages=history_pages, messages=messages)
        reader = GmailReader(_token_store(), http_get=http)
        result = reader.fetch_incremental("100")
        msg = result.messages[0]
        # All fields run_sync_step reads must be present.
        assert msg["provider_message_id"] == "msg-1"
        assert msg["provider_thread_id"] == "thread-1"
        assert msg["subject"] == "Interview invite"
        assert msg["snippet"] == "We'd love to..."
        assert msg["body_text"] == "We'd love to interview you."
        assert msg["sender_email"] == "recruiter@company.com"
        assert msg["sender_name"] == "Recruiter"
        assert msg["history_id"] == "500"
        assert isinstance(msg["received_at"], datetime)
        assert msg["received_at"].tzinfo is not None

    def test_cold_start_seeds_cursor_from_profile(self) -> None:
        http = _FakeHttp(history_pages=[], messages={}, profile={"historyId": "seeded-1"})
        reader = GmailReader(_token_store(), http_get=http)
        result = reader.fetch_incremental("")  # no cursor
        assert result.messages == ()
        assert result.last_history_id == "seeded-1"
        assert result.history_expired is False

    def test_history_expired_returns_flag(self) -> None:
        http = _FakeHttp(
            history_pages=[],
            messages={},
            raise_on_history="Gmail API HTTP 404: historyNotAvailable",
        )
        reader = GmailReader(_token_store(), http_get=http)
        result = reader.fetch_incremental("1")
        assert result.history_expired is True
        assert result.messages == ()

    def test_pagination(self) -> None:
        history_pages = [
            {
                "history": [
                    {"id": "600", "messages": [{"id": "msg-1", "threadId": "t1"}]},
                ],
                "nextPageToken": "page2",
            },
            {
                "history": [
                    {"id": "700", "messages": [{"id": "msg-2", "threadId": "t2"}]},
                ],
            },
        ]
        messages = {
            "msg-1": _msg_payload(message_id="msg-1"),
            "msg-2": _msg_payload(message_id="msg-2", thread_id="t2"),
        }
        http = _FakeHttp(history_pages=history_pages, messages=messages)
        reader = GmailReader(_token_store(), http_get=http)
        result = reader.fetch_incremental("100")
        assert {m["provider_message_id"] for m in result.messages} == {"msg-1", "msg-2"}
        assert result.last_history_id == "700"

    def test_message_cap_bounds_batch(self) -> None:
        history = {
            "history": [
                {
                    "id": "600",
                    "messages": [
                        {"id": f"msg-{i}", "threadId": "t"} for i in range(5)
                    ],
                }
            ]
        }
        messages = {f"msg-{i}": _msg_payload(message_id=f"msg-{i}") for i in range(5)}
        http = _FakeHttp(history_pages=[history], messages=messages)
        reader = GmailReader(_token_store(), http_get=http, message_cap=2)
        result = reader.fetch_incremental("100")
        assert len(result.messages) == 2

    def test_duplicate_message_id_within_history_deduped(self) -> None:
        history = {
            "history": [
                {
                    "id": "600",
                    "messages": [
                        {"id": "msg-1", "threadId": "t"},
                        {"id": "msg-1", "threadId": "t"},  # dup ref
                    ],
                }
            ]
        }
        messages = {"msg-1": _msg_payload(message_id="msg-1")}
        http = _FakeHttp(history_pages=[history], messages=messages)
        reader = GmailReader(_token_store(), http_get=http)
        result = reader.fetch_incremental("100")
        assert len(result.messages) == 1


class TestMessageParsing:
    def test_message_to_dict_returns_none_without_id(self) -> None:
        assert _message_to_dict({"threadId": "t"}) is None

    def test_parse_from_bare_address(self) -> None:
        email, name = _parse_from("a@b.com")
        assert email == "a@b.com"
        assert name == ""

    def test_parse_from_named(self) -> None:
        email, name = _parse_from("Jane Doe <jane@corp.com>")
        assert email == "jane@corp.com"
        assert name == "Jane Doe"

    def test_internal_date_parses_ms_epoch(self) -> None:
        dt = _internal_date_to_datetime("1753526400000")
        # 1753526400 s == 2025-07-26T10:40:00Z
        assert dt == datetime(2025, 7, 26, 10, 40, tzinfo=UTC)

    def test_internal_date_falls_back_on_garbage(self) -> None:
        dt = _internal_date_to_datetime("not-a-number")
        assert dt.tzinfo is not None  # falls back to now(UTC)

    def test_top_level_text_plain_body(self) -> None:
        data = {
            "id": "m1",
            "threadId": "t1",
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": "S"}],
                "body": {"data": _b64url("hello body")},
            },
        }
        msg = _message_to_dict(data)
        assert msg is not None
        assert msg["body_text"] == "hello body"
