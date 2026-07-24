from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

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
from careerops.domain.applications import (
    ApplicationPackage,
    FollowUpReminder,
    ResumeVersion,
)
from careerops.infrastructure.auth import create_console_auth_service
from careerops.infrastructure.dashboard import RuntimeDashboardSnapshotProvider
from careerops.infrastructure.memory_repos import InMemoryApplicationRepository
from careerops.infrastructure.redis import RedisAuthRateLimiter
from careerops.infrastructure.runtime import RuntimeResources
from careerops.observability import Metrics
from careerops.web import ConsoleWebSettings, install_console_web
from careerops.web.jobs_ui import web_router as jobs_ui_router
from careerops.web.matching_ui import router as matching_ui_router

# ---------------------------------------------------------------------------
# Protocol adapters for InMemoryApplicationRepository
# ---------------------------------------------------------------------------
# ApplicationService.__init__ expects four separate Protocol-typed repos,
# each with a ``save`` method.  InMemoryApplicationRepository stores
# everything in one class but renames the write methods to avoid
# signature collisions (save_resume, save_package, save_follow_up).
# These thin wrappers bridge the gap.


class _ResumeRepoAdapter:
    def __init__(self, repo: InMemoryApplicationRepository) -> None:
        self._repo = repo

    def find_latest_version(self, candidate_id: UUID) -> ResumeVersion | None:
        return self._repo.find_latest_version(candidate_id)

    def save(self, version: ResumeVersion) -> None:
        self._repo.save_resume(version)


class _PackageRepoAdapter:
    def __init__(self, repo: InMemoryApplicationRepository) -> None:
        self._repo = repo

    def find_by_application(self, application_id: UUID) -> ApplicationPackage | None:
        return self._repo.find_by_application(application_id)

    def save(self, package: ApplicationPackage) -> None:
        self._repo.save_package(package)


class _FollowUpRepoAdapter:
    def __init__(self, repo: InMemoryApplicationRepository) -> None:
        self._repo = repo

    def find_by_id(self, reminder_id: UUID) -> FollowUpReminder | None:
        return self._repo.find_follow_up_by_id(reminder_id)

    def find_active_by_application_and_rule(
        self, application_id: UUID, rule_version: str
    ) -> FollowUpReminder | None:
        return self._repo.find_active_by_application_and_rule(application_id, rule_version)

    def save(self, reminder: FollowUpReminder) -> None:
        self._repo.save_follow_up(reminder)


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

    # Wire API route repositories and services when using RuntimeResources.
    # Routes gracefully degrade to empty results when these are absent, but
    # wiring them here lets the REST endpoints return real data.
    if isinstance(probe, RuntimeResources):
        from careerops.application.applications import ApplicationService
        from careerops.application.contacts import ContactService
        from careerops.application.matching import (
            EvidenceImportService,
            MatchOrchestrator,
        )

        app.state.matching_repository = probe.matching_read_repo
        app.state.evidence_import_service = EvidenceImportService(probe.matching_read_repo)
        app.state.match_orchestrator = MatchOrchestrator(data_repository=probe.matching_read_repo)
        app.state.job_read_repository = probe.job_read_repo
        app.state.contact_repository = probe.contact_repo
        app.state.contact_service = ContactService(probe.contact_repo)
        app.state.application_repository = probe.application_repo
        app.state.application_service = ApplicationService(
            application_repo=probe.application_repo,
            resume_repo=_ResumeRepoAdapter(probe.application_repo),
            package_repo=_PackageRepoAdapter(probe.application_repo),
            follow_up_repo=_FollowUpRepoAdapter(probe.application_repo),
        )
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
    app.include_router(jobs_ui_router, dependencies=[Depends(require_api_auth)])
    app.include_router(matching_ui_router, dependencies=[Depends(require_api_auth)])

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
