from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from careerops.auth.contracts import (
    AuthAction,
    AuthAuditEvent,
    AuthRateLimited,
    AuthRequestContext,
    ConsoleUserRecord,
    CsrfRejected,
    InvalidBootstrapCredential,
    InvalidCredentials,
    InvalidSession,
    PasswordRecord,
    SessionInsert,
    SessionRecord,
    SessionState,
    SingleUserAlreadyExists,
)
from careerops.auth.crypto import Argon2idPasswordHasher, hash_token
from careerops.auth.rate_limit import FixedWindowAuthRateLimiter
from careerops.auth.service import ConsoleAuthService

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
CONTEXT = AuthRequestContext(trace_id="auth-test", client_key="127.0.0.1")


class MemoryRepository:
    def __init__(self) -> None:
        self.bootstrap: tuple[str, datetime, datetime, datetime | None] | None = None
        self.user: ConsoleUserRecord | None = None
        self.sessions: dict[UUID, SessionRecord] = {}

    def replace_bootstrap_token(
        self, *, token_hash: str, issued_at: datetime, expires_at: datetime
    ) -> None:
        self.bootstrap = (token_hash, issued_at, expires_at, None)

    def consume_bootstrap_token(
        self,
        *,
        token_hash: str,
        username: str,
        password: PasswordRecord,
        now: datetime,
    ) -> ConsoleUserRecord:
        if self.user is not None:
            raise SingleUserAlreadyExists
        if self.bootstrap is None:
            raise InvalidBootstrapCredential
        expected, _issued, expires, used = self.bootstrap
        if token_hash != expected or used is not None or now >= expires:
            raise InvalidBootstrapCredential
        user = ConsoleUserRecord(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            username=username,
            password=password,
            password_changed_at=now,
        )
        self.user = user
        self.bootstrap = (expected, _issued, expires, now)
        return user

    def get_user_by_username(self, username: str) -> ConsoleUserRecord | None:
        return self.user if self.user is not None and self.user.username == username else None

    def update_password(self, user_id: UUID, password: PasswordRecord, *, now: datetime) -> None:
        assert self.user is not None and self.user.id == user_id
        self.user = replace(self.user, password=password, password_changed_at=now)

    def create_session(self, session: SessionInsert) -> None:
        self.sessions[session.id] = self._record(session)

    def get_session(self, token_hash: str) -> SessionRecord | None:
        return next(
            (record for record in self.sessions.values() if record.token_hash == token_hash),
            None,
        )

    def touch_session(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        now: datetime,
        idle_expires_at: datetime,
    ) -> bool:
        record = self.sessions[session_id]
        if record.token_hash != token_hash or record.revoked_at is not None:
            return False
        self.sessions[session_id] = replace(
            record,
            last_seen_at=now,
            idle_expires_at=idle_expires_at,
        )
        return True

    def rotate_session(
        self,
        old_session_id: UUID,
        *,
        old_token_hash: str,
        replacement: SessionInsert,
        now: datetime,
    ) -> bool:
        old = self.sessions[old_session_id]
        if old.token_hash != old_token_hash or old.revoked_at is not None:
            return False
        self.sessions[old_session_id] = replace(
            old,
            state=SessionState.REVOKED,
            revoked_at=now,
        )
        username = self.user.username if replacement.user_id is not None and self.user else None
        self.sessions[replacement.id] = self._record(replacement, username=username)
        return True

    def revoke_session(self, session_id: UUID, *, token_hash: str, now: datetime) -> bool:
        record = self.sessions[session_id]
        if record.token_hash != token_hash or record.revoked_at is not None:
            return False
        self.sessions[session_id] = replace(
            record,
            state=SessionState.REVOKED,
            revoked_at=now,
        )
        return True

    def revoke_user_sessions(
        self,
        user_id: UUID,
        *,
        now: datetime,
        except_session_id: UUID | None = None,
    ) -> int:
        count = 0
        for session_id, record in tuple(self.sessions.items()):
            if (
                record.user_id == user_id
                and record.revoked_at is None
                and session_id != except_session_id
            ):
                self.sessions[session_id] = replace(
                    record,
                    state=SessionState.REVOKED,
                    revoked_at=now,
                )
                count += 1
        return count

    @staticmethod
    def _record(session: SessionInsert, *, username: str | None = None) -> SessionRecord:
        return SessionRecord(
            id=session.id,
            user_id=session.user_id,
            username=username,
            state=session.state,
            token_hash=session.token_hash,
            csrf_token_hash=session.csrf_token_hash,
            created_at=session.created_at,
            last_seen_at=session.last_seen_at,
            idle_expires_at=session.idle_expires_at,
            absolute_expires_at=session.absolute_expires_at,
            revoked_at=None,
        )


class AuditRecorder:
    def __init__(self) -> None:
        self.events: list[AuthAuditEvent] = []

    def record(self, event: AuthAuditEvent) -> None:
        self.events.append(event)


def make_service() -> tuple[ConsoleAuthService, MemoryRepository, AuditRecorder]:
    repository = MemoryRepository()
    audit = AuditRecorder()
    service = ConsoleAuthService(
        repository,
        Argon2idPasswordHasher(memory_cost=8192, time_cost=1, parallelism=1),
        FixedWindowAuthRateLimiter(),
        audit,
    )
    return service, repository, audit


def bootstrap_owner(
    service: ConsoleAuthService,
    repository: MemoryRepository,
) -> tuple[str, str]:
    bootstrap = service.issue_bootstrap_token(now=NOW)
    assert repository.bootstrap is not None
    assert bootstrap.token != repository.bootstrap[0]
    preauth = service.begin_preauth(now=NOW, context=CONTEXT)
    session = service.complete_bootstrap(
        preauth_token=preauth.token,
        csrf_token=preauth.csrf_token,
        bootstrap_token=bootstrap.token,
        username="Owner.User",
        password="correct horse battery staple",
        now=NOW,
        context=CONTEXT,
    )
    return session.token, session.csrf_token


def test_bootstrap_is_single_use_and_rotates_preauth_session() -> None:
    service, repository, audit = make_service()
    session_token, csrf_token = bootstrap_owner(service, repository)

    principal = service.authenticate(session_token, now=NOW)
    assert principal.username == "owner.user"
    service.validate_csrf(principal, csrf_token)
    with pytest.raises(CsrfRejected):
        service.validate_csrf(principal, "wrong-csrf")
    assert repository.bootstrap is not None and repository.bootstrap[3] == NOW
    assert session_token not in repr(repository.sessions)
    assert audit.events[-1].reason_code == "BOOTSTRAP_COMPLETED"

    second_preauth = service.begin_preauth(now=NOW, context=CONTEXT)
    with pytest.raises(InvalidBootstrapCredential):
        service.complete_bootstrap(
            preauth_token=second_preauth.token,
            csrf_token=second_preauth.csrf_token,
            bootstrap_token="already-consumed-token",
            username="owner.user",
            password="correct horse battery staple",
            now=NOW,
            context=CONTEXT,
        )


def test_login_uses_generic_failure_and_logout_revokes_session() -> None:
    service, _repository, audit = make_service()
    bootstrap_session, bootstrap_csrf = bootstrap_owner(service, _repository)
    principal = service.authenticate(bootstrap_session, now=NOW)
    service.validate_csrf(principal, bootstrap_csrf)

    preauth = service.begin_preauth(now=NOW, context=CONTEXT)
    with pytest.raises(InvalidCredentials, match="username or password"):
        service.login(
            preauth_token=preauth.token,
            csrf_token=preauth.csrf_token,
            username="owner.user",
            password="incorrect password value",
            now=NOW,
            context=CONTEXT,
        )

    preauth = service.begin_preauth(now=NOW, context=CONTEXT)
    session = service.login(
        preauth_token=preauth.token,
        csrf_token=preauth.csrf_token,
        username="owner.user",
        password="correct horse battery staple",
        now=NOW,
        context=CONTEXT,
    )
    service.logout(
        session_token=session.token,
        csrf_token=session.csrf_token,
        now=NOW,
        context=CONTEXT,
    )
    with pytest.raises(InvalidSession):
        service.authenticate(session.token, now=NOW)
    assert audit.events[-1].action is AuthAction.LOGOUT


def test_repository_only_receives_hashes_not_raw_session_or_bootstrap_tokens() -> None:
    service, repository, _audit = make_service()
    bootstrap = service.issue_bootstrap_token(now=NOW)
    preauth = service.begin_preauth(now=NOW, context=CONTEXT)

    assert repository.bootstrap is not None
    assert repository.bootstrap[0] == hash_token(bootstrap.token)
    stored_session = repository.sessions[preauth.session_id]
    assert stored_session.token_hash == hash_token(preauth.token)
    assert stored_session.csrf_token_hash == hash_token(preauth.csrf_token)
    assert preauth.token not in repr(stored_session)


def test_bootstrap_and_session_expiry_fail_closed() -> None:
    service, _repository, _audit = make_service()
    bootstrap = service.issue_bootstrap_token(now=NOW)
    preauth = service.begin_preauth(now=NOW, context=CONTEXT)

    with pytest.raises(InvalidBootstrapCredential):
        service.complete_bootstrap(
            preauth_token=preauth.token,
            csrf_token=preauth.csrf_token,
            bootstrap_token=bootstrap.token,
            username="owner.user",
            password="correct horse battery staple",
            now=NOW + timedelta(minutes=16),
            context=CONTEXT,
        )

    service, repository, _audit = make_service()
    session_token, _csrf_token = bootstrap_owner(service, repository)
    with pytest.raises(InvalidSession):
        service.authenticate(session_token, now=NOW + timedelta(hours=8))


def test_authenticated_session_rotation_invalidates_the_old_token() -> None:
    service, repository, _audit = make_service()
    old_token, old_csrf = bootstrap_owner(service, repository)

    replacement = service.rotate_authenticated_session(
        session_token=old_token,
        csrf_token=old_csrf,
        now=NOW + timedelta(minutes=1),
        context=CONTEXT,
    )

    with pytest.raises(InvalidSession):
        service.authenticate(old_token, now=NOW + timedelta(minutes=2))
    principal = service.authenticate(replacement.token, now=NOW + timedelta(minutes=2))
    service.validate_csrf(principal, replacement.csrf_token)


def test_login_rate_limit_is_audited_and_blocks_the_sixth_attempt() -> None:
    service, repository, audit = make_service()
    bootstrap_owner(service, repository)

    for _attempt in range(100):
        preauth = service.begin_preauth(now=NOW, context=CONTEXT)
        with pytest.raises(InvalidCredentials):
            service.login(
                preauth_token=preauth.token,
                csrf_token=preauth.csrf_token,
                username="owner.user",
                password="incorrect password value",
                now=NOW,
                context=CONTEXT,
            )

    preauth = service.begin_preauth(now=NOW, context=CONTEXT)
    with pytest.raises(AuthRateLimited):
        service.login(
            preauth_token=preauth.token,
            csrf_token=preauth.csrf_token,
            username="owner.user",
            password="incorrect password value",
            now=NOW,
            context=CONTEXT,
        )
    assert audit.events[-1].reason_code == "RATE_LIMITED"


def test_preauth_creation_is_bounded_and_rate_limit_is_audited() -> None:
    service, repository, audit = make_service()

    for _attempt in range(200):
        service.begin_preauth(now=NOW, context=CONTEXT)

    with pytest.raises(AuthRateLimited):
        service.begin_preauth(now=NOW, context=CONTEXT)

    assert len(repository.sessions) == 200
    assert audit.events[-1].action is AuthAction.PREAUTH
    assert audit.events[-1].reason_code == "RATE_LIMITED"
