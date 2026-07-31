"""Agent run control: retry / stop / stages semantics (qa7).

Covers the synchronous execution model:
- retry replays the stored input into a NEW run with a different
  idempotency key, and only for {failed, abstained, unavailable, cancelled};
- retrying the same source run twice is idempotent; retrying a retry gets a
  fresh key;
- stop only cancels a PENDING run (RUNNING/terminal are 409);
- the stage timeline is always empty for synchronous runs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from careerops.api.errors import InvalidStateError, NotFoundError
from careerops.application.agent_runtime import (
    AgentInputBundle,
    AgentRuntime,
    canonical_input_hash,
)
from careerops.application.agent_services import (
    AgentStartInput,
    InterviewPreparationService,
    ResumeReviewService,
)
from careerops.config import RuntimeEnvironment, Settings
from careerops.domain.agent_runs import (
    AgentCapability,
    AgentReviewDecision,
    AgentRun,
    AgentRunState,
)
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


def _pair(
    *,
    enabled: bool = True,
    fail_count: int = 0,
) -> tuple[
    ResumeReviewService,
    InterviewPreparationService,
    InMemoryAgentRunRepository,
    AgentRuntime,
    _ToggleModel,
]:
    resumes, evidence, profiles, jobs = _seed()
    model = _ToggleModel(fail_count=fail_count)
    settings = Settings.model_validate(
        {"environment": RuntimeEnvironment.TEST, "model_tailoring_enabled": enabled}
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
    return (
        ResumeReviewService(**kwargs),
        InterviewPreparationService(**kwargs),
        repo,
        runtime,
        model,
    )


def _input() -> AgentStartInput:
    return AgentStartInput(
        resume_version_id=RESUME,
        canonical_job_id=JOB,
        job_version_id=JOB_VERSION,
        evidence_ids=(EVIDENCE,),
        user_context="Focus on distributed systems.",
    )


def _replayable_identities(identities: Mapping[str, str]) -> dict[str, str]:
    """Stored identities minus the non-replayable user-context hash."""
    return {key: value for key, value in dict(identities).items() if key != "user_context_hash"}


def _pending_run(runtime: AgentRuntime) -> AgentRun:
    return runtime.start(
        CANDIDATE,
        AgentCapability.RESUME_REVIEW,
        AgentInputBundle(identities={"a": "1"}),
        schema_version="agent-output-v1",
        prompt_version="resume_review-v1",
    )


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


class TestRetry:
    def test_retry_replays_stored_input_into_a_new_run(self) -> None:
        resume_service, _, _, _, model = _pair(fail_count=1)

        # A model provider failure yields ABSTAINED (the service catches the
        # exception); that is the real-world retryable path.
        original = resume_service.start(CANDIDATE, _input())
        assert original.state is AgentRunState.ABSTAINED

        retried = resume_service.retry(CANDIDATE, original.id)

        assert retried.id != original.id
        assert retried.state is AgentRunState.SUCCEEDED
        assert retried.capability is AgentCapability.RESUME_REVIEW
        assert retried.idempotency_key != original.idempotency_key
        # Everything replays except user_context (deliberately not persisted):
        # the retried run's identity is the empty-context hash.
        assert _replayable_identities(retried.input_identities) == _replayable_identities(
            original.input_identities
        )
        assert retried.input_identities["user_context_hash"] == hashlib.sha256(b"").hexdigest()
        assert retried.evidence_ids == original.evidence_ids
        assert retried.schema_version == original.schema_version
        assert retried.prompt_version == original.prompt_version
        assert len(model.requests) == 2  # original failed call + retry call

    def test_retry_failed_run_with_stored_identities(self) -> None:
        """A FAILED run (hard execution error) is retryable from stored input."""
        resume_service, _, repo, _, _ = _pair()
        run = resume_service.start(CANDIDATE, _input())
        failed = repo.update(replace(run, state=AgentRunState.FAILED))

        retried = resume_service.retry(CANDIDATE, failed.id)
        assert retried.id != failed.id
        assert retried.state is AgentRunState.SUCCEEDED
        assert _replayable_identities(retried.input_identities) == _replayable_identities(
            failed.input_identities
        )

    def test_retry_key_is_derived_from_the_source_run_and_idempotent(self) -> None:
        resume_service, _, _, _, _ = _pair(fail_count=100)  # everything abstains

        original = resume_service.start(CANDIDATE, _input())
        assert original.state is AgentRunState.ABSTAINED

        first = resume_service.retry(CANDIDATE, original.id)
        second = resume_service.retry(CANDIDATE, original.id)  # same source -> same run

        assert first.state is AgentRunState.ABSTAINED
        assert second.id == first.id
        # The retry key derives from the REPLAYED input hash (user_context is
        # dropped, so the hash differs from the original) + the source run id.
        replay_hash = canonical_input_hash(dict(first.input_identities))
        expected_key = f"{AgentCapability.RESUME_REVIEW.value}:{replay_hash}:retry:{original.id}"
        assert first.idempotency_key == expected_key

        # Retrying the retry run gets a fresh key / run.
        third = resume_service.retry(CANDIDATE, first.id)
        assert third.id not in {original.id, first.id}
        assert third.idempotency_key != first.idempotency_key
        assert third.idempotency_key == (
            f"{AgentCapability.RESUME_REVIEW.value}:{replay_hash}:retry:{first.id}"
        )

    def test_retry_interview_run_through_its_own_service(self) -> None:
        _, interview_service, _, _, _ = _pair(fail_count=1)

        original = interview_service.start(CANDIDATE, _input())
        assert original.state is AgentRunState.ABSTAINED

        retried = interview_service.retry(CANDIDATE, original.id)
        assert retried.id != original.id
        assert retried.capability is AgentCapability.INTERVIEW_PREPARATION
        assert retried.state is AgentRunState.SUCCEEDED
        assert retried.evidence_ids == original.evidence_ids

    def test_retry_rejects_run_from_another_capability(self) -> None:
        resume_service, interview_service, _, _, _ = _pair(fail_count=1)
        interview_run = interview_service.start(CANDIDATE, _input())
        with pytest.raises(InvalidStateError):
            resume_service.retry(CANDIDATE, interview_run.id)

    def test_retry_unavailable_run_stays_unavailable_while_capability_denied(self) -> None:
        resume_service, _, _, _, _ = _pair(enabled=False)

        original = resume_service.start(CANDIDATE, _input())
        assert original.state is AgentRunState.UNAVAILABLE

        retried = resume_service.retry(CANDIDATE, original.id)
        assert retried.id != original.id
        assert retried.state is AgentRunState.UNAVAILABLE
        assert retried.result["status"] == "unavailable"

    @pytest.mark.parametrize(
        "state",
        [
            AgentRunState.SUCCEEDED,
            AgentRunState.REVIEWED,
            AgentRunState.PENDING,
            AgentRunState.RUNNING,
            AgentRunState.STALE,
        ],
    )
    def test_retry_rejects_non_retryable_states(self, state: AgentRunState) -> None:
        resume_service, _, repo, runtime, _ = _pair()
        run = resume_service.start(CANDIDATE, _input())
        if state is AgentRunState.REVIEWED:
            reviewed = runtime.review(
                CANDIDATE,
                run.id,
                decision=AgentReviewDecision.ACCEPTED,
                actor_id=str(CANDIDATE),
            )
            assert reviewed.state is AgentRunState.REVIEWED
        elif state is AgentRunState.PENDING:
            repo.update(
                replace(
                    run,
                    state=AgentRunState.PENDING,
                    result={},
                    started_at=None,
                    finished_at=None,
                )
            )
        elif state is AgentRunState.RUNNING:
            repo.update(replace(run, state=AgentRunState.RUNNING, started_at=NOW))
        elif state is AgentRunState.STALE:
            repo.update(replace(run, state=AgentRunState.STALE))
        else:
            assert run.state is AgentRunState.SUCCEEDED
        with pytest.raises(InvalidStateError):
            resume_service.retry(CANDIDATE, run.id)

    def test_retry_unknown_run_raises_not_found(self) -> None:
        resume_service, _, _, _, _ = _pair()
        with pytest.raises(NotFoundError):
            resume_service.retry(CANDIDATE, uuid4())

    def test_retry_replays_derived_evidence_when_none_selected(self) -> None:
        resume_service, _, _, _, model = _pair(fail_count=1)
        request = AgentStartInput(
            resume_version_id=RESUME,
            canonical_job_id=JOB,
            job_version_id=JOB_VERSION,
            evidence_ids=(),
        )
        original = resume_service.start(CANDIDATE, request)
        assert original.evidence_ids == (EVIDENCE,)  # derived from the resume

        retried = resume_service.retry(CANDIDATE, original.id)
        assert retried.evidence_ids == (EVIDENCE,)
        assert retried.state is AgentRunState.SUCCEEDED
        assert len(model.requests) == 2


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_cancels_a_pending_run(self) -> None:
        _, _, _, runtime, _ = _pair()
        pending = _pending_run(runtime)
        assert pending.state is AgentRunState.PENDING

        stopped = runtime.stop(CANDIDATE, pending.id, reason_code="user_requested", now=NOW)

        assert stopped.id == pending.id
        assert stopped.state is AgentRunState.CANCELLED
        assert stopped.error_category == "user_requested"
        assert stopped.finished_at == NOW
        assert runtime.get(CANDIDATE, pending.id).state is AgentRunState.CANCELLED

    def test_stop_defaults_reason_code(self) -> None:
        _, _, _, runtime, _ = _pair()
        pending = _pending_run(runtime)
        stopped = runtime.stop(CANDIDATE, pending.id)
        assert stopped.error_category == "user_requested"

    @pytest.mark.parametrize(
        "state",
        [
            AgentRunState.SUCCEEDED,
            AgentRunState.RUNNING,
            AgentRunState.FAILED,
            AgentRunState.CANCELLED,
            AgentRunState.UNAVAILABLE,
        ],
    )
    def test_stop_rejects_non_pending_runs(self, state: AgentRunState) -> None:
        _, _, repo, runtime, _ = _pair()
        run = _pending_run(runtime)
        repo.update(replace(run, state=state))
        with pytest.raises(InvalidStateError):
            runtime.stop(CANDIDATE, run.id)

    def test_stop_unknown_run_raises_not_found(self) -> None:
        _, _, _, runtime, _ = _pair()
        with pytest.raises(NotFoundError):
            runtime.stop(CANDIDATE, uuid4())


# ---------------------------------------------------------------------------
# Stages: synchronous execution has no stage events
# ---------------------------------------------------------------------------


class TestStagesEmpty:
    def test_runtime_has_no_stage_query_but_runs_are_listable(self) -> None:
        """Sync runs never produce stage events; the surface stays empty."""
        resume_service, _, _, runtime, _ = _pair()
        run = resume_service.start(CANDIDATE, _input())
        runs = runtime.list(CANDIDATE)
        assert [item.id for item in runs if item.id == run.id] == [run.id]
        assert run.started_at is not None and run.finished_at is not None


def test_agent_runtime_start_retry_of_produces_distinct_key() -> None:
    _, _, _, runtime, _ = _pair()
    bundle = AgentInputBundle(identities={"resume_version_id": str(RESUME), "evidence_ids": "x"})
    first = runtime.start(
        CANDIDATE,
        AgentCapability.RESUME_REVIEW,
        bundle,
        schema_version="v1",
        prompt_version="p1",
    )
    second = runtime.start(
        CANDIDATE,
        AgentCapability.RESUME_REVIEW,
        bundle,
        schema_version="v1",
        prompt_version="p1",
        retry_of=first.id,
    )
    assert second.id != first.id
    assert second.idempotency_key == f"{first.idempotency_key}:retry:{first.id}"
    assert runtime.get(CANDIDATE, first.id) is not None
