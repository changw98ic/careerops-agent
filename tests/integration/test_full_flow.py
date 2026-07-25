"""Integration test: full API lifecycle via real Postgres + Redis containers.

Requires Docker.  Uses one-time PostgreSQL and Redis via ``testcontainers``.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx2
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from careerops.api.app import create_app
from careerops.config import RuntimeEnvironment, Settings
from careerops.infrastructure.database.engine import create_database_engine

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PG_IMAGE = "postgres:16-alpine"
_REDIS_IMAGE = "redis:7-alpine"

_CAPABILITY_ROLES = (
    "careerops_api",
    "careerops_retention",
    "careerops_outbox",
    "careerops_side_effect",
    "careerops_readonly",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redis_url(container: RedisContainer) -> str:
    """Build a redis:// URL from a running RedisContainer."""
    host = container.get_container_host_ip()
    port = container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


def _preauth(client: httpx2.Client) -> httpx2.Cookies:
    """Call /preauth and return cookies."""
    resp = client.post("/api/v1/auth/preauth")
    assert resp.status_code == 200, f"preauth failed: {resp.text}"
    data = resp.json()
    assert data["ok"] is True
    assert data["csrf_token"]
    return resp.cookies


def _bootstrap(
    client: httpx2.Client,
    bootstrap_token: str,
    username: str = "admin",
    password: str = "correct horse battery staple",
) -> tuple[httpx2.Cookies, str]:
    """Call /bootstrap and return (cookies, csrf_token)."""
    resp = client.post(
        "/api/v1/auth/bootstrap",
        json={
            "bootstrap_token": bootstrap_token,
            "username": username,
            "password": password,
        },
    )
    assert resp.status_code == 200, f"bootstrap failed: {resp.text}"
    data = resp.json()
    assert data["ok"] is True
    assert data["authenticated"] is True
    assert data["csrf_token"]
    return resp.cookies, data["csrf_token"]


def _login(
    client: httpx2.Client,
    username: str = "admin",
    password: str = "correct horse battery staple",
) -> tuple[httpx2.Cookies, str]:
    """Call /preauth then /login, return (cookies, csrf_token)."""
    _preauth(client)
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, f"login failed: {resp.text}"
    data = resp.json()
    assert data["ok"] is True
    assert data["authenticated"] is True
    return resp.cookies, data["csrf_token"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _containers() -> Iterator[tuple[str, str]]:
    """Spin up one-time PostgreSQL and Redis containers."""
    with (
        PostgresContainer(image=_PG_IMAGE) as pg,
        RedisContainer(image=_REDIS_IMAGE) as rd,
    ):
        yield pg.get_connection_url(), _redis_url(rd)


@pytest.fixture(scope="module")
def _migrated_engine(
    _containers: tuple[str, str],
) -> Iterator[Engine]:
    """Run Alembic migrations, create capability roles, return superuser engine."""
    database_url, _ = _containers
    url = database_url.replace("+psycopg2", "+psycopg")

    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    command.upgrade(config, "head")

    engine = sa.create_engine(url)
    with engine.begin() as conn:
        for role in _CAPABILITY_ROLES:
            conn.execute(sa.text(f"CREATE ROLE {role} NOLOGIN"))
            conn.execute(
                sa.text(
                    f"ALTER ROLE {role} WITH NOLOGIN NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                )
            )

    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def _settings(
    _containers: tuple[str, str],
    _migrated_engine: Engine,
) -> Iterator[Settings]:
    """Create a runtime user and build Settings for the test containers."""
    database_url, redis_url = _containers
    url = database_url.replace("+psycopg2", "+psycopg")
    parsed = sa.engine.make_url(url)
    login = f"careerops_it_{uuid4().hex[:8]}"
    password = "integration-test-password"

    with _migrated_engine.begin() as conn:
        conn.execute(
            sa.text(
                f"CREATE ROLE {login} LOGIN INHERIT NOSUPERUSER NOCREATEDB "
                f"NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{password}'"
            )
        )
        conn.execute(sa.text(f"GRANT careerops_api TO {login}"))
        conn.execute(
            sa.text(f"GRANT USAGE ON SCHEMA careerops TO {login}")
        )
        conn.execute(
            sa.text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE "
                f"ON ALL TABLES IN SCHEMA careerops TO {login}"
            )
        )
        conn.execute(
            sa.text(
                f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA careerops TO {login}"
            )
        )
        conn.execute(
            sa.text(
                f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA careerops "
                f"TO {login}"
            )
        )

    runtime_url = parsed.set(username=login, password=password)
    # /private/tmp avoids macOS /var symlink (O_NOFOLLOW in storage layer).
    storage_root = Path(f"/private/tmp/careerops-it-{uuid4().hex[:8]}")
    storage_root.mkdir(parents=True, exist_ok=True)

    yield Settings.model_validate(
        {
            "environment": RuntimeEnvironment.DEVELOPMENT,
            "database_url": runtime_url.render_as_string(
                hide_password=False,
            ),
            "redis_url": redis_url,
            "console_cookie_secure": False,
            "console_allowed_hosts": ("testserver:80",),
            "console_allowed_origins": ("http://testserver",),
            "storage_root": str(storage_root),
        }
    )
    shutil.rmtree(storage_root, ignore_errors=True)


@pytest.fixture(scope="module")
def _client(_settings: Settings) -> Iterator[httpx2.Client]:
    """Create a FastAPI test client with full RuntimeResources wiring."""
    app = create_app(_settings)
    yield cast("httpx2.Client", TestClient(app, raise_server_exceptions=False))


@pytest.fixture(scope="module")
def _seeded_data(
    _migrated_engine: Engine,
) -> dict[str, Any]:
    """Seed a company + job for the integration test."""
    company_id = uuid4()
    source_id = uuid4()
    job_id = uuid4()
    posting_id = uuid4()
    decision_id = uuid4()
    now = datetime.now(UTC)

    with _migrated_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO careerops.companies "
                "(id, name, normalized_name) "
                "VALUES (:id, :name, :nn)"
            ),
            {"id": company_id, "name": "Acme Corp", "nn": "acme corp"},
        )
        conn.execute(
            sa.text(
                "INSERT INTO careerops.job_sources "
                "(id, company_id, source_type, source_identifier, "
                "base_url, state, verified_at) "
                "VALUES (:id, :cid, 'greenhouse', 'acme', "
                "'https://acme.com/jobs', 'active', :now)"
            ),
            {"id": source_id, "cid": company_id, "now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO careerops.canonical_jobs "
                "(id, company_id, canonical_title, normalized_title) "
                "VALUES (:id, :cid, "
                "'Senior Software Engineer', "
                "'senior software engineer')"
            ),
            {"id": job_id, "cid": company_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO careerops.job_postings "
                "(id, source_id, external_id, canonical_url, "
                "source_state, first_seen_at, last_seen_at) "
                "VALUES (:id, :sid, 'ext-001', "
                "'https://acme.com/jobs/1', 'active', :now, :now)"
            ),
            {"id": posting_id, "sid": source_id, "now": now},
        )
        conn.execute(
            sa.text(
                "INSERT INTO careerops.job_merge_decisions "
                "(id, job_posting_id, to_canonical_job_id, "
                "decision_kind, rule, reason, actor_type) "
                "VALUES (:did, :pid, :jid, 'merge', 'auto', "
                "'initial', 'rule')"
            ),
            {"did": decision_id, "pid": posting_id, "jid": job_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO careerops.job_posting_assignments "
                "(job_posting_id, canonical_job_id, "
                "decision_id, assigned_at) "
                "VALUES (:pid, :jid, :did, :now)"
            ),
            {
                "pid": posting_id,
                "jid": job_id,
                "did": decision_id,
                "now": now,
            },
        )

    return {
        "company_id": company_id,
        "job_id": job_id,
        "posting_id": posting_id,
    }


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_full_lifecycle(
    _client: httpx2.Client,
    _seeded_data: dict[str, Any],
    _settings: Settings,
) -> None:
    """Bootstrap -> login -> session -> jobs -> job detail -> create
    application -> duplicate -> logout -> old session rejected."""
    client = _client
    job_id = str(_seeded_data["job_id"])

    # 1. Issue a bootstrap token via the auth service directly.
    from careerops.infrastructure.auth import (
        create_console_auth_service,
    )

    engine = create_database_engine(_settings, enforce_role=False)
    auth_service = create_console_auth_service(engine)
    bootstrap_cred = auth_service.issue_bootstrap_token(
        now=datetime.now(UTC),
    )
    engine.dispose()

    # 2. Bootstrap (create the admin user).
    _preauth(client)
    _bootstrap(client, bootstrap_cred.token)

    # 3. Login.
    _, csrf = _login(client)
    api_headers = {"X-CSRF-Token": csrf}

    # 4. Session check (refresh).
    resp = client.get("/api/v1/auth/session")
    assert resp.status_code == 200
    session_data = resp.json()
    assert session_data["authenticated"] is True
    assert session_data["user"]["username"] == "admin"
    assert session_data["csrf_token"]

    # 5. List jobs (should contain the seeded job).
    resp = client.get("/api/v1/jobs", headers=api_headers)
    assert resp.status_code == 200
    jobs = resp.json()
    assert jobs["total"] >= 1
    assert any(j["id"] == job_id for j in jobs["items"])

    # 6. Job detail.
    resp = client.get(
        f"/api/v1/jobs/{job_id}", headers=api_headers,
    )
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["canonical_job"]["id"] == job_id
    assert (
        detail["canonical_job"]["canonical_title"]
        == "Senior Software Engineer"
    )
    assert len(detail["canonical_job"]["postings"]) >= 1

    # 7. Create application.
    resp = client.post(
        "/api/v1/applications",
        json={
            "canonical_job_id": job_id,
            "apply_url": "https://acme.com/apply",
        },
        headers=api_headers,
    )
    assert resp.status_code == 201, f"create application: {resp.text}"
    app_data = resp.json()
    app_id = app_data["id"]
    assert app_data["canonical_job_id"] == job_id
    assert app_data["state"] == "favorited"

    # 8. Duplicate application (idempotent).
    resp = client.post(
        "/api/v1/applications",
        json={
            "canonical_job_id": job_id,
            "apply_url": "https://acme.com/apply",
        },
        headers=api_headers,
    )
    assert resp.status_code == 200, f"duplicate app: {resp.text}"
    dup = resp.json()
    assert dup["id"] == app_id, "duplicate must return same application"

    # 9. Logout.
    resp = client.post(
        "/api/v1/auth/logout", headers=api_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # 10. Old session rejected.
    resp = client.get("/api/v1/auth/session")
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False

    # Protected endpoints must reject after logout.
    resp = client.get("/api/v1/jobs", headers=api_headers)
    assert resp.status_code == 401
