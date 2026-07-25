"""Tests for POST /api/v1/auth/bootstrap endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.auth.contracts import (
    AuthRateLimited,
    InvalidBootstrapCredential,
    InvalidSession,
    SessionSecrets,
    SingleUserAlreadyExists,
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


def _body(token: str = "valid-bootstrap-token") -> dict[str, str]:
    return {
        "bootstrap_token": token,
        "username": "owner.user",
        "password": "correct horse battery staple",
    }


class TestBootstrapSuccess:
    def test_valid_token_creates_admin_and_sets_cookies(self) -> None:
        svc = MagicMock()
        svc.complete_bootstrap.return_value = _secrets()
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/bootstrap",
            json=_body(),
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["authenticated"] is True
        assert body["user"]["username"] == "owner.user"
        assert body["csrf_token"] == "csrf-token"
        assert body["expires_at"]
        assert "careerops_session" in resp.cookies
        assert "careerops_csrf" in resp.cookies


class TestBootstrapExpired:
    def test_expired_bootstrap_token_returns_401(self) -> None:
        svc = MagicMock()
        svc.complete_bootstrap.side_effect = InvalidBootstrapCredential("expired")
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/bootstrap",
            json=_body("expired-token"),
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "INVALID_BOOTSTRAP_CREDENTIAL"
        assert body["error"] == "Bootstrap token is invalid or expired"


class TestBootstrapReuse:
    def test_already_consumed_token_returns_401(self) -> None:
        svc = MagicMock()
        svc.complete_bootstrap.side_effect = InvalidBootstrapCredential("already used")
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/bootstrap",
            json=_body("consumed-token"),
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "INVALID_BOOTSTRAP_CREDENTIAL"


class TestBootstrapClosed:
    def test_user_already_exists_returns_409(self) -> None:
        svc = MagicMock()
        svc.complete_bootstrap.side_effect = SingleUserAlreadyExists()
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/bootstrap",
            json=_body(),
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "BOOTSTRAP_CLOSED"
        assert body["error"] == "A user already exists; bootstrap is closed"


class TestBootstrapRateLimiting:
    def test_rate_limited_returns_429(self) -> None:
        svc = MagicMock()
        svc.complete_bootstrap.side_effect = AuthRateLimited("rate limited")
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/bootstrap",
            json=_body(),
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 429
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "RATE_LIMITED"


class TestBootstrapInvalidSession:
    def test_invalid_preauth_session_returns_401(self) -> None:
        svc = MagicMock()
        svc.complete_bootstrap.side_effect = InvalidSession("bad preauth")
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/bootstrap",
            json=_body(),
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "INVALID_BOOTSTRAP_CREDENTIAL"


class TestBootstrapNoAuthService:
    def test_returns_503_when_auth_service_missing(self) -> None:
        app = _app(None)
        resp = TestClient(app).post(
            "/api/v1/auth/bootstrap",
            json=_body(),
            cookies=_preauth_cookies(),
        )
        assert resp.status_code == 503
        body = resp.json()
        assert body["ok"] is False
        assert body["code"] == "DEPENDENCY_NOT_READY"


class TestBootstrapMissingCookies:
    def test_missing_preauth_returns_422(self) -> None:
        svc = MagicMock()
        app = _app(svc)
        resp = TestClient(app).post(
            "/api/v1/auth/bootstrap",
            json=_body(),
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "VALIDATION_ERROR"
        svc.complete_bootstrap.assert_not_called()
