"""Session-scoped bootstrap for integration tests.

Ensures the five PostgreSQL capability roles exist and are granted to the
runtime identity so that ``SET LOCAL ROLE`` works inside test transactions.
Role creation requires a superuser, so we shell out to ``psql`` (peer auth)
using the database owner implied by the test database URL's database name.
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


def _db_name_from_url(url: str) -> str:
    """Extract the database name from a SQLAlchemy URL string."""
    return sa.engine.make_url(url).database or ""


def _runtime_user_from_url(url: str) -> str:
    """Extract the username from a SQLAlchemy URL string."""
    return sa.engine.make_url(url).username or ""


@pytest.fixture(scope="session", autouse=True)
def _ensure_capability_roles() -> None:
    """Create capability roles and grant them to the test runtime user.

    Uses ``psql`` with peer auth (no password needed for the local superuser).
    """
    database_url = os.environ.get("CAREEROPS_TEST_DATABASE_URL")
    if database_url is None:
        return

    db_name = _db_name_from_url(database_url)
    runtime_user = _runtime_user_from_url(database_url)

    # Create capability roles (requires superuser).
    subprocess.run(
        ["psql", "-d", db_name, "-c", _BOOTSTRAP_SQL],
        check=True,
        capture_output=True,
        text=True,
    )

    # Grant capability roles to the runtime user so SET ROLE works.
    grants = " ".join(f"GRANT {r} TO {runtime_user};" for r in _CAPABILITY_ROLES)
    subprocess.run(
        ["psql", "-d", db_name, "-c", grants],
        check=True,
        capture_output=True,
        text=True,
    )

    # Grant CREATEROLE so test fixtures that create temporary LOGIN roles work.
    subprocess.run(
        ["psql", "-d", db_name, "-c", f"ALTER ROLE {runtime_user} CREATEROLE;"],
        check=True,
        capture_output=True,
        text=True,
    )
