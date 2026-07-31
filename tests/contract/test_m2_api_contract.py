"""Contract tests for M2 API endpoints: candidates, evidence, matching, compensation, UI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID, uuid4

import httpx2
from fastapi.testclient import TestClient

from careerops.api.app import create_app
from careerops.application.ports.readiness import ReadinessReport, ReadinessState
from careerops.config import RuntimeEnvironment, Settings
from careerops.orchestration.capability_resolver import SettingsCapabilityResolver

TEST_CANDIDATE_ID = UUID("11111111-1111-4111-8111-111111111111")


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


def make_client() -> httpx2.Client:
    settings = Settings.model_validate({"environment": RuntimeEnvironment.TEST})
    app = create_app(settings, readiness_probe=FixedReadinessProbe())
    # No session/CSRF auth anymore (auth-rm Task 9); the capability resolver
    # stays wired for capability-gated routes.
    app.state.capability_resolver = SettingsCapabilityResolver(settings)
    return cast("httpx2.Client", TestClient(app))


class TestCandidatesAPI:
    def test_list_candidates_returns_empty_without_repository(self) -> None:
        response = make_client().get("/api/v1/candidates")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_candidates_cache_control(self) -> None:
        response = make_client().get("/api/v1/candidates")
        assert response.headers["cache-control"] == "no-store"

    def test_list_candidate_evidence_503_without_service(self) -> None:
        # The evidence route is the canonical evidence.py handler, which fails
        # closed (503) when evidence_service is not wired (auth-rm Task 9
        # removed the matching.py shadow that returned a bare empty list).
        candidate_id = str(TEST_CANDIDATE_ID)
        response = make_client().get(f"/api/v1/candidates/{candidate_id}/evidence")
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "DEPENDENCY_NOT_READY"


class TestEvidenceImportAPI:
    def test_import_evidence_returns_503_without_service(self) -> None:
        response = make_client().post(
            f"/api/v1/candidates/{TEST_CANDIDATE_ID}/evidence/import",
            json={
                "kind": "skill",
                "name": "Python",
            },
        )
        assert response.status_code == 503


class TestMatchesAPI:
    def test_list_matches_returns_empty_without_repository(self) -> None:
        response = make_client().get(f"/api/v1/candidates/{TEST_CANDIDATE_ID}/matches")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_run_match_returns_503_without_orchestrator(self) -> None:
        response = make_client().post(
            f"/api/v1/candidates/{TEST_CANDIDATE_ID}/matches/run",
            json={"canonical_job_id": str(uuid4())},
        )
        assert response.status_code == 503


class TestRemoteEligibilityAPI:
    def test_remote_eligibility_returns_error_without_repository(self) -> None:
        # When the matching repository is not wired, the route raises
        # DependencyNotReadyError → 503 with the standard ErrorResponse envelope
        # (Phase 0 contract: dependency-not-ready MUST use the error envelope).
        job_id = str(uuid4())
        response = make_client().get(f"/api/v1/jobs/{job_id}/remote-eligibility")
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "DEPENDENCY_NOT_READY"
        assert body["error"]["retryable"] is True


class TestCompensationAPI:
    def test_compensation_returns_error_without_repository(self) -> None:
        # Same dependency-not-ready contract as remote-eligibility above.
        job_id = str(uuid4())
        response = make_client().get(f"/api/v1/jobs/{job_id}/compensation")
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "DEPENDENCY_NOT_READY"
        assert body["error"]["retryable"] is True
