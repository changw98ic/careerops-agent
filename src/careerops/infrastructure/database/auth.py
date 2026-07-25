from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from careerops.auth.contracts import (
    ConsoleUserRecord,
    InvalidBootstrapCredential,
    InvalidSession,
    PasswordRecord,
    SessionInsert,
    SessionRecord,
    SessionState,
    SingleUserAlreadyExists,
)
from careerops.auth.crypto import HASH_SCHEME
from careerops.infrastructure.database.schema import (
    bootstrap_tokens,
    candidates,
    console_sessions,
    console_users,
)


class PostgresAuthRepository:
    """Transaction-bounded persistence for hashes and authentication lifecycle state."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def replace_bootstrap_token(
        self,
        *,
        token_hash: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(bootstrap_tokens)
                .where(
                    bootstrap_tokens.c.used_at.is_(None),
                    bootstrap_tokens.c.revoked_at.is_(None),
                )
                .values(revoked_at=issued_at)
            )
            connection.execute(
                sa.insert(bootstrap_tokens).values(
                    id=uuid4(),
                    token_hash=token_hash,
                    issued_at=issued_at,
                    expires_at=expires_at,
                    created_at=issued_at,
                )
            )

    def consume_bootstrap_token(
        self,
        *,
        token_hash: str,
        username: str,
        password: PasswordRecord,
        now: datetime,
    ) -> ConsoleUserRecord:
        with self._engine.begin() as connection:
            token = (
                connection.execute(
                    sa.select(
                        bootstrap_tokens.c.id,
                        bootstrap_tokens.c.expires_at,
                        bootstrap_tokens.c.used_at,
                        bootstrap_tokens.c.revoked_at,
                    )
                    .where(bootstrap_tokens.c.token_hash == token_hash)
                    .with_for_update(of=bootstrap_tokens)
                )
                .mappings()
                .first()
            )
            if (
                token is None
                or token["used_at"] is not None
                or token["revoked_at"] is not None
                or now >= token["expires_at"]
            ):
                raise InvalidBootstrapCredential("bootstrap token is invalid or expired")

            user_id = uuid4()
            candidate_id = uuid4()
            try:
                connection.execute(
                    sa.insert(candidates).values(
                        id=candidate_id,
                        display_name=username,
                        created_at=now,
                        updated_at=now,
                    )
                )
                connection.execute(
                    sa.insert(console_users).values(
                        id=user_id,
                        candidate_id=candidate_id,
                        username=username,
                        password_hash=password.encoded_hash,
                        password_algorithm=HASH_SCHEME,
                        password_parameters=dict(password.parameters),
                        password_changed_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
            except IntegrityError as error:
                raise SingleUserAlreadyExists("the console owner already exists") from error

            result = connection.execute(
                sa.update(bootstrap_tokens)
                .where(
                    bootstrap_tokens.c.id == token["id"],
                    bootstrap_tokens.c.used_at.is_(None),
                    bootstrap_tokens.c.revoked_at.is_(None),
                )
                .values(used_at=now, used_by_user_id=user_id)
            )
            if result.rowcount != 1:
                raise InvalidBootstrapCredential("bootstrap token lost its one-time claim")

        return ConsoleUserRecord(
            id=user_id,
            username=username,
            password=password,
            password_changed_at=now,
            candidate_id=candidate_id,
        )

    def get_user_by_username(self, username: str) -> ConsoleUserRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(
                        console_users.c.id,
                        console_users.c.candidate_id,
                        console_users.c.username,
                        console_users.c.password_hash,
                        console_users.c.password_parameters,
                        console_users.c.password_changed_at,
                        console_users.c.disabled_at,
                    ).where(console_users.c.username == username)
                )
                .mappings()
                .first()
            )
        return self._to_user(row) if row is not None else None

    def update_password(self, user_id: UUID, password: PasswordRecord, *, now: datetime) -> None:
        with self._engine.begin() as connection:
            result = connection.execute(
                sa.update(console_users)
                .where(console_users.c.id == user_id, console_users.c.disabled_at.is_(None))
                .values(
                    password_hash=password.encoded_hash,
                    password_algorithm=HASH_SCHEME,
                    password_parameters=dict(password.parameters),
                    password_changed_at=now,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise InvalidCredentialsState("console user is unavailable")

    def create_session(self, session: SessionInsert) -> None:
        with self._engine.begin() as connection:
            connection.execute(sa.insert(console_sessions).values(**self._session_values(session)))

    def get_session(self, token_hash: str) -> SessionRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(
                        console_sessions.c.id,
                        console_sessions.c.user_id,
                        console_users.c.username,
                        console_users.c.candidate_id,
                        console_sessions.c.state,
                        console_sessions.c.token_hash,
                        console_sessions.c.csrf_token_hash,
                        console_sessions.c.created_at,
                        console_sessions.c.last_seen_at,
                        console_sessions.c.idle_expires_at,
                        console_sessions.c.absolute_expires_at,
                        console_sessions.c.revoked_at,
                    )
                    .select_from(
                        console_sessions.outerjoin(
                            console_users,
                            console_users.c.id == console_sessions.c.user_id,
                        )
                    )
                    .where(
                        console_sessions.c.token_hash == token_hash,
                        sa.or_(
                            console_sessions.c.user_id.is_(None),
                            console_users.c.disabled_at.is_(None),
                        ),
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return SessionRecord(
            id=cast("UUID", row["id"]),
            user_id=cast("UUID | None", row["user_id"]),
            username=cast("str | None", row["username"]),
            state=SessionState(cast("str", row["state"])),
            token_hash=cast("str", row["token_hash"]),
            csrf_token_hash=cast("str", row["csrf_token_hash"]),
            created_at=cast("datetime", row["created_at"]),
            last_seen_at=cast("datetime", row["last_seen_at"]),
            idle_expires_at=cast("datetime", row["idle_expires_at"]),
            absolute_expires_at=cast("datetime", row["absolute_expires_at"]),
            revoked_at=cast("datetime | None", row["revoked_at"]),
            candidate_id=cast("UUID | None", row.get("candidate_id")),
        )

    def touch_session(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        now: datetime,
        idle_expires_at: datetime,
    ) -> bool:
        with self._engine.begin() as connection:
            result = connection.execute(
                sa.update(console_sessions)
                .where(
                    console_sessions.c.id == session_id,
                    console_sessions.c.token_hash == token_hash,
                    console_sessions.c.state == SessionState.AUTHENTICATED.value,
                    console_sessions.c.revoked_at.is_(None),
                    console_sessions.c.idle_expires_at > now,
                    console_sessions.c.absolute_expires_at > now,
                )
                .values(last_seen_at=now, idle_expires_at=idle_expires_at)
            )
            return result.rowcount == 1

    def rotate_session(
        self,
        old_session_id: UUID,
        *,
        old_token_hash: str,
        replacement: SessionInsert,
        now: datetime,
    ) -> bool:
        with self._engine.begin() as connection:
            existing = (
                connection.execute(
                    sa.select(
                        console_sessions.c.id,
                        console_sessions.c.state,
                        console_sessions.c.idle_expires_at,
                        console_sessions.c.absolute_expires_at,
                    )
                    .where(
                        console_sessions.c.id == old_session_id,
                        console_sessions.c.token_hash == old_token_hash,
                        console_sessions.c.revoked_at.is_(None),
                    )
                    .with_for_update(of=console_sessions)
                )
                .mappings()
                .first()
            )
            if (
                existing is None
                or existing["state"]
                not in {
                    SessionState.PREAUTH.value,
                    SessionState.AUTHENTICATED.value,
                }
                or now >= existing["idle_expires_at"]
                or now >= existing["absolute_expires_at"]
            ):
                return False
            connection.execute(
                sa.insert(console_sessions).values(**self._session_values(replacement))
            )
            result = connection.execute(
                sa.update(console_sessions)
                .where(
                    console_sessions.c.id == old_session_id,
                    console_sessions.c.token_hash == old_token_hash,
                    console_sessions.c.revoked_at.is_(None),
                )
                .values(
                    state=SessionState.REVOKED.value,
                    revoked_at=now,
                    rotated_to_session_id=replacement.id,
                )
            )
            if result.rowcount != 1:
                raise InvalidSession("session rotation changed concurrently")
            return True

    def revoke_session(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        now: datetime,
    ) -> bool:
        with self._engine.begin() as connection:
            result = connection.execute(
                sa.update(console_sessions)
                .where(
                    console_sessions.c.id == session_id,
                    console_sessions.c.token_hash == token_hash,
                    console_sessions.c.state == SessionState.AUTHENTICATED.value,
                    console_sessions.c.revoked_at.is_(None),
                )
                .values(state=SessionState.REVOKED.value, revoked_at=now)
            )
            return result.rowcount == 1

    def revoke_user_sessions(
        self,
        user_id: UUID,
        *,
        now: datetime,
        except_session_id: UUID | None = None,
    ) -> int:
        statement = sa.update(console_sessions).where(
            console_sessions.c.user_id == user_id,
            console_sessions.c.state == SessionState.AUTHENTICATED.value,
            console_sessions.c.revoked_at.is_(None),
        )
        if except_session_id is not None:
            statement = statement.where(console_sessions.c.id != except_session_id)
        with self._engine.begin() as connection:
            result = connection.execute(
                statement.values(state=SessionState.REVOKED.value, revoked_at=now)
            )
            return result.rowcount

    @staticmethod
    def _session_values(session: SessionInsert) -> Mapping[str, object]:
        return {
            "id": session.id,
            "user_id": session.user_id,
            "state": session.state.value,
            "token_hash": session.token_hash,
            "csrf_token_hash": session.csrf_token_hash,
            "client_fingerprint": session.client_fingerprint,
            "created_at": session.created_at,
            "last_seen_at": session.last_seen_at,
            "idle_expires_at": session.idle_expires_at,
            "absolute_expires_at": session.absolute_expires_at,
        }

    @staticmethod
    def _to_user(row: RowMapping) -> ConsoleUserRecord:
        parameters = cast("Mapping[str, int | str]", row["password_parameters"])
        return ConsoleUserRecord(
            id=cast("UUID", row["id"]),
            username=cast("str", row["username"]),
            password=PasswordRecord(
                encoded_hash=cast("str", row["password_hash"]),
                parameters=dict(parameters),
            ),
            password_changed_at=cast("datetime", row["password_changed_at"]),
            candidate_id=cast("UUID | None", row.get("candidate_id")),
            disabled_at=cast("datetime | None", row["disabled_at"]),
        )


class InvalidCredentialsState(RuntimeError):
    pass


__all__ = ["PostgresAuthRepository"]
