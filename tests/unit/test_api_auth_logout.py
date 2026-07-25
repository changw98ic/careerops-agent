"""Tests for POST /api/v1/auth/logout endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.auth.contracts import InvalidSession
from careerops.web.api_auth import router


def _app(auth_service: MagicMock | None) -> FastAPI:
    app = FastAPI()
    app.state.auth_service = auth_service
    app.include_router(router)
    return app


def _auth_cookies() -> dict[str, str]:
    return {"careerops_session": "valid-session", "careerops_csrf": "valid-csrf"}


class TestLogoutRevokesSession:
    def test_valid_session_and_csrf_revokes_and_clears_cookies(self) -> None:
        svc = MagicMock()
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/logout",
            headers={"x-csrf-token": "valid-csrf"},
            cookies=_auth_cookies(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["trace_id"]
        svc.logout.assert_called_once()

    def test_session_cookie_deleted_on_logout(self) -> None:
        svc = MagicMock()
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/logout",
            headers={"x-csrf-token": "valid-csrf"},
            cookies=_auth_cookies(),
        )
        assert resp.status_code == 200
        # Cookies are cleared via Set-Cookie headers with empty/Max-Age=0 values.
        set_cookie_headers = resp.headers.get("set-cookie", "")
        assert "careerops_session" in set_cookie_headers
        assert "careerops_csrf" in set_cookie_headers


class TestLogoutMissingSession:
    def test_no_session_cookie_is_graceful_noop(self) -> None:
        svc = MagicMock()
        app = _app(svc)
        resp = TestClient(app).post("/api/v1/auth/logout")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        svc.logout.assert_not_called()


class TestLogoutMissingCsrf:
    def test_session_without_csrf_header_returns_403(self) -> None:
        svc = MagicMock()
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/logout",
            cookies=_auth_cookies(),
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["ok"] is False
        svc.logout.assert_not_called()


class TestLogoutInvalidSession:
    def test_already_revoked_session_treated_as_success(self) -> None:
        svc = MagicMock()
        svc.logout.side_effect = InvalidSession("already revoked")
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/logout",
            headers={"x-csrf-token": "valid-csrf"},
            cookies=_auth_cookies(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True


class TestLogoutNoAuthService:
    def test_clears_cookies_even_without_auth_service(self) -> None:
        app = _app(None)
        resp = TestClient(app).post(
            "/api/v1/auth/logout",
            headers={"x-csrf-token": "valid-csrf"},
            cookies=_auth_cookies(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
