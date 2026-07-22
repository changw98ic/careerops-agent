from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime

import pytest

from careerops.infrastructure.google_oauth import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SEND_SCOPE,
    GOOGLE_AUTH_URI,
    GOOGLE_REVOKE_URI,
    GOOGLE_TOKEN_URI,
    GoogleInstalledClient,
    GoogleOAuthClient,
    GoogleOAuthError,
    GoogleOAuthTokens,
    HttpRequest,
    HttpResponse,
    InMemoryGoogleOAuthVault,
    new_oauth_handle,
    scope_for_capability,
    urllib_transport,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _client_secret_json(**overrides: object) -> str:
    installed: dict[str, object] = {
        "client_id": "client-id.apps.googleusercontent.com",
        "client_secret": "client-secret-value",
        "project_id": "career-test-project",
        "auth_uri": GOOGLE_AUTH_URI,
        "token_uri": GOOGLE_TOKEN_URI,
        "redirect_uris": ["http://localhost"],
    }
    installed.update(overrides)
    return json.dumps({"installed": installed})


def test_installed_client_parser_requires_fixed_google_endpoints_and_redacts_repr() -> None:
    installed_client = GoogleInstalledClient.from_json(_client_secret_json())

    assert installed_client.client_id == "client-id.apps.googleusercontent.com"
    assert installed_client.client_secret == "client-secret-value"
    assert installed_client.redirect_uri == "http://localhost"
    assert installed_client.project_id == "career-test-project"
    assert "client-secret-value" not in repr(installed_client)

    with pytest.raises(ValueError, match="auth_uri"):
        GoogleInstalledClient.from_json(_client_secret_json(auth_uri="https://evil.example/auth"))

    with pytest.raises(ValueError, match="token_uri"):
        GoogleInstalledClient.from_json(_client_secret_json(token_uri="https://evil.example/token"))

    with pytest.raises(ValueError, match="loopback"):
        GoogleInstalledClient.from_json(
            _client_secret_json(redirect_uris=["https://evil.example/callback"])
        )


def test_installed_client_accepts_google_downloaded_desktop_auth_uri() -> None:
    installed_client = GoogleInstalledClient.from_json(
        _client_secret_json(auth_uri="https://accounts.google.com/o/oauth2/auth")
    )

    assert installed_client.project_id == "career-test-project"


def test_capabilities_map_to_exactly_one_gmail_scope() -> None:
    assert scope_for_capability("gmail_readonly") == GMAIL_READONLY_SCOPE
    assert scope_for_capability("gmail_send") == GMAIL_SEND_SCOPE

    with pytest.raises(ValueError, match="unsupported"):
        scope_for_capability("gmail_wide")


def test_authorization_url_contains_state_pkce_and_offline_consent_parameters() -> None:
    oauth = GoogleOAuthClient(
        installed_client=GoogleInstalledClient.from_json(_client_secret_json()),
        now=lambda: NOW,
    )
    session = oauth.authorization_url(
        capability="gmail_readonly",
        redirect_uri="http://127.0.0.1:43123/oauth2/callback",
        state="state-123",
        code_verifier="verifier-value-long-enough",
        login_hint="jobs@example.com",
    )

    parsed = urllib.parse.urlparse(session.authorization_url)
    params = urllib.parse.parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == GOOGLE_AUTH_URI
    assert params["scope"] == [GMAIL_READONLY_SCOPE]
    assert params["state"] == ["state-123"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["include_granted_scopes"] == ["false"]
    assert params["response_type"] == ["code"]
    assert params["login_hint"] == ["jobs@example.com"]
    assert "verifier-value-long-enough" not in repr(session)
    assert session.authorization_url not in repr(session)


def test_authorization_code_exchange_requires_refresh_token_expiry_and_exact_scope() -> None:
    requests: list[HttpRequest] = []

    def transport(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        return HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "access_token": "ya29.access-token",
                    "refresh_token": "refresh-token-value",
                    "expires_in": 3600,
                    "scope": GMAIL_SEND_SCOPE,
                    "token_type": "Bearer",
                }
            ).encode("utf-8"),
        )

    oauth = GoogleOAuthClient(
        installed_client=GoogleInstalledClient.from_json(_client_secret_json()),
        transport=transport,
        now=lambda: NOW,
    )
    session = oauth.authorization_url(
        capability="gmail_send",
        redirect_uri="http://127.0.0.1:43123/oauth2/callback",
        state="state-123",
        code_verifier="verifier-value-long-enough",
    )
    tokens = oauth.exchange_authorization_code(code="authorization-code", session=session)

    assert requests[0].url == GOOGLE_TOKEN_URI
    assert requests[0].method == "POST"
    body = urllib.parse.parse_qs(requests[0].body.decode("utf-8"))
    assert body["grant_type"] == ["authorization_code"]
    assert body["code_verifier"] == ["verifier-value-long-enough"]
    assert tokens.refresh_token == "refresh-token-value"
    assert tokens.scope == GMAIL_SEND_SCOPE
    assert tokens.expires_at > NOW
    assert "refresh-token-value" not in repr(tokens)

    def missing_refresh_transport(request: HttpRequest) -> HttpResponse:
        del request
        return HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "access_token": "ya29.access-token",
                    "expires_in": 3600,
                    "scope": GMAIL_SEND_SCOPE,
                    "token_type": "Bearer",
                }
            ).encode("utf-8"),
        )

    oauth_without_refresh = GoogleOAuthClient(
        installed_client=GoogleInstalledClient.from_json(_client_secret_json()),
        transport=missing_refresh_transport,
        now=lambda: NOW,
    )
    with pytest.raises(GoogleOAuthError, match="refresh_token"):
        oauth_without_refresh.exchange_authorization_code(
            code="authorization-code",
            session=session,
        )


def test_exchange_and_refresh_reject_scope_mismatch() -> None:
    def readonly_transport(request: HttpRequest) -> HttpResponse:
        del request
        return HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "access_token": "ya29.access-token",
                    "refresh_token": "refresh-token-value",
                    "expires_in": 3600,
                    "scope": GMAIL_READONLY_SCOPE,
                    "token_type": "Bearer",
                }
            ).encode("utf-8"),
        )

    oauth = GoogleOAuthClient(
        installed_client=GoogleInstalledClient.from_json(_client_secret_json()),
        transport=readonly_transport,
        now=lambda: NOW,
    )
    send_session = oauth.authorization_url(
        capability="gmail_send",
        redirect_uri="http://127.0.0.1:43123/oauth2/callback",
        state="state-123",
        code_verifier="verifier-value-long-enough",
    )

    with pytest.raises(GoogleOAuthError, match="scope mismatch"):
        oauth.exchange_authorization_code(code="authorization-code", session=send_session)

    def wide_refresh_transport(request: HttpRequest) -> HttpResponse:
        del request
        return HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "access_token": "ya29.new-access",
                    "expires_in": 3600,
                    "scope": f"{GMAIL_READONLY_SCOPE} {GMAIL_SEND_SCOPE}",
                    "token_type": "Bearer",
                }
            ).encode("utf-8"),
        )

    wide_oauth = GoogleOAuthClient(
        installed_client=GoogleInstalledClient.from_json(_client_secret_json()),
        transport=wide_refresh_transport,
        now=lambda: NOW,
    )
    tokens = GoogleOAuthTokens(
        access_token="ya29.old-access",
        refresh_token="refresh-token-value",
        expires_at=NOW,
        capability="gmail_readonly",
        scope=GMAIL_READONLY_SCOPE,
    )
    with pytest.raises(GoogleOAuthError, match="scope mismatch"):
        wide_oauth.refresh(tokens)


def test_refresh_reuses_original_refresh_token_and_revoke_posts_to_google_revoke_endpoint() -> None:
    requests: list[HttpRequest] = []

    def transport(request: HttpRequest) -> HttpResponse:
        requests.append(request)
        if request.url == GOOGLE_REVOKE_URI:
            return HttpResponse(status=200, headers={}, body=b"")
        return HttpResponse(
            status=200,
            headers={},
            body=json.dumps(
                {
                    "access_token": "ya29.new-access",
                    "expires_in": 120,
                    "scope": GMAIL_READONLY_SCOPE,
                    "token_type": "Bearer",
                }
            ).encode("utf-8"),
        )

    oauth = GoogleOAuthClient(
        installed_client=GoogleInstalledClient.from_json(_client_secret_json()),
        transport=transport,
        now=lambda: NOW,
    )
    tokens = GoogleOAuthTokens(
        access_token="ya29.old-access",
        refresh_token="refresh-token-value",
        expires_at=NOW,
        capability="gmail_readonly",
        scope=GMAIL_READONLY_SCOPE,
    )

    refreshed = oauth.refresh(tokens)
    oauth.revoke(refreshed.refresh_token)

    assert refreshed.access_token == "ya29.new-access"
    assert refreshed.refresh_token == "refresh-token-value"
    assert requests[0].url == GOOGLE_TOKEN_URI
    refresh_body = urllib.parse.parse_qs(requests[0].body.decode("utf-8"))
    assert refresh_body["scope"] == [GMAIL_READONLY_SCOPE]
    assert requests[1].url == GOOGLE_REVOKE_URI


def test_http_envelopes_redact_secret_bodies() -> None:
    request = HttpRequest(method="POST", url=GOOGLE_TOKEN_URI, headers={}, body=b"secret-body")
    response = HttpResponse(status=200, headers={}, body=b"secret-response")

    assert "secret-body" not in repr(request)
    assert "secret-response" not in repr(response)


def test_http_envelopes_redact_sensitive_headers_case_insensitively() -> None:
    request = HttpRequest(
        method="POST",
        url=GOOGLE_TOKEN_URI,
        headers={
            "Authorization": "Bearer oauth-request-secret-123",
            "proxy-authorization": "Basic proxy-secret-456",
            "COOKIE": "session=request-cookie-secret-789",
            "X-API-Key": "request-api-key-secret-abc",
            "Accept": "application/json",
        },
        body=b"request-body-secret",
    )
    response = HttpResponse(
        status=200,
        headers={
            "set-cookie": "session=response-cookie-secret-def",
            "Api-Key": "response-api-key-secret-ghi",
            "Content-Type": "application/json",
        },
        body=b"response-body-secret",
    )

    rendered = f"{request!r}\n{response!r}"

    for secret in (
        "oauth-request-secret-123",
        "proxy-secret-456",
        "request-cookie-secret-789",
        "request-api-key-secret-abc",
        "response-cookie-secret-def",
        "response-api-key-secret-ghi",
        "request-body-secret",
        "response-body-secret",
    ):
        assert secret not in rendered
    for safe_fragment in (
        "'Authorization': '<redacted>'",
        "'proxy-authorization': '<redacted>'",
        "'COOKIE': '<redacted>'",
        "'X-API-Key': '<redacted>'",
        "'set-cookie': '<redacted>'",
        "'Api-Key': '<redacted>'",
        "'Accept': 'application/json'",
        "'Content-Type': 'application/json'",
    ):
        assert safe_fragment in rendered


def test_urllib_transport_rejects_non_allowlisted_urls_and_methods_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def forbidden_urlopen(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(
        "careerops.infrastructure.google_oauth.urllib.request.urlopen",
        forbidden_urlopen,
    )

    for request in (
        HttpRequest(method="POST", url="file:///etc/passwd", headers={}, body=b""),
        HttpRequest(method="POST", url="https://evil.example/token", headers={}, body=b""),
        HttpRequest(method="GET", url=GOOGLE_TOKEN_URI, headers={}, body=b""),
    ):
        with pytest.raises(GoogleOAuthError, match="non-allowlisted endpoint"):
            urllib_transport(request)

    assert calls == 0


def test_in_memory_vault_validates_opaque_handles() -> None:
    tokens = GoogleOAuthTokens(
        access_token="ya29.access-token",
        refresh_token="refresh-token-value",
        expires_at=NOW,
        capability="gmail_readonly",
        scope=GMAIL_READONLY_SCOPE,
    )
    handle = new_oauth_handle(capability="gmail_readonly")
    memory = InMemoryGoogleOAuthVault()
    memory.put(handle, tokens)

    assert memory.get(handle) == tokens
    with pytest.raises(ValueError, match="opaque handle"):
        memory.put("bad handle with spaces", tokens)
