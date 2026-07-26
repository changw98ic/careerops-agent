from __future__ import annotations

import re
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from careerops.auth.contracts import (
    AuthAction,
    AuthAuditEvent,
    AuthAuditSink,
    AuthenticatedPrincipal,
    AuthError,
    AuthOutcome,
    AuthRateLimited,
    AuthRateLimiter,
    AuthRepository,
    AuthRequestContext,
    BootstrapCredential,
    CsrfRejected,
    InvalidBootstrapCredential,
    InvalidCredentials,
    InvalidSession,
    PasswordHasher,
    SessionInsert,
    SessionRecord,
    SessionSecrets,
    SessionState,
)
from careerops.auth.crypto import (
    constant_time_token_match,
    generate_token,
    hash_subject,
    hash_token,
)

BOOTSTRAP_TTL = timedelta(minutes=15)
PREAUTH_TTL = timedelta(minutes=15)
SESSION_IDLE_TTL = timedelta(hours=8)
SESSION_ABSOLUTE_TTL = timedelta(days=7)
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


class ConsoleAuthService:
    """Single-user authentication service; callers never supply an authenticated flag."""

    def __init__(
        self,
        repository: AuthRepository,
        password_hasher: PasswordHasher,
        rate_limiter: AuthRateLimiter,
        audit_sink: AuthAuditSink,
    ) -> None:
        self._repository = repository
        self._password_hasher = password_hasher
        self._rate_limiter = rate_limiter
        self._audit_sink = audit_sink

    def issue_bootstrap_token(
        self,
        *,
        now: datetime,
        trace_id: str = "local-bootstrap-cli",
    ) -> BootstrapCredential:
        self._require_aware(now)
        raw_token = generate_token()
        expires_at = now + BOOTSTRAP_TTL
        self._repository.replace_bootstrap_token(
            token_hash=hash_token(raw_token),
            issued_at=now,
            expires_at=expires_at,
        )
        self._audit_sink.record(
            AuthAuditEvent(
                action=AuthAction.BOOTSTRAP,
                outcome=AuthOutcome.SUCCEEDED,
                occurred_at=now,
                trace_id=trace_id,
                subject_hash=hash_subject("local-bootstrap-cli"),
                reason_code="BOOTSTRAP_TOKEN_ISSUED",
            )
        )
        return BootstrapCredential(token=raw_token, expires_at=expires_at)

    def begin_preauth(
        self,
        *,
        now: datetime,
        context: AuthRequestContext,
    ) -> SessionSecrets:
        self._require_aware(now)
        self._limit(AuthAction.PREAUTH, context, now=now)
        return self._create_session(
            user_id=None,
            state=SessionState.PREAUTH,
            now=now,
            absolute_ttl=PREAUTH_TTL,
            idle_ttl=PREAUTH_TTL,
            client_fingerprint=context.client_key,
        )

    def complete_bootstrap(
        self,
        *,
        preauth_token: str,
        csrf_token: str,
        bootstrap_token: str,
        username: str,
        password: str,
        now: datetime,
        context: AuthRequestContext,
    ) -> SessionSecrets:
        self._require_aware(now)
        subject_hash = self._limit(AuthAction.BOOTSTRAP, context, now=now)
        try:
            preauth = self._require_session(
                preauth_token,
                expected_state=SessionState.PREAUTH,
                now=now,
            )
            self._verify_csrf(csrf_token, preauth.csrf_token_hash)
            normalized_username = self._normalize_username(username)
            password_record = self._password_hasher.hash_password(password)
            user = self._repository.consume_bootstrap_token(
                token_hash=hash_token(bootstrap_token),
                username=normalized_username,
                password=password_record,
                now=now,
            )
            replacement = self._new_session_insert(
                user_id=user.id,
                state=SessionState.AUTHENTICATED,
                now=now,
                absolute_ttl=SESSION_ABSOLUTE_TTL,
                idle_ttl=SESSION_IDLE_TTL,
                client_fingerprint=subject_hash,
            )
            if not self._repository.rotate_session(
                preauth.id,
                old_token_hash=preauth.token_hash,
                replacement=replacement[0],
                now=now,
            ):
                raise InvalidSession("pre-authentication session was already consumed")
        except (AuthError, ValueError):
            self._audit(
                AuthAction.BOOTSTRAP,
                AuthOutcome.DENIED,
                now=now,
                context=context,
                subject_hash=subject_hash,
                reason_code="BOOTSTRAP_DENIED",
            )
            raise InvalidBootstrapCredential("bootstrap credential is invalid or expired") from None

        secrets = replacement[1]
        self._audit(
            AuthAction.BOOTSTRAP,
            AuthOutcome.SUCCEEDED,
            now=now,
            context=context,
            subject_hash=subject_hash,
            reason_code="BOOTSTRAP_COMPLETED",
            user_id=user.id,
            session_id=secrets.session_id,
        )
        return secrets

    def login(
        self,
        *,
        preauth_token: str,
        csrf_token: str,
        username: str,
        password: str,
        now: datetime,
        context: AuthRequestContext,
    ) -> SessionSecrets:
        self._require_aware(now)
        subject_hash = self._limit(AuthAction.LOGIN, context, now=now)
        try:
            preauth = self._require_session(
                preauth_token,
                expected_state=SessionState.PREAUTH,
                now=now,
            )
            self._verify_csrf(csrf_token, preauth.csrf_token_hash)
            normalized_username = self._normalize_username(username)
            user = self._repository.get_user_by_username(normalized_username)
            if (
                user is None
                or user.disabled_at is not None
                or not self._password_hasher.verify_password(password, user.password)
            ):
                raise InvalidCredentials("invalid credentials")
            if self._password_hasher.needs_rehash(user.password):
                self._repository.update_password(
                    user.id,
                    self._password_hasher.hash_password(password),
                    now=now,
                )
            replacement = self._new_session_insert(
                user_id=user.id,
                state=SessionState.AUTHENTICATED,
                now=now,
                absolute_ttl=SESSION_ABSOLUTE_TTL,
                idle_ttl=SESSION_IDLE_TTL,
                client_fingerprint=subject_hash,
            )
            if not self._repository.rotate_session(
                preauth.id,
                old_token_hash=preauth.token_hash,
                replacement=replacement[0],
                now=now,
            ):
                raise InvalidSession("pre-authentication session was already consumed")
        except (InvalidCredentials, InvalidSession, CsrfRejected, ValueError):
            self._audit(
                AuthAction.LOGIN,
                AuthOutcome.DENIED,
                now=now,
                context=context,
                subject_hash=subject_hash,
                reason_code="INVALID_CREDENTIALS",
            )
            raise InvalidCredentials("username or password is invalid") from None

        secrets = replacement[1]
        self._audit(
            AuthAction.LOGIN,
            AuthOutcome.SUCCEEDED,
            now=now,
            context=context,
            subject_hash=subject_hash,
            reason_code="LOGIN_SUCCEEDED",
            user_id=user.id,
            session_id=secrets.session_id,
        )
        return secrets

    def authenticate(self, session_token: str, *, now: datetime) -> AuthenticatedPrincipal:
        self._require_aware(now)
        record = self._require_session(
            session_token,
            expected_state=SessionState.AUTHENTICATED,
            now=now,
        )
        if record.user_id is None or record.username is None:
            raise InvalidSession("authenticated session has no user")
        idle_expires_at = min(now + SESSION_IDLE_TTL, record.absolute_expires_at)
        if not self._repository.touch_session(
            record.id,
            token_hash=record.token_hash,
            now=now,
            idle_expires_at=idle_expires_at,
        ):
            raise InvalidSession("session expired while being refreshed")
        return AuthenticatedPrincipal(
            user_id=record.user_id,
            username=record.username,
            session_id=record.id,
            csrf_token_hash=record.csrf_token_hash,
            absolute_expires_at=record.absolute_expires_at,
            candidate_id=record.candidate_id,
        )

    def validate_csrf(self, principal: AuthenticatedPrincipal, csrf_token: str) -> None:
        self._verify_csrf(csrf_token, principal.csrf_token_hash)

    def logout(
        self,
        *,
        session_token: str,
        csrf_token: str,
        now: datetime,
        context: AuthRequestContext,
    ) -> None:
        self._require_aware(now)
        subject_hash = self._limit(AuthAction.LOGOUT, context, now=now)
        principal = self.authenticate(session_token, now=now)
        self.validate_csrf(principal, csrf_token)
        token_hash = hash_token(session_token)
        if not self._repository.revoke_session(
            principal.session_id,
            token_hash=token_hash,
            now=now,
        ):
            raise InvalidSession("session was already revoked")
        self._audit(
            AuthAction.LOGOUT,
            AuthOutcome.SUCCEEDED,
            now=now,
            context=context,
            subject_hash=subject_hash,
            reason_code="LOGOUT_SUCCEEDED",
            user_id=principal.user_id,
            session_id=principal.session_id,
        )

    def rotate_authenticated_session(
        self,
        *,
        session_token: str,
        csrf_token: str,
        now: datetime,
        context: AuthRequestContext,
    ) -> SessionSecrets:
        self._require_aware(now)
        subject_hash = self._limit(AuthAction.SESSION_ROTATE, context, now=now)
        principal = self.authenticate(session_token, now=now)
        self.validate_csrf(principal, csrf_token)
        replacement = self._new_session_insert(
            user_id=principal.user_id,
            state=SessionState.AUTHENTICATED,
            now=now,
            absolute_ttl=SESSION_ABSOLUTE_TTL,
            idle_ttl=SESSION_IDLE_TTL,
            client_fingerprint=subject_hash,
        )
        if not self._repository.rotate_session(
            principal.session_id,
            old_token_hash=hash_token(session_token),
            replacement=replacement[0],
            now=now,
        ):
            raise InvalidSession("session rotation lost its lease")
        self._audit(
            AuthAction.SESSION_ROTATE,
            AuthOutcome.SUCCEEDED,
            now=now,
            context=context,
            subject_hash=subject_hash,
            reason_code="SESSION_ROTATED",
            user_id=principal.user_id,
            session_id=replacement[1].session_id,
        )
        return replacement[1]

    def revoke_user_sessions(
        self,
        user_id: UUID,
        *,
        now: datetime,
        except_session_id: UUID | None = None,
    ) -> int:
        self._require_aware(now)
        return self._repository.revoke_user_sessions(
            user_id,
            now=now,
            except_session_id=except_session_id,
        )

    def _create_session(
        self,
        *,
        user_id: UUID | None,
        state: SessionState,
        now: datetime,
        absolute_ttl: timedelta,
        idle_ttl: timedelta,
        client_fingerprint: str,
    ) -> SessionSecrets:
        session, secrets = self._new_session_insert(
            user_id=user_id,
            state=state,
            now=now,
            absolute_ttl=absolute_ttl,
            idle_ttl=idle_ttl,
            client_fingerprint=hash_subject(client_fingerprint),
        )
        self._repository.create_session(session)
        return secrets

    @staticmethod
    def _new_session_insert(
        *,
        user_id: UUID | None,
        state: SessionState,
        now: datetime,
        absolute_ttl: timedelta,
        idle_ttl: timedelta,
        client_fingerprint: str,
    ) -> tuple[SessionInsert, SessionSecrets]:
        raw_token = generate_token()
        raw_csrf = generate_token()
        session_id = uuid4()
        absolute_expires_at = now + absolute_ttl
        idle_expires_at = min(now + idle_ttl, absolute_expires_at)
        session = SessionInsert(
            id=session_id,
            user_id=user_id,
            state=state,
            token_hash=hash_token(raw_token),
            csrf_token_hash=hash_token(raw_csrf),
            created_at=now,
            last_seen_at=now,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
            client_fingerprint=client_fingerprint,
        )
        return session, SessionSecrets(
            session_id=session_id,
            token=raw_token,
            csrf_token=raw_csrf,
            absolute_expires_at=absolute_expires_at,
        )

    def _require_session(
        self,
        raw_token: str,
        *,
        expected_state: SessionState,
        now: datetime,
    ) -> SessionRecord:
        try:
            token_hash = hash_token(raw_token)
        except ValueError:
            raise InvalidSession("session token is invalid") from None
        record = self._repository.get_session(token_hash)
        if record is None or not constant_time_token_match(raw_token, record.token_hash):
            raise InvalidSession("session token is invalid")
        if (
            record.state is not expected_state
            or record.revoked_at is not None
            or now >= record.idle_expires_at
            or now >= record.absolute_expires_at
        ):
            raise InvalidSession("session is expired or revoked")
        return record

    @staticmethod
    def _verify_csrf(raw_token: str, expected_hash: str) -> None:
        if not constant_time_token_match(raw_token, expected_hash):
            raise CsrfRejected("CSRF token does not match its session")

    def _limit(
        self,
        action: AuthAction,
        context: AuthRequestContext,
        *,
        now: datetime,
    ) -> str:
        subject_hash = hash_subject(context.client_key)
        if self._rate_limiter.check(action, subject_hash, now=now):
            return subject_hash
        self._audit(
            action,
            AuthOutcome.DENIED,
            now=now,
            context=context,
            subject_hash=subject_hash,
            reason_code="RATE_LIMITED",
        )
        raise AuthRateLimited("authentication rate limit exceeded")

    def _audit(
        self,
        action: AuthAction,
        outcome: AuthOutcome,
        *,
        now: datetime,
        context: AuthRequestContext,
        subject_hash: str,
        reason_code: str,
        user_id: UUID | None = None,
        session_id: UUID | None = None,
    ) -> None:
        self._audit_sink.record(
            AuthAuditEvent(
                action=action,
                outcome=outcome,
                occurred_at=now,
                trace_id=context.trace_id,
                subject_hash=subject_hash,
                reason_code=reason_code,
                user_id=user_id,
                session_id=session_id,
            )
        )

    @staticmethod
    def _normalize_username(username: str) -> str:
        normalized = username.strip().lower()
        if _USERNAME.fullmatch(normalized) is None:
            raise ValueError("username must be a bounded canonical identifier")
        return normalized

    @staticmethod
    def _require_aware(now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("authentication timestamps must be timezone-aware")


__all__ = [
    "BOOTSTRAP_TTL",
    "PREAUTH_TTL",
    "SESSION_ABSOLUTE_TTL",
    "SESSION_IDLE_TTL",
    "ConsoleAuthService",
]
