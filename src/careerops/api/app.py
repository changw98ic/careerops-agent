from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from temporalio.client import Client

from careerops import __version__
from careerops.api.errors import install_error_handlers
from careerops.api.gmail_readonly import (
    DisabledGmailReadonlyOperatorProvider,
    GmailReadonlyOperatorProvider,
    GmailReadonlyUnavailable,
    install_gmail_readonly_operator_api,
)
from careerops.api.gmail_send import (
    DisabledGmailSendOperatorProvider,
    GmailSendOperatorProvider,
    GmailSendUnavailable,
    install_gmail_send_operator_api,
)
from careerops.api.goal_runs import (
    DisabledGoalRunOperatorProvider,
    GoalRunOperatorProvider,
    install_goal_run_operator_api,
)
from careerops.api.greenhouse_submit import (
    DisabledGreenhouseSubmitOperatorProvider,
    GreenhouseSubmitOperatorProvider,
    install_greenhouse_submit_operator_api,
)
from careerops.api.metrics_middleware import MetricsMiddleware
from careerops.api.middleware import RequestIdMiddleware
from careerops.api.routes.health import router as health_router
from careerops.api.routes.metrics import router as metrics_router
from careerops.api.source_registry import (
    RuntimeSourceRegistryOperatorProvider,
    SourceRegistryOperatorProvider,
    install_source_registry_operator_api,
)
from careerops.application.autopilot_control import AutopilotControlPlane
from careerops.application.dashboard import DashboardSnapshotProvider
from careerops.application.ports.readiness import ReadinessProbe
from careerops.auth.service import ConsoleAuthService
from careerops.config import RuntimeEnvironment, Settings, get_settings
from careerops.infrastructure.auth import create_console_auth_service
from careerops.infrastructure.dashboard import RuntimeDashboardSnapshotProvider
from careerops.infrastructure.database.autopilot_control import RuntimeAutopilotControlPlane
from careerops.infrastructure.database.crawler_execution_console import (
    RuntimeCrawlerExecutionConsoleProvider,
)
from careerops.infrastructure.database.goal_run import PostgresGoalRunRepository
from careerops.infrastructure.gmail_operator import RuntimeGmailReadonlyOperatorProvider
from careerops.infrastructure.gmail_send_operator import RuntimeGmailSendOperatorProvider
from careerops.infrastructure.goal_run_operator import RuntimeGoalRunOperatorProvider
from careerops.infrastructure.greenhouse_submit_operator import (
    RuntimeGreenhouseSubmitOperatorProvider,
)
from careerops.infrastructure.redis import RedisAuthRateLimiter
from careerops.infrastructure.runtime import RuntimeResources, create_runtime_resources
from careerops.observability import Metrics
from careerops.web import ConsoleWebSettings, CrawlerExecutionConsoleProvider, install_console_web


def create_app(
    settings: Settings | None = None,
    *,
    readiness_probe: ReadinessProbe | None = None,
    console_auth_service: ConsoleAuthService | None = None,
    dashboard_provider: DashboardSnapshotProvider | None = None,
    control_plane_provider: AutopilotControlPlane | None = None,
    crawler_execution_provider: CrawlerExecutionConsoleProvider | None = None,
    source_registry_provider: SourceRegistryOperatorProvider | None = None,
    goal_run_provider: GoalRunOperatorProvider | None = None,
    gmail_readonly_provider: GmailReadonlyOperatorProvider | None = None,
    gmail_send_provider: GmailSendOperatorProvider | None = None,
    greenhouse_submit_provider: GreenhouseSubmitOperatorProvider | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    probe = readiness_probe or create_runtime_resources(resolved)
    auth_service = console_auth_service
    if auth_service is None and isinstance(probe, RuntimeResources):
        auth_service = create_console_auth_service(
            probe.database,
            rate_limiter=RedisAuthRateLimiter(probe.redis_sync),
        )
    resolved_dashboard_provider = dashboard_provider
    if resolved_dashboard_provider is None and isinstance(probe, RuntimeResources):
        resolved_dashboard_provider = RuntimeDashboardSnapshotProvider(
            probe,
            probe.database,
            resolved,
        )
    resolved_control_plane_provider = control_plane_provider
    if resolved_control_plane_provider is None and isinstance(probe, RuntimeResources):
        resolved_control_plane_provider = RuntimeAutopilotControlPlane(probe.database)
    resolved_crawler_execution_provider = crawler_execution_provider
    if resolved_crawler_execution_provider is None and isinstance(probe, RuntimeResources):
        resolved_crawler_execution_provider = RuntimeCrawlerExecutionConsoleProvider(
            probe.database,
            workspace_root=resolved.crawler_workspace_root,
        )
    resolved_source_registry_provider = source_registry_provider
    if (
        resolved_source_registry_provider is None
        and auth_service is not None
        and isinstance(probe, RuntimeResources)
    ):
        resolved_source_registry_provider = RuntimeSourceRegistryOperatorProvider(
            probe.database,
            resolved,
        )
    resolved_goal_run_provider = goal_run_provider
    if resolved_goal_run_provider is None and auth_service is not None:
        if isinstance(probe, RuntimeResources):
            resolved_goal_run_provider = RuntimeGoalRunOperatorProvider(
                settings=resolved,
                transaction_factory=probe.database.begin,
                repository_factory=lambda connection: PostgresGoalRunRepository(connection),
                temporal_client_factory=lambda: Client.connect(
                    resolved.temporal_address,
                    namespace=resolved.temporal_namespace,
                    lazy=True,
                ),
            )
        else:
            resolved_goal_run_provider = DisabledGoalRunOperatorProvider()
    resolved_gmail_readonly_provider = gmail_readonly_provider
    if resolved_gmail_readonly_provider is None and auth_service is not None:
        if isinstance(probe, RuntimeResources):
            resolved_gmail_readonly_provider = _ControlPlaneGatedGmailReadonlyOperatorProvider(
                RuntimeGmailReadonlyOperatorProvider(
                    transaction_factory=probe.database.begin,
                ),
                execution_enabled=resolved.google_oauth_enabled,
            )
        else:
            resolved_gmail_readonly_provider = DisabledGmailReadonlyOperatorProvider()
    resolved_gmail_send_provider = gmail_send_provider
    if resolved_gmail_send_provider is None and auth_service is not None:
        if isinstance(probe, RuntimeResources):
            resolved_gmail_send_provider = _ControlPlaneGatedGmailSendOperatorProvider(
                RuntimeGmailSendOperatorProvider(
                    transaction_factory=probe.database.begin,
                ),
                execution_enabled=resolved.gmail_send_runtime_enabled,
            )
        else:
            resolved_gmail_send_provider = DisabledGmailSendOperatorProvider()
    resolved_greenhouse_submit_provider = greenhouse_submit_provider
    if resolved_greenhouse_submit_provider is None and auth_service is not None:
        if resolved.greenhouse_submit_runtime_enabled and isinstance(probe, RuntimeResources):
            resolved_greenhouse_submit_provider = RuntimeGreenhouseSubmitOperatorProvider(
                transaction_factory=probe.database.begin,
            )
        else:
            resolved_greenhouse_submit_provider = DisabledGreenhouseSubmitOperatorProvider()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            close_goal_run_provider = getattr(resolved_goal_run_provider, "close", None)
            if close_goal_run_provider is not None:
                await close_goal_run_provider()
            close_gmail_readonly_provider = getattr(
                resolved_gmail_readonly_provider,
                "close",
                None,
            )
            if close_gmail_readonly_provider is not None:
                await close_gmail_readonly_provider()
            close_gmail_send_provider = getattr(
                resolved_gmail_send_provider,
                "close",
                None,
            )
            if close_gmail_send_provider is not None:
                await close_gmail_send_provider()
            close_greenhouse_submit_provider = getattr(
                resolved_greenhouse_submit_provider,
                "close",
                None,
            )
            if close_greenhouse_submit_provider is not None:
                await close_greenhouse_submit_provider()
            await probe.close()

    docs_url = None if resolved.environment is RuntimeEnvironment.PRODUCTION else "/docs"
    app = FastAPI(
        title="CareerOps API",
        version=__version__,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.readiness_probe = probe
    metrics = Metrics(version=__version__, settings=resolved)
    app.state.metrics = metrics
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(MetricsMiddleware, metrics=metrics)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(metrics_router)
    if auth_service is not None:
        if resolved_dashboard_provider is None:
            raise ValueError(
                "a dashboard provider is required when console authentication is installed"
            )
        install_console_web(
            app,
            auth_service,
            resolved_dashboard_provider,
            ConsoleWebSettings(
                allowed_hosts=frozenset(resolved.console_allowed_hosts),
                allowed_origins=frozenset(resolved.console_allowed_origins),
                cookie_secure=resolved.console_cookie_secure,
            ),
            control_plane_provider=resolved_control_plane_provider,
            crawler_execution_provider=resolved_crawler_execution_provider,
            goal_run_provider=resolved_goal_run_provider,
        )
        if resolved_source_registry_provider is not None:
            install_source_registry_operator_api(
                app,
                auth_service=auth_service,
                settings=ConsoleWebSettings(
                    allowed_hosts=frozenset(resolved.console_allowed_hosts),
                    allowed_origins=frozenset(resolved.console_allowed_origins),
                    cookie_secure=resolved.console_cookie_secure,
                ),
                provider=resolved_source_registry_provider,
            )
        if resolved_goal_run_provider is not None:
            install_goal_run_operator_api(
                app,
                auth_service=auth_service,
                settings=ConsoleWebSettings(
                    allowed_hosts=frozenset(resolved.console_allowed_hosts),
                    allowed_origins=frozenset(resolved.console_allowed_origins),
                    cookie_secure=resolved.console_cookie_secure,
                ),
                provider=resolved_goal_run_provider,
            )
        if resolved_gmail_readonly_provider is not None:
            install_gmail_readonly_operator_api(
                app,
                auth_service=auth_service,
                settings=ConsoleWebSettings(
                    allowed_hosts=frozenset(resolved.console_allowed_hosts),
                    allowed_origins=frozenset(resolved.console_allowed_origins),
                    cookie_secure=resolved.console_cookie_secure,
                ),
                provider=resolved_gmail_readonly_provider,
            )
        if resolved_gmail_send_provider is not None:
            install_gmail_send_operator_api(
                app,
                auth_service=auth_service,
                settings=ConsoleWebSettings(
                    allowed_hosts=frozenset(resolved.console_allowed_hosts),
                    allowed_origins=frozenset(resolved.console_allowed_origins),
                    cookie_secure=resolved.console_cookie_secure,
                ),
                provider=resolved_gmail_send_provider,
            )
        if resolved_greenhouse_submit_provider is not None:
            install_greenhouse_submit_operator_api(
                app,
                auth_service=auth_service,
                settings=ConsoleWebSettings(
                    allowed_hosts=frozenset(resolved.console_allowed_hosts),
                    allowed_origins=frozenset(resolved.console_allowed_origins),
                    cookie_secure=resolved.console_cookie_secure,
                ),
                provider=resolved_greenhouse_submit_provider,
            )
    return app


class _ControlPlaneGatedGmailSendOperatorProvider(DisabledGmailSendOperatorProvider):
    """Expose send account control-plane state before enabling external send execution."""

    def __init__(
        self,
        delegate: GmailSendOperatorProvider,
        *,
        execution_enabled: bool,
    ) -> None:
        self._delegate = delegate
        self._execution_enabled = execution_enabled

    async def register_account(self, **kwargs: Any) -> Any:
        if not self._execution_enabled and kwargs.get("requested_status") != "disabled":
            raise GmailSendUnavailable("gmail send execution runtime is not configured")
        return await self._delegate.register_account(**kwargs)

    async def list_accounts(self, **kwargs: Any) -> Any:
        return await self._delegate.list_accounts(**kwargs)

    async def account_status(self, **kwargs: Any) -> Any:
        return await self._delegate.account_status(**kwargs)

    async def create_draft(self, **kwargs: Any) -> Any:
        self._require_execution_enabled()
        return await self._delegate.create_draft(**kwargs)

    async def review_draft(self, **kwargs: Any) -> Any:
        self._require_execution_enabled()
        return await self._delegate.review_draft(**kwargs)

    async def reserve_intent(self, **kwargs: Any) -> Any:
        self._require_execution_enabled()
        return await self._delegate.reserve_intent(**kwargs)

    async def close(self) -> None:
        close = getattr(self._delegate, "close", None)
        if close is not None:
            await close()

    def _require_execution_enabled(self) -> None:
        if not self._execution_enabled:
            raise GmailSendUnavailable("gmail send execution runtime is not configured")


class _ControlPlaneGatedGmailReadonlyOperatorProvider(DisabledGmailReadonlyOperatorProvider):
    """Expose read-only Gmail account control-plane state before enabling mailbox sync."""

    def __init__(
        self,
        delegate: GmailReadonlyOperatorProvider,
        *,
        execution_enabled: bool,
    ) -> None:
        self._delegate = delegate
        self._execution_enabled = execution_enabled

    async def register_account(self, **kwargs: Any) -> Any:
        return await self._delegate.register_account(**kwargs)

    async def list_accounts(self, **kwargs: Any) -> Any:
        return await self._delegate.list_accounts(**kwargs)

    async def account_status(self, **kwargs: Any) -> Any:
        return await self._delegate.account_status(**kwargs)

    async def request_sync(self, **kwargs: Any) -> Any:
        self._require_execution_enabled()
        return await self._delegate.request_sync(**kwargs)

    async def list_sync_runs(self, **kwargs: Any) -> Any:
        return await self._delegate.list_sync_runs(**kwargs)

    async def list_proposals(self, **kwargs: Any) -> Any:
        return await self._delegate.list_proposals(**kwargs)

    async def review_proposal(self, **kwargs: Any) -> Any:
        self._require_execution_enabled()
        return await self._delegate.review_proposal(**kwargs)

    async def reset_history(self, **kwargs: Any) -> Any:
        self._require_execution_enabled()
        return await self._delegate.reset_history(**kwargs)

    async def revoke_account(self, **kwargs: Any) -> Any:
        return await self._delegate.revoke_account(**kwargs)

    async def close(self) -> None:
        close = getattr(self._delegate, "close", None)
        if close is not None:
            await close()

    def _require_execution_enabled(self) -> None:
        if not self._execution_enabled:
            raise GmailReadonlyUnavailable("gmail read-only execution runtime is not configured")


app = create_app()
