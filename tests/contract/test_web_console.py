from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.application.dashboard import (
    DashboardSnapshot,
    DashboardSnapshotProvider,
    DashboardSystemStatus,
)
from careerops.auth.contracts import (
    AuthenticatedPrincipal,
    AuthRateLimited,
    AuthRequestContext,
    SessionSecrets,
)
from careerops.web import ConsoleWebSettings, install_console_web

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


class StubAuthService:
    def __init__(self) -> None:
        self.login_calls = 0
        self.logged_out = False
        self.login_context: AuthRequestContext | None = None

    def begin_preauth(
        self,
        *,
        now: datetime,
        context: AuthRequestContext,
    ) -> SessionSecrets:
        assert now == NOW and context.client_key
        return SessionSecrets(
            session_id=UUID("00000000-0000-0000-0000-000000000010"),
            token="preauth-session",
            csrf_token="preauth-csrf",
            absolute_expires_at=NOW + timedelta(minutes=15),
        )

    def complete_bootstrap(self, **_kwargs: object) -> SessionSecrets:
        return self._authenticated_secrets()

    def login(
        self,
        *,
        preauth_token: str,
        csrf_token: str,
        username: str,
        password: str,
        now: datetime,
        context: AuthRequestContext,
    ) -> SessionSecrets:
        assert (preauth_token, csrf_token) == ("preauth-session", "preauth-csrf")
        assert (username, password) == ("owner", "correct horse battery staple")
        assert now == NOW and context.trace_id
        self.login_context = context
        self.login_calls += 1
        return self._authenticated_secrets()

    def authenticate(self, session_token: str, *, now: datetime) -> AuthenticatedPrincipal:
        if session_token != "authenticated-session" or self.logged_out:
            raise ValueError("invalid session")
        return AuthenticatedPrincipal(
            user_id=UUID("00000000-0000-0000-0000-000000000001"),
            username="owner<script>",
            session_id=UUID("00000000-0000-0000-0000-000000000011"),
            csrf_token_hash="not-exposed",
            absolute_expires_at=NOW + timedelta(days=7),
        )

    def validate_csrf(self, principal: AuthenticatedPrincipal, csrf_token: str) -> None:
        assert principal.session_id
        if csrf_token != "authenticated-csrf":
            raise ValueError("bad csrf")

    def logout(self, **kwargs: object) -> None:
        assert kwargs["session_token"] == "authenticated-session"
        assert kwargs["csrf_token"] == "authenticated-csrf"
        self.logged_out = True

    @staticmethod
    def _authenticated_secrets() -> SessionSecrets:
        return SessionSecrets(
            session_id=UUID("00000000-0000-0000-0000-000000000011"),
            token="authenticated-session",
            csrf_token="authenticated-csrf",
            absolute_expires_at=NOW + timedelta(days=7),
        )


class StubDashboardProvider:
    async def snapshot(self) -> DashboardSnapshot:
        return DashboardSnapshot(
            system_status=DashboardSystemStatus.READY,
            dependency_checks=(
                ("database", "ok"),
                ("redis", "ok"),
                ("temporal", "ok"),
                ("storage", "ok"),
            ),
            integration_checks=(
                ("model_provider", "disabled"),
                ("google_oauth", "disabled"),
                ("external_writes", "disabled"),
            ),
            pending_approvals=3,
            pending_outbox_events=4,
        )


class PreauthLimitedStubAuthService(StubAuthService):
    def begin_preauth(
        self,
        *,
        now: datetime,
        context: AuthRequestContext,
    ) -> SessionSecrets:
        raise AuthRateLimited("bounded preauth creation")


class UnavailableCountsDashboardProvider:
    async def snapshot(self) -> DashboardSnapshot:
        return DashboardSnapshot(
            system_status=DashboardSystemStatus.READY,
            dependency_checks=(("database", "ok"),),
            integration_checks=(("external_writes", "disabled"),),
            pending_approvals=None,
            pending_outbox_events=None,
        )


def make_client(
    service: StubAuthService,
    dashboard_provider: DashboardSnapshotProvider | None = None,
) -> TestClient:
    app = FastAPI()

    @app.get("/api/v1/health/live")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    install_console_web(
        app,
        service,
        dashboard_provider or StubDashboardProvider(),
        ConsoleWebSettings(
            allowed_hosts=frozenset({"testserver"}),
            allowed_origins=frozenset({"http://testserver"}),
        ),
        now_provider=lambda: NOW,
    )
    return TestClient(app, follow_redirects=False)


def test_health_stays_anonymous_and_protected_web_redirects() -> None:
    client = make_client(StubAuthService())

    health = client.get("/api/v1/health/live")
    protected = client.get("/")

    assert health.status_code == 200
    assert protected.status_code == 303
    assert protected.headers["location"] == "/login"
    assert "default-src 'none'" in health.headers["content-security-policy"]


def test_login_form_binds_preauth_session_and_csrf_in_httponly_cookies() -> None:
    client = make_client(StubAuthService())

    response = client.get("/login")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'name="csrf_token" value="preauth-csrf"' in response.text
    cookies = "; ".join(response.headers.get_list("set-cookie")).lower()
    assert "careerops_session=preauth-session" in cookies
    assert "careerops_csrf=preauth-csrf" in cookies
    assert "httponly" in cookies and "samesite=strict" in cookies


def test_login_form_fails_closed_when_preauth_creation_is_rate_limited() -> None:
    response = make_client(PreauthLimitedStubAuthService()).get("/login")

    assert response.status_code == 429
    assert "Try again later" in response.text
    assert "careerops_session" not in response.headers.get("set-cookie", "")


def test_login_rejects_wrong_origin_before_authentication() -> None:
    service = StubAuthService()
    client = make_client(service)
    client.get("/login")

    response = client.post(
        "/login",
        headers={"Origin": "https://evil.example"},
        data={
            "username": "owner",
            "password": "correct horse battery staple",
            "csrf_token": "preauth-csrf",
        },
    )

    assert response.status_code == 400
    assert service.login_calls == 0


def test_login_dashboard_escaping_and_logout_flow() -> None:
    service = StubAuthService()
    client = make_client(service)
    client.get("/login")
    login = client.post(
        "/login",
        headers={"Origin": "http://testserver"},
        data={
            "username": "owner",
            "password": "correct horse battery staple",
            "csrf_token": "preauth-csrf",
        },
    )

    assert login.status_code == 303
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "owner&lt;script&gt;" in dashboard.text
    assert not re.search(r"owner<script>", dashboard.text)
    assert "系统健康" in dashboard.text
    assert "database</dt><dd>ok" in dashboard.text
    assert "集成状态" in dashboard.text
    assert "待审批</dt>" in dashboard.text and ">3</dd>" in dashboard.text
    assert "待发布内部事件</dt>" in dashboard.text and ">4</dd>" in dashboard.text

    logout = client.post(
        "/logout",
        headers={"Origin": "http://testserver"},
        data={"csrf_token": "authenticated-csrf"},
    )
    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"


def test_dashboard_renders_unavailable_counts_without_leaking_database_errors() -> None:
    service = StubAuthService()
    client = make_client(service, UnavailableCountsDashboardProvider())
    client.get("/login")
    login = client.post(
        "/login",
        headers={"Origin": "http://testserver"},
        data={
            "username": "owner",
            "password": "correct horse battery staple",
            "csrf_token": "preauth-csrf",
        },
    )

    assert login.status_code == 303
    dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert dashboard.headers["cache-control"] == "no-store"
    assert "不可用" in dashboard.text
    assert "sensitive database failure" not in dashboard.text
    assert "Traceback" not in dashboard.text


def test_login_rate_limit_subject_uses_trusted_client_host_not_spoofable_headers() -> None:
    service = StubAuthService()
    client = make_client(service)
    client.get("/login")

    response = client.post(
        "/login",
        headers={
            "Origin": "http://testserver",
            "User-Agent": "Mozilla attacker-variant",
            "X-Forwarded-For": "203.0.113.66",
        },
        data={
            "username": "owner",
            "password": "correct horse battery staple",
            "csrf_token": "preauth-csrf",
        },
    )

    assert response.status_code == 303
    assert service.login_context is not None
    assert "\0" not in service.login_context.client_key
    assert "Mozilla" not in service.login_context.client_key
    assert "203.0.113.66" not in service.login_context.client_key
