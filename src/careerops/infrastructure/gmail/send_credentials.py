from __future__ import annotations

import errno
import json
import re
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Self, cast

GMAIL_SEND_SCOPE: Final = "https://www.googleapis.com/auth/gmail.send"
_BROKER_VERSION: Final = "careerops.gmail.send-credential-broker.v1"
_BOUNDED_HANDLE = re.compile(r"^[A-Za-z0-9._:@/-]{1,255}$")
_BOUNDED_ACCOUNT = re.compile(r"^[^@\s]{1,160}@[^@\s]{1,160}$")
_FORBIDDEN_SECRET_FIELDS: Final = frozenset(
    {"refresh_token", "client_secret", "client_id", "token_uri", "private_key"}
)


class GmailSendCredentialBrokerError(RuntimeError):
    """Credential broker returned an invalid or unusable send-only envelope."""


@dataclass(frozen=True, slots=True)
class GmailSendCredentialHandle:
    """Opaque reference to a Gmail send credential stored outside CareerOps."""

    opaque_handle: str
    account_subject: str
    granted_scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_handle(self.opaque_handle, "opaque_handle")
        _validate_account(self.account_subject)
        object.__setattr__(
            self,
            "granted_scopes",
            _normalize_exact_send_scopes(self.granted_scopes),
        )


@dataclass(frozen=True, slots=True)
class GmailSendAccessToken:
    """Short-lived send token; repr/str intentionally hide token material."""

    access_token: str
    account_subject: str
    granted_scopes: tuple[str, ...]
    expires_at: datetime

    def __post_init__(self) -> None:
        _validate_secret_value(self.access_token, "access_token")
        _validate_account(self.account_subject)
        object.__setattr__(
            self,
            "granted_scopes",
            _normalize_exact_send_scopes(self.granted_scopes),
        )
        _validate_aware(self.expires_at, "expires_at")

    @property
    def value(self) -> str:
        return self.access_token

    def is_expired(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        _validate_aware(current, "now")
        return self.expires_at <= current

    def __repr__(self) -> str:
        return (
            "GmailSendAccessToken("
            f"account_subject={self.account_subject!r}, "
            f"granted_scopes={self.granted_scopes!r}, "
            f"expires_at={self.expires_at.isoformat()!r}, "
            "access_token=<redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class GmailSendBrokerCredentialEnvelope:
    access_token: GmailSendAccessToken

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        expected_account_subject: str,
        now: datetime,
    ) -> Self:
        _validate_aware(now, "now")
        _validate_account(expected_account_subject)
        forbidden = sorted(_FORBIDDEN_SECRET_FIELDS.intersection(payload))
        if forbidden:
            raise ValueError(f"broker envelope contains forbidden secret field: {forbidden[0]}")
        account_subject = _required_str(payload, "account_subject")
        if account_subject != expected_account_subject:
            raise GmailSendCredentialBrokerError("account mismatch")
        try:
            scopes = _normalize_exact_send_scopes(_extract_scopes(payload))
        except ValueError as exc:
            raise GmailSendCredentialBrokerError("scope mismatch") from exc
        token = GmailSendAccessToken(
            access_token=_required_str(payload, "access_token"),
            account_subject=account_subject,
            granted_scopes=scopes,
            expires_at=_parse_datetime(_required_str(payload, "expires_at"), "expires_at"),
        )
        if token.is_expired(now=now):
            raise GmailSendCredentialBrokerError("broker access token expired")
        return cls(access_token=token)


class UnixGmailSendCredentialBroker:
    """Resolve opaque Gmail send handles through a local Unix-domain envelope broker."""

    def __init__(
        self,
        *,
        socket_path: Path,
        timeout_seconds: float = 2.0,
        max_response_bytes: int = 8192,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._socket_path = Path(socket_path)
        if not self._socket_path.is_absolute():
            raise ValueError("credential broker socket path must be absolute")
        if timeout_seconds <= 0 or timeout_seconds > 30:
            raise ValueError("timeout_seconds must be between 0 and 30")
        if max_response_bytes < 256 or max_response_bytes > 65536:
            raise ValueError("max_response_bytes must be between 256 and 65536")
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._now = now or (lambda: datetime.now(UTC))

    def resolve(self, handle: GmailSendCredentialHandle) -> GmailSendAccessToken:
        request = {
            "version": _BROKER_VERSION,
            "provider": "gmail",
            "action": "send_email",
            "opaque_handle": handle.opaque_handle,
            "account_subject": handle.account_subject,
            "scope": GMAIL_SEND_SCOPE,
        }
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self._timeout_seconds)
                client.connect(str(self._socket_path))
                client.sendall(encoded)
                try:
                    client.shutdown(socket.SHUT_WR)
                except OSError as exc:
                    if exc.errno not in {errno.ENOTCONN, errno.EPIPE}:
                        raise
                response = _recv_bounded(client, self._max_response_bytes)
        except OSError as exc:
            raise GmailSendCredentialBrokerError("credential broker unavailable") from exc
        try:
            decoded = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GmailSendCredentialBrokerError("credential broker returned invalid json") from exc
        if not isinstance(decoded, dict):
            raise GmailSendCredentialBrokerError("credential broker envelope must be an object")
        envelope = GmailSendBrokerCredentialEnvelope.from_mapping(
            cast("Mapping[str, object]", decoded),
            expected_account_subject=handle.account_subject,
            now=self._now(),
        )
        return envelope.access_token

    def __repr__(self) -> str:
        return (
            "UnixGmailSendCredentialBroker("
            f"socket_path={str(self._socket_path)!r}, "
            f"timeout_seconds={self._timeout_seconds!r}, "
            f"max_response_bytes={self._max_response_bytes!r})"
        )


def _recv_bounded(client: socket.socket, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = client.recv(min(4096, max_bytes + 1 - received))
        if not chunk:
            break
        received += len(chunk)
        if received > max_bytes:
            raise GmailSendCredentialBrokerError("credential broker response too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _extract_scopes(payload: Mapping[str, object]) -> tuple[str, ...]:
    if "scope" in payload:
        return (_required_str(payload, "scope"),)
    value = payload.get("scopes")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise GmailSendCredentialBrokerError("scope mismatch")
    return tuple(str(item) for item in cast("Sequence[object]", value))


def _normalize_exact_send_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({scope.strip() for scope in scopes if scope and scope.strip()}))
    if normalized != (GMAIL_SEND_SCOPE,):
        raise ValueError("Gmail send credentials must grant exactly gmail.send")
    return normalized


def _required_str(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise GmailSendCredentialBrokerError(f"broker envelope missing {field_name}")
    return value.strip()


def _parse_datetime(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailSendCredentialBrokerError(f"invalid {field_name}") from exc
    _validate_aware(parsed, field_name)
    return parsed


def _validate_handle(value: str, field_name: str) -> None:
    if _BOUNDED_HANDLE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded opaque handle")


def _validate_account(value: str) -> None:
    if _BOUNDED_ACCOUNT.fullmatch(value) is None:
        raise ValueError("account_subject must be a bounded email subject")


def _validate_secret_value(value: str, field_name: str) -> None:
    if len(value) < 8 or len(value) > 4096 or not value.strip():
        raise ValueError(f"{field_name} must be a bounded non-empty secret")


def _validate_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = [
    "GMAIL_SEND_SCOPE",
    "GmailSendAccessToken",
    "GmailSendBrokerCredentialEnvelope",
    "GmailSendCredentialBrokerError",
    "GmailSendCredentialHandle",
    "UnixGmailSendCredentialBroker",
]
