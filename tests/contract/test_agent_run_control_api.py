"""Contract tests for agent run retry / stages / stop endpoints (qa7).

The review-only agents execute runs synchronously in-process, so:
- retry replays the stored input into a NEW run (different idempotency key)
  and only for {failed, abstained, unavailable, cancelled} (409 otherwise);
- stages is always empty (no workflow stage events);
- stop only cancels a PENDING run (409 for RUNNING/terminal).

Services are wired onto ``app.state`` over in-memory repos, mirroring the
RuntimeResources wiring in ``create_app``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import httpx2
from fastapi.testclient import TestClient

from careerops.api.app import create_app
from careerops.application.agent_runtime import AgentInputBundle, AgentRuntime
from careerops.application.agent_services import InterviewPreparationService, ResumeReviewService
from careerops.config import RuntimeEnvironment, Settings
from careerops.domain.agent_runs import AgentCapability
from careerops.domain.applications import ConfirmationStatus, ResumeParseStatus, ResumeVersion
from careerops.domain.candidates import EvidenceItem, EvidenceKind
from careerops.domain.jobs import CanonicalJob, JobPosting, JobPostingVersion
from careerops.infrastructure.agent_run_memory import InMemoryAgentRunRepository
from careerops.infrastructure.memory_repos import (
    InMemoryApplicationRepository,
    InMemoryEvidenceRepository,
    InMemoryJobReadRepository,
    InMemoryProfileRepository,
)
from careerops.model_gateway.base import StructuredModelRequest, StructuredModelResponse
from careerops.orchestration.capability_resolver import SettingsCapabilityResolver

CANDIDATE = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime(2026, 7, 30, tzinfo=UTC)

RESUME = uuid4()
JOB = uuid4()
JOB_VERSION = uuid4()
POSTING = uuid4()
EVIDENCE = uuid4()


class _FixedReadinessProbe:
    async def check(self):
        return SimpleNamespace(checks={})

    async def close(self) -> None:
        return None


class _PermissiveCandidateService:
    """Lets any well-formed candidate id through the ``path_candidate_id`` gate."""

    def get(self, candidate_id: UUID):
        return SimpleNamespace(id=candidate_id)


class _ToggleModel:
    """Fake structured model; fails the first ``fail_count`` invocations."""

    is_enabled = True

    def __init__(self, *, fail_count: int = 0) -> None:
        self.fail_count = fail_count
        self.requests: list[StructuredModelRequest] = []

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:
        self.requests.append(request)
        if self.fail_count > 0:
            self.fail_count -= 1
            raise RuntimeError("provider boom")
        result = (
            {
                "strengths": [{"evidence_id": str(EVIDENCE), "statement": "Python"}],
                "gaps": [],
                "risk_flags": [],
                "recommendations": [],
                "confidence": 0.9,
            }
            if request.task_type == "resume_review"
            else {
                "focus_areas": ["Python"],
                "questions": ["Describe a Python project."],
                "star_prompts": ["Prepare a Python STAR example."],
                "uncertainties": [],
                "confidence": 0.9,
            }
        )
        return StructuredModelResponse(
            task_type=request.task_type,
            result=result,
            model_id="test-model",
            input_tokens=10,
            output_tokens=5,
            trace_id=request.trace_id,
        )


def _seed() -> tuple[
    InMemoryApplicationRepository,
    InMemoryEvidenceRepository,
    InMemoryProfileRepository,
    InMemoryJobReadRepository,
]:
    resumes = InMemoryApplicationRepository()
    evidence = InMemoryEvidenceRepository()
    profiles = InMemoryProfileRepository()
    jobs = InMemoryJobReadRepository()
    resume = ResumeVersion(
        id=RESUME,
        candidate_id=CANDIDATE,
        version_number=1,
        file_reference="sha256/resume",
        content_hash="a" * 64,
        parse_status=ResumeParseStatus.PARSED,
        confirmation_status=ConfirmationStatus.CONFIRMED,
        created_at=NOW,
    )
    resumes.save_resume(resume)
    evidence.store(
        EvidenceItem(
            id=EVIDENCE,
            candidate_id=CANDIDATE,
            kind=EvidenceKind.SKILL,
            name="Python",
            description="Confirmed Python work",
            source_span="Python service",
            confirmation_status=ConfirmationStatus.CONFIRMED,
            evidence_hash="b" * 64,
            resume_version_id=RESUME,
            created_at=NOW,
        )
    )
    jobs.save_canonical_job(
        CanonicalJob(
            id=JOB,
            company_id=uuid4(),
            canonical_title="Backend Engineer",
            normalized_title="backend engineer",
            created_at=NOW,
        )
    )
    jobs.save_posting(
        JobPosting(
            id=POSTING,
            source_id=uuid4(),
            external_id="job-1",
            canonical_url="https://jobs.example.test/1",
            created_at=NOW,
        )
    )
    jobs.save_posting_assignment(POSTING, JOB, uuid4())
    jobs.save_version(
        JobPostingVersion(
            id=JOB_VERSION,
            job_posting_id=POSTING,
            content_hash="c" * 64,
            structured_data={
                "title": "Backend Engineer",
                "requirements": ["Python", "Kubernetes"],
                "description": "Backend engineering at example",
            },
            captured_at=NOW,
        )
    )
    return resumes, evidence, profiles, jobs


def _make_client(
    *, fail_count: int = 0,
) -> tuple[httpx2.Client, AgentRuntime, _ToggleModel]:
    resumes, evidence, profiles, jobs = _seed()
    model = _ToggleModel(fail_count=fail_count)
    settings = Settings.model_validate(
        {"environment": RuntimeEnvironment.TEST, "model_tailoring_enabled": True}
    )
    repo = InMemoryAgentRunRepository()
    runtime = AgentRuntime(repo, capability_resolver=SettingsCapabilityResolver(settings))
    kwargs = {
        "runtime": runtime,
        "resume_repository": resumes,
        "evidence_repository": evidence,
        "profile_repository": profiles,
        "job_repository": jobs,
        "model_client": model,
    }
    resume_service = ResumeReviewService(**kwargs)
    interview_service = InterviewPreparationService(**kwargs)

    probe = _FixedReadinessProbe()
    app = create_app(settings, readiness_probe=probe)
    app.state.candidate_service = _PermissiveCandidateService()
    app.state.agent_runtime = runtime
    app.state.resume_review_service = resume_service
    app.state.interview_preparation_service = interview_service
    return cast("httpx2.Client", TestClient(app)), runtime, model


def _start_body() -> dict[str, object]:
    return {
        "resume_version_id": str(RESUME),
        "canonical_job_id": str(JOB),
        "job_version_id": str(JOB_VERSION),
        "evidence_ids": [str(EVIDENCE)],
        # Non-empty on purpose: user_context is hash-only and NOT replayed,
        # so the retried run's input_hash must differ from the original.
        "user_context": "Focus on distributed systems.",
    }


def _start_run(client: httpx2.Client, agent: str) -> httpx2.Response:
    return client.post(f"/api/v1/candidates/{CANDIDATE}/agents/{agent}", json=_start_body())


def _run_url(run_id: str, action: str = "") -> str:
    base = f"/api/v1/candidates/{CANDIDATE}/agents/runs/{run_id}"
    return f"{base}/{action}" if action else base


def _error_code(body: dict[str, object]) -> str:
    error = body["error"]
    assert isinstance(error, dict)
    return str(error.get("code", ""))


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/retry
# ---------------------------------------------------------------------------


class TestRetryEndpoint:
    def test_retry_failed_run_replays_input_into_new_run(self) -> None:
        client, _, _ = _make_client(fail_count=1)
        original = client.post(
            f"/api/v1/candidates/{CANDIDATE}/agents/resume-review", json=_start_body()
        )
        assert original.status_code == 200
        original_body = original.json()
        assert original_body["state"] == "abstained"

        response = client.post(
            _run_url(str(original_body["id"]), "retry"),
            json={"expected_state": "abstained"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["id"] != original_body["id"]
        assert body["state"] == "succeeded"
        assert body["capability"] == "resume_review"
        assert body["idempotency_key"] != original_body["idempotency_key"]
        assert body["input_hash"] != original_body["input_hash"]
        assert body["evidence_ids"] == original_body["evidence_ids"]
        for key, value in original_body["input_identities"].items():
            if key == "user_context_hash":
                continue
            assert body["input_identities"][key] == value

    def test_retry_rejects_succeeded_run_with_409(self) -> None:
        client, _, _ = _make_client()
        run = _start_run(client, "resume-review")
        assert run.status_code == 200 and run.json()["state"] == "succeeded"

        response = client.post(_run_url(str(run.json()["id"]), "retry"), json={})

        assert response.status_code == 409
        assert _error_code(response.json()) == "INVALID_STATE"

    def test_retry_rejects_stale_expected_state_with_409(self) -> None:
        client, _, _ = _make_client()
        run = _start_run(client, "resume-review")
        assert run.status_code == 200

        response = client.post(
            _run_url(str(run.json()["id"]), "retry"),
            json={"expected_state": "failed"},
        )

        assert response.status_code == 409
        assert _error_code(response.json()) == "INVALID_STATE"

    def test_retry_unknown_run_returns_404(self) -> None:
        client, _, _ = _make_client()
        response = client.post(_run_url(str(uuid4()), "retry"), json={})
        assert response.status_code == 404

    def test_retry_interview_run_dispatches_to_interview_service(self) -> None:
        client, _, _ = _make_client(fail_count=1)
        run = _start_run(client, "interview-preparation")
        assert run.status_code == 200 and run.json()["state"] == "abstained"

        response = client.post(_run_url(str(run.json()["id"]), "retry"), json={})

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "succeeded"
        assert body["capability"] == "interview_preparation"
        assert body["id"] != run.json()["id"]

    def test_retry_unavailable_run_stays_unavailable_until_released(self) -> None:
        settings = Settings.model_validate(
            {"environment": RuntimeEnvironment.TEST, "model_tailoring_enabled": False}
        )
        resumes, evidence, profiles, jobs = _seed()
        repo = InMemoryAgentRunRepository()
        runtime = AgentRuntime(repo, capability_resolver=SettingsCapabilityResolver(settings))
        model = _ToggleModel()
        kwargs = {
            "runtime": runtime,
            "resume_repository": resumes,
            "evidence_repository": evidence,
            "profile_repository": profiles,
            "job_repository": jobs,
            "model_client": model,
        }
        probe = _FixedReadinessProbe()
        app = create_app(settings, readiness_probe=probe)
        app.state.candidate_service = _PermissiveCandidateService()
        app.state.agent_runtime = runtime
        app.state.resume_review_service = ResumeReviewService(**kwargs)
        app.state.interview_preparation_service = InterviewPreparationService(**kwargs)
        client = cast("httpx2.Client", TestClient(app))

        run = client.post(
            f"/api/v1/candidates/{CANDIDATE}/agents/resume-review", json=_start_body()
        )
        assert run.status_code == 200 and run.json()["state"] == "unavailable"

        response = client.post(_run_url(str(run.json()["id"]), "retry"), json={})

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "unavailable"
        assert body["id"] != run.json()["id"]


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/stages
# ---------------------------------------------------------------------------


class TestStagesEndpoint:
    def test_stages_are_empty_for_synchronous_runs(self) -> None:
        client, _, _ = _make_client()
        run = _start_run(client, "resume-review")
        assert run.status_code == 200

        response = client.get(_run_url(str(run.json()["id"]), "stages"))

        assert response.status_code == 200
        body = response.json()
        assert body == {"items": [], "total": 0}

    def test_stages_unknown_run_returns_404(self) -> None:
        client, _, _ = _make_client()
        response = client.get(_run_url(str(uuid4()), "stages"))
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/stop
# ---------------------------------------------------------------------------


class TestStopEndpoint:
    def test_stop_cancels_a_pending_run(self) -> None:
        client, runtime, _ = _make_client()
        pending = runtime.start(
            CANDIDATE,
            AgentCapability.RESUME_REVIEW,
            AgentInputBundle(identities={"resume_version_id": str(RESUME)}),
            schema_version="agent-output-v1",
            prompt_version="resume_review-v1",
        )
        assert pending.state.value == "pending"

        response = client.post(
            _run_url(str(pending.id), "stop"),
            json={"expected_state": "pending", "reason_code": "user_requested"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(pending.id)
        assert body["state"] == "cancelled"
        assert body["error_category"] == "user_requested"
        assert body["finished_at"] is not None

    def test_stop_without_body_defaults_reason(self) -> None:
        client, runtime, _ = _make_client()
        pending = runtime.start(
            CANDIDATE,
            AgentCapability.RESUME_REVIEW,
            AgentInputBundle(identities={"resume_version_id": str(RESUME)}),
            schema_version="agent-output-v1",
            prompt_version="resume_review-v1",
        )
        response = client.post(_run_url(str(pending.id), "stop"))
        assert response.status_code == 200
        assert response.json()["error_category"] == "user_requested"

    def test_stop_rejects_terminal_run_with_409(self) -> None:
        client, _, _ = _make_client()
        run = _start_run(client, "resume-review")
        assert run.status_code == 200 and run.json()["state"] == "succeeded"

        response = client.post(_run_url(str(run.json()["id"]), "stop"), json={})

        assert response.status_code == 409
        assert _error_code(response.json()) == "INVALID_STATE"

    def test_stop_rejects_stale_expected_state_with_409(self) -> None:
        client, _, _ = _make_client()
        run = _start_run(client, "resume-review")
        assert run.status_code == 200

        response = client.post(
            _run_url(str(run.json()["id"]), "stop"),
            json={"expected_state": "pending"},
        )

        assert response.status_code == 409

    def test_stop_unknown_run_returns_404(self) -> None:
        client, _, _ = _make_client()
        response = client.post(_run_url(str(uuid4()), "stop"), json={})
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# OpenAPI surface
# ---------------------------------------------------------------------------


class TestOpenAPIRunControl:
    def test_run_control_routes_are_in_openapi(self) -> None:
        client, _, _ = _make_client()
        response = client.get("/api/v1/openapi.json")
        assert response.status_code == 200
        paths = set(response.json()["paths"].keys())
        for route in (
            "/api/v1/candidates/{candidate_id}/agents/runs/{run_id}/retry",
            "/api/v1/candidates/{candidate_id}/agents/runs/{run_id}/stages",
            "/api/v1/candidates/{candidate_id}/agents/runs/{run_id}/stop",
        ):
            assert route in paths, f"Required route missing from OpenAPI: {route}"
