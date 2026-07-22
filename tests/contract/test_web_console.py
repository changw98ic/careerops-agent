from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from careerops.application.autopilot_control import (
    AutopilotCommandResult,
    AutopilotCommandState,
    AutopilotControlCapability,
    AutopilotControlCapabilityState,
    CampaignGrantSnapshot,
    CampaignGrantStatus,
    ReviewQueueCount,
    ReviewQueueSnapshot,
    ReviewQueueTab,
    ReviewResolutionMode,
)
from careerops.application.crawler_execution import CrawlerExecutionRequestSummary
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
from careerops.web import (
    AutopilotGrantSummary,
    ConsoleWebSettings,
    CrawlerExecutionConsoleProvider,
    ReviewQueueItem,
    install_console_web,
)
from careerops.web.crawler_execution import (
    CrawlerExecutionConsoleCapability,
    CrawlerExecutionConsoleCapabilityState,
    CrawlerExecutionConsoleCommandResult,
    CrawlerExecutionReviewSnapshot,
)

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


class StubControlPlaneProvider:
    def __init__(self) -> None:
        self.last_items_returned = 0

    async def campaign_grants(
        self,
        *,
        actor_id: UUID,
        now: datetime,
    ) -> CampaignGrantSnapshot:
        assert actor_id == UUID("00000000-0000-0000-0000-000000000001")
        assert now == NOW
        return CampaignGrantSnapshot(
            capability=_capability(),
            generated_at=now,
            grants=(
                AutopilotGrantSummary(
                    grant_id=UUID("00000000-0000-0000-0000-000000000101"),
                    campaign_id=UUID("00000000-0000-0000-0000-000000000201"),
                    version=1,
                    status=CampaignGrantStatus.ACTIVE,
                    title_zh="平台工程<script>",
                    scope_summary_zh="平台工程岗位",
                    channels=("greenhouse", "lever<script>"),
                    action_kinds=("prepare_application", "submit_application"),
                    target_hosts=("greenhouse.io",),
                    approved_material_hashes=("a" * 64,),
                    max_submissions=3,
                    remaining_submissions=2,
                    expires_at=NOW + timedelta(days=3),
                    created_at=NOW,
                ),
            ),
        )

    async def review_queue(
        self,
        *,
        actor_id: UUID,
        tab: ReviewQueueTab,
        now: datetime,
    ) -> ReviewQueueSnapshot:
        assert actor_id == UUID("00000000-0000-0000-0000-000000000001")
        assert now == NOW
        all_items = (
            ReviewQueueItem(
                item_id=UUID("00000000-0000-0000-0000-000000000301"),
                tab=ReviewQueueTab.EXCEPTIONS,
                title_zh="站点禁止自动化<script>",
                summary_zh="Example Corp<script>",
                reason_code="SITE_POLICY_PROHIBITED",
                resolution_mode=ReviewResolutionMode.MANUAL_ONLY,
                created_at=NOW,
            ),
            ReviewQueueItem(
                item_id=UUID("00000000-0000-0000-0000-000000000302"),
                tab=ReviewQueueTab.RECOVERY,
                title_zh="材料证据缺失",
                summary_zh="Another Corp",
                reason_code="MATERIAL_EVIDENCE_MISSING",
                resolution_mode=ReviewResolutionMode.REMEDIATION_REQUIRED,
                target_host="another-corp",
                created_at=NOW + timedelta(minutes=1),
            ),
            ReviewQueueItem(
                item_id=UUID("00000000-0000-0000-0000-000000000303"),
                tab=ReviewQueueTab.PENDING,
                title_zh="确认候选职位",
                summary_zh="Pending Corp",
                reason_code="AMBIGUOUS_MATCH",
                resolution_mode=ReviewResolutionMode.AGENT_APPROVABLE,
                target_host="pending-corp",
                created_at=NOW + timedelta(minutes=2),
            ),
        )
        self.last_items_returned = len(all_items)
        return ReviewQueueSnapshot(
            capability=_capability(),
            generated_at=now,
            active_tab=tab,
            counts=tuple(
                ReviewQueueCount(
                    tab=queue_tab,
                    count=sum(1 for item in all_items if item.tab is queue_tab),
                )
                for queue_tab in ReviewQueueTab
            ),
            items=tuple(item for item in all_items if item.tab is tab),
        )

    async def request_grant_activation(
        self,
        *,
        actor_id: UUID,
        grant_id: UUID,
    ) -> AutopilotCommandResult:
        del actor_id, grant_id
        return _command_result()

    async def request_grant_revocation(
        self,
        *,
        actor_id: UUID,
        grant_id: UUID,
    ) -> AutopilotCommandResult:
        del actor_id, grant_id
        return _command_result()

    async def request_review_resolution(
        self,
        *,
        actor_id: UUID,
        review_item_id: UUID,
        mode: ReviewResolutionMode,
    ) -> AutopilotCommandResult:
        del actor_id, review_item_id, mode
        return _command_result()


class StubCrawlerExecutionProvider:
    def __init__(self, *, capability_state: CrawlerExecutionConsoleCapabilityState) -> None:
        self.capability_state = capability_state
        self.pending_actor_ids: list[UUID] = []
        self.approvals: list[tuple[UUID, UUID]] = []
        self.rejections: list[tuple[UUID, UUID, str]] = []

    async def pending_requests(
        self,
        *,
        actor_id: UUID,
        now: datetime,
        limit: int = 50,
    ) -> CrawlerExecutionReviewSnapshot:
        assert now == NOW
        assert limit == 50
        self.pending_actor_ids.append(actor_id)
        return CrawlerExecutionReviewSnapshot(
            capability=CrawlerExecutionConsoleCapability(
                state=self.capability_state,
                title_zh="Crawler 审核已连接<script>",
                description_zh="只允许已认证 owner 审核",
                reason_code="TEST_CRAWLER_CONNECTED",
            ),
            generated_at=now,
            requests=(
                CrawlerExecutionRequestSummary(
                    request_id=UUID("00000000-0000-0000-0000-000000000901"),
                    owner_user_id=UUID("00000000-0000-0000-0000-000000000001"),
                    manifest_path="datasets/manifests/recruitment-crawler-sources.example.json",
                    request_artifact_path=(
                        "datasets/private/crawler-execution-reviews/request.json"
                    ),
                    manifest_sha256="a" * 64,
                    request_sha256="b" * 64,
                    reviewed_plan_sha256="c" * 64,
                    source_ids=("lever<script>", "greenhouse"),
                    reason="reviewed crawler plan",
                    created_at=NOW,
                    expires_at=NOW + timedelta(hours=2),
                ),
            ),
        )

    async def approve_request(
        self,
        *,
        actor_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> CrawlerExecutionConsoleCommandResult:
        assert now == NOW
        self.approvals.append((actor_id, request_id))
        return CrawlerExecutionConsoleCommandResult(
            accepted=True,
            reason_code="CRAWLER_APPROVED",
            title_zh="已批准",
            description_zh="已写入内部派发队列",
        )

    async def reject_request(
        self,
        *,
        actor_id: UUID,
        request_id: UUID,
        reason: str,
        now: datetime,
    ) -> CrawlerExecutionConsoleCommandResult:
        assert now == NOW
        self.rejections.append((actor_id, request_id, reason))
        return CrawlerExecutionConsoleCommandResult(
            accepted=True,
            reason_code="CRAWLER_REJECTED",
            title_zh="已拒绝",
            description_zh="已记录拒绝",
        )


class StubGoalRunProvider:
    def __init__(self) -> None:
        self.list_actor_ids: list[UUID] = []
        self.submissions: list[tuple[UUID, UUID, UUID, str, Literal["approve", "reject"]]] = []

    async def list_goal_runs(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> object:
        assert limit == 50
        self.list_actor_ids.append(actor_id)
        return SimpleNamespace(
            goal_runs=(
                SimpleNamespace(
                    goal_run_id=UUID("00000000-0000-0000-0000-000000000a01"),
                    owner_user_id=actor_id,
                    registry_id=UUID("00000000-0000-0000-0000-000000000a02"),
                    source_id="public-ats<script>",
                    status="waiting_review",
                    phase="review",
                    status_message="等待人工确认<script>",
                    pending_review=SimpleNamespace(
                        review_id=UUID("00000000-0000-0000-0000-000000000a03"),
                        kind="gmail_send",
                        snapshot_sha256="d" * 64,
                        payload={
                            "company": "Example<script>",
                            "job_title": "Agent Engineer",
                            "recipient": "recruiting@example.com",
                            "credential_handle": "must-not-render",
                            "nested": {"oauth_token": "also-hidden", "body": "您好"},
                        },
                    ),
                ),
            ),
        )

    async def submit_review(
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
        review_id: UUID,
        command_id: UUID,
        snapshot_sha256: str,
        decision: Literal["approve", "reject"],
        now: datetime,
    ) -> object:
        assert now == NOW
        assert command_id
        self.submissions.append((actor_id, goal_run_id, review_id, snapshot_sha256, decision))
        return SimpleNamespace(status="submitted")


def _capability() -> AutopilotControlCapability:
    return AutopilotControlCapability(
        state=AutopilotControlCapabilityState.READ_ONLY,
        title_zh="高自治控制面只读",
        description_zh="测试只读投影",
        reason_code="TEST_READ_ONLY",
    )


def _command_result() -> AutopilotCommandResult:
    return AutopilotCommandResult(
        state=AutopilotCommandState.REJECTED,
        reason_code="TEST_READ_ONLY",
        title_zh="操作未启用",
        description_zh="测试只读投影",
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
    control_plane_provider: StubControlPlaneProvider | None = None,
    crawler_execution_provider: CrawlerExecutionConsoleProvider | None = None,
    goal_run_provider: object | None = None,
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
        control_plane_provider=control_plane_provider,
        crawler_execution_provider=crawler_execution_provider,
        goal_run_provider=cast(Any, goal_run_provider),
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
    assert "<title>登录 · CareerOps</title>" in response.text
    assert 'name="csrf_token" value="preauth-csrf"' in response.text
    cookies = "; ".join(response.headers.get_list("set-cookie")).lower()
    assert "careerops_session=preauth-session" in cookies
    assert "careerops_csrf=preauth-csrf" in cookies
    assert "httponly" in cookies and "samesite=strict" in cookies
    assert response.headers["referrer-policy"] == "same-origin"


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


def test_review_and_grant_pages_redirect_to_login_when_unauthenticated() -> None:
    client = make_client(StubAuthService())

    review = client.get("/review")
    grants = client.get("/grants/autopilot")
    crawlers = client.get("/crawlers/review")
    goal_runs = client.get("/goal-runs")

    assert review.status_code == 303
    assert review.headers["location"] == "/login"
    assert review.headers["cache-control"] == "no-store"
    assert grants.status_code == 303
    assert grants.headers["location"] == "/login"
    assert grants.headers["cache-control"] == "no-store"
    assert crawlers.status_code == 303
    assert crawlers.headers["location"] == "/login"
    assert crawlers.headers["cache-control"] == "no-store"
    assert goal_runs.status_code == 303
    assert goal_runs.headers["location"] == "/login"
    assert goal_runs.headers["cache-control"] == "no-store"


def test_default_review_and_grant_pages_are_read_only_and_empty() -> None:
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
    review = client.get("/review")
    grants = client.get("/grants/autopilot")

    assert review.status_code == 200
    assert review.headers["cache-control"] == "no-store"
    assert "审核队列" in review.text
    assert "待确认" in review.text
    assert "异常" in review.text
    assert "恢复" in review.text
    assert 'href="/review?tab=pending" aria-current="page"' in review.text
    assert "当前没有待确认事项" in review.text
    assert "不会批准、提交或触发任何外部写入" in review.text
    assert "高自治求职未启用" in review.text
    assert "AUTOPILOT_CONTROL_PLANE_DISABLED" in review.text
    assert "<form" not in review.text

    assert grants.status_code == 200
    assert grants.headers["cache-control"] == "no-store"
    assert "Autopilot 授权" in grants.text
    assert "当前没有活跃 Autopilot 授权" in grants.text
    assert "不会绕过站点规则" in grants.text
    assert "高自治求职未启用" in grants.text
    assert "<form" not in grants.text

    crawlers = client.get("/crawlers/review")
    assert crawlers.status_code == 200
    assert crawlers.headers["cache-control"] == "no-store"
    assert "Crawler 执行审核未启用" in crawlers.text
    assert "CRAWLER_EXECUTION_CONSOLE_DISABLED" in crawlers.text
    assert "当前没有待审核 crawler request" in crawlers.text
    assert "<form" not in crawlers.text

    goal_runs = client.get("/goal-runs")
    assert goal_runs.status_code == 200
    assert goal_runs.headers["cache-control"] == "no-store"
    assert "GoalRun 审核未启用" in goal_runs.text
    assert "当前没有 GoalRun 审核事项" in goal_runs.text
    assert "<form" not in goal_runs.text


def test_goal_run_review_page_is_owner_scoped_chinese_first_and_hides_secrets() -> None:
    service = StubAuthService()
    goal_runs = StubGoalRunProvider()
    client = make_client(service, goal_run_provider=goal_runs)
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
    response = client.get("/goal-runs")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert goal_runs.list_actor_ids == [UUID("00000000-0000-0000-0000-000000000001")]
    assert "GoalRun 审核" in response.text
    assert "批准" in response.text
    assert "拒绝" in response.text
    assert "public-ats&lt;script&gt;" in response.text
    assert "等待人工确认&lt;script&gt;" in response.text
    assert "Example&lt;script&gt;" in response.text
    assert "Agent Engineer" in response.text
    assert "recruiting@example.com" in response.text
    assert 'name="csrf_token" value="authenticated-csrf"' in response.text
    assert 'name="snapshot_sha256" value="' + ("d" * 64) + '"' in response.text
    assert "/goal-runs/00000000-0000-0000-0000-000000000a01/reviews/" in response.text
    assert 'name="credential_handle"' not in response.text
    assert "must-not-render" not in response.text
    assert "also-hidden" not in response.text
    assert not re.search(r"<script>", response.text)


def test_goal_run_approval_and_rejection_require_origin_csrf_and_snapshot_binding() -> None:
    service = StubAuthService()
    goal_runs = StubGoalRunProvider()
    client = make_client(service, goal_run_provider=goal_runs)
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
    goal_run_id = "00000000-0000-0000-0000-000000000a01"
    review_id = "00000000-0000-0000-0000-000000000a03"
    path = f"/goal-runs/{goal_run_id}/reviews/{review_id}"

    wrong_origin = client.post(
        f"{path}/approve",
        headers={"Origin": "https://evil.example"},
        data={"csrf_token": "authenticated-csrf", "snapshot_sha256": "d" * 64},
    )
    wrong_csrf = client.post(
        f"{path}/approve",
        headers={"Origin": "http://testserver"},
        data={"csrf_token": "wrong-csrf", "snapshot_sha256": "d" * 64},
    )
    approve = client.post(
        f"{path}/approve",
        headers={"Origin": "http://testserver"},
        data={"csrf_token": "authenticated-csrf", "snapshot_sha256": "d" * 64},
    )
    reject = client.post(
        f"{path}/reject",
        headers={"Origin": "http://testserver"},
        data={"csrf_token": "authenticated-csrf", "snapshot_sha256": "d" * 64},
    )

    actor_id = UUID("00000000-0000-0000-0000-000000000001")
    parsed_goal_run_id = UUID(goal_run_id)
    parsed_review_id = UUID(review_id)
    assert wrong_origin.status_code == 400
    assert wrong_csrf.status_code == 400
    assert approve.status_code == 303
    assert approve.headers["location"] == "/goal-runs?status=approved"
    assert reject.status_code == 303
    assert reject.headers["location"] == "/goal-runs?status=rejected"
    assert goal_runs.submissions == [
        (actor_id, parsed_goal_run_id, parsed_review_id, "d" * 64, "approve"),
        (actor_id, parsed_goal_run_id, parsed_review_id, "d" * 64, "reject"),
    ]


def test_crawler_review_page_escapes_data_and_keeps_inputs_bounded_to_decisions() -> None:
    service = StubAuthService()
    crawler_provider = StubCrawlerExecutionProvider(
        capability_state=CrawlerExecutionConsoleCapabilityState.REVIEWABLE
    )
    client = make_client(service, crawler_execution_provider=crawler_provider)
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
    response = client.get("/crawlers/review")

    assert response.status_code == 200
    assert crawler_provider.pending_actor_ids == [UUID("00000000-0000-0000-0000-000000000001")]
    assert "Crawler 审核已连接&lt;script&gt;" in response.text
    assert "lever&lt;script&gt;" in response.text
    assert not re.search(r"<script>", response.text)
    assert "/crawlers/review/00000000-0000-0000-0000-000000000901/approve" in response.text
    assert "/crawlers/review/00000000-0000-0000-0000-000000000901/reject" in response.text
    assert 'name="csrf_token" value="authenticated-csrf"' in response.text
    assert 'name="reason"' in response.text
    assert 'name="url"' not in response.text
    assert 'name="script"' not in response.text
    assert 'name="proxy"' not in response.text
    assert 'name="headers"' not in response.text
    assert 'name="manifest_path"' not in response.text


def test_crawler_approval_and_rejection_require_origin_csrf_and_authenticated_actor() -> None:
    service = StubAuthService()
    crawler_provider = StubCrawlerExecutionProvider(
        capability_state=CrawlerExecutionConsoleCapabilityState.REVIEWABLE
    )
    client = make_client(service, crawler_execution_provider=crawler_provider)
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
    request_id = "00000000-0000-0000-0000-000000000901"

    wrong_origin = client.post(
        f"/crawlers/review/{request_id}/approve",
        headers={"Origin": "https://evil.example"},
        data={"csrf_token": "authenticated-csrf"},
    )
    wrong_csrf = client.post(
        f"/crawlers/review/{request_id}/approve",
        headers={"Origin": "http://testserver"},
        data={"csrf_token": "wrong-csrf"},
    )
    approve = client.post(
        f"/crawlers/review/{request_id}/approve",
        headers={"Origin": "http://testserver"},
        data={"csrf_token": "authenticated-csrf"},
    )
    reject = client.post(
        f"/crawlers/review/{request_id}/reject",
        headers={"Origin": "http://testserver"},
        data={"csrf_token": "authenticated-csrf", "reason": "source stale"},
    )

    actor_id = UUID("00000000-0000-0000-0000-000000000001")
    parsed_request_id = UUID(request_id)
    assert wrong_origin.status_code == 400
    assert wrong_csrf.status_code == 400
    assert approve.status_code == 303
    assert approve.headers["location"] == "/crawlers/review?status=approved"
    assert reject.status_code == 303
    assert reject.headers["location"] == "/crawlers/review?status=rejected"
    assert crawler_provider.approvals == [(actor_id, parsed_request_id)]
    assert crawler_provider.rejections == [(actor_id, parsed_request_id, "source stale")]


def test_review_and_grant_pages_escape_projection_data_and_label_manual_only() -> None:
    service = StubAuthService()
    control_plane = StubControlPlaneProvider()
    client = make_client(service, control_plane_provider=control_plane)
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
    review = client.get("/review?tab=exceptions")
    grants = client.get("/grants/autopilot")

    assert review.status_code == 200
    assert control_plane.last_items_returned == 3
    assert 'href="/review?tab=exceptions" aria-current="page"' in review.text
    assert "站点禁止自动化&lt;script&gt;" in review.text
    assert "Example Corp&lt;script&gt;" in review.text
    assert "材料证据缺失" not in review.text
    assert "确认候选职位" not in review.text
    assert not re.search(r"<script>", review.text)
    assert "仅人工处理" in review.text
    assert "SITE_POLICY_PROHIBITED" in review.text
    assert "\u5f02\u5e38\uff081\uff09" in review.text
    assert "高自治控制面只读" in review.text
    assert "<form" not in review.text

    assert grants.status_code == 200
    assert "平台工程&lt;script&gt;" in grants.text
    assert "lever&lt;script&gt;" in grants.text
    assert not re.search(r"<script>", grants.text)
    assert "剩余额度</dt><dd>2</dd>" in grants.text
    assert "高自治控制面只读" in grants.text
    assert "<form" not in grants.text


def test_review_queue_tabs_filter_each_bounded_queue() -> None:
    service = StubAuthService()
    client = make_client(service, control_plane_provider=StubControlPlaneProvider())
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
    expected: dict[ReviewQueueTab, tuple[str, tuple[str, ...]]] = {
        ReviewQueueTab.PENDING: (
            "待确认",
            ("确认候选职位", "材料证据缺失", "站点禁止自动化"),
        ),
        ReviewQueueTab.EXCEPTIONS: (
            "异常",
            ("站点禁止自动化&lt;script&gt;", "确认候选职位", "材料证据缺失"),
        ),
        ReviewQueueTab.RECOVERY: (
            "恢复",
            ("材料证据缺失", "确认候选职位", "站点禁止自动化"),
        ),
    }
    for tab, (label, visible_or_hidden) in expected.items():
        response = client.get(f"/review?tab={tab}")
        visible = visible_or_hidden[0]
        hidden = visible_or_hidden[1:]

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert f'href="/review?tab={tab}" aria-current="page"' in response.text
        assert f"<h2>{label}</h2>" in response.text
        assert visible in response.text
        for value in hidden:
            assert value not in response.text
        assert "<form" not in response.text


def test_review_queue_rejects_unbounded_tab_without_openapi_exposure() -> None:
    service = StubAuthService()
    client = make_client(service, control_plane_provider=StubControlPlaneProvider())
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
    invalid = client.get("/review?tab=submit_now")
    openapi = client.get("/openapi.json")

    assert invalid.status_code == 400
    assert invalid.headers["cache-control"] == "no-store"
    assert "Invalid review queue tab" in invalid.text
    assert openapi.status_code == 200
    assert "/review" not in openapi.text
    assert "/grants/autopilot" not in openapi.text


def test_web_projection_rejects_manual_only_pending_item() -> None:
    with pytest.raises(ValueError, match="manual-only"):
        ReviewQueueItem(
            item_id=UUID("00000000-0000-0000-0000-000000000399"),
            tab=ReviewQueueTab.PENDING,
            title_zh="站点禁止自动化",
            summary_zh="Example Corp",
            reason_code="SITE_POLICY_PROHIBITED",
            resolution_mode=ReviewResolutionMode.MANUAL_ONLY,
            created_at=NOW,
        )
