from __future__ import annotations

import json
import socket
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from careerops.infrastructure.gmail.credentials import (
    GMAIL_READONLY_SCOPE,
    GmailAccessToken,
    GmailBrokerCredentialEnvelope,
    GmailCredentialBrokerError,
    GmailCredentialHandle,
    UnixGmailCredentialBroker,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def test_handle_is_dedicated_to_exact_gmail_readonly_scope() -> None:
    handle = GmailCredentialHandle(
        opaque_handle="gmail:acct:opaque",
        account_subject="candidate@example.com",
        granted_scopes=(GMAIL_READONLY_SCOPE,),
    )

    assert handle.granted_scopes == (GMAIL_READONLY_SCOPE,)

    with pytest.raises(ValueError, match=r"exactly gmail\.readonly"):
        GmailCredentialHandle(
            opaque_handle="gmail:acct:metadata",
            account_subject="candidate@example.com",
            granted_scopes=("https://www.googleapis.com/auth/gmail.metadata",),
        )

    with pytest.raises(ValueError, match=r"exactly gmail\.readonly"):
        GmailCredentialHandle(
            opaque_handle="gmail:acct:send",
            account_subject="candidate@example.com",
            granted_scopes=(GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.send"),
        )


def test_access_token_repr_and_envelope_reject_secret_material() -> None:
    token = GmailAccessToken(
        access_token="ya29.secret-value",
        account_subject="candidate@example.com",
        granted_scopes=(GMAIL_READONLY_SCOPE,),
        expires_at=NOW + timedelta(minutes=5),
    )

    assert token.value == "ya29.secret-value"
    assert "secret-value" not in repr(token)
    assert "secret-value" not in str(token)

    with pytest.raises(ValueError, match="forbidden secret field"):
        GmailBrokerCredentialEnvelope.from_mapping(
            {
                "access_token": "ya29.short",
                "refresh_token": "refresh-is-forbidden",
                "account_subject": "candidate@example.com",
                "scope": GMAIL_READONLY_SCOPE,
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            },
            expected_account_subject="candidate@example.com",
            now=NOW,
        )


def test_broker_cross_checks_account_scope_and_expiry_without_persisting_token(
    tmp_path,
) -> None:
    del tmp_path
    socket_path = Path(tempfile.mkdtemp(prefix="gmail-broker-", dir="/tmp")) / "broker.sock"
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
                        "access_token": "ya29.runtime-only",
                        "account_subject": "candidate@example.com",
                        "scope": GMAIL_READONLY_SCOPE,
                        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                    }
                ).encode("utf-8")
            )

    thread = threading.Thread(target=broker)
    thread.start()
    assert ready.wait(timeout=2)

    resolver = UnixGmailCredentialBroker(socket_path=socket_path, now=lambda: NOW)
    token = resolver.resolve(
        GmailCredentialHandle(
            opaque_handle="gmail:opaque-handle",
            account_subject="candidate@example.com",
            granted_scopes=(GMAIL_READONLY_SCOPE,),
        )
    )
    thread.join(timeout=2)

    assert captured_request == {
        "version": "careerops.gmail.credential-broker.v1",
        "provider": "gmail",
        "opaque_handle": "gmail:opaque-handle",
        "account_subject": "candidate@example.com",
        "scope": GMAIL_READONLY_SCOPE,
    }
    assert token.value == "ya29.runtime-only"
    assert token.account_subject == "candidate@example.com"
    assert token.granted_scopes == (GMAIL_READONLY_SCOPE,)
    assert "runtime-only" not in repr(resolver)


def test_broker_rejects_mismatched_or_expired_envelopes() -> None:
    with pytest.raises(GmailCredentialBrokerError, match="scope mismatch"):
        GmailBrokerCredentialEnvelope.from_mapping(
            {
                "access_token": "ya29.token",
                "account_subject": "candidate@example.com",
                "scope": "https://www.googleapis.com/auth/gmail.metadata",
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            },
            expected_account_subject="candidate@example.com",
            now=NOW,
        )

    with pytest.raises(GmailCredentialBrokerError, match="account mismatch"):
        GmailBrokerCredentialEnvelope.from_mapping(
            {
                "access_token": "ya29.token",
                "account_subject": "other@example.com",
                "scope": GMAIL_READONLY_SCOPE,
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            },
            expected_account_subject="candidate@example.com",
            now=NOW,
        )

    with pytest.raises(GmailCredentialBrokerError, match="expired"):
        GmailBrokerCredentialEnvelope.from_mapping(
            {
                "access_token": "ya29.token",
                "account_subject": "candidate@example.com",
                "scope": GMAIL_READONLY_SCOPE,
                "expires_at": (NOW - timedelta(seconds=1)).isoformat(),
            },
            expected_account_subject="candidate@example.com",
            now=NOW,
        )
