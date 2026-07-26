"""Tests for API route authentication enforcement.

Verifies that protected endpoints (jobs, matching, applications) reject
unauthenticated requests with 401 and missing/invalid CSRF with 403,
while public endpoints (health, metrics) remain accessible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from careerops.api.auth_dependency import require_api_auth
from careerops.auth.contracts import (
    AuthenticatedPrincipal,
    CsrfRejected,
    InvalidSession,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SESSION_COOKIE = "careerops_session"
_CSRF_HEADER = "X-CSRF-Token"


@dataclass(frozen=True, slots=True)
class _FakeSettings:
    session_cookie_name: str = _SESSION_COOKIE
    csrf_cookie_name: str = "careerops_csrf"


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=uuid4(),
        username="testuser",
        session_id=uuid4(),
        csrf_token_hash="irrelevant",
        absolute_expires_at=datetime.now(tz=UTC) + timedelta(days=1),
    )


def _make_auth_service(
    *,
    principal: AuthenticatedPrincipal | None = None,
    fail_auth: bool = False,
    fail_csrf: bool = False,
) -> MagicMock:
    svc = MagicMock()
    effective = principal or _principal()
    if fail_auth:
        svc.authenticate.side_effect = InvalidSession("bad session")
    else:
        svc.authenticate.return_value = effective
    if fail_csrf:
        svc.validate_csrf.side_effect = CsrfRejected("bad csrf")
    return svc


def _make_app(auth_service: Any, web_settings: Any) -> FastAPI:
    app = FastAPI()
    app.state.auth_service = auth_service
    app.state.web_settings = web_settings

    @app.get("/api/v1/jobs")
    async def _protected(  # pyright: ignore[reportUnusedFunction]
        principal: AuthenticatedPrincipal | None = Depends(require_api_auth),  # noqa: B008
    ) -> dict[str, str]:
        return {"user": principal.username if principal else "anonymous"}

    # CSRF protection only applies to mutating methods (see require_api_auth:
    # "Per spec §4: GET/HEAD only check session cookie; mutating methods also
    # check CSRF"). This POST route lets tests exercise the CSRF branch.
    @app.post("/api/v1/jobs")
    async def _protected_post(  # pyright: ignore[reportUnusedFunction]
        principal: AuthenticatedPrincipal | None = Depends(require_api_auth),  # noqa: B008
    ) -> dict[str, str]:
        return {"user": principal.username if principal else "anonymous"}

    @app.get("/api/v1/health/live")
    async def _public() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRequireApiAuth:
    """Unit tests for the ``require_api_auth`` FastAPI dependency."""

    def test_no_session_cookie_returns_401(self) -> None:
        app = _make_app(_make_auth_service(), _FakeSettings())
        resp = TestClient(app).get("/api/v1/jobs")
        assert resp.status_code == 401
        # WIP's UnauthorizedError.message_default is title-cased ("Authentication required").
        assert resp.json()["detail"] == "Authentication required"

    def test_invalid_session_token_returns_401(self) -> None:
        app = _make_app(_make_auth_service(fail_auth=True), _FakeSettings())
        resp = TestClient(app).get(
            "/api/v1/jobs",
            cookies={_SESSION_COOKIE: "bad-token"},
        )
        assert resp.status_code == 401

    def test_valid_session_missing_csrf_returns_403(self) -> None:
        # CSRF is only enforced on mutating methods (POST/PUT/DELETE) per
        # require_api_auth's spec §4 note. Use POST so the missing CSRF token
        # is actually validated and rejected.
        app = _make_app(_make_auth_service(fail_csrf=True), _FakeSettings())
        resp = TestClient(app).post(
            "/api/v1/jobs",
            cookies={_SESSION_COOKIE: "valid-token"},
        )
        assert resp.status_code == 403
        # WIP's CSRFRejectedError.message_default is title-cased.
        assert resp.json()["detail"] == "CSRF token rejected"

    def test_valid_session_and_csrf_passes(self) -> None:
        app = _make_app(_make_auth_service(), _FakeSettings())
        resp = TestClient(app).get(
            "/api/v1/jobs",
            cookies={_SESSION_COOKIE: "valid-token"},
            headers={_CSRF_HEADER: "valid-csrf"},
        )
        assert resp.status_code == 200
        assert resp.json()["user"] == "testuser"

    def test_no_auth_service_or_settings_allows_through(self) -> None:
        """When auth is not configured at all, allow through (backward compat)."""
        app = _make_app(None, None)
        resp = TestClient(app).get("/api/v1/jobs")
        assert resp.status_code == 200

    def test_auth_service_without_web_settings_rejects(self) -> None:
        """Partial config (auth_service set, web_settings missing) rejects."""
        app = _make_app(_make_auth_service(), None)
        resp = TestClient(app).get(
            "/api/v1/jobs",
            cookies={_SESSION_COOKIE: "any"},
            headers={_CSRF_HEADER: "any"},
        )
        assert resp.status_code == 401

    def test_public_endpoint_bypasses_auth(self) -> None:
        app = _make_app(None, None)
        resp = TestClient(app).get("/api/v1/health/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
