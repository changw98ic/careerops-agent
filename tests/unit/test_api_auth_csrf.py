"""Tests for CSRF enforcement: GET exempt, POST/PUT/DELETE require CSRF header."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from careerops.api.auth_dependency import require_api_auth, require_web_auth
from careerops.auth.contracts import AuthenticatedPrincipal, CsrfRejected


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=uuid4(),
        username="testuser",
        session_id=uuid4(),
        csrf_token_hash="hash",
        absolute_expires_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _app_with_auth(auth_service: MagicMock) -> FastAPI:
    """Build an app with both API (CSRF-required) and web (no CSRF) routes."""
    app = FastAPI()
    app.state.auth_service = auth_service
    app.state.web_settings = MagicMock()  # non-None to enable auth checks

    # Web route (GET, no CSRF)
    @app.get("/api/v1/auth/session")
    async def web_endpoint(
        principal: Annotated[AuthenticatedPrincipal | None, Depends(require_web_auth)] = None,
    ) -> dict[str, bool]:
        return {"authenticated": principal is not None}

    # API route (POST, requires CSRF)
    @app.post("/api/v1/test-mutation")
    async def api_endpoint(
        principal: Annotated[AuthenticatedPrincipal | None, Depends(require_api_auth)] = None,
    ) -> dict[str, bool]:
        return {"ok": True}

    # API route (PUT, requires CSRF)
    @app.put("/api/v1/test-mutation/{id}")
    async def api_put(
        id: str,
        principal: Annotated[AuthenticatedPrincipal | None, Depends(require_api_auth)] = None,
    ) -> dict[str, bool]:
        return {"ok": True}

    # API route (DELETE, requires CSRF)
    @app.delete("/api/v1/test-mutation/{id}")
    async def api_delete(
        id: str,
        principal: Annotated[AuthenticatedPrincipal | None, Depends(require_api_auth)] = None,
    ) -> dict[str, bool]:
        return {"ok": True}

    return app


class TestGetDoesNotRequireCsrf:
    def test_get_succeeds_without_csrf_header(self) -> None:
        svc = MagicMock()
        svc.authenticate.return_value = _principal()
        app = _app_with_auth(svc)
        resp = TestClient(app).get(
            "/api/v1/auth/session",
            cookies={"careerops_session": "valid-token"},
        )
        assert resp.status_code == 200
        svc.validate_csrf.assert_not_called()

    def test_get_succeeds_with_session_only(self) -> None:
        svc = MagicMock()
        svc.authenticate.return_value = _principal()
        app = _app_with_auth(svc)
        resp = TestClient(app).get(
            "/api/v1/auth/session",
            cookies={"careerops_session": "valid-token"},
        )
        assert resp.status_code == 200


class TestPostRequiresCsrf:
    def test_post_without_csrf_header_returns_403(self) -> None:
        svc = MagicMock()
        svc.authenticate.return_value = _principal()
        svc.validate_csrf.side_effect = CsrfRejected("missing")
        app = _app_with_auth(svc)
        resp = TestClient(app).post(
            "/api/v1/test-mutation",
            cookies={"careerops_session": "valid-token"},
        )
        assert resp.status_code == 403

    def test_post_with_valid_csrf_succeeds(self) -> None:
        svc = MagicMock()
        svc.authenticate.return_value = _principal()
        app = _app_with_auth(svc)
        resp = TestClient(app).post(
            "/api/v1/test-mutation",
            cookies={"careerops_session": "valid-token"},
            headers={"x-csrf-token": "valid-csrf"},
        )
        assert resp.status_code == 200

    def test_post_with_wrong_csrf_returns_403(self) -> None:
        svc = MagicMock()
        svc.authenticate.return_value = _principal()
        svc.validate_csrf.side_effect = CsrfRejected("mismatch")
        app = _app_with_auth(svc)
        resp = TestClient(app).post(
            "/api/v1/test-mutation",
            cookies={"careerops_session": "valid-token"},
            headers={"x-csrf-token": "wrong-csrf"},
        )
        assert resp.status_code == 403


class TestPutRequiresCsrf:
    def test_put_without_csrf_header_returns_403(self) -> None:
        svc = MagicMock()
        svc.authenticate.return_value = _principal()
        svc.validate_csrf.side_effect = CsrfRejected("missing")
        app = _app_with_auth(svc)
        resp = TestClient(app).put(
            "/api/v1/test-mutation/123",
            cookies={"careerops_session": "valid-token"},
        )
        assert resp.status_code == 403

    def test_put_with_valid_csrf_succeeds(self) -> None:
        svc = MagicMock()
        svc.authenticate.return_value = _principal()
        app = _app_with_auth(svc)
        resp = TestClient(app).put(
            "/api/v1/test-mutation/123",
            cookies={"careerops_session": "valid-token"},
            headers={"x-csrf-token": "valid-csrf"},
        )
        assert resp.status_code == 200


class TestDeleteRequiresCsrf:
    def test_delete_without_csrf_header_returns_403(self) -> None:
        svc = MagicMock()
        svc.authenticate.return_value = _principal()
        svc.validate_csrf.side_effect = CsrfRejected("missing")
        app = _app_with_auth(svc)
        resp = TestClient(app).delete(
            "/api/v1/test-mutation/123",
            cookies={"careerops_session": "valid-token"},
        )
        assert resp.status_code == 403

    def test_delete_with_valid_csrf_succeeds(self) -> None:
        svc = MagicMock()
        svc.authenticate.return_value = _principal()
        app = _app_with_auth(svc)
        resp = TestClient(app).delete(
            "/api/v1/test-mutation/123",
            cookies={"careerops_session": "valid-token"},
            headers={"x-csrf-token": "valid-csrf"},
        )
        assert resp.status_code == 200


class TestCsrfDependencyBehavior:
    def test_require_api_auth_rejects_no_session(self) -> None:
        svc = MagicMock()
        app = _app_with_auth(svc)
        resp = TestClient(app).post(
            "/api/v1/test-mutation",
            headers={"x-csrf-token": "any-csrf"},
        )
        assert resp.status_code == 401

    def test_require_api_auth_rejects_invalid_session(self) -> None:
        from careerops.auth.contracts import InvalidSession as InvalidSessionError

        svc = MagicMock()
        svc.authenticate.side_effect = InvalidSessionError("bad")
        app = _app_with_auth(svc)
        resp = TestClient(app).post(
            "/api/v1/test-mutation",
            cookies={"careerops_session": "bad-token"},
            headers={"x-csrf-token": "any-csrf"},
        )
        assert resp.status_code == 401
