from __future__ import annotations

from sqlalchemy.engine import Engine

from careerops.auth import (
    Argon2idPasswordHasher,
    AuthRateLimiter,
    ConsoleAuthService,
    FixedWindowAuthRateLimiter,
)
from careerops.infrastructure.database.auth import PostgresAuthRepository
from careerops.infrastructure.database.auth_audit import PostgresAuthAuditSink


def create_console_auth_service(
    engine: Engine,
    *,
    rate_limiter: AuthRateLimiter | None = None,
) -> ConsoleAuthService:
    return ConsoleAuthService(
        PostgresAuthRepository(engine),
        Argon2idPasswordHasher(),
        rate_limiter or FixedWindowAuthRateLimiter(),
        PostgresAuthAuditSink(engine),
    )
