import re
from collections.abc import Mapping
from typing import cast

import httpx2
from fastapi import HTTPException
from fastapi.testclient import TestClient

from careerops.api.app import create_app
from careerops.application.ports.readiness import ReadinessReport, ReadinessState
from careerops.config import RuntimeEnvironment, Settings


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
            "retryable": False,
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
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["message"] == "Request could not be processed"


def test_openapi_is_versioned_and_contains_only_declared_health_routes() -> None:
    response = make_client().get("/api/v1/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "CareerOps API"
    # Declared route set covers health + jobs + matching + applications + auth
    # (SPA session flow: preauth/session/me/login/bootstrap/logout) + email
    # drafting (email-draft/send-email) added by the e2e-career-application-loop
    # change, plus the Section-3 additive profile/resumes/evidence routers.
    # ErrorResponse business codes are validated separately by the Phase 0
    # contract freeze tests.
    assert set(response.json()["paths"]) == {
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/preauth",
        "/api/v1/auth/bootstrap",
        "/api/v1/auth/session",
        "/api/v1/me",
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/api/v1/companies",
        "/api/v1/companies/{company_id}/contacts",
        "/api/v1/contacts",
        "/api/v1/jobs",
        "/api/v1/jobs/{job_id}",
        "/api/v1/jobs/{job_id}/remote-eligibility",
        "/api/v1/jobs/{job_id}/compensation",
        "/api/v1/candidates",
        "/api/v1/candidates/{candidate_id}/evidence",
        "/api/v1/evidence/import",
        "/api/v1/matches",
        "/api/v1/matches/run",
        "/api/v1/applications",
        "/api/v1/applications/{application_id}/transition",
        "/api/v1/applications/{application_id}/submit",
        "/api/v1/applications/{application_id}/events",
        "/api/v1/applications/{application_id}/email-draft",
        "/api/v1/applications/{application_id}/send-email",
        "/api/v1/resume-versions",
        "/api/v1/application-packages",
        "/api/v1/follow-ups",
        # Section-3 additive routers (career-profile-and-resume spec).
        "/api/v1/profile",
        "/api/v1/profile/versions",
        "/api/v1/profile/versions/{version_id}",
        "/api/v1/profile/versions/{version_id}/activate",
        "/api/v1/resumes",
        "/api/v1/resumes/eligible",
        "/api/v1/resumes/{version_id}",
        "/api/v1/resumes/{version_id}/confirm",
        "/api/v1/resumes/{version_id}/evidence",
        "/api/v1/evidence",
        "/api/v1/evidence/{evidence_id}",
        "/api/v1/evidence/{evidence_id}/confirm",
        "/api/v1/evidence/{evidence_id}/reject",
        # Section-4 additive routers (crawl-plan-management spec).
        "/api/v1/crawl-sources",
        "/api/v1/crawl-sources/{source_id}",
        "/api/v1/crawl-sources/{source_id}/pause",
        "/api/v1/crawl-sources/{source_id}/resume",
        "/api/v1/crawl-plans",
        "/api/v1/crawl-plans/versions",
        "/api/v1/crawl-plans/versions/{version_id}",
        "/api/v1/crawl-plans/versions/{version_id}/activate",
        "/api/v1/crawl-plans/pause",
        "/api/v1/crawl-plans/resume",
        "/api/v1/crawl-plans/run-now",
        "/api/v1/crawl-runs",
        "/api/v1/crawl-runs/{run_id}",
    }


def test_interactive_docs_are_disabled_in_production() -> None:
    response = make_client(RuntimeEnvironment.PRODUCTION).get("/docs")

    assert response.status_code == 404
