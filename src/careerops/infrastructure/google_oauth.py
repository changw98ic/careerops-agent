from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol, Self, cast

GOOGLE_AUTH_URI: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_DOWNLOADED_CLIENT_AUTH_URI: Final = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI: Final = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URI: Final = "https://oauth2.googleapis.com/revoke"
GMAIL_READONLY_SCOPE: Final = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE: Final = "https://www.googleapis.com/auth/gmail.send"

_GOOGLE_OAUTH_HTTP_ENDPOINTS: Final = frozenset({GOOGLE_TOKEN_URI, GOOGLE_REVOKE_URI})

_CAPABILITY_SCOPES: Final = {
    "gmail_readonly": GMAIL_READONLY_SCOPE,
    "gmail_send": GMAIL_SEND_SCOPE,
}
_BOUNDED_HANDLE = re.compile(r"^[A-Za-z0-9._:@/-]{1,255}$")
_SECRET_MIN_LENGTH: Final = 8
_SECRET_MAX_LENGTH: Final = 8192
_MAX_ERROR_DETAIL: Final = 160
_MAX_HTTP_RESPONSE_BYTES: Final = 1_048_576
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})
_EMAIL = re.compile(r"^[^@\s]{1,160}@[^@\s]{1,160}$")
_SENSITIVE_HTTP_HEADERS: Final = frozenset(
    {
        "api-key",
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
    }
)


class GoogleOAuthError(RuntimeError):
    """OAuth failure with bounded, non-secret details."""


class GoogleOAuthVault(Protocol):
    def put(self, handle: str, tokens: GoogleOAuthTokens) -> None: ...

    def get(self, handle: str) -> GoogleOAuthTokens: ...

    def delete(self, handle: str) -> None: ...


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes

    def __repr__(self) -> str:
        return (
            "HttpRequest("
            f"method={self.method!r}, url={self.url!r}, "
            f"headers={_redacted_http_headers(self.headers)!r}, "
            "body=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def __repr__(self) -> str:
        return (
            "HttpResponse("
            f"status={self.status!r}, headers={_redacted_http_headers(self.headers)!r}, "
            "body=<redacted>)"
        )


HttpTransport = Callable[[HttpRequest], HttpResponse]


@dataclass(frozen=True, slots=True)
class GoogleInstalledClient:
    client_id: str
    client_secret: str
    project_id: str
    redirect_uris: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_secret_value(self.client_id, "client_id")
        _validate_secret_value(self.client_secret, "client_secret")
        if not self.project_id or len(self.project_id) > 255:
            raise ValueError("project_id must be a bounded non-empty value")
        if not self.redirect_uris:
            raise ValueError("installed client must include at least one redirect_uri")
        for redirect_uri in self.redirect_uris:
            _validate_installed_redirect_uri(redirect_uri)
        object.__setattr__(self, "redirect_uris", tuple(self.redirect_uris))

    @property
    def redirect_uri(self) -> str:
        return self.redirect_uris[0]

    @classmethod
    def from_json(cls, value: str | bytes) -> Self:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("client secret json is invalid") from exc
        if not isinstance(decoded, dict):
            raise ValueError("client secret json must be an object")
        root = cast("Mapping[str, object]", decoded)
        installed = root.get("installed")
        if not isinstance(installed, dict):
            raise ValueError("client secret json must contain installed client")
        payload = cast("Mapping[str, object]", installed)
        if payload.get("auth_uri") not in {
            GOOGLE_AUTH_URI,
            GOOGLE_DOWNLOADED_CLIENT_AUTH_URI,
        }:
            raise ValueError("installed client auth_uri must be Google's fixed OAuth endpoint")
        if payload.get("token_uri") != GOOGLE_TOKEN_URI:
            raise ValueError("installed client token_uri must be Google's fixed token endpoint")
        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, Sequence) or isinstance(redirect_uris, (str, bytes)):
            raise ValueError("installed client redirect_uris must be a list")
        return cls(
            client_id=_required_str(payload, "client_id"),
            client_secret=_required_str(payload, "client_secret"),
            project_id=_required_str(payload, "project_id"),
            redirect_uris=tuple(str(item) for item in cast("Sequence[object]", redirect_uris)),
        )

    @classmethod
    def from_file(cls, path: Path) -> Self:
        return cls.from_json(path.read_text(encoding="utf-8"))

    def to_json(self) -> str:
        return json.dumps(
            {
                "installed": {
                    "auth_uri": GOOGLE_AUTH_URI,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "project_id": self.project_id,
                    "redirect_uris": list(self.redirect_uris),
                    "token_uri": GOOGLE_TOKEN_URI,
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def __repr__(self) -> str:
        return (
            "GoogleInstalledClient("
            f"client_id={_redact_identifier(self.client_id)!r}, "
            f"project_id={self.project_id!r}, "
            f"redirect_uris={self.redirect_uris!r}, "
            "client_secret=<redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class GoogleAuthorizationSession:
    authorization_url: str
    state: str
    code_verifier: str
    capability: str
    scope: str
    redirect_uri: str

    def __post_init__(self) -> None:
        _validate_handle(self.state, "state")
        _validate_secret_value(self.code_verifier, "code_verifier")
        scope = scope_for_capability(self.capability)
        if self.scope != scope:
            raise ValueError("authorization session scope mismatch")

    def __repr__(self) -> str:
        return (
            "GoogleAuthorizationSession("
            "authorization_url=<redacted>, "
            f"state={_redact_identifier(self.state)!r}, "
            f"capability={self.capability!r}, "
            f"scope={self.scope!r}, "
            f"redirect_uri={self.redirect_uri!r}, "
            "code_verifier=<redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class GoogleOAuthTokens:
    access_token: str
    refresh_token: str
    expires_at: datetime
    capability: str
    scope: str
    token_type: str = "Bearer"

    def __post_init__(self) -> None:
        _validate_secret_value(self.access_token, "access_token")
        _validate_secret_value(self.refresh_token, "refresh_token")
        _validate_aware(self.expires_at, "expires_at")
        expected_scope = scope_for_capability(self.capability)
        if self.scope != expected_scope:
            raise ValueError("OAuth token scope mismatch")
        if self.token_type.lower() != "bearer":
            raise ValueError("OAuth token_type must be Bearer")

    @property
    def granted_scopes(self) -> tuple[str, ...]:
        return (self.scope,)

    def is_expired(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        _validate_aware(current, "now")
        return self.expires_at <= current

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at.isoformat(),
                "capability": self.capability,
                "scope": self.scope,
                "token_type": self.token_type,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> Self:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise GoogleOAuthError("stored OAuth token json is invalid") from exc
        if not isinstance(decoded, dict):
            raise GoogleOAuthError("stored OAuth token json must be an object")
        payload = cast("Mapping[str, object]", decoded)
        return cls(
            access_token=_required_str(payload, "access_token"),
            refresh_token=_required_str(payload, "refresh_token"),
            expires_at=_parse_datetime(_required_str(payload, "expires_at"), "expires_at"),
            capability=_required_str(payload, "capability"),
            scope=_required_str(payload, "scope"),
            token_type=_required_str(payload, "token_type"),
        )

    def __repr__(self) -> str:
        return (
            "GoogleOAuthTokens("
            f"expires_at={self.expires_at.isoformat()!r}, "
            f"capability={self.capability!r}, "
            f"scope={self.scope!r}, "
            f"token_type={self.token_type!r}, "
            "access_token=<redacted>, refresh_token=<redacted>)"
        )

    __str__ = __repr__


class GoogleOAuthClient:
    def __init__(
        self,
        *,
        installed_client: GoogleInstalledClient,
        transport: HttpTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._installed_client = installed_client
        self._transport = transport or urllib_transport
        self._now = now or (lambda: datetime.now(UTC))

    def authorization_url(
        self,
        *,
        capability: str,
        redirect_uri: str | None = None,
        state: str | None = None,
        code_verifier: str | None = None,
        login_hint: str | None = None,
    ) -> GoogleAuthorizationSession:
        scope = scope_for_capability(capability)
        selected_redirect_uri = redirect_uri or self._installed_client.redirect_uri
        _validate_selected_redirect_uri(
            selected_redirect_uri,
            declared=self._installed_client.redirect_uris,
        )
        if login_hint is not None:
            _validate_account_subject(login_hint)
        selected_state = state or _new_urlsafe_secret(32)
        selected_code_verifier = code_verifier or _new_urlsafe_secret(64)
        _validate_handle(selected_state, "state")
        _validate_secret_value(selected_code_verifier, "code_verifier")
        challenge = _pkce_s256(selected_code_verifier)
        query_parameters = {
            "client_id": self._installed_client.client_id,
            "redirect_uri": selected_redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": selected_state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
        }
        if login_hint is not None:
            query_parameters["login_hint"] = login_hint
        query = urllib.parse.urlencode(query_parameters)
        return GoogleAuthorizationSession(
            authorization_url=f"{GOOGLE_AUTH_URI}?{query}",
            state=selected_state,
            code_verifier=selected_code_verifier,
            capability=capability,
            scope=scope,
            redirect_uri=selected_redirect_uri,
        )

    def exchange_authorization_code(
        self,
        *,
        code: str,
        session: GoogleAuthorizationSession,
    ) -> GoogleOAuthTokens:
        _validate_secret_value(code, "code")
        payload = {
            "client_id": self._installed_client.client_id,
            "client_secret": self._installed_client.client_secret,
            "code": code,
            "code_verifier": session.code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": session.redirect_uri,
        }
        decoded = self._post_form(GOOGLE_TOKEN_URI, payload)
        if "refresh_token" not in decoded:
            raise GoogleOAuthError("authorization-code exchange did not return refresh_token")
        return self._tokens_from_payload(
            decoded,
            capability=session.capability,
            require_refresh=True,
        )

    def refresh(self, tokens: GoogleOAuthTokens) -> GoogleOAuthTokens:
        payload = {
            "client_id": self._installed_client.client_id,
            "client_secret": self._installed_client.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "scope": tokens.scope,
        }
        decoded = self._post_form(GOOGLE_TOKEN_URI, payload)
        refreshed = self._tokens_from_payload(
            decoded,
            capability=tokens.capability,
            require_refresh=False,
        )
        return GoogleOAuthTokens(
            access_token=refreshed.access_token,
            refresh_token=tokens.refresh_token,
            expires_at=refreshed.expires_at,
            capability=refreshed.capability,
            scope=refreshed.scope,
            token_type=refreshed.token_type,
        )

    def revoke(self, token: str) -> None:
        _validate_secret_value(token, "token")
        self._post_form(GOOGLE_REVOKE_URI, {"token": token}, expect_json=False)

    def _post_form(
        self,
        url: str,
        payload: Mapping[str, str],
        *,
        expect_json: bool = True,
    ) -> Mapping[str, object]:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        response = self._transport(
            HttpRequest(
                method="POST",
                url=url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=body,
            )
        )
        if response.status < 200 or response.status >= 300:
            raise GoogleOAuthError(
                f"Google OAuth endpoint rejected request: status {response.status}"
            )
        if not expect_json:
            return {}
        try:
            decoded = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GoogleOAuthError("Google OAuth endpoint returned invalid json") from exc
        if not isinstance(decoded, dict):
            raise GoogleOAuthError("Google OAuth endpoint response must be an object")
        response_payload = cast("Mapping[str, object]", decoded)
        oauth_error = response_payload.get("error")
        if isinstance(oauth_error, str) and oauth_error:
            raise GoogleOAuthError(f"Google OAuth error: {_bounded_detail(oauth_error)}")
        return response_payload

    def _tokens_from_payload(
        self,
        payload: Mapping[str, object],
        *,
        capability: str,
        require_refresh: bool,
    ) -> GoogleOAuthTokens:
        scope = _required_str(payload, "scope")
        expected_scope = scope_for_capability(capability)
        if _normalize_scope_value(scope) != expected_scope:
            raise GoogleOAuthError("Google OAuth response scope mismatch")
        expires_in = _required_positive_int(payload, "expires_in")
        refresh_token = (
            _required_str(payload, "refresh_token") if require_refresh else "placeholder"
        )
        return GoogleOAuthTokens(
            access_token=_required_str(payload, "access_token"),
            refresh_token=refresh_token,
            expires_at=self._now() + timedelta(seconds=expires_in),
            capability=capability,
            scope=expected_scope,
            token_type=_required_str(payload, "token_type"),
        )


class InMemoryGoogleOAuthVault:
    def __init__(self) -> None:
        self._tokens: MutableMapping[str, GoogleOAuthTokens] = {}

    def put(self, handle: str, tokens: GoogleOAuthTokens) -> None:
        _validate_handle(handle, "handle")
        self._tokens[handle] = tokens

    def get(self, handle: str) -> GoogleOAuthTokens:
        _validate_handle(handle, "handle")
        try:
            return self._tokens[handle]
        except KeyError as exc:
            raise KeyError("OAuth token handle not found") from exc

    def delete(self, handle: str) -> None:
        _validate_handle(handle, "handle")
        self._tokens.pop(handle, None)


def scope_for_capability(capability: str) -> str:
    try:
        return _CAPABILITY_SCOPES[capability]
    except KeyError as exc:
        raise ValueError("unsupported Google OAuth capability") from exc


def new_oauth_handle(*, capability: str) -> str:
    scope_for_capability(capability)
    return f"google-oauth:{capability}:{_new_urlsafe_secret(24)}"


def urllib_transport(request: HttpRequest) -> HttpResponse:
    if request.method != "POST" or request.url not in _GOOGLE_OAUTH_HTTP_ENDPOINTS:
        raise GoogleOAuthError("Google OAuth transport refused non-allowlisted endpoint")
    urllib_request = urllib.request.Request(
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    try:
        # The exact HTTPS endpoint allowlist above excludes file/custom schemes and redirects are
        # not caller-controlled. Keep this guard adjacent to the audited network sink.
        with urllib.request.urlopen(urllib_request, timeout=15) as response:  # nosec B310
            body = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
            if len(body) > _MAX_HTTP_RESPONSE_BYTES:
                raise GoogleOAuthError("Google OAuth endpoint response too large")
            return HttpResponse(
                status=response.status,
                headers=dict(response.headers.items()),
                body=body,
            )
    except urllib.error.HTTPError as exc:
        body = exc.read(_MAX_HTTP_RESPONSE_BYTES + 1)
        if len(body) > _MAX_HTTP_RESPONSE_BYTES:
            raise GoogleOAuthError("Google OAuth endpoint response too large") from exc
        return HttpResponse(status=exc.code, headers=dict(exc.headers.items()), body=body)
    except urllib.error.URLError as exc:
        raise GoogleOAuthError("Google OAuth endpoint unavailable") from exc


def _pkce_s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _new_urlsafe_secret(bytes_count: int) -> str:
    return secrets.token_urlsafe(bytes_count)


def _normalize_scope_value(scope: str) -> str:
    scopes = tuple(sorted({part.strip() for part in scope.split(" ") if part.strip()}))
    if len(scopes) != 1:
        raise GoogleOAuthError("Google OAuth response scope mismatch")
    return scopes[0]


def _required_str(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise GoogleOAuthError(f"OAuth payload missing {field_name}")
    return value.strip()


def _required_positive_int(payload: Mapping[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool):
        raise GoogleOAuthError(f"OAuth payload missing {field_name}")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isdecimal():
        parsed = int(value)
    else:
        raise GoogleOAuthError(f"OAuth payload missing {field_name}")
    if parsed <= 0:
        raise GoogleOAuthError(f"OAuth payload {field_name} must be positive")
    return parsed


def _parse_datetime(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GoogleOAuthError(f"invalid {field_name}") from exc
    _validate_aware(parsed, field_name)
    return parsed


def _validate_handle(value: str, field_name: str) -> None:
    if _BOUNDED_HANDLE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded opaque handle")


def _validate_account_subject(value: str) -> None:
    if _EMAIL.fullmatch(value) is None:
        raise ValueError("account_subject must be a bounded email address")


def _validate_installed_redirect_uri(value: str) -> None:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("installed client redirect_uri must be loopback HTTP")


def _validate_selected_redirect_uri(value: str, *, declared: Sequence[str]) -> None:
    _validate_installed_redirect_uri(value)
    parsed = urllib.parse.urlparse(value)
    if parsed.port is None:
        raise ValueError("OAuth callback redirect_uri must use an ephemeral loopback port")
    if parsed.hostname not in {"127.0.0.1", "::1"}:
        raise ValueError("OAuth callback redirect_uri must bind a numeric loopback address")
    declared_hosts = {urllib.parse.urlparse(item).hostname for item in declared}
    if not declared_hosts.intersection(_LOOPBACK_HOSTS):
        raise ValueError("installed client does not declare a loopback redirect_uri")


def _validate_secret_value(value: str, field_name: str) -> None:
    if not value.strip() or len(value) < _SECRET_MIN_LENGTH or len(value) > _SECRET_MAX_LENGTH:
        raise ValueError(f"{field_name} must be a bounded non-empty secret")


def _validate_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _redact_identifier(value: str) -> str:
    if len(value) <= 8:
        return "<redacted>"
    return f"{value[:4]}...{value[-4:]}"


def _redacted_http_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: "<redacted>" if name.casefold() in _SENSITIVE_HTTP_HEADERS else value
        for name, value in headers.items()
    }


def _bounded_detail(value: str) -> str:
    sanitized = " ".join(value.split())
    if len(sanitized) > _MAX_ERROR_DETAIL:
        return f"{sanitized[:_MAX_ERROR_DETAIL]}..."
    return sanitized


__all__ = [
    "GMAIL_READONLY_SCOPE",
    "GMAIL_SEND_SCOPE",
    "GOOGLE_AUTH_URI",
    "GOOGLE_REVOKE_URI",
    "GOOGLE_TOKEN_URI",
    "GoogleAuthorizationSession",
    "GoogleInstalledClient",
    "GoogleOAuthClient",
    "GoogleOAuthError",
    "GoogleOAuthTokens",
    "GoogleOAuthVault",
    "HttpRequest",
    "HttpResponse",
    "InMemoryGoogleOAuthVault",
    "new_oauth_handle",
    "scope_for_capability",
    "urllib_transport",
]
