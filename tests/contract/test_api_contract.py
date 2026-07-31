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
    # Declared route set covers health + global catalog (jobs/companies) +
    # applications + auth (SPA session flow) + the per-candidate path-scoped
    # routers (profile/evidence/crawl surface — auth-rm migration) + the
    # remaining session-scoped routers (resumes, agent-console, mail, …).
    # ErrorResponse business codes are validated separately by the Phase 0
    # contract freeze tests.
    assert set(response.json()["paths"]) == {
        # Auth + console identity (SPA session flow).
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/preauth",
        "/api/v1/auth/bootstrap",
        "/api/v1/auth/session",
        "/api/v1/me",
        # Health.
        "/api/v1/health/live",
        "/api/v1/health/ready",
        # Global catalog: companies / contacts / jobs.
        "/api/v1/companies",
        "/api/v1/companies/{company_id}/contacts",
        "/api/v1/contacts",
        "/api/v1/jobs",
        "/api/v1/jobs/{job_id}",
        "/api/v1/jobs/{job_id}/remote-eligibility",
        "/api/v1/jobs/{job_id}/compensation",
        # Candidates (global list/create + per-candidate path-scoped routers
        # added by the auth-rm migration: profile, evidence, crawl surface).
        "/api/v1/candidates",
        "/api/v1/candidates/{candidate_id}",
        "/api/v1/candidates/{candidate_id}/profile",
        "/api/v1/candidates/{candidate_id}/profile/versions",
        "/api/v1/candidates/{candidate_id}/profile/versions/{version_id}",
        "/api/v1/candidates/{candidate_id}/profile/versions/{version_id}/activate",
        "/api/v1/candidates/{candidate_id}/evidence",
        "/api/v1/candidates/{candidate_id}/evidence/{evidence_id}",
        "/api/v1/candidates/{candidate_id}/evidence/{evidence_id}/confirm",
        "/api/v1/candidates/{candidate_id}/evidence/{evidence_id}/reject",
        # Per-candidate crawl routers (crawl-plan-management spec).
        "/api/v1/candidates/{candidate_id}/crawl-sources",
        "/api/v1/candidates/{candidate_id}/crawl-sources/{source_id}",
        "/api/v1/candidates/{candidate_id}/crawl-sources/{source_id}/pause",
        "/api/v1/candidates/{candidate_id}/crawl-sources/{source_id}/resume",
        "/api/v1/candidates/{candidate_id}/crawl-sources/{source_id}/permissions",
        "/api/v1/candidates/{candidate_id}/crawl-plans",
        "/api/v1/candidates/{candidate_id}/crawl-plans/versions",
        "/api/v1/candidates/{candidate_id}/crawl-plans/versions/{version_id}",
        "/api/v1/candidates/{candidate_id}/crawl-plans/versions/{version_id}/activate",
        "/api/v1/candidates/{candidate_id}/crawl-plans/pause",
        "/api/v1/candidates/{candidate_id}/crawl-plans/resume",
        "/api/v1/candidates/{candidate_id}/crawl-plans/run-now",
        "/api/v1/candidates/{candidate_id}/crawl-plans/readiness",
        "/api/v1/candidates/{candidate_id}/crawl-plans/empty-state-cta",
        "/api/v1/candidates/{candidate_id}/crawl-plans/scope-preview",
        "/api/v1/candidates/{candidate_id}/crawl-runs",
        "/api/v1/candidates/{candidate_id}/crawl-runs/{run_id}",
        "/api/v1/candidates/{candidate_id}/crawl-permissions",
        "/api/v1/candidates/{candidate_id}/crawl-permissions/{permission_id}",
        "/api/v1/candidates/{candidate_id}/crawl-permissions/{permission_id}/grant",
        "/api/v1/candidates/{candidate_id}/crawl-permissions/{permission_id}/deny",
        "/api/v1/candidates/{candidate_id}/crawl-permissions/{permission_id}/revoke",
        "/api/v1/candidates/{candidate_id}/crawl-permissions/{permission_id}/open-login",
        # Matching + inbox projection.
        "/api/v1/matches",
        "/api/v1/matches/run",
        "/api/v1/inbox",
        "/api/v1/inbox/{job_id}",
        "/api/v1/inbox/{job_id}/excluded-reasons",
        "/api/v1/inbox/{job_id}/favorite",
        "/api/v1/inbox/{job_id}/ignore",
        "/api/v1/inbox/{job_id}/snooze",
        # Per-candidate applications + workspace sub-routes (auth-rm path-param
        # migration: applications, application_workspace, email_payloads,
        # system_send, reply_drafts routers).
        "/api/v1/candidates/{candidate_id}/applications",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/transition",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/submit",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/events",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/email-draft",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/send-email",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/prepare",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/channels",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/channel",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/package",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/timeline",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/confirm-external-submission",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/state",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/packages",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/packages/latest",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/packages/{version_id}/approve",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/packages/{version_id}/edits",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/recruiting-contacts",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/submission-preview",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/system-send",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/system-send/{intent_id}",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/system-send/{intent_id}/reconcile",
        "/api/v1/candidates/{candidate_id}/applications/{application_id}/follow-ups",
        "/api/v1/candidates/{candidate_id}/application-packages",
        "/api/v1/candidates/{candidate_id}/resume-versions",
        "/api/v1/candidates/{candidate_id}/follow-ups",
        "/api/v1/candidates/{candidate_id}/follow-ups/{reminder_id}/snooze",
        "/api/v1/candidates/{candidate_id}/follow-ups/{reminder_id}/reschedule",
        "/api/v1/candidates/{candidate_id}/follow-ups/{reminder_id}/cancel",
        "/api/v1/candidates/{candidate_id}/follow-ups/{reminder_id}/complete",
        # Resumes (global resume-version router; still session-scoped) + bulk
        # evidence import.
        "/api/v1/resumes",
        "/api/v1/resumes/eligible",
        "/api/v1/resumes/{version_id}",
        "/api/v1/resumes/{version_id}/confirm",
        "/api/v1/resumes/{version_id}/evidence",
        "/api/v1/evidence/import",
        # Recruiting-email intelligence (Gmail sync + proposals).
        "/api/v1/mail/account",
        "/api/v1/mail/account/revoke",
        "/api/v1/mail/sync-now",
        "/api/v1/mail/sync-history",
        "/api/v1/mail/threads",
        "/api/v1/mail/threads/{thread_id}/messages",
        "/api/v1/mail/unresolved-links",
        "/api/v1/mail/unresolved-links/{link_id}/confirm",
        "/api/v1/mail/messages/{message_id}/proposal",
        "/api/v1/mail/proposals",
        "/api/v1/mail/proposals/{proposal_id}",
        "/api/v1/mail/proposals/{proposal_id}/accept",
        "/api/v1/mail/proposals/{proposal_id}/reject",
        # Per-candidate reply drafts + follow-up rules (reply_drafts router).
        "/api/v1/candidates/{candidate_id}/reply/drafts",
        "/api/v1/candidates/{candidate_id}/reply/drafts/{draft_id}",
        "/api/v1/candidates/{candidate_id}/reply/drafts/{draft_id}/edit",
        "/api/v1/candidates/{candidate_id}/reply/drafts/{draft_id}/approve",
        "/api/v1/candidates/{candidate_id}/reply/drafts/{draft_id}/reject",
        "/api/v1/candidates/{candidate_id}/reply/drafts/{draft_id}/send",
        "/api/v1/candidates/{candidate_id}/reply/drafts/{draft_id}/send-status",
        "/api/v1/candidates/{candidate_id}/reply/follow-up-rules",
        # Review-only LLM agent runs (resume review / interview preparation).
        "/api/v1/agents/resume-review",
        "/api/v1/agents/interview-preparation",
        "/api/v1/agents/runs",
        "/api/v1/agents/runs/{run_id}",
        "/api/v1/agents/runs/{run_id}/review",
        "/api/v1/agents/runs/{run_id}/reviews",
        # Review-only smart-intake previews + draft decisions.
        "/api/v1/smart-intake/capability",
        "/api/v1/smart-intake/previews",
        "/api/v1/smart-intake/previews/{preview_id}",
        "/api/v1/smart-intake/previews/{preview_id}/apply",
        # Agent-console orchestration + preflight.
        "/api/v1/agent-console/actions",
        "/api/v1/agent-console/actions/{action_key}/accept",
        "/api/v1/agent-console/actions/{action_key}/snooze",
        "/api/v1/agent-console/actions/{action_key}/dismiss",
        "/api/v1/agent-console/actions/{action_key}/complete",
        "/api/v1/agent-console/contexts",
        "/api/v1/agent-console/contexts/{context_id}",
        "/api/v1/agent-console/preflight",
        "/api/v1/capabilities/agent",
        # Notification outlet (SSE stream + recovery).
        "/api/v1/notifications",
        "/api/v1/notifications/stream",
    }


def test_interactive_docs_are_disabled_in_production() -> None:
    response = make_client(RuntimeEnvironment.PRODUCTION).get("/docs")

    assert response.status_code == 404
