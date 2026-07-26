"""Contract tests for M2 API endpoints: candidates, evidence, matching, compensation, UI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import uuid4

import httpx2
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


def make_client() -> httpx2.Client:
    settings = Settings.model_validate({"environment": RuntimeEnvironment.TEST})
    return cast(
        "httpx2.Client", TestClient(create_app(settings, readiness_probe=FixedReadinessProbe()))
    )


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

    def test_list_candidate_evidence_returns_empty_without_repository(self) -> None:
        candidate_id = str(uuid4())
        response = make_client().get(f"/api/v1/candidates/{candidate_id}/evidence")
        assert response.status_code == 200
        assert response.json() == []


class TestEvidenceImportAPI:
    def test_import_evidence_returns_503_without_service(self) -> None:
        response = make_client().post(
            "/api/v1/evidence/import",
            json={
                "candidate_id": str(uuid4()),
                "kind": "skill",
                "name": "Python",
            },
        )
        assert response.status_code == 503


class TestMatchesAPI:
    def test_list_matches_returns_empty_without_repository(self) -> None:
        response = make_client().get("/api/v1/matches")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_run_match_returns_503_without_orchestrator(self) -> None:
        response = make_client().post(
            "/api/v1/matches/run",
            json={
                "candidate_id": str(uuid4()),
                "canonical_job_id": str(uuid4()),
            },
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


class TestMatchingUI:
    def test_matches_page_renders_empty(self) -> None:
        response = make_client().get("/matches")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "匹配结果" in response.text

    def test_matches_page_cache_control(self) -> None:
        response = make_client().get("/matches")
        assert response.headers["cache-control"] == "no-store"

    def test_match_detail_returns_503_without_repository(self) -> None:
        response = make_client().get(f"/matches/{uuid4()}")
        assert response.status_code == 503
        assert "服务不可用" in response.text
