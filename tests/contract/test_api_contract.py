import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import httpx2
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from careerops.api import app as app_module
from careerops.api.app import create_app
from careerops.api.source_registry import (
    ClaimSourceResponse,
    CompleteSourceResponse,
    DueSourcesResponse,
    DueSourceSummary,
    FailSourceResponse,
    IngestPublicAtsResponse,
    ListSourceRegistriesResponse,
    RegisterSourceRegistryResponse,
    SourceRegistryOperatorProvider,
    SourceRegistrySummary,
)
from careerops.application.autopilot_control import (
    AutopilotCommandResult,
    AutopilotControlCapability,
    AutopilotControlCapabilityState,
    AutopilotControlPlane,
    CampaignGrantSnapshot,
    ReviewQueueCount,
    ReviewQueueSnapshot,
    ReviewQueueTab,
    ReviewResolutionMode,
)
from careerops.application.dashboard import DashboardSnapshot, DashboardSystemStatus
from careerops.application.ports.readiness import ReadinessReport, ReadinessState
from careerops.auth.contracts import AuthenticatedPrincipal, AuthRequestContext, SessionSecrets
from careerops.auth.service import ConsoleAuthService
from careerops.config import RuntimeEnvironment, Settings
from careerops.infrastructure.database.crawler_source_registry import (
    CrawlerSourceLeaseConflictError,
    CrawlerSourceRegistryRepositoryError,
)
from careerops.web.crawler_execution import (
    CrawlerExecutionConsoleCommandResult,
    CrawlerExecutionReviewSnapshot,
    DisabledCrawlerExecutionConsoleProvider,
)

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000701")
SOURCE_REGISTRY_MUTATION_HEADERS = {
    "Origin": "http://testserver",
    "X-CSRF-Token": "authenticated-csrf",
}


class FixedReadinessProbe:
    def __init__(self, checks: Mapping[str, ReadinessState] | None = None) -> None:
        self._checks = checks or {
            "database": ReadinessState.OK,
            "redis": ReadinessState.OK,
            "temporal": ReadinessState.OK,
            "storage": ReadinessState.OK,
        }

    async def check(self) -> ReadinessReport:
        return ReadinessReport(checks=self._checks)

    async def close(self) -> None:
        return None


class FixedRuntimeResources(FixedReadinessProbe):
    class Database:
        def begin(self) -> object:
            return object()

    database = Database()
    redis_sync = object()


class FixedConsoleAuthService:
    def begin_preauth(
        self,
        *,
        now: datetime,
        context: AuthRequestContext,
    ) -> SessionSecrets:
        assert now and context.trace_id
        return self._authenticated_secrets()

    def complete_bootstrap(self, **_kwargs: object) -> SessionSecrets:
        return self._authenticated_secrets()

    def login(self, **_kwargs: object) -> SessionSecrets:
        return self._authenticated_secrets()

    def authenticate(self, session_token: str, *, now: datetime) -> AuthenticatedPrincipal:
        assert now.tzinfo is not None
        if session_token != "authenticated-session":
            raise ValueError("invalid session")
        return AuthenticatedPrincipal(
            user_id=ACTOR_ID,
            username="owner",
            session_id=UUID("00000000-0000-0000-0000-000000000702"),
            csrf_token_hash="not-exposed",
            absolute_expires_at=NOW + timedelta(days=7),
        )

    def validate_csrf(self, principal: AuthenticatedPrincipal, csrf_token: str) -> None:
        assert principal.user_id == ACTOR_ID
        if csrf_token != "authenticated-csrf":
            raise ValueError("bad csrf")

    def logout(self, **_kwargs: object) -> None:
        return None

    @staticmethod
    def _authenticated_secrets() -> SessionSecrets:
        return SessionSecrets(
            session_id=UUID("00000000-0000-0000-0000-000000000702"),
            token="authenticated-session",
            csrf_token="authenticated-csrf",
            absolute_expires_at=NOW + timedelta(days=7),
        )


class FixedDashboardProvider:
    async def snapshot(self) -> DashboardSnapshot:
        return DashboardSnapshot(
            system_status=DashboardSystemStatus.READY,
            dependency_checks=(("database", "ok"),),
            integration_checks=(("external_writes", "disabled"),),
            pending_approvals=0,
            pending_outbox_events=0,
        )


class RecordingAutopilotControlPlane:
    def __init__(self) -> None:
        self.review_actor_ids: list[UUID] = []
        self.grant_actor_ids: list[UUID] = []
        self.review_tabs: list[ReviewQueueTab] = []
        self.mutation_requests = 0

    async def campaign_grants(
        self,
        *,
        actor_id: UUID,
        now: datetime,
    ) -> CampaignGrantSnapshot:
        assert now.tzinfo is not None
        self.grant_actor_ids.append(actor_id)
        return CampaignGrantSnapshot(
            capability=_read_only_capability(),
            generated_at=now,
            grants=(),
        )

    async def review_queue(
        self,
        *,
        actor_id: UUID,
        tab: ReviewQueueTab,
        now: datetime,
    ) -> ReviewQueueSnapshot:
        assert now.tzinfo is not None
        self.review_actor_ids.append(actor_id)
        self.review_tabs.append(tab)
        return ReviewQueueSnapshot(
            capability=_read_only_capability(),
            generated_at=now,
            active_tab=tab,
            counts=tuple(ReviewQueueCount(tab=queue_tab, count=0) for queue_tab in ReviewQueueTab),
            items=(),
        )

    async def request_grant_activation(
        self,
        *,
        actor_id: UUID,
        grant_id: UUID,
    ) -> AutopilotCommandResult:
        del actor_id, grant_id
        self.mutation_requests += 1
        raise AssertionError("read-only console route must not request grant activation")

    async def request_grant_revocation(
        self,
        *,
        actor_id: UUID,
        grant_id: UUID,
    ) -> AutopilotCommandResult:
        del actor_id, grant_id
        self.mutation_requests += 1
        raise AssertionError("read-only console route must not request grant revocation")

    async def request_review_resolution(
        self,
        *,
        actor_id: UUID,
        review_item_id: UUID,
        mode: ReviewResolutionMode,
    ) -> AutopilotCommandResult:
        del actor_id, review_item_id, mode
        self.mutation_requests += 1
        raise AssertionError("read-only console route must not request review resolution")


class RecordingCrawlerExecutionProvider(DisabledCrawlerExecutionConsoleProvider):
    def __init__(self) -> None:
        self.pending_actor_ids: list[UUID] = []
        self.approvals = 0

    async def pending_requests(
        self,
        *,
        actor_id: UUID,
        now: datetime,
        limit: int = 50,
    ) -> CrawlerExecutionReviewSnapshot:
        self.pending_actor_ids.append(actor_id)
        return await super().pending_requests(actor_id=actor_id, now=now, limit=limit)

    async def approve_request(
        self,
        *,
        actor_id: UUID,
        request_id: UUID,
        now: datetime,
    ) -> CrawlerExecutionConsoleCommandResult:
        del actor_id, request_id, now
        self.approvals += 1
        raise AssertionError("crawler mutations require explicit console form submission")


class RecordingSourceRegistryProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.registry_id = UUID("00000000-0000-0000-0000-000000000801")
        self.lease_token = UUID("00000000-0000-0000-0000-000000000802")
        self.run_id = UUID("00000000-0000-0000-0000-000000000803")
        self.source_row_id = UUID("00000000-0000-0000-0000-000000000804")
        self.run_event_id = UUID("00000000-0000-0000-0000-000000000805")

    async def register(
        self,
        *,
        manifest_ref: str,
        actor_id: UUID,
        now: datetime,
    ) -> RegisterSourceRegistryResponse:
        self.calls.append(("register", (manifest_ref, actor_id, now)))
        return RegisterSourceRegistryResponse(
            registry_id=self.registry_id,
            manifest_sha256="a" * 64,
            registry_sha256="b" * 64,
            source_count=2,
        )

    async def list_registries(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListSourceRegistriesResponse:
        self.calls.append(("list", (actor_id, limit)))
        return ListSourceRegistriesResponse(
            registries=(
                SourceRegistrySummary(
                    registry_id=self.registry_id,
                    manifest_path="datasets/manifests/crawler-sources.json",
                    manifest_sha256="a" * 64,
                    registry_sha256="b" * 64,
                    source_count=2,
                    created_by=str(actor_id),
                    generated_at=NOW,
                    created_at=NOW,
                ),
            )
        )

    async def due_sources(
        self,
        *,
        registry_id: UUID,
        actor_id: UUID,
        now: datetime,
        limit: int,
    ) -> DueSourcesResponse:
        self.calls.append(("due", (registry_id, actor_id, now, limit)))
        return DueSourcesResponse(
            registry_id=registry_id,
            due_sources=(
                DueSourceSummary(
                    registry_id=registry_id,
                    source_id="public-ats",
                    adapter="recruitment.public_ats_feed",
                    enabled=True,
                    input_artifact="datasets/private/triage.jsonl",
                    output_dir="datasets/raw/ats",
                    dependency_source_ids=(),
                    dependency_artifacts=(),
                    command_sha256="c" * 64,
                    source_sha256="d" * 64,
                    cadence_seconds=3600,
                    retry_rounds=1,
                    next_run_at=NOW,
                ),
            ),
        )

    async def claim_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        worker_id: str,
        lease_for: timedelta,
    ) -> ClaimSourceResponse:
        self.calls.append(("claim", (registry_id, source_id, actor_id, worker_id, lease_for)))
        return ClaimSourceResponse(
            registry_id=registry_id,
            run_id=self.run_id,
            source_row_id=self.source_row_id,
            source_id=source_id,
            lease_token=self.lease_token,
            lease_owner=worker_id,
            lease_expires_at=NOW + lease_for,
        )

    async def complete_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        run_id: UUID,
        source_row_id: UUID,
        worker_id: str,
        lease_token: UUID,
        output_manifest_sha256: str | None,
        cursor: str | None,
        result: str,
    ) -> CompleteSourceResponse:
        self.calls.append(
            (
                "complete",
                (
                    registry_id,
                    source_id,
                    actor_id,
                    run_id,
                    source_row_id,
                    worker_id,
                    lease_token,
                    output_manifest_sha256,
                    cursor,
                    result,
                ),
            )
        )
        return CompleteSourceResponse(
            registry_id=registry_id,
            run_id=run_id,
            source_row_id=source_row_id,
            source_id=source_id,
            lease_owner=worker_id,
            output_manifest_sha256=output_manifest_sha256,
            cursor=cursor,
        )

    async def fail_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        run_id: UUID,
        source_row_id: UUID,
        worker_id: str,
        lease_token: UUID,
        error: str,
    ) -> FailSourceResponse:
        self.calls.append(
            (
                "fail",
                (
                    registry_id,
                    source_id,
                    actor_id,
                    run_id,
                    source_row_id,
                    worker_id,
                    lease_token,
                    error,
                ),
            )
        )
        return FailSourceResponse(
            registry_id=registry_id,
            run_id=run_id,
            source_row_id=source_row_id,
            source_id=source_id,
            lease_owner=worker_id,
        )

    async def ingest_public_ats(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        run_id: UUID,
        source_row_id: UUID,
        actor_id: UUID,
        max_records: int,
    ) -> IngestPublicAtsResponse:
        self.calls.append(
            (
                "ingest-public-ats",
                (registry_id, source_id, run_id, source_row_id, actor_id, max_records),
            )
        )
        return IngestPublicAtsResponse(
            registry_id=registry_id,
            source_row_id=source_row_id,
            source_id=source_id,
            run_id=run_id,
            run_event_id=self.run_event_id,
            observed_records=3,
            inserted_versions=2,
            reused_versions=1,
        )


class ConflictingSourceRegistryProvider(RecordingSourceRegistryProvider):
    async def register(
        self,
        *,
        manifest_ref: str,
        actor_id: UUID,
        now: datetime,
    ) -> RegisterSourceRegistryResponse:
        del manifest_ref, actor_id, now
        raise CrawlerSourceRegistryRepositoryError("secret snapshot collision details")

    async def claim_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        worker_id: str,
        lease_for: timedelta,
    ) -> ClaimSourceResponse:
        del registry_id, source_id, actor_id, worker_id, lease_for
        raise CrawlerSourceLeaseConflictError("secret lease and SQL details")


class SyntheticDatabaseError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("secret database error")
        self.sqlstate = sqlstate


class SqlStateSourceRegistryProvider(RecordingSourceRegistryProvider):
    def __init__(self, sqlstate: str) -> None:
        super().__init__()
        self.sqlstate = sqlstate

    async def claim_source(
        self,
        *,
        registry_id: UUID,
        source_id: str,
        actor_id: UUID,
        worker_id: str,
        lease_for: timedelta,
    ) -> ClaimSourceResponse:
        del registry_id, source_id, actor_id, worker_id, lease_for
        raise DBAPIError(
            "SECRET SELECT",
            {},
            SyntheticDatabaseError(self.sqlstate),
            False,
        )


def _read_only_capability() -> AutopilotControlCapability:
    return AutopilotControlCapability(
        state=AutopilotControlCapabilityState.READ_ONLY,
        title_zh="高自治控制面只读",
        description_zh="测试只读投影",
        reason_code="TEST_READ_ONLY",
    )


def make_client(
    environment: RuntimeEnvironment = RuntimeEnvironment.TEST,
    *,
    readiness_probe: FixedReadinessProbe | None = None,
) -> httpx2.Client:
    values: dict[str, object] = {"environment": environment}
    if environment is RuntimeEnvironment.PRODUCTION:
        values.update(
            {
                "console_cookie_secure": True,
                "console_allowed_hosts": ("careerops.example",),
                "console_allowed_origins": ("https://careerops.example",),
            }
        )
    settings = Settings.model_validate(values)
    probe = readiness_probe or FixedReadinessProbe()
    return cast("httpx2.Client", TestClient(create_app(settings, readiness_probe=probe)))


def make_authenticated_console_client(
    control_plane: RecordingAutopilotControlPlane,
    crawler_execution_provider: RecordingCrawlerExecutionProvider | None = None,
    source_registry_provider: SourceRegistryOperatorProvider | None = None,
    *,
    raise_server_exceptions: bool = True,
) -> httpx2.Client:
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "console_allowed_hosts": ("testserver",),
            "console_allowed_origins": ("http://testserver",),
        }
    )
    app = create_app(
        settings,
        readiness_probe=FixedReadinessProbe(),
        console_auth_service=cast(ConsoleAuthService, FixedConsoleAuthService()),
        dashboard_provider=FixedDashboardProvider(),
        control_plane_provider=cast(AutopilotControlPlane, control_plane),
        crawler_execution_provider=crawler_execution_provider,
        source_registry_provider=source_registry_provider,
    )
    client = TestClient(
        app,
        follow_redirects=False,
        raise_server_exceptions=raise_server_exceptions,
    )
    client.cookies.set("careerops_session", "authenticated-session")
    client.cookies.set("careerops_csrf", "authenticated-csrf")
    return cast("httpx2.Client", client)


def test_liveness_contract_and_generated_trace_id() -> None:
    response = make_client().get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "careerops",
        "version": "0.1.0",
    }
    assert response.headers["cache-control"] == "no-store"
    assert re.fullmatch(r"[a-f0-9]{32}", response.headers["x-request-id"])


def test_safe_client_request_id_is_echoed() -> None:
    response = make_client().get(
        "/api/v1/health/live", headers={"X-Request-ID": "client.trace-123"}
    )

    assert response.headers["x-request-id"] == "client.trace-123"


def test_unsafe_client_request_id_is_replaced() -> None:
    response = make_client().get(
        "/api/v1/health/live", headers={"X-Request-ID": "contains spaces and secrets"}
    )

    assert response.headers["x-request-id"] != "contains spaces and secrets"
    assert re.fullmatch(r"[a-f0-9]{32}", response.headers["x-request-id"])


def test_readiness_reports_unreleased_integrations_as_disabled() -> None:
    response = make_client().get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {
            "config": "ok",
            "database": "ok",
            "redis": "ok",
            "temporal": "ok",
            "storage": "ok",
            "model_provider": "disabled",
            "google_oauth": "disabled",
            "external_writes": "disabled",
        },
    }


def test_runtime_app_binds_crawler_execution_console_provider_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[object, object]] = []

    class RecordingRuntimeCrawlerProvider(DisabledCrawlerExecutionConsoleProvider):
        def __init__(self, engine: object, *, workspace_root: object) -> None:
            created.append((engine, workspace_root))

    monkeypatch.setattr(app_module, "RuntimeResources", FixedRuntimeResources)
    monkeypatch.setattr(
        app_module,
        "create_runtime_resources",
        lambda _settings: FixedRuntimeResources(),
    )
    monkeypatch.setattr(app_module, "create_console_auth_service", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        app_module,
        "RuntimeDashboardSnapshotProvider",
        lambda *_args: FixedDashboardProvider(),
    )
    monkeypatch.setattr(app_module, "RuntimeAutopilotControlPlane", lambda _engine: object())
    monkeypatch.setattr(
        app_module,
        "RuntimeCrawlerExecutionConsoleProvider",
        RecordingRuntimeCrawlerProvider,
    )

    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "console_allowed_hosts": ("testserver",),
            "console_allowed_origins": ("http://testserver",),
        }
    )

    create_app(settings)

    assert created == [(FixedRuntimeResources.database, settings.crawler_workspace_root)]


def test_runtime_app_binds_greenhouse_submit_provider_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[object] = []

    class RecordingRuntimeGreenhouseSubmitProvider:
        def __init__(self, *, transaction_factory: object) -> None:
            created.append(transaction_factory)

    monkeypatch.setattr(app_module, "RuntimeResources", FixedRuntimeResources)
    monkeypatch.setattr(
        app_module,
        "create_runtime_resources",
        lambda _settings: FixedRuntimeResources(),
    )
    monkeypatch.setattr(
        app_module,
        "create_console_auth_service",
        lambda *_args, **_kwargs: FixedConsoleAuthService(),
    )
    monkeypatch.setattr(
        app_module,
        "RuntimeDashboardSnapshotProvider",
        lambda *_args: FixedDashboardProvider(),
    )
    monkeypatch.setattr(app_module, "RuntimeAutopilotControlPlane", lambda _engine: object())
    monkeypatch.setattr(
        app_module,
        "RuntimeGreenhouseSubmitOperatorProvider",
        RecordingRuntimeGreenhouseSubmitProvider,
    )

    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "console_allowed_hosts": ("testserver",),
            "console_allowed_origins": ("http://testserver",),
            "external_writes_enabled": True,
            "auto_submit_enabled": True,
            "greenhouse_submit_enabled": True,
            "greenhouse_submit_release_attested": True,
        }
    )

    create_app(settings)

    assert created == [FixedRuntimeResources.database.begin]


def test_readiness_fails_closed_when_a_required_dependency_is_unavailable() -> None:
    response = make_client(
        readiness_probe=FixedReadinessProbe(
            {
                "database": ReadinessState.OK,
                "redis": ReadinessState.NOT_READY,
                "temporal": ReadinessState.OK,
                "storage": ReadinessState.OK,
            }
        )
    ).get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["redis"] == "not_ready"


def test_not_found_uses_stable_error_envelope_without_framework_detail() -> None:
    response = make_client().get("/api/v1/does-not-exist", headers={"X-Request-ID": "known-trace"})

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Resource not found",
            "details": None,
            "trace_id": "known-trace",
        }
    }


def test_method_not_allowed_uses_machine_code() -> None:
    response = make_client().post("/api/v1/health/live")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_nonstandard_http_status_keeps_stable_fallback_error() -> None:
    settings = Settings.model_validate({"environment": RuntimeEnvironment.TEST})
    app = create_app(settings, readiness_probe=FixedReadinessProbe())

    @app.get("/api/v1/test/nonstandard", include_in_schema=False)
    async def nonstandard_error() -> None:
        raise HTTPException(status_code=499, detail="must not leak")

    response = TestClient(app).get("/api/v1/test/nonstandard")

    assert response.status_code == 499
    assert response.json()["error"]["code"] == "HTTP_ERROR"
    assert response.json()["error"]["message"] == "Request could not be processed"


def test_openapi_is_versioned_and_contains_only_declared_health_routes() -> None:
    response = make_client().get("/api/v1/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "CareerOps API"
    assert set(response.json()["paths"]) == {
        "/api/v1/health/live",
        "/api/v1/health/ready",
    }


def test_create_app_forwards_injected_control_plane_to_authenticated_review_queue() -> None:
    control_plane = RecordingAutopilotControlPlane()
    client = make_authenticated_console_client(control_plane)

    response = client.get("/review?tab=exceptions")

    assert response.status_code == 200
    assert control_plane.review_actor_ids == [ACTOR_ID]
    assert control_plane.review_tabs == [ReviewQueueTab.EXCEPTIONS]
    assert control_plane.grant_actor_ids == []
    assert control_plane.mutation_requests == 0
    assert "read_only" in response.text
    assert "TEST_READ_ONLY" in response.text
    assert "<form" not in response.text


def test_create_app_forwards_injected_control_plane_to_authenticated_autopilot_grants() -> None:
    control_plane = RecordingAutopilotControlPlane()
    client = make_authenticated_console_client(control_plane)

    response = client.get("/grants/autopilot")

    assert response.status_code == 200
    assert control_plane.grant_actor_ids == [ACTOR_ID]
    assert control_plane.review_actor_ids == []
    assert control_plane.mutation_requests == 0
    assert "read_only" in response.text
    assert "TEST_READ_ONLY" in response.text
    assert "<form" not in response.text


def test_create_app_keeps_authenticated_console_routes_out_of_openapi() -> None:
    control_plane = RecordingAutopilotControlPlane()
    client = make_authenticated_console_client(control_plane)

    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    assert "/review" not in response.json()["paths"]
    assert "/grants/autopilot" not in response.json()["paths"]
    assert "/crawlers/review" not in response.json()["paths"]


def test_authenticated_source_registry_operator_routes_are_injected_and_internal() -> None:
    provider = RecordingSourceRegistryProvider()
    client = make_authenticated_console_client(
        RecordingAutopilotControlPlane(),
        source_registry_provider=provider,
    )

    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/internal/source-registries/register" in response.json()["paths"]
    assert (
        "/api/v1/internal/source-registries/{registry_id}/sources/{source_id}/claim"
        in (response.json()["paths"])
    )
    assert (
        "/api/v1/internal/source-registries/{registry_id}/sources/{source_id}/runs/"
        "{run_id}/ingest-public-ats"
    ) in response.json()["paths"]


def test_authenticated_greenhouse_submit_operator_routes_are_installed_by_default() -> None:
    client = make_authenticated_console_client(RecordingAutopilotControlPlane())

    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/internal/greenhouse-submit/accounts" in response.json()["paths"]
    assert "/api/v1/internal/greenhouse-submit/drafts" in response.json()["paths"]
    assert (
        "/api/v1/internal/greenhouse-submit/reconciliation-cases/{reconciliation_case_id}"
        in response.json()["paths"]
    )


def test_source_registry_operator_requires_authenticated_session() -> None:
    provider = RecordingSourceRegistryProvider()
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "console_allowed_hosts": ("testserver",),
            "console_allowed_origins": ("http://testserver",),
        }
    )
    app = create_app(
        settings,
        readiness_probe=FixedReadinessProbe(),
        console_auth_service=cast(ConsoleAuthService, FixedConsoleAuthService()),
        dashboard_provider=FixedDashboardProvider(),
        control_plane_provider=cast(AutopilotControlPlane, RecordingAutopilotControlPlane()),
        source_registry_provider=provider,
    )

    response = TestClient(app).get("/api/v1/internal/source-registries")

    assert response.status_code == 401
    assert provider.calls == []


def test_source_registry_operator_rejects_read_with_bad_host() -> None:
    provider = RecordingSourceRegistryProvider()
    client = make_authenticated_console_client(
        RecordingAutopilotControlPlane(),
        source_registry_provider=provider,
    )

    response = client.get(
        "/api/v1/internal/source-registries",
        headers={"Host": "attacker.example"},
    )

    assert response.status_code == 403
    assert provider.calls == []


def test_source_registry_operator_forwards_authenticated_register_without_running_crawler() -> None:
    provider = RecordingSourceRegistryProvider()
    client = make_authenticated_console_client(
        RecordingAutopilotControlPlane(),
        source_registry_provider=provider,
    )

    response = client.post(
        "/api/v1/internal/source-registries/register",
        headers=SOURCE_REGISTRY_MUTATION_HEADERS,
        json={"manifest_ref": "crawler-sources.json"},
    )

    assert response.status_code == 201
    assert response.json()["registry_id"] == str(provider.registry_id)
    assert provider.calls[0][0] == "register"
    manifest_ref, actor_id, observed_now = cast(tuple[object, UUID, datetime], provider.calls[0][1])
    assert manifest_ref == "crawler-sources.json"
    assert actor_id == ACTOR_ID
    assert observed_now.tzinfo is not None


def test_source_registry_operator_rejects_mutation_without_csrf_header() -> None:
    provider = RecordingSourceRegistryProvider()
    client = make_authenticated_console_client(
        RecordingAutopilotControlPlane(),
        source_registry_provider=provider,
    )

    response = client.post(
        "/api/v1/internal/source-registries/register",
        headers={"Origin": "http://testserver"},
        json={"manifest_ref": "crawler-sources.json"},
    )

    assert response.status_code == 422
    assert provider.calls == []


def test_source_registry_operator_rejects_mutation_with_bad_origin() -> None:
    provider = RecordingSourceRegistryProvider()
    client = make_authenticated_console_client(
        RecordingAutopilotControlPlane(),
        source_registry_provider=provider,
    )

    response = client.post(
        "/api/v1/internal/source-registries/register",
        headers={
            "Origin": "http://attacker.example",
            "X-CSRF-Token": "authenticated-csrf",
        },
        json={"manifest_ref": "crawler-sources.json"},
    )

    assert response.status_code == 403
    assert provider.calls == []


def test_source_registry_operator_returns_sanitized_conflicts() -> None:
    provider = ConflictingSourceRegistryProvider()
    client = make_authenticated_console_client(
        RecordingAutopilotControlPlane(),
        source_registry_provider=provider,
    )
    headers = SOURCE_REGISTRY_MUTATION_HEADERS

    register_response = client.post(
        "/api/v1/internal/source-registries/register",
        headers=headers,
        json={"manifest_ref": "crawler-sources.json"},
    )
    claim_response = client.post(
        f"/api/v1/internal/source-registries/{provider.registry_id}/sources/public-ats/claim",
        headers=headers,
        json={"worker_id": "operator@example", "lease_seconds": 60},
    )

    assert register_response.status_code == 409
    assert claim_response.status_code == 409
    assert register_response.json()["error"]["code"] == "CONFLICT"
    assert claim_response.json()["error"]["code"] == "CONFLICT"
    assert register_response.json()["error"]["message"] == "Request could not be processed"
    assert claim_response.json()["error"]["message"] == "Request could not be processed"
    assert "secret" not in register_response.text
    assert "secret" not in claim_response.text


@pytest.mark.parametrize(
    ("sqlstate", "expected_status", "expected_code"),
    [
        ("55000", 409, "CONFLICT"),
        ("22023", 400, "BAD_REQUEST"),
    ],
)
def test_source_registry_operator_maps_known_database_states_without_sql(
    sqlstate: str,
    expected_status: int,
    expected_code: str,
) -> None:
    provider = SqlStateSourceRegistryProvider(sqlstate)
    client = make_authenticated_console_client(
        RecordingAutopilotControlPlane(),
        source_registry_provider=provider,
    )

    response = client.post(
        f"/api/v1/internal/source-registries/{provider.registry_id}/sources/public-ats/claim",
        headers=SOURCE_REGISTRY_MUTATION_HEADERS,
        json={"worker_id": "operator@example", "lease_seconds": 60},
    )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert response.json()["error"]["message"] == "Request could not be processed"
    assert "SECRET" not in response.text


def test_source_registry_operator_hides_unknown_database_errors() -> None:
    provider = SqlStateSourceRegistryProvider("XX000")
    client = make_authenticated_console_client(
        RecordingAutopilotControlPlane(),
        source_registry_provider=provider,
        raise_server_exceptions=False,
    )

    response = client.post(
        f"/api/v1/internal/source-registries/{provider.registry_id}/sources/public-ats/claim",
        headers=SOURCE_REGISTRY_MUTATION_HEADERS,
        json={"worker_id": "operator@example", "lease_seconds": 60},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["message"] == "An internal error occurred"
    assert "SECRET" not in response.text


def test_source_registry_operator_forwards_due_claim_complete_and_fail() -> None:
    provider = RecordingSourceRegistryProvider()
    client = make_authenticated_console_client(
        RecordingAutopilotControlPlane(),
        source_registry_provider=provider,
    )
    registry_id = str(provider.registry_id)
    lease_token = str(provider.lease_token)

    due_response = client.get(f"/api/v1/internal/source-registries/{registry_id}/due?limit=5")
    claim_response = client.post(
        f"/api/v1/internal/source-registries/{registry_id}/sources/public-ats/claim",
        headers=SOURCE_REGISTRY_MUTATION_HEADERS,
        json={"worker_id": "operator@example", "lease_seconds": 60},
    )
    complete_response = client.post(
        f"/api/v1/internal/source-registries/{registry_id}/sources/public-ats/complete",
        headers=SOURCE_REGISTRY_MUTATION_HEADERS,
        json={
            "run_id": str(provider.run_id),
            "source_row_id": str(provider.source_row_id),
            "worker_id": "operator@example",
            "lease_token": lease_token,
            "output_manifest_sha256": "e" * 64,
            "cursor": "page-2",
            "result": "ok",
        },
    )
    fail_response = client.post(
        f"/api/v1/internal/source-registries/{registry_id}/sources/public-ats/fail",
        headers=SOURCE_REGISTRY_MUTATION_HEADERS,
        json={
            "run_id": str(provider.run_id),
            "source_row_id": str(provider.source_row_id),
            "worker_id": "operator@example",
            "lease_token": lease_token,
            "error": "temporary upstream refusal",
        },
    )

    assert due_response.status_code == 200
    assert due_response.json()["due_sources"][0]["source_id"] == "public-ats"
    assert claim_response.status_code == 200
    assert claim_response.json()["lease_token"] == lease_token
    assert claim_response.json()["run_id"] == str(provider.run_id)
    assert claim_response.json()["source_row_id"] == str(provider.source_row_id)
    assert claim_response.json()["lease_owner"] == "operator@example"
    assert complete_response.status_code == 200
    assert complete_response.json()["status"] == "completed"
    assert complete_response.json()["run_id"] == str(provider.run_id)
    assert complete_response.json()["source_row_id"] == str(provider.source_row_id)
    assert complete_response.json()["lease_owner"] == "operator@example"
    assert complete_response.json()["output_manifest_sha256"] == "e" * 64
    assert complete_response.json()["cursor"] == "page-2"
    assert fail_response.status_code == 200
    assert fail_response.json()["status"] == "failed"
    assert fail_response.json()["run_id"] == str(provider.run_id)
    assert fail_response.json()["source_row_id"] == str(provider.source_row_id)
    assert fail_response.json()["lease_owner"] == "operator@example"
    assert [call[0] for call in provider.calls] == ["due", "claim", "complete", "fail"]


def test_source_registry_operator_forwards_bounded_public_ats_ingestion() -> None:
    provider = RecordingSourceRegistryProvider()
    client = make_authenticated_console_client(
        RecordingAutopilotControlPlane(),
        source_registry_provider=provider,
    )
    endpoint = (
        f"/api/v1/internal/source-registries/{provider.registry_id}/sources/public-ats/"
        f"runs/{provider.run_id}/ingest-public-ats"
    )

    response = client.post(
        endpoint,
        headers=SOURCE_REGISTRY_MUTATION_HEADERS,
        json={"source_row_id": str(provider.source_row_id), "max_records": 3},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ingested",
        "registry_id": str(provider.registry_id),
        "source_row_id": str(provider.source_row_id),
        "source_id": "public-ats",
        "run_id": str(provider.run_id),
        "run_event_id": str(provider.run_event_id),
        "observed_records": 3,
        "inserted_versions": 2,
        "reused_versions": 1,
    }
    assert provider.calls == [
        (
            "ingest-public-ats",
            (
                provider.registry_id,
                "public-ats",
                provider.run_id,
                provider.source_row_id,
                ACTOR_ID,
                3,
            ),
        )
    ]

    forbidden = client.post(
        endpoint,
        headers=SOURCE_REGISTRY_MUTATION_HEADERS,
        json={
            "source_row_id": str(provider.source_row_id),
            "max_records": 3,
            "artifact_path": "/tmp/untrusted.jsonl",
        },
    )

    assert forbidden.status_code == 422
    assert len(provider.calls) == 1


def test_create_app_forwards_injected_crawler_provider_to_authenticated_review_page() -> None:
    control_plane = RecordingAutopilotControlPlane()
    crawler_provider = RecordingCrawlerExecutionProvider()
    client = make_authenticated_console_client(control_plane, crawler_provider)

    response = client.get("/crawlers/review")

    assert response.status_code == 200
    assert crawler_provider.pending_actor_ids == [ACTOR_ID]
    assert crawler_provider.approvals == 0
    assert control_plane.review_actor_ids == []
    assert "CRAWLER_EXECUTION_CONSOLE_DISABLED" in response.text


def test_interactive_docs_are_disabled_in_production() -> None:
    response = make_client(RuntimeEnvironment.PRODUCTION).get("/docs")

    assert response.status_code == 404
