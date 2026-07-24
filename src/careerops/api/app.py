from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from careerops import __version__
from careerops.api.auth_dependency import require_api_auth
from careerops.api.errors import install_error_handlers
from careerops.api.metrics_middleware import MetricsMiddleware
from careerops.api.middleware import RequestIdMiddleware
from careerops.api.routes.applications import router as applications_router
from careerops.api.routes.health import router as health_router
from careerops.api.routes.jobs import router as jobs_router
from careerops.api.routes.matching import router as matching_router
from careerops.api.routes.metrics import router as metrics_router
from careerops.api.routes.review import install_review_endpoint
from careerops.application.dashboard import DashboardSnapshotProvider
from careerops.application.ports.readiness import ReadinessProbe
from careerops.auth.service import ConsoleAuthService
from careerops.config import RuntimeEnvironment, Settings, get_settings
from careerops.infrastructure.auth import create_console_auth_service
from careerops.infrastructure.dashboard import RuntimeDashboardSnapshotProvider
from careerops.infrastructure.redis import RedisAuthRateLimiter
from careerops.infrastructure.runtime import RuntimeResources
from careerops.observability import Metrics
from careerops.web import ConsoleWebSettings, install_console_web
from careerops.web.matching_ui import router as matching_ui_router


def create_app(
    settings: Settings | None = None,
    *,
    readiness_probe: ReadinessProbe | None = None,
    console_auth_service: ConsoleAuthService | None = None,
    dashboard_provider: DashboardSnapshotProvider | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    # Create metrics first so it can be wired into the runtime's graph
    # (LLM token recording + apply_submitted counter).
    metrics = Metrics(version=__version__, settings=resolved)
    if readiness_probe is not None:
        probe = readiness_probe
    else:
        probe = RuntimeResources(resolved, metrics=metrics)
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
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
    app.state.auth_service = auth_service
    app.state.metrics = metrics
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(MetricsMiddleware, metrics=metrics)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(metrics_router)

    # Build web_settings once; needed for both console web and API auth.
    web_settings: ConsoleWebSettings | None = None
    if auth_service is not None:
        web_settings = ConsoleWebSettings(
            allowed_hosts=frozenset(resolved.console_allowed_hosts),
            allowed_origins=frozenset(resolved.console_allowed_origins),
            cookie_secure=resolved.console_cookie_secure,
        )
        app.state.web_settings = web_settings

    # Protected API routers — require session cookie + CSRF header.
    app.include_router(jobs_router, dependencies=[Depends(require_api_auth)])
    app.include_router(matching_router, dependencies=[Depends(require_api_auth)])
    app.include_router(applications_router, dependencies=[Depends(require_api_auth)])
    app.include_router(matching_ui_router)

    if auth_service is not None:
        if web_settings is None:
            raise ValueError("web_settings is required when console authentication is installed")
        if resolved_dashboard_provider is None:
            raise ValueError(
                "a dashboard provider is required when console authentication is installed"
            )
        install_console_web(app, auth_service, resolved_dashboard_provider, web_settings)
        # Review endpoint (plan v0.4 §2.7 / §3 Stage 3): mounted when the
        # runtime actually compiled the graph (durable in PRODUCTION via
        # PostgresSaver + PostgresSideEffectStore, in-memory otherwise).
        if (
            resolved.environment is not RuntimeEnvironment.PRODUCTION
            and isinstance(probe, RuntimeResources)
            and probe.career_graph is not None
            and probe.review_mapping is not None
            and probe.side_effect_kernel is not None
        ):
            install_review_endpoint(
                app,
                auth_service=auth_service,
                rate_limiter=RedisAuthRateLimiter(probe.redis_sync),
                review_mapping=probe.review_mapping,
                career_graph=probe.career_graph,
                side_effect_kernel=probe.side_effect_kernel,
                web_settings=web_settings,
            )
    return app


app = create_app()
