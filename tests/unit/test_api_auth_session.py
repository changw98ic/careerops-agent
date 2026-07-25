"""Tests for GET /api/v1/auth/session endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.auth.contracts import AuthenticatedPrincipal, InvalidSession
from careerops.web.api_auth import router


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        user_id=uuid4(),
        username="testuser",
        session_id=uuid4(),
        csrf_token_hash="hash",
        absolute_expires_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _app(auth_service: MagicMock | None) -> FastAPI:
    app = FastAPI()
    app.state.auth_service = auth_service
    app.include_router(router)
    return app


class TestSessionEndpoint:
    def test_no_auth_service_returns_unauthenticated(self) -> None:
        app = _app(None)
        resp = TestClient(app).get("/api/v1/auth/session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is False
        assert body["user"] is None
        assert body["trace_id"]

    def test_no_session_cookie_returns_unauthenticated(self) -> None:
        svc = MagicMock()
        app = _app(svc)
        resp = TestClient(app).get("/api/v1/auth/session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is False
        svc.authenticate.assert_not_called()

    def test_invalid_session_returns_unauthenticated(self) -> None:
        svc = MagicMock()
        svc.authenticate.side_effect = InvalidSession("bad")
        app = _app(svc)
        resp = TestClient(app).get(
            "/api/v1/auth/session",
            cookies={"careerops_session": "bad-token"},
        )
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is False

    def test_valid_session_returns_user_and_csrf(self) -> None:
        svc = MagicMock()
        principal = _principal()
        svc.authenticate.return_value = principal
        app = _app(svc)
        resp = TestClient(app).get(
            "/api/v1/auth/session",
            cookies={
                "careerops_session": "valid-token",
                "careerops_csrf": "csrf-value",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is True
        assert body["user"]["username"] == "testuser"
        assert body["csrf_token"] == "csrf-value"
        assert body["expires_at"] == principal.absolute_expires_at.isoformat()
        assert body["trace_id"]

    def test_valid_session_without_csrf_cookie(self) -> None:
        svc = MagicMock()
        svc.authenticate.return_value = _principal()
        app = _app(svc)
        resp = TestClient(app).get(
            "/api/v1/auth/session",
            cookies={"careerops_session": "valid-token"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is True
        assert body["csrf_token"] == ""
