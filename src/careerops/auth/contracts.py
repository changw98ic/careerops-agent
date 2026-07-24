from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from uuid import UUID


class SessionState(StrEnum):
    PREAUTH = "preauth"
    AUTHENTICATED = "authenticated"
    REVOKED = "revoked"


class AuthAction(StrEnum):
    PREAUTH = "preauth"
    BOOTSTRAP = "bootstrap"
    LOGIN = "login"
    LOGOUT = "logout"
    SESSION_ROTATE = "session_rotate"
    SESSION_REVOKE = "session_revoke"
    REVIEW = "review"


class AuthOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    DENIED = "denied"


class AuthError(Exception):
    """Base class for deliberately non-specific authentication failures."""


class InvalidCredentials(AuthError):
    pass


class InvalidBootstrapCredential(AuthError):
    pass


class InvalidSession(AuthError):
    pass


class CsrfRejected(AuthError):
    pass


class AuthRateLimited(AuthError):
    pass


class SingleUserAlreadyExists(AuthError):
    pass


@dataclass(frozen=True, slots=True)
class PasswordRecord:
    encoded_hash: str = field(repr=False)
    parameters: Mapping[str, int | str] = field(
        default_factory=lambda: MappingProxyType({}),
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class ConsoleUserRecord:
    id: UUID
    username: str
    password: PasswordRecord = field(repr=False)
    password_changed_at: datetime
    disabled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: UUID
    user_id: UUID | None
    username: str | None
    state: SessionState
    token_hash: str = field(repr=False)
    csrf_token_hash: str = field(repr=False)
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class SessionInsert:
    id: UUID
    user_id: UUID | None
    state: SessionState
    token_hash: str = field(repr=False)
    csrf_token_hash: str = field(repr=False)
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    client_fingerprint: str


@dataclass(frozen=True, slots=True)
class SessionSecrets:
    session_id: UUID
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    absolute_expires_at: datetime


@dataclass(frozen=True, slots=True)
class BootstrapCredential:
    token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    user_id: UUID
    username: str
    session_id: UUID
    csrf_token_hash: str = field(repr=False)
    absolute_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthRequestContext:
    trace_id: str
    client_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuthAuditEvent:
    action: AuthAction
    outcome: AuthOutcome
    occurred_at: datetime
    trace_id: str
    subject_hash: str
    reason_code: str
    user_id: UUID | None = None
    session_id: UUID | None = None


class PasswordHasher(Protocol):
    def hash_password(self, password: str) -> PasswordRecord: ...

    def verify_password(self, password: str, stored: PasswordRecord) -> bool: ...

    def needs_rehash(self, stored: PasswordRecord) -> bool: ...


class AuthAuditSink(Protocol):
    def record(self, event: AuthAuditEvent) -> None: ...


class AuthRateLimiter(Protocol):
    def check(self, action: AuthAction, subject_hash: str, *, now: datetime) -> bool: ...


class AuthRepository(Protocol):
    def replace_bootstrap_token(
        self,
        *,
        token_hash: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> None: ...

    def consume_bootstrap_token(
        self,
        *,
        token_hash: str,
        username: str,
        password: PasswordRecord,
        now: datetime,
    ) -> ConsoleUserRecord: ...

    def get_user_by_username(self, username: str) -> ConsoleUserRecord | None: ...

    def update_password(
        self,
        user_id: UUID,
        password: PasswordRecord,
        *,
        now: datetime,
    ) -> None: ...

    def create_session(self, session: SessionInsert) -> None: ...

    def get_session(self, token_hash: str) -> SessionRecord | None: ...

    def touch_session(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        now: datetime,
        idle_expires_at: datetime,
    ) -> bool: ...

    def rotate_session(
        self,
        old_session_id: UUID,
        *,
        old_token_hash: str,
        replacement: SessionInsert,
        now: datetime,
    ) -> bool: ...

    def revoke_session(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        now: datetime,
    ) -> bool: ...

    def revoke_user_sessions(
        self,
        user_id: UUID,
        *,
        now: datetime,
        except_session_id: UUID | None = None,
    ) -> int: ...


__all__ = [
    "AuthAction",
    "AuthAuditEvent",
    "AuthAuditSink",
    "AuthError",
    "AuthOutcome",
    "AuthRateLimited",
    "AuthRateLimiter",
    "AuthRepository",
    "AuthRequestContext",
    "AuthenticatedPrincipal",
    "BootstrapCredential",
    "ConsoleUserRecord",
    "CsrfRejected",
    "InvalidBootstrapCredential",
    "InvalidCredentials",
    "InvalidSession",
    "PasswordHasher",
    "PasswordRecord",
    "SessionInsert",
    "SessionRecord",
    "SessionSecrets",
    "SessionState",
    "SingleUserAlreadyExists",
]
