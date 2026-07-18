from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from careerops.application.dashboard import DashboardSystemStatus
from careerops.application.ports.readiness import ReadinessReport, ReadinessState
from careerops.auth import (
    Argon2idPasswordHasher,
    AuthRequestContext,
    ConsoleAuthService,
    FixedWindowAuthRateLimiter,
)
from careerops.config import DatabaseCapabilityRole, Settings
from careerops.infrastructure.dashboard import RuntimeDashboardSnapshotProvider
from careerops.infrastructure.database.auth import PostgresAuthRepository
from careerops.infrastructure.database.auth_audit import PostgresAuthAuditSink
from careerops.infrastructure.database.engine import create_database_engine

pytestmark = pytest.mark.integration


class ReadyProbe:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            checks={
                "database": ReadinessState.OK,
                "redis": ReadinessState.OK,
                "temporal": ReadinessState.OK,
                "storage": ReadinessState.OK,
            }
        )

    async def close(self) -> None:
        return None


@pytest.fixture(scope="module")
def runtime_engine() -> Iterator[Engine]:
    database_url = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("CAREEROPS_TEST_DATABASE_URL is required for auth integration tests")
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    owner_engine = sa.create_engine(database_url)
    login = f"careerops_auth_test_{uuid4().hex}"
    password = "disposable-auth-integration-password"
    with owner_engine.begin() as connection:
        connection.execute(
            sa.text(
                f"CREATE ROLE {login} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                f"NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{password}'"
            )
        )
        connection.execute(sa.text(f"GRANT careerops_api TO {login}"))
        connection.execute(
            sa.text(
                "TRUNCATE careerops.console_sessions, careerops.bootstrap_tokens, "
                "careerops.console_users CASCADE"
            )
        )
    runtime_url = sa.engine.make_url(database_url).set(username=login, password=password)
    engine = create_database_engine(
        Settings.model_validate(
            {
                "database_url": runtime_url.render_as_string(hide_password=False),
                "database_role": DatabaseCapabilityRole.API,
            }
        )
    )
    yield engine
    engine.dispose()
    with owner_engine.begin() as connection:
        connection.execute(sa.text(f"REVOKE careerops_api FROM {login}"))
        connection.execute(sa.text(f"DROP ROLE {login}"))
    owner_engine.dispose()


def test_bootstrap_login_and_logout_use_hash_only_database_state(runtime_engine: Engine) -> None:
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    service = ConsoleAuthService(
        PostgresAuthRepository(runtime_engine),
        Argon2idPasswordHasher(memory_cost=8192, time_cost=1, parallelism=1),
        FixedWindowAuthRateLimiter(),
        PostgresAuthAuditSink(runtime_engine),
    )
    context = AuthRequestContext(trace_id="auth-postgres", client_key="127.0.0.1")

    bootstrap = service.issue_bootstrap_token(now=now)
    preauth = service.begin_preauth(now=now, context=context)
    session = service.complete_bootstrap(
        preauth_token=preauth.token,
        csrf_token=preauth.csrf_token,
        bootstrap_token=bootstrap.token,
        username="owner.user",
        password="correct horse battery staple",
        now=now,
        context=context,
    )
    principal = service.authenticate(session.token, now=now)
    service.logout(
        session_token=session.token,
        csrf_token=session.csrf_token,
        now=now,
        context=context,
    )

    with runtime_engine.connect() as connection:
        stored = connection.execute(
            sa.text(
                "SELECT user_info.password_hash, "
                "bootstrap.token_hash AS bootstrap_hash, "
                "session.token_hash AS session_hash, session.csrf_token_hash "
                "FROM careerops.console_users AS user_info "
                "JOIN careerops.bootstrap_tokens AS bootstrap "
                "ON bootstrap.used_by_user_id = user_info.id "
                "JOIN careerops.console_sessions AS session ON session.user_id = user_info.id "
                "WHERE session.id = :session_id"
            ),
            {"session_id": principal.session_id},
        ).one()
        auth_audit_count = connection.scalar(
            sa.text(
                "SELECT count(*) FROM careerops.audit_events "
                "WHERE trace_id IN ('local-bootstrap-cli', 'auth-postgres')"
            )
        )

    assert stored.password_hash.startswith("$argon2id$")
    assert stored.bootstrap_hash != bootstrap.token
    assert stored.session_hash != session.token
    assert stored.csrf_token_hash != session.csrf_token
    assert auth_audit_count == 3


@pytest.mark.asyncio
async def test_dashboard_reads_live_pending_counts_with_api_capability(
    runtime_engine: Engine,
) -> None:
    provider = RuntimeDashboardSnapshotProvider(
        ReadyProbe(),
        runtime_engine,
        Settings(),
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
    )

    snapshot = await provider.snapshot()

    assert snapshot.system_status is DashboardSystemStatus.READY
    assert isinstance(snapshot.pending_approvals, int)
    assert isinstance(snapshot.pending_outbox_events, int)
