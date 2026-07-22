from __future__ import annotations

import hashlib
import json
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from careerops.cli.gmail_broker import _pkce_challenge
from careerops.infrastructure.gmail import oauth_broker
from careerops.infrastructure.gmail.broker_server import (
    READONLY_CREDENTIAL_BROKER_VERSION,
    BrokerServerError,
)
from careerops.infrastructure.gmail.client import (
    GmailApiError,
    GmailMetadataMessage,
    GmailSentSmokeProof,
)
from careerops.infrastructure.gmail.credentials import GmailAccessToken
from careerops.infrastructure.gmail.oauth_broker import (
    CLIENT_KEYCHAIN_SERVICE,
    GMAIL_PROFILE_URI,
    GmailOAuthBrokerError,
    InMemorySecretStore,
    KeychainGmailOAuthBrokerHost,
    MacOSKeychainSecretStore,
)
from careerops.infrastructure.gmail.send_client import GmailSendReceipt
from careerops.infrastructure.gmail.send_credentials import GmailSendAccessToken
from careerops.infrastructure.google_oauth import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
    GOOGLE_AUTH_URI,
    GOOGLE_REVOKE_URI,
    GOOGLE_TOKEN_URI,
    GoogleOAuthTokens,
    HttpRequest,
    HttpResponse,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Request:
    scope: str
    profile: str
    scopes: tuple[str, ...]
    redirect_uri: str
    state: str
    code_challenge: str
    code_verifier: str = "verifier-value-long-enough"
    code_challenge_method: str = "S256"
    account_subject: str | None = None


@dataclass(frozen=True, slots=True)
class _Grant:
    scope: str
    profile: str
    scopes: tuple[str, ...]
    redirect_uri: str
    code: str
    state: str
    code_verifier: str
    account_subject: str | None = None


def test_import_client_validates_profile_and_stores_raw_json_in_fixed_keychain_slot() -> None:
    client_store = InMemorySecretStore()
    host = KeychainGmailOAuthBrokerHost(
        client_store=client_store,
        credential_store=InMemorySecretStore(),
    )
    payload = _client_payload()

    result = host.import_client(payload, label="readonly")

    assert result == {
        "client_configured": True,
        "ok": True,
        "profile": "readonly",
        "scope": "readonly",
    }
    assert json.loads(client_store.get("readonly")) == payload
    with pytest.raises(ValueError, match="readonly or send"):
        host.import_client(payload, label="wide")
    with pytest.raises(ValueError, match="installed client"):
        host.import_client({"web": {}}, label="send")


def test_macos_keychain_store_uses_fixed_service_and_in_process_backend() -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.values: dict[tuple[str, str], str] = {}
            self.calls: list[tuple[str, str, str]] = []

        def put(self, service: str, account: str, value: str) -> None:
            self.calls.append(("put", service, account))
            self.values[(service, account)] = value

        def get(self, service: str, account: str) -> str:
            self.calls.append(("get", service, account))
            return self.values[(service, account)]

        def delete(self, service: str, account: str) -> None:
            self.calls.append(("delete", service, account))
            self.values.pop((service, account), None)

    backend = FakeBackend()
    store = MacOSKeychainSecretStore(service=CLIENT_KEYCHAIN_SERVICE, backend=backend)
    store.put("send", '{"client_secret":"secret-value"}')
    assert store.get("send") == '{"client_secret":"secret-value"}'
    store.delete("send")

    assert backend.calls == [
        ("put", CLIENT_KEYCHAIN_SERVICE, "send"),
        ("get", CLIENT_KEYCHAIN_SERVICE, "send"),
        ("delete", CLIENT_KEYCHAIN_SERVICE, "send"),
    ]


def test_gmail_oauth_credential_record_repr_redacts_handle_and_tokens() -> None:
    raw_handle = "gmail:readonly:opaque-handle-client-secret-value"
    access_token = "ya29.readonly-access"
    refresh_token = "refresh-token-value"
    record = oauth_broker.GmailOAuthCredentialRecord(
        handle=raw_handle,
        account_subject="candidate@example.com",
        profile="readonly",
        capability="gmail_readonly",
        scope=GMAIL_READONLY_SCOPE,
        tokens=GoogleOAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=NOW + timedelta(hours=1),
            capability="gmail_readonly",
            scope=GMAIL_READONLY_SCOPE,
        ),
        active=True,
        live_qualified=True,
    )

    rendered = repr(record)

    assert "handle_sha256=" in rendered
    assert "tokens=<redacted>" in rendered
    assert raw_handle not in rendered
    assert "opaque-handle" not in rendered
    assert "client-secret-value" not in rendered
    assert access_token not in rendered
    assert refresh_token not in rendered


def test_build_authorization_url_uses_cli_pkce_challenge_and_exact_readonly_scope() -> None:
    host = _host()
    host.import_client(_client_payload(), label="readonly")

    url = host.build_authorization_url(
        _Request(
            scope="readonly",
            profile="readonly",
            scopes=(GMAIL_READONLY_SCOPE,),
            redirect_uri="http://127.0.0.1:43123/oauth2/callback",
            state="state-123",
            code_challenge=_pkce_challenge("verifier-value-long-enough"),
            account_subject="candidate@example.com",
        )
    )

    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == GOOGLE_AUTH_URI
    assert params["scope"] == [GMAIL_READONLY_SCOPE]
    assert params["state"] == ["state-123"]
    assert params["code_challenge"] == [_pkce_challenge("verifier-value-long-enough")]
    assert params["include_granted_scopes"] == ["false"]
    assert params["login_hint"] == ["candidate@example.com"]
    with pytest.raises(ValueError, match="exactly one scope"):
        host.build_authorization_url(
            _Request(
                scope="readonly",
                profile="readonly",
                scopes=(GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE),
                redirect_uri="http://127.0.0.1:43123/oauth2/callback",
                state="state-123",
                code_challenge=_pkce_challenge("verifier-value-long-enough"),
            )
        )


def test_readonly_exchange_verifies_gmail_profile_before_storing_active_handle() -> None:
    requests: list[HttpRequest] = []
    host = _host(transport=_transport(requests))
    host.import_client(_client_payload(), label="readonly")

    result = host.exchange_authorization_code(
        _Grant(
            scope="readonly",
            profile="readonly",
            scopes=(GMAIL_READONLY_SCOPE,),
            redirect_uri="http://127.0.0.1:43123/oauth2/callback",
            code="authorization-code",
            state="state-123",
            code_verifier="verifier-value-long-enough",
            account_subject="candidate@example.com",
        )
    )
    status = host.status()
    credentials = cast("Mapping[str, Mapping[str, object]]", status["credentials"])

    assert result["account_subject"] == "candidate@example.com"
    assert result["active"] is True
    assert result["scope"] == "readonly"
    assert "handle" not in result
    assert len(cast("str", result["handle_sha256"])) == 64
    assert credentials["readonly"]["active"] is True
    assert "handle" not in credentials["readonly"]
    assert len(cast("str", credentials["readonly"]["handle_sha256"])) == 64
    assert requests[0].url == GOOGLE_TOKEN_URI
    assert requests[1].url == GMAIL_PROFILE_URI
    assert requests[1].headers["Authorization"] == "Bearer ya29.readonly-access"
    assert "refresh-token-value" not in json.dumps(result)


def test_readonly_exchange_rejects_unexpected_gmail_profile_account() -> None:
    host = _host(transport=_transport([], profile_email="other@example.com"))
    host.import_client(_client_payload(), label="readonly")

    with pytest.raises(GmailOAuthBrokerError, match="expected account"):
        host.exchange_authorization_code(
            _Grant(
                scope="readonly",
                profile="readonly",
                scopes=(GMAIL_READONLY_SCOPE,),
                redirect_uri="http://127.0.0.1:43123/oauth2/callback",
                code="authorization-code",
                state="state-123",
                code_verifier="verifier-value-long-enough",
                account_subject="candidate@example.com",
            )
        )
    credentials = cast("Mapping[str, Mapping[str, object]]", host.status()["credentials"])
    assert credentials["readonly"] == {
        "configured": False,
        "profile": "readonly",
    }


def test_send_exchange_requires_operator_bound_address_and_stores_unqualified_credential() -> None:
    host = _host(transport=_transport([], scope=GMAIL_SEND_SCOPE, access_token="ya29.send-access"))
    host.import_client(_client_payload(), label="send")

    with pytest.raises(ValueError, match="expected_account_subject"):
        host.exchange_authorization_code(
            _Grant(
                scope="send",
                profile="send",
                scopes=(GMAIL_SEND_SCOPE,),
                redirect_uri="http://127.0.0.1:43123/oauth2/callback",
                code="authorization-code",
                state="state-123",
                code_verifier="verifier-value-long-enough",
            )
        )

    result = host.exchange_authorization_code(
        _Grant(
            scope="send",
            profile="send",
            scopes=(GMAIL_SEND_SCOPE,),
            redirect_uri="http://127.0.0.1:43123/oauth2/callback",
            code="send-authorization-code",
            state="state-123",
            code_verifier="verifier-value-long-enough",
            account_subject="candidate@example.com",
        )
    )

    assert result["account_subject"] == "candidate@example.com"
    assert result["active"] is True
    assert result["account_bound_by_operator"] is True
    assert result["live_qualified"] is False
    internal_credentials = cast(
        "Mapping[str, Mapping[str, object]]",
        host.local_onboarding_status()["credentials"],
    )
    with pytest.raises(BrokerServerError, match="live qualified"):
        host.issue_token(
            opaque_handle=cast("str", internal_credentials["send"]["handle"]),
            account_subject="candidate@example.com",
            scope=GMAIL_SEND_SCOPE,
            action="send_email",
        )


def test_smoke_send_qualifies_send_credential_when_sent_metadata_matches() -> None:
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FakeReadonlyClient(send_client)
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)

    result = host.smoke_send(account_subject="candidate@example.com")
    status = host.status()
    credentials = cast("Mapping[str, Mapping[str, object]]", status["credentials"])

    assert result["account_subject"] == "candidate@example.com"
    assert result["live_qualified"] is True
    assert isinstance(result["evidence_sha256"], str)
    assert len(result["evidence_sha256"]) == 64
    assert result["evidence_sha256"].isalnum()
    assert result["evidence_sha256"].lower() == result["evidence_sha256"]
    assert credentials["send"]["live_qualified"] is True
    assert credentials["send"]["qualification_evidence_sha256"] == result["evidence_sha256"]
    assert send_client.payload is not None
    assert send_client.token is not None
    assert readonly_client.token is not None
    assert send_client.token.account_subject == "candidate@example.com"
    assert send_client.token.granted_scopes == (GMAIL_SEND_SCOPE,)
    assert readonly_client.token.account_subject == "candidate@example.com"
    assert readonly_client.token.granted_scopes == (GMAIL_READONLY_SCOPE,)
    assert readonly_client.message_id == "smoke-message-1"
    assert readonly_client.metadata_headers == (
        "From",
        "To",
        "Subject",
        "Message-ID",
    )
    internal_credentials = cast(
        "Mapping[str, Mapping[str, object]]",
        host.local_onboarding_status()["credentials"],
    )
    qualified_status = cast("Mapping[str, object]", internal_credentials["send"])
    envelope = host.issue_token(
        opaque_handle=cast("str", qualified_status["handle"]),
        account_subject="candidate@example.com",
        scope=GMAIL_SEND_SCOPE,
        action="send_email",
    )
    assert envelope.access_token == "ya29.send-access"


def test_smoke_send_is_idempotent_after_live_qualification() -> None:
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FakeReadonlyClient(send_client)
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)

    first = host.smoke_send(account_subject="candidate@example.com")
    readonly_message_id = readonly_client.message_id

    second = host.smoke_send(account_subject=" CANDIDATE@EXAMPLE.COM ")

    assert second["already_qualified"] is True
    assert second["live_qualified"] is True
    assert second["evidence_sha256"] == first["evidence_sha256"]
    assert send_client.send_count == 1
    assert readonly_client.message_id == readonly_message_id


def test_smoke_send_qualifies_when_provider_rewrites_rfc_message_id_but_sent_metadata_matches() -> (
    None
):
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FakeReadonlyClient(
        send_client,
        message_id_header="<provider.rewritten.message-id@example.gmail>",
    )
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)

    result = host.smoke_send(account_subject="candidate@example.com")
    credentials = cast("Mapping[str, Mapping[str, object]]", host.status()["credentials"])

    assert result["live_qualified"] is True
    assert credentials["send"]["live_qualified"] is True
    assert readonly_client.message_id == "smoke-message-1"
    assert readonly_client.metadata_headers == (
        "From",
        "To",
        "Subject",
        "Message-ID",
    )
    assert send_client.payload is not None
    assert readonly_client.observed_headers["from"] == send_client.payload.sender
    assert readonly_client.observed_headers["to"] == send_client.payload.recipient
    assert readonly_client.observed_headers["subject"] == send_client.payload.subject


def test_smoke_send_qualifies_with_exact_sent_lookup_after_get_transport_error() -> None:
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FallbackReadonlyClient(send_client)
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)

    result = host.smoke_send(account_subject="candidate@example.com")
    credentials = cast("Mapping[str, Mapping[str, object]]", host.status()["credentials"])

    assert result["live_qualified"] is True
    assert credentials["send"]["live_qualified"] is True
    assert send_client.send_count == 1
    assert readonly_client.provider_get_attempts == 1
    assert readonly_client.exact_lookup_attempts == 1


def test_smoke_send_fails_closed_when_exact_sent_lookup_has_no_match_after_provider_get_error() -> (
    None
):
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FallbackReadonlyClient(send_client, fallback_message=None)
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)

    with pytest.raises(GmailOAuthBrokerError, match="evidence"):
        host.smoke_send(account_subject="candidate@example.com")

    credentials = cast("Mapping[str, Mapping[str, object]]", host.status()["credentials"])
    assert credentials["send"]["live_qualified"] is False
    assert credentials["send"]["qualification_evidence_sha256"] is None
    assert send_client.send_count == 1
    assert readonly_client.exact_lookup_attempts == 1


def test_smoke_send_fails_closed_when_exact_sent_lookup_is_ambiguous_after_provider_get_error() -> (
    None
):
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FallbackReadonlyClient(
        send_client,
        fallback_error=GmailApiError("GMAIL_SENT_SMOKE_LOOKUP_AMBIGUOUS"),
    )
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)

    with pytest.raises(GmailOAuthBrokerError, match="evidence"):
        host.smoke_send(account_subject="candidate@example.com")

    credentials = cast("Mapping[str, Mapping[str, object]]", host.status()["credentials"])
    assert credentials["send"]["live_qualified"] is False
    assert credentials["send"]["qualification_evidence_sha256"] is None
    assert send_client.send_count == 1
    assert readonly_client.exact_lookup_attempts == 1


def test_smoke_send_rejects_account_mismatch_without_qualifying_send_credential() -> None:
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FakeReadonlyClient(send_client)
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)

    with pytest.raises(GmailOAuthBrokerError, match="account"):
        host.smoke_send(account_subject="other@example.com")

    credentials = cast("Mapping[str, Mapping[str, object]]", host.status()["credentials"])
    assert credentials["send"]["live_qualified"] is False
    assert credentials["send"]["qualification_evidence_sha256"] is None
    assert send_client.payload is None
    assert readonly_client.message_id is None


def test_smoke_send_rejects_sent_metadata_mismatch_without_qualifying_send_credential() -> None:
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FakeReadonlyClient(send_client, subject="unexpected subject")
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)

    with pytest.raises(GmailOAuthBrokerError, match="evidence"):
        host.smoke_send(account_subject="candidate@example.com")

    credentials = cast("Mapping[str, Mapping[str, object]]", host.status()["credentials"])
    assert credentials["send"]["live_qualified"] is False
    assert credentials["send"]["qualification_evidence_sha256"] is None


def test_recover_smoke_send_qualifies_from_one_exact_recent_proof_without_sending() -> None:
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FakeReadonlyClient(
        send_client,
        recent_smoke_proofs=(_smoke_recovery_proof(),),
    )
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)

    result = host.recover_smoke_send(account_subject="candidate@example.com")
    credentials = cast("Mapping[str, Mapping[str, object]]", host.status()["credentials"])

    assert result["live_qualified"] is True
    assert result["recovered_without_send"] is True
    assert result["verification_path"] == "unique_recent_exact_raw_sent_message"
    assert credentials["send"]["live_qualified"] is True
    assert credentials["send"]["qualification_evidence_sha256"] == result["evidence_sha256"]
    assert send_client.send_count == 0
    assert send_client.payload is None
    assert readonly_client.recent_lookup_attempts == 1
    assert readonly_client.recent_account_subject == "candidate@example.com"
    assert readonly_client.recent_token is not None
    assert readonly_client.recent_token.granted_scopes == (GMAIL_READONLY_SCOPE,)
    assert readonly_client.recent_expected_body_sha256 == _smoke_body_sha256()
    assert readonly_client.recent_not_before == NOW - timedelta(days=1)


def test_recover_smoke_send_fails_closed_when_no_exact_recent_proof_exists() -> None:
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FakeReadonlyClient(send_client, recent_smoke_proofs=())
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)

    with pytest.raises(GmailOAuthBrokerError, match="one exact recent Sent message"):
        host.recover_smoke_send(account_subject="candidate@example.com")

    credentials = cast("Mapping[str, Mapping[str, object]]", host.status()["credentials"])
    assert credentials["send"]["live_qualified"] is False
    assert credentials["send"]["qualification_evidence_sha256"] is None
    assert send_client.send_count == 0
    assert readonly_client.recent_lookup_attempts == 1


def test_recover_smoke_send_fails_closed_when_recent_proofs_are_ambiguous() -> None:
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FakeReadonlyClient(
        send_client,
        recent_smoke_proofs=(
            _smoke_recovery_proof(nonce="0123456789abcdef", provider_message_id="smoke-1"),
            _smoke_recovery_proof(nonce="fedcba9876543210", provider_message_id="smoke-2"),
        ),
    )
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)

    with pytest.raises(GmailOAuthBrokerError, match="one exact recent Sent message"):
        host.recover_smoke_send(account_subject="candidate@example.com")

    credentials = cast("Mapping[str, Mapping[str, object]]", host.status()["credentials"])
    assert credentials["send"]["live_qualified"] is False
    assert credentials["send"]["qualification_evidence_sha256"] is None
    assert send_client.send_count == 0
    assert readonly_client.recent_lookup_attempts == 1


def test_recover_smoke_send_uses_explicit_subject_digest_to_resolve_retry_ambiguity() -> None:
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    selected_nonce = "fedcba9876543210"
    readonly_client = _FakeReadonlyClient(
        send_client,
        recent_smoke_proofs=(
            _smoke_recovery_proof(nonce="0123456789abcdef", provider_message_id="smoke-1"),
            _smoke_recovery_proof(nonce=selected_nonce, provider_message_id="smoke-2"),
        ),
    )
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)
    subject = f"CareerOps Gmail qualification {selected_nonce}"

    result = host.recover_smoke_send(
        account_subject="candidate@example.com",
        expected_subject_sha256=hashlib.sha256(subject.encode()).hexdigest(),
    )

    assert result["live_qualified"] is True
    assert result["recovered_without_send"] is True
    assert result["verification_path"] == "expected_subject_exact_raw_sent_message"
    assert send_client.send_count == 0
    assert readonly_client.recent_lookup_attempts == 1


def test_recover_smoke_send_rejects_account_mismatch_before_readonly_search() -> None:
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FakeReadonlyClient(
        send_client,
        recent_smoke_proofs=(_smoke_recovery_proof(),),
    )
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)

    with pytest.raises(GmailOAuthBrokerError, match="matching readonly and send accounts"):
        host.recover_smoke_send(account_subject="other@example.com")

    credentials = cast("Mapping[str, Mapping[str, object]]", host.status()["credentials"])
    assert credentials["send"]["live_qualified"] is False
    assert send_client.send_count == 0
    assert readonly_client.recent_lookup_attempts == 0


def test_recover_smoke_send_rejects_inactive_send_credential_before_readonly_search() -> None:
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FakeReadonlyClient(
        send_client,
        recent_smoke_proofs=(_smoke_recovery_proof(),),
    )
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)
    send_record = oauth_broker.GmailOAuthCredentialRecord.from_json(credential_store.get("send"))
    credential_store.put("send", replace(send_record, active=False).to_json())

    with pytest.raises(GmailOAuthBrokerError, match="active readonly and send credentials"):
        host.recover_smoke_send(account_subject="candidate@example.com")

    assert send_client.send_count == 0
    assert readonly_client.recent_lookup_attempts == 0


def test_recover_smoke_send_is_idempotent_when_send_credential_is_already_qualified() -> None:
    credential_store = InMemorySecretStore()
    send_client = _FakeSendClient()
    readonly_client = _FakeReadonlyClient(
        send_client,
        recent_smoke_proofs=(_smoke_recovery_proof(),),
    )
    host = _host(
        credential_store=credential_store,
        transport=_smoke_transport(),
        send_client=send_client,
        readonly_client=readonly_client,
    )
    _authorize_readonly(host)
    _authorize_send(host)
    first = host.recover_smoke_send(account_subject="candidate@example.com")
    lookup_count = readonly_client.recent_lookup_attempts

    second = host.recover_smoke_send(account_subject="candidate@example.com")

    assert second["already_qualified"] is True
    assert second["live_qualified"] is True
    assert second["evidence_sha256"] == first["evidence_sha256"]
    assert send_client.send_count == 0
    assert readonly_client.recent_lookup_attempts == lookup_count


def test_revoke_posts_refresh_token_to_google_and_deletes_fixed_credential_slot() -> None:
    requests: list[HttpRequest] = []
    credential_store = InMemorySecretStore()
    host = _host(credential_store=credential_store, transport=_transport(requests))
    host.import_client(_client_payload(), label="readonly")
    host.exchange_authorization_code(
        _Grant(
            scope="readonly",
            profile="readonly",
            scopes=(GMAIL_READONLY_SCOPE,),
            redirect_uri="http://127.0.0.1:43123/oauth2/callback",
            code="authorization-code",
            state="state-123",
            code_verifier="verifier-value-long-enough",
            account_subject="candidate@example.com",
        )
    )

    result = host.revoke(scope="readonly", account_subject="candidate@example.com")
    revoked = cast("Sequence[Mapping[str, object]]", result["revoked"])

    assert result["ok"] is True
    assert revoked[0]["account_subject"] == "candidate@example.com"
    assert requests[-1].url == GOOGLE_REVOKE_URI
    assert urllib.parse.parse_qs(requests[-1].body.decode()) == {"token": ["refresh-token-value"]}
    with pytest.raises(GmailOAuthBrokerError):
        credential_store.get("readonly")


def test_revoke_normalizes_account_subject_before_matching_credential() -> None:
    requests: list[HttpRequest] = []
    credential_store = InMemorySecretStore()
    host = _host(credential_store=credential_store, transport=_transport(requests))
    host.import_client(_client_payload(), label="readonly")
    host.exchange_authorization_code(
        _Grant(
            scope="readonly",
            profile="readonly",
            scopes=(GMAIL_READONLY_SCOPE,),
            redirect_uri="http://127.0.0.1:43123/oauth2/callback",
            code="authorization-code",
            state="state-123",
            code_verifier="verifier-value-long-enough",
            account_subject="candidate@example.com",
        )
    )

    result = host.revoke(scope="readonly", account_subject=" CANDIDATE@EXAMPLE.COM ")

    assert len(cast("Sequence[Mapping[str, object]]", result["revoked"])) == 1
    assert requests[-1].url == GOOGLE_REVOKE_URI
    with pytest.raises(GmailOAuthBrokerError):
        credential_store.get("readonly")


def test_issue_token_refreshes_expired_active_record_and_serves_existing_broker_protocol() -> None:
    requests: list[HttpRequest] = []
    credential_store = InMemorySecretStore()
    current_time = NOW
    host = KeychainGmailOAuthBrokerHost(
        client_store=InMemorySecretStore(),
        credential_store=credential_store,
        transport=_transport(requests, expires_in=1, refresh_access_token="ya29.refreshed"),
        now=lambda: current_time,
        token_urlsafe=lambda byte_count: f"opaque-{byte_count}",
    )
    host.import_client(_client_payload(), label="readonly")
    host.exchange_authorization_code(
        _Grant(
            scope="readonly",
            profile="readonly",
            scopes=(GMAIL_READONLY_SCOPE,),
            redirect_uri="http://127.0.0.1:43123/oauth2/callback",
            code="authorization-code",
            state="state-123",
            code_verifier="verifier-value-long-enough",
            account_subject="candidate@example.com",
        )
    )
    current_time = NOW + timedelta(minutes=2)
    internal_credentials = cast(
        "Mapping[str, Mapping[str, object]]",
        host.local_onboarding_status()["credentials"],
    )
    raw_handle = cast("str", internal_credentials["readonly"]["handle"])

    envelope = host.issue_token(
        opaque_handle=raw_handle,
        account_subject="candidate@example.com",
        scope=GMAIL_READONLY_SCOPE,
        action=None,
    )

    assert envelope.access_token == "ya29.refreshed"
    assert requests[-1].url == GOOGLE_TOKEN_URI
    assert urllib.parse.parse_qs(requests[-1].body.decode())["grant_type"] == ["refresh_token"]
    server = oauth_broker.GmailCredentialBrokerServer(
        socket_path=Path("/tmp/careerops-test-gmail.sock"),
        token_provider=host,
        now=lambda: current_time,
    )
    response = json.loads(
        server.handle_request_bytes(
            json.dumps(
                {
                    "account_subject": "candidate@example.com",
                    "opaque_handle": raw_handle,
                    "provider": "gmail",
                    "scope": GMAIL_READONLY_SCOPE,
                    "version": READONLY_CREDENTIAL_BROKER_VERSION,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
    )
    assert response["access_token"] == "ya29.refreshed"
    assert "refresh-token-value" not in json.dumps(response)


def test_serve_wires_readonly_and_send_credential_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple[Path, object]] = []

    class _StopServe(RuntimeError):
        pass

    class FakeServer:
        def __init__(self, *, socket_path: Path, token_provider: object, **kwargs: object) -> None:
            del kwargs
            created.append((socket_path, token_provider))

        def serve_forever(self, *, stop_event: object | None = None) -> None:
            del stop_event
            raise _StopServe

    host = _host()
    monkeypatch.setattr(oauth_broker, "GmailCredentialBrokerServer", FakeServer)

    with pytest.raises(_StopServe):
        host.serve(
            readonly_socket=Path("/tmp/readonly.sock"),
            send_socket=None,
            attachment_socket=None,
        )

    assert created == [(Path("/tmp/readonly.sock"), host)]
    with pytest.raises(RuntimeError, match="attachment_root"):
        host.serve(attachment_socket=Path("/tmp/attachment.sock"))


def _host(
    *,
    credential_store: InMemorySecretStore | None = None,
    transport: Any | None = None,
    send_client: Any | None = None,
    readonly_client: Any | None = None,
) -> KeychainGmailOAuthBrokerHost:
    return KeychainGmailOAuthBrokerHost(
        client_store=InMemorySecretStore(),
        credential_store=credential_store or InMemorySecretStore(),
        transport=transport or _transport([]),
        now=lambda: NOW,
        token_urlsafe=lambda byte_count: f"opaque-{byte_count}",
        send_client=send_client,
        readonly_client=readonly_client,
    )


class _FakeSendClient:
    def __init__(self) -> None:
        self.token: GmailSendAccessToken | None = None
        self.payload: Any | None = None
        self.attachments: Sequence[object] | None = None
        self.send_count = 0

    def send_message(
        self,
        token: GmailSendAccessToken,
        *,
        payload: Any,
        attachments: Sequence[object] = (),
    ) -> GmailSendReceipt:
        self.send_count += 1
        self.token = token
        self.payload = payload
        self.attachments = attachments
        return GmailSendReceipt(
            provider_message_id="smoke-message-1",
            provider_thread_id="smoke-thread-1",
        )


class _FakeReadonlyClient:
    def __init__(
        self,
        send_client: _FakeSendClient,
        *,
        label_ids: tuple[str, ...] = ("SENT",),
        from_header: str | None = None,
        to_header: str | None = None,
        subject: str | None = None,
        message_id_header: str | None = None,
        recent_smoke_proofs: tuple[GmailSentSmokeProof, ...] = (),
        recent_smoke_error: GmailApiError | None = None,
    ) -> None:
        self._send_client = send_client
        self._label_ids = label_ids
        self._from_header = from_header
        self._to_header = to_header
        self._subject = subject
        self._message_id_header = message_id_header
        self._recent_smoke_proofs = recent_smoke_proofs
        self._recent_smoke_error = recent_smoke_error
        self.token: GmailAccessToken | None = None
        self.message_id: str | None = None
        self.metadata_headers: tuple[str, ...] | None = None
        self.observed_headers: dict[str, str] = {}
        self.recent_lookup_attempts = 0
        self.recent_token: GmailAccessToken | None = None
        self.recent_account_subject: str | None = None
        self.recent_not_before: datetime | None = None
        self.recent_expected_body_sha256: str | None = None

    def get_message(
        self,
        token: GmailAccessToken,
        *,
        message_id: str,
        metadata_headers: Sequence[str],
    ) -> GmailMetadataMessage:
        if self._send_client.payload is None:
            raise AssertionError("readonly lookup happened before smoke send")
        payload = self._send_client.payload
        self.token = token
        self.message_id = message_id
        self.metadata_headers = tuple(metadata_headers)
        self.observed_headers = {
            "from": self._from_header or payload.sender,
            "to": self._to_header or payload.recipient,
            "subject": self._subject or payload.subject,
            "message-id": self._message_id_header or payload.message_id_header,
        }
        return GmailMetadataMessage(
            id=message_id,
            thread_id="smoke-thread-1",
            history_id="123",
            headers=self.observed_headers,
            label_ids=self._label_ids,
            snippet="CareerOps Gmail OAuth smoke send",
            internal_date=NOW,
        )

    def find_recent_sent_smoke_messages(
        self,
        token: GmailAccessToken,
        *,
        account_subject: str,
        not_before: datetime,
        expected_body_sha256: str,
    ) -> tuple[GmailSentSmokeProof, ...]:
        self.recent_lookup_attempts += 1
        self.recent_token = token
        self.recent_account_subject = account_subject
        self.recent_not_before = not_before
        self.recent_expected_body_sha256 = expected_body_sha256
        if self._recent_smoke_error is not None:
            raise self._recent_smoke_error
        return self._recent_smoke_proofs


class _FallbackReadonlyClient(_FakeReadonlyClient):
    def __init__(
        self,
        send_client: _FakeSendClient,
        *,
        fallback_message: GmailMetadataMessage | None | object = ...,
        fallback_error: GmailApiError | None = None,
    ) -> None:
        super().__init__(
            send_client, message_id_header="<provider.rewritten.message-id@example.gmail>"
        )
        self.provider_get_attempts = 0
        self.exact_lookup_attempts = 0
        self._fallback_message = fallback_message
        self._fallback_error = fallback_error

    def get_message(
        self,
        token: GmailAccessToken,
        *,
        message_id: str,
        metadata_headers: Sequence[str],
    ) -> GmailMetadataMessage:
        del token, message_id, metadata_headers
        self.provider_get_attempts += 1
        raise GmailApiError("GMAIL_TRANSPORT_ERROR")

    def find_sent_smoke_message(
        self,
        token: GmailAccessToken,
        *,
        provider_message_id: str,
        provider_thread_id: str,
        sender: str,
        recipient: str,
        subject: str,
        metadata_headers: Sequence[str],
    ) -> GmailMetadataMessage | None:
        del token, provider_message_id, provider_thread_id
        if self._send_client.payload is None:
            raise AssertionError("readonly lookup happened before smoke send")
        self.exact_lookup_attempts += 1
        self.metadata_headers = tuple(metadata_headers)
        if self._fallback_error is not None:
            raise self._fallback_error
        if self._fallback_message is None:
            return None
        if self._fallback_message is not ...:
            return cast("GmailMetadataMessage", self._fallback_message)
        self.observed_headers = {
            "from": sender,
            "to": recipient,
            "subject": subject,
            "message-id": "<provider.rewritten.message-id@example.gmail>",
        }
        return GmailMetadataMessage(
            id="smoke-message-1",
            thread_id="smoke-thread-1",
            history_id="123",
            headers=self.observed_headers,
            label_ids=("SENT",),
            snippet="CareerOps Gmail OAuth smoke send",
            internal_date=NOW,
        )


def _authorize_readonly(host: KeychainGmailOAuthBrokerHost) -> None:
    host.import_client(_client_payload(), label="readonly")
    host.exchange_authorization_code(
        _Grant(
            scope="readonly",
            profile="readonly",
            scopes=(GMAIL_READONLY_SCOPE,),
            redirect_uri="http://127.0.0.1:43123/oauth2/callback",
            code="authorization-code",
            state="state-123",
            code_verifier="verifier-value-long-enough",
            account_subject="candidate@example.com",
        )
    )


def _authorize_send(host: KeychainGmailOAuthBrokerHost) -> None:
    host.import_client(_client_payload(), label="send")
    host.exchange_authorization_code(
        _Grant(
            scope="send",
            profile="send",
            scopes=(GMAIL_SEND_SCOPE,),
            redirect_uri="http://127.0.0.1:43123/oauth2/callback",
            code="send-authorization-code",
            state="state-123",
            code_verifier="verifier-value-long-enough",
            account_subject="candidate@example.com",
        )
    )


def _smoke_recovery_proof(
    *,
    account: str = "candidate@example.com",
    nonce: str = "0123456789abcdef",
    provider_message_id: str = "smoke-message-1",
    provider_thread_id: str = "smoke-thread-1",
    label_ids: tuple[str, ...] = ("SENT",),
    message_id_header: str = "<provider.rewritten.message-id@example.gmail>",
) -> GmailSentSmokeProof:
    local_part, domain = account.split("@", 1)
    return GmailSentSmokeProof(
        message=GmailMetadataMessage(
            id=provider_message_id,
            thread_id=provider_thread_id,
            history_id="123",
            headers={
                "from": account,
                "to": f"{local_part}+careerops-smoke-{nonce}@{domain}",
                "subject": f"CareerOps Gmail qualification {nonce}",
                "message-id": message_id_header,
            },
            label_ids=label_ids,
            snippet="CareerOps Gmail OAuth smoke send",
            internal_date=NOW,
        ),
        body_sha256=_smoke_body_sha256(),
    )


def _smoke_body_sha256() -> str:
    body = (
        "CareerOps controlled Gmail OAuth qualification message. "
        "It verifies the reviewed local send path and may be safely deleted."
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _client_payload() -> Mapping[str, object]:
    return {
        "installed": {
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
            "client_id": "client-id.apps.googleusercontent.com",
            "client_secret": "client-secret-value",
            "project_id": "careerops-test",
            "redirect_uris": ["http://localhost"],
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _transport(
    requests: list[HttpRequest],
    *,
    scope: str = GMAIL_READONLY_SCOPE,
    access_token: str = "ya29.readonly-access",
    refresh_access_token: str = "ya29.refreshed-access",
    profile_email: str = "candidate@example.com",
    expires_in: int = 3600,
) -> Any:
    def transport(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        if request.url == GOOGLE_TOKEN_URI:
            body = urllib.parse.parse_qs(request.body.decode())
            if body["grant_type"] == ["refresh_token"]:
                return HttpResponse(
                    status=200,
                    headers={},
                    body=json.dumps(
                        {
                            "access_token": refresh_access_token,
                            "expires_in": 3600,
                            "scope": scope,
                            "token_type": "Bearer",
                        }
                    ).encode(),
                )
            return HttpResponse(
                status=200,
                headers={},
                body=json.dumps(
                    {
                        "access_token": access_token,
                        "expires_in": expires_in,
                        "refresh_token": "refresh-token-value",
                        "scope": scope,
                        "token_type": "Bearer",
                    }
                ).encode(),
            )
        if request.url == GMAIL_PROFILE_URI:
            return HttpResponse(
                status=200,
                headers={},
                body=json.dumps({"emailAddress": profile_email}).encode(),
            )
        if request.url == GOOGLE_REVOKE_URI:
            return HttpResponse(status=200, headers={}, body=b"")
        raise AssertionError(f"unexpected request: {request.url}")

    return transport


def _smoke_transport() -> Any:
    def transport(request: HttpRequest) -> HttpResponse:
        if request.url == GOOGLE_TOKEN_URI:
            body = urllib.parse.parse_qs(request.body.decode())
            requested_scope = body.get("scope", [None])[0]
            scope = (
                GMAIL_SEND_SCOPE
                if requested_scope == GMAIL_SEND_SCOPE
                or body.get("code") == ["send-authorization-code"]
                else GMAIL_READONLY_SCOPE
            )
            access_token = (
                "ya29.send-access" if scope == GMAIL_SEND_SCOPE else "ya29.readonly-access"
            )
            return HttpResponse(
                status=200,
                headers={},
                body=json.dumps(
                    {
                        "access_token": access_token,
                        "expires_in": 3600,
                        "refresh_token": "refresh-token-value",
                        "scope": scope,
                        "token_type": "Bearer",
                    }
                ).encode(),
            )
        if request.url == GMAIL_PROFILE_URI:
            return HttpResponse(
                status=200,
                headers={},
                body=json.dumps({"emailAddress": "candidate@example.com"}).encode(),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    return transport
