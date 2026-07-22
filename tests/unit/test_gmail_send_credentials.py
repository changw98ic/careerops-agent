from __future__ import annotations

import json
import socket
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from careerops.infrastructure.gmail.send_credentials import (
    GMAIL_SEND_SCOPE,
    GmailSendAccessToken,
    GmailSendBrokerCredentialEnvelope,
    GmailSendCredentialBrokerError,
    GmailSendCredentialHandle,
    UnixGmailSendCredentialBroker,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def test_handle_is_dedicated_to_exact_gmail_send_scope() -> None:
    handle = GmailSendCredentialHandle(
        opaque_handle="gmail:send:opaque",
        account_subject="candidate@example.com",
        granted_scopes=(GMAIL_SEND_SCOPE,),
    )

    assert handle.granted_scopes == (GMAIL_SEND_SCOPE,)

    with pytest.raises(ValueError, match=r"exactly gmail\.send"):
        GmailSendCredentialHandle(
            opaque_handle="gmail:readonly",
            account_subject="candidate@example.com",
            granted_scopes=("https://www.googleapis.com/auth/gmail.readonly",),
        )

    with pytest.raises(ValueError, match=r"exactly gmail\.send"):
        GmailSendCredentialHandle(
            opaque_handle="gmail:wide",
            account_subject="candidate@example.com",
            granted_scopes=(GMAIL_SEND_SCOPE, "https://mail.google.com/"),
        )


def test_send_access_token_repr_and_envelope_reject_secret_material() -> None:
    token = GmailSendAccessToken(
        access_token="ya29.send-secret",
        account_subject="candidate@example.com",
        granted_scopes=(GMAIL_SEND_SCOPE,),
        expires_at=NOW + timedelta(minutes=5),
    )

    assert token.value == "ya29.send-secret"
    assert "send-secret" not in repr(token)
    assert "send-secret" not in str(token)

    with pytest.raises(ValueError, match="forbidden secret field"):
        GmailSendBrokerCredentialEnvelope.from_mapping(
            {
                "access_token": "ya29.send",
                "refresh_token": "refresh-is-forbidden",
                "account_subject": "candidate@example.com",
                "scope": GMAIL_SEND_SCOPE,
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            },
            expected_account_subject="candidate@example.com",
            now=NOW,
        )


def test_send_broker_requests_send_only_scope_without_persisting_token() -> None:
    socket_path = Path(tempfile.mkdtemp(prefix="gmail-send-broker-", dir="/tmp")) / "broker.sock"
    captured_request: dict[str, object] = {}
    ready = threading.Event()

    def broker() -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.listen(1)
        ready.set()
        connection, _ = server.accept()
        with connection, server:
            request = connection.recv(4096)
            captured_request.update(json.loads(request.decode("utf-8")))
            connection.sendall(
                json.dumps(
                    {
                        "access_token": "ya29.runtime-send-only",
                        "account_subject": "candidate@example.com",
                        "scope": GMAIL_SEND_SCOPE,
                        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                    }
                ).encode("utf-8")
            )

    thread = threading.Thread(target=broker)
    thread.start()
    assert ready.wait(timeout=2)

    resolver = UnixGmailSendCredentialBroker(socket_path=socket_path, now=lambda: NOW)
    token = resolver.resolve(
        GmailSendCredentialHandle(
            opaque_handle="gmail:send:opaque-handle",
            account_subject="candidate@example.com",
            granted_scopes=(GMAIL_SEND_SCOPE,),
        )
    )
    thread.join(timeout=2)

    assert captured_request == {
        "version": "careerops.gmail.send-credential-broker.v1",
        "provider": "gmail",
        "action": "send_email",
        "opaque_handle": "gmail:send:opaque-handle",
        "account_subject": "candidate@example.com",
        "scope": GMAIL_SEND_SCOPE,
    }
    assert token.value == "ya29.runtime-send-only"
    assert "runtime-send-only" not in repr(resolver)


def test_send_broker_rejects_mismatched_or_expired_envelopes() -> None:
    with pytest.raises(GmailSendCredentialBrokerError, match="scope mismatch"):
        GmailSendBrokerCredentialEnvelope.from_mapping(
            {
                "access_token": "ya29.token",
                "account_subject": "candidate@example.com",
                "scope": "https://www.googleapis.com/auth/gmail.readonly",
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            },
            expected_account_subject="candidate@example.com",
            now=NOW,
        )

    with pytest.raises(GmailSendCredentialBrokerError, match="account mismatch"):
        GmailSendBrokerCredentialEnvelope.from_mapping(
            {
                "access_token": "ya29.token",
                "account_subject": "other@example.com",
                "scope": GMAIL_SEND_SCOPE,
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            },
            expected_account_subject="candidate@example.com",
            now=NOW,
        )

    with pytest.raises(GmailSendCredentialBrokerError, match="expired"):
        GmailSendBrokerCredentialEnvelope.from_mapping(
            {
                "access_token": "ya29.token",
                "account_subject": "candidate@example.com",
                "scope": GMAIL_SEND_SCOPE,
                "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
            },
            expected_account_subject="candidate@example.com",
            now=NOW,
        )
