"""Unit tests for HTTP 429 Retry-After backoff in the Anthropic-compat adapter.

Plan v0.4 §3 Stage 3.5: ``AnthropicCompatClient._call_messages`` retries 429
responses honoring the server's ``Retry-After`` header (delta-seconds or
HTTP-date), falling back to exponential backoff, with at most 3 retries.

No real network: ``urllib.request.urlopen`` and ``_sleep`` are monkeypatched.
The repair logic in ``invoke`` is verified to remain orthogonal to 429 retries.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from http.client import HTTPMessage
from typing import Any

import pytest

from careerops.model_gateway import anthropic_compat
from careerops.model_gateway.anthropic_compat import (
    AnthropicCompatClient,
    AnthropicCompatConfig,
    ModelInvocationError,
    _backoff_seconds,
    _parse_retry_after,
)
from careerops.model_gateway.base import StructuredModelRequest


def _config() -> AnthropicCompatConfig:
    return AnthropicCompatConfig(
        base_url="https://provider.test",
        api_key="test-key",
        model="test-model",
    )


def _messages_body(
    text: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> bytes:
    return json.dumps(
        {
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    ).encode("utf-8")


class _FakeResp:
    """Minimal urllib response double for a successful Messages-API call."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _rate_limited(retry_after: str | None) -> urllib.error.HTTPError:
    headers = HTTPMessage()
    if retry_after is not None:
        headers.add_header("Retry-After", retry_after)
    return urllib.error.HTTPError(
        "https://provider.test/v1/messages",
        429,
        "Too Many Requests",
        headers,
        io.BytesIO(b"{}"),
    )


def _stub_urlopen(
    outcomes: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    """Wire ``urllib.request.urlopen`` to a scripted queue of outcomes.

    Each outcome is either an exception to raise or a ``_FakeResp`` to return.
    Returns a mutable counter exposing how many times ``urlopen`` was called.
    """
    calls = {"n": 0}

    def _fake_open(req: urllib.request.Request, timeout: float = 0.0) -> _FakeResp:
        idx = calls["n"]
        calls["n"] += 1
        outcome = outcomes[idx]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", _fake_open)
    return calls


def _stub_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(anthropic_compat, "_sleep", lambda s: sleeps.append(s))
    return sleeps


# ---------------------------------------------------------------------------
# Retry-After parsing and backoff
# ---------------------------------------------------------------------------


class TestRetryAfterParsing:
    def test_delta_seconds(self) -> None:
        assert _parse_retry_after("120") == 120.0

    def test_zero_clamped(self) -> None:
        assert _parse_retry_after("0") == 0.0

    def test_negative_clamped(self) -> None:
        assert _parse_retry_after("-5") == 0.0

    def test_future_http_date_is_positive(self) -> None:
        # A far-future RFC 7231 date must parse to a positive wait.
        wait = _parse_retry_after("Wed, 21 Oct 2999 07:28:00 GMT")
        assert wait is not None and wait > 0

    def test_past_http_date_clamped_to_zero(self) -> None:
        assert _parse_retry_after("Wed, 21 Oct 1999 07:28:00 GMT") == 0.0

    def test_absent_returns_none(self) -> None:
        assert _parse_retry_after(None) is None

    def test_empty_returns_none(self) -> None:
        assert _parse_retry_after("   ") is None

    def test_garbage_returns_none(self) -> None:
        assert _parse_retry_after("not-a-date-or-number") is None


class TestBackoffSeconds:
    def test_retry_after_is_honored(self) -> None:
        assert _backoff_seconds(0, 5.0) == 5.0
        assert _backoff_seconds(2, 7.5) == 7.5

    def test_exponential_fallback_without_retry_after(self) -> None:
        assert _backoff_seconds(0, None) == pytest.approx(1.0)
        assert _backoff_seconds(1, None) == pytest.approx(2.0)
        assert _backoff_seconds(2, None) == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# _call_messages 429 retry behavior
# ---------------------------------------------------------------------------


class TestCallMessages429Backoff:
    def test_retries_on_429_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        outcomes = [
            _rate_limited("2"),
            _rate_limited("1"),
            _FakeResp(_messages_body('{"ok": true}', input_tokens=3, output_tokens=5)),
        ]
        calls = _stub_urlopen(outcomes, monkeypatch)
        sleeps = _stub_sleep(monkeypatch)
        client = AnthropicCompatClient(_config())

        text = client._call_messages(system="", user="u", max_tokens=16, timeout=1.0)

        assert str(text) == '{"ok": true}'
        assert calls["n"] == 3
        # Retry-After honored exactly (no exponential fallback when header present).
        assert sleeps == [2.0, 1.0]

    def test_exponential_backoff_when_no_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
        outcomes = [
            _rate_limited(None),
            _rate_limited(None),
            _FakeResp(_messages_body("done")),
        ]
        calls = _stub_urlopen(outcomes, monkeypatch)
        sleeps = _stub_sleep(monkeypatch)
        client = AnthropicCompatClient(_config())

        text = client._call_messages(system="", user="u", max_tokens=16, timeout=1.0)

        assert str(text) == "done"
        assert calls["n"] == 3
        # Exponential fallback: 1.0, then 2.0.
        assert sleeps == [pytest.approx(1.0), pytest.approx(2.0)]

    def test_exhausting_retries_raises_model_invocation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 4 retries allowed means 4 attempts total (attempt 0 + 3 retries).
        outcomes = [_rate_limited("0")] * 4
        calls = _stub_urlopen(outcomes, monkeypatch)
        sleeps = _stub_sleep(monkeypatch)
        client = AnthropicCompatClient(_config())

        with pytest.raises(ModelInvocationError, match="HTTP 429"):
            client._call_messages(system="", user="u", max_tokens=16, timeout=1.0)

        # 1 initial attempt + 3 retries == 4 total urlopen calls.
        assert calls["n"] == 4
        assert len(sleeps) == 3

    def test_non_429_error_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server_error = urllib.error.HTTPError(
            "https://provider.test/v1/messages",
            500,
            "Internal Server Error",
            HTTPMessage(),
            io.BytesIO(b"boom"),
        )
        calls = _stub_urlopen([server_error, _FakeResp(_messages_body("never"))], monkeypatch)
        sleeps = _stub_sleep(monkeypatch)
        client = AnthropicCompatClient(_config())

        with pytest.raises(ModelInvocationError, match="HTTP 500"):
            client._call_messages(system="", user="u", max_tokens=16, timeout=1.0)

        assert calls["n"] == 1  # no retry on non-429
        assert sleeps == []


# ---------------------------------------------------------------------------
# Repair logic remains orthogonal to 429 retries
# ---------------------------------------------------------------------------


class TestRepairAfter429:
    def test_repair_path_still_works_after_a_429_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # First _call_messages: one 429, then malformed JSON.
        # Repair _call_messages: valid JSON.
        outcomes = [
            _rate_limited("1"),
            _FakeResp(_messages_body("<<<not json>>>")),
            _FakeResp(_messages_body('{"answer": 42}')),
        ]
        _stub_urlopen(outcomes, monkeypatch)
        sleeps = _stub_sleep(monkeypatch)
        client = AnthropicCompatClient(_config())

        request = StructuredModelRequest(task_type="test", user_prompt="q")
        response = client.invoke(request)

        assert response.repair_attempted is True
        assert response.result == {"answer": 42}
        # Only the first call hit a 429 (the repair call succeeded first try).
        assert sleeps == [pytest.approx(1.0)]
        assert response.is_review_only is True
