"""Unit tests for ``GmailSideEffectProvider``.

Verifies the ``SideEffectProvider`` protocol contract using a mocked
``GmailSender``: execute -> send -> receipt, idempotent duplicate, reconcile,
validation failure, and revoke (unsupported).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from careerops.domain.side_effects import (
    ProviderCallResult,
    ProviderFailureClass,
    ProviderResultKind,
    ReceiptFinalState,
)
from careerops.integrations.gmail_sender import GmailSendError, SendResult
from careerops.integrations.gmail_side_effect_provider import GmailSideEffectProvider


@pytest.fixture()
def mock_sender() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def provider(mock_sender: MagicMock) -> GmailSideEffectProvider:
    return GmailSideEffectProvider(mock_sender)


@pytest.fixture()
def now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _exec(
    provider: GmailSideEffectProvider,
    *,
    now: datetime,
    key: str = "key1:hash1",
    target: dict[str, object] | None = None,
    payload: dict[str, object] | None = None,
) -> ProviderCallResult:
    return provider.execute(
        reconciliation_key=key,
        request_fingerprint="fp1",
        target=target or {"to": "recruiter@example.com"},
        payload=payload or {"subject": "Hello", "body": "World"},
        now=now,
    )


class TestExecute:
    def test_success(
        self,
        provider: GmailSideEffectProvider,
        mock_sender: MagicMock,
        now: datetime,
    ) -> None:
        mock_sender.send.return_value = SendResult(
            provider_message_id="msg-123",
            thread_id="thread-456",
            label_ids=("SENT",),
        )
        result = _exec(provider, now=now)

        assert result.kind is ProviderResultKind.SUCCESS
        assert result.provider_resource_id == "msg-123"
        mock_sender.send.assert_called_once()

    def test_idempotent_duplicate(
        self,
        provider: GmailSideEffectProvider,
        mock_sender: MagicMock,
        now: datetime,
    ) -> None:
        mock_sender.send.return_value = SendResult(
            provider_message_id="msg-123",
            thread_id="thread-456",
            label_ids=("SENT",),
        )
        r1 = _exec(provider, now=now)
        r2 = _exec(provider, now=now)

        assert r1.kind is ProviderResultKind.SUCCESS
        assert r2.kind is ProviderResultKind.SUCCESS
        assert r2.metadata.get("duplicate") is True
        assert mock_sender.send.call_count == 1

    def test_no_recipient(
        self,
        provider: GmailSideEffectProvider,
        now: datetime,
    ) -> None:
        result = _exec(provider, now=now, target={"to": ""})

        assert result.kind is ProviderResultKind.FAILURE
        assert result.error_code == "NO_RECIPIENT"
        assert result.failure_class is ProviderFailureClass.VALIDATION

    def test_gmail_4xx_error(
        self,
        provider: GmailSideEffectProvider,
        mock_sender: MagicMock,
        now: datetime,
    ) -> None:
        mock_sender.send.side_effect = GmailSendError("Gmail API HTTP 403: forbidden")
        result = _exec(provider, now=now)

        assert result.kind is ProviderResultKind.FAILURE
        assert result.failure_class is ProviderFailureClass.VALIDATION

    def test_gmail_5xx_error(
        self,
        provider: GmailSideEffectProvider,
        mock_sender: MagicMock,
        now: datetime,
    ) -> None:
        mock_sender.send.side_effect = GmailSendError("Gmail API HTTP 500: internal")
        result = _exec(provider, now=now)

        assert result.kind is ProviderResultKind.FAILURE
        assert result.failure_class is ProviderFailureClass.TRANSIENT

    def test_gmail_network_error(
        self,
        provider: GmailSideEffectProvider,
        mock_sender: MagicMock,
        now: datetime,
    ) -> None:
        mock_sender.send.side_effect = GmailSendError("Gmail API network error: timeout")
        result = _exec(provider, now=now)

        assert result.kind is ProviderResultKind.FAILURE
        assert result.failure_class is ProviderFailureClass.TRANSIENT


class TestReconcile:
    def test_not_found(self, provider: GmailSideEffectProvider, now: datetime) -> None:
        result = provider.reconcile(reconciliation_key="nonexistent", now=now)
        assert result.found is False

    def test_found_after_execute(
        self,
        provider: GmailSideEffectProvider,
        mock_sender: MagicMock,
        now: datetime,
    ) -> None:
        mock_sender.send.return_value = SendResult(
            provider_message_id="msg-789",
            thread_id="thread-012",
            label_ids=("SENT",),
        )
        _exec(provider, now=now)
        result = provider.reconcile(reconciliation_key="key1:hash1", now=now)

        assert result.found is True
        assert result.final_state is ReceiptFinalState.SUCCEEDED
        assert result.provider_resource_id == "msg-789"


class TestRevoke:
    def test_not_found(self, provider: GmailSideEffectProvider, now: datetime) -> None:
        result = provider.revoke(
            reconciliation_key="nonexistent",
            provider_resource_id="msg-123",
            now=now,
        )
        assert result.found is False

    def test_gmail_unsupported(
        self,
        provider: GmailSideEffectProvider,
        mock_sender: MagicMock,
        now: datetime,
    ) -> None:
        mock_sender.send.return_value = SendResult(
            provider_message_id="msg-123",
            thread_id="thread-456",
            label_ids=("SENT",),
        )
        _exec(provider, now=now)
        result = provider.revoke(
            reconciliation_key="key1:hash1",
            provider_resource_id="msg-123",
            now=now,
        )
        # Gmail does not support revoke; returns not-found.
        assert result.found is False


class TestProviderName:
    def test_name(self, provider: GmailSideEffectProvider) -> None:
        assert provider.provider_name == "gmail"
