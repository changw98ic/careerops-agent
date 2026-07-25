"""Tests for POST /api/v1/auth/login endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.auth.contracts import (
    AuthRateLimited,
    AuthRequestContext,
    InvalidCredentials,
    InvalidSession,
    SessionSecrets,
)
from careerops.web.api_auth import router


def _secrets() -> SessionSecrets:
    return SessionSecrets(
        session_id=uuid4(),
        token="session-token",
        csrf_token="csrf-token",
        absolute_expires_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _app(auth_service: MagicMock | None) -> FastAPI:
    app = FastAPI()
    app.state.auth_service = auth_service
    app.include_router(router)
    return app


def _preauth_cookies() -> dict[str, str]:
    return {"careerops_session": "preauth-token", "careerops_csrf": "preauth-csrf"}


class TestLoginSuccess:
    def test_valid_credentials_returns_session_cookies(self) -> None:
        svc = MagicMock()
        svc.login.return_value = _secrets()
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/login",
            json={"username": "owner.user", "password": "correct horse battery staple"},
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["authenticated"] is True
        assert body["user"]["username"] == "owner.user"
        assert body["csrf_token"] == "csrf-token"
        assert body["expires_at"]
        assert body["trace_id"]

    def test_sets_httponly_session_cookie(self) -> None:
        svc = MagicMock()
        svc.login.return_value = _secrets()
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/login",
            json={"username": "owner.user", "password": "correct"},
            cookies=_preauth_cookies(),
        )
        assert "careerops_session" in resp.cookies
        assert "careerops_csrf" in resp.cookies


class TestLoginFailure:
    def test_wrong_password_returns_401(self) -> None:
        svc = MagicMock()
        svc.login.side_effect = InvalidCredentials("bad")
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/login",
            json={"username": "owner.user", "password": "wrong"},
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "INVALID_CREDENTIALS"
        assert body["error"] == "Username or password is invalid"

    def test_invalid_preauth_session_returns_401(self) -> None:
        svc = MagicMock()
        svc.login.side_effect = InvalidSession("expired")
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/login",
            json={"username": "owner.user", "password": "correct"},
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "INVALID_CREDENTIALS"

    def test_missing_preauth_cookies_returns_422(self) -> None:
        svc = MagicMock()
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/login",
            json={"username": "owner.user", "password": "correct"},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "VALIDATION_ERROR"
        svc.login.assert_not_called()

    def test_missing_csrf_cookie_returns_422(self) -> None:
        svc = MagicMock()
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/login",
            json={"username": "owner.user", "password": "correct"},
            cookies={"careerops_session": "preauth-token"},
        )
        assert resp.status_code == 422
        svc.login.assert_not_called()


class TestLoginRateLimiting:
    def test_rate_limited_returns_429(self) -> None:
        svc = MagicMock()
        svc.login.side_effect = AuthRateLimited("rate limited")
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/login",
            json={"username": "owner.user", "password": "wrong"},
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 429
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "RATE_LIMITED"
        assert body["error"] == "Too many attempts; try again later"


class TestLoginNoAuthService:
    def test_returns_503_when_auth_service_missing(self) -> None:
        app = _app(None)
        resp = TestClient(app).post(
            "/api/v1/auth/login",
            json={"username": "owner.user", "password": "correct"},
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 503
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "DEPENDENCY_NOT_READY"


class TestLoginDbException:
    def test_unexpected_auth_error_returns_401(self) -> None:
        svc = MagicMock()
        svc.login.side_effect = InvalidSession("db connection lost")
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/login",
            json={"username": "owner.user", "password": "correct"},
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "INVALID_CREDENTIALS"
        assert body["error"] == "Session is invalid or expired"
