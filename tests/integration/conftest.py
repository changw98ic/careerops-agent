"""Session-scoped bootstrap for integration tests.

Ensures the five PostgreSQL capability roles exist and are granted to the
runtime identity so that ``SET LOCAL ROLE`` works inside test transactions.
Role creation requires a superuser, so we shell out to ``psql``
using the connection settings from the disposable test database URL.
"""

from __future__ import annotations

import os
import subprocess

import pytest
import sqlalchemy as sa

_CAPABILITY_ROLES = (
    "careerops_api",
    "careerops_retention",
    "careerops_outbox",
    "careerops_side_effect",
    "careerops_readonly",
    "careerops_worker",
    "careerops_legacy_agent",
)

_BOOTSTRAP_SQL = (
    "DO $$\n"
    "BEGIN\n"
    + "".join(
        f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"    CREATE ROLE {role} NOLOGIN;\n"
        f"  END IF;\n"
        for role in _CAPABILITY_ROLES
    )
    + "END\n$$;\n"
    + "".join(
        f"ALTER ROLE {role} WITH NOLOGIN NOSUPERUSER NOCREATEDB "
        f"NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
        for role in _CAPABILITY_ROLES
    )
)


def _runtime_user_from_url(url: str) -> str:
    """Extract the username from a SQLAlchemy URL string."""
    return sa.engine.make_url(url).username or ""


def _psql_command(url: str, statement: str) -> tuple[list[str], dict[str, str]]:
    """Build a psql command from the disposable test database URL."""
    parsed = sa.engine.make_url(url)
    command = ["psql"]
    if parsed.host:
        command.extend(["-h", parsed.host])
    if parsed.port:
        command.extend(["-p", str(parsed.port)])
    if parsed.username:
        command.extend(["-U", parsed.username])
    command.extend(["-d", parsed.database or "", "-v", "ON_ERROR_STOP=1", "-c", statement])
    environment = os.environ.copy()
    if parsed.password is not None:
        environment["PGPASSWORD"] = parsed.password
    return command, environment


@pytest.fixture(scope="session", autouse=True)
def _ensure_capability_roles() -> None:
    """Create capability roles and grant them to the test runtime user.

    Uses the disposable test URL, including host, port, user, and password.
    """
    database_url = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if database_url is None:
        return

    runtime_user = _runtime_user_from_url(database_url)

    # Create capability roles (requires the disposable database URL's admin user).
    bootstrap_command, bootstrap_environment = _psql_command(database_url, _BOOTSTRAP_SQL)
    subprocess.run(
        bootstrap_command,
        env=bootstrap_environment,
        check=True,
        capture_output=True,
        text=True,
    )

    # Grant capability roles to the runtime user so SET ROLE works.
    grants = " ".join(f"GRANT {r} TO {runtime_user};" for r in _CAPABILITY_ROLES)
    grant_command, grant_environment = _psql_command(database_url, grants)
    subprocess.run(
        grant_command,
        env=grant_environment,
        check=True,
        capture_output=True,
        text=True,
    )

    # Grant CREATEROLE so test fixtures that create temporary LOGIN roles work.
    role_command, role_environment = _psql_command(
        database_url, f"ALTER ROLE {runtime_user} CREATEROLE;"
    )
    subprocess.run(
        role_command,
        env=role_environment,
        check=True,
        capture_output=True,
        text=True,
    )
