from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

import careerops.infrastructure.goal_run_operator as goal_run_operator_module
from careerops.api.goal_runs import (
    GoalRunConflict,
    GoalRunGmailAttachmentRef,
    GoalRunMatchConfig,
    GoalRunNotFound,
    GoalRunReviewedGmailDispatchConfig,
    GoalRunUnavailable,
)
from careerops.application.candidate_profile import (
    CandidateProfileDecision,
    CandidateProfileRepositoryError,
    CandidateProfileSnapshotRecord,
)
from careerops.application.goal_run import GoalRunRepositoryError
from careerops.config import Settings
from careerops.infrastructure.goal_run_operator import RuntimeGoalRunOperatorProvider
from careerops.workflows.goal_run_contracts import GoalRunMode

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000c01")
OTHER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000d01")
GOAL_RUN_ID = UUID("00000000-0000-0000-0000-000000000c02")
REGISTRY_ID = UUID("00000000-0000-0000-0000-000000000c03")
FENCING_TOKEN = UUID("00000000-0000-0000-0000-000000000c04")
REVIEW_ID = UUID("00000000-0000-0000-0000-000000000c05")
CREATE_COMMAND_ID = UUID("00000000-0000-0000-0000-000000000c06")
SECOND_CREATE_COMMAND_ID = UUID("00000000-0000-0000-0000-000000000c07")
RESUME_COMMAND_ID = UUID("00000000-0000-0000-0000-000000000c08")
SECOND_RESUME_COMMAND_ID = UUID("00000000-0000-0000-0000-000000000c09")
CANCEL_COMMAND_ID = UUID("00000000-0000-0000-0000-000000000c0a")
REVIEW_COMMAND_ID = UUID("00000000-0000-0000-0000-000000000c0b")
PROFILE_VERSION_ID = UUID("00000000-0000-0000-0000-000000000c0c")
SECOND_PROFILE_VERSION_ID = UUID("00000000-0000-0000-0000-000000000c0e")
SNAPSHOT_SHA256 = "a" * 64
CALLER_SNAPSHOT_SHA256 = "b" * 64


class NoopTransaction:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> None:
        return None


class FakeRepository:
    def __init__(self, record: SimpleNamespace | None = None) -> None:
        self.record = record or _record()
        self.create_count = 0
        self.calls: list[tuple[str, object]] = []
        self.create_receipts: dict[str, SimpleNamespace] = {}
        self.status_error: GoalRunRepositoryError | None = None
        self.create_error: GoalRunRepositoryError | None = None

    def find_create(self, query: object) -> SimpleNamespace | None:
        self.calls.append(("find_create", query))
        return self.create_receipts.get(cast(Any, query).idempotency_key)

    def create(self, command: object) -> SimpleNamespace:
        self.calls.append(("create", command))
        if self.create_error is not None:
            raise self.create_error
        self.create_count += 1
        create_command = cast(Any, command)
        self.record = _record(
            goal_run_id=UUID(f"00000000-0000-0000-0000-{self.create_count:012d}"),
            actor_id=create_command.actor_id,
            context=create_command.context,
            temporal_workflow_id=create_command.temporal_workflow_id,
            idempotency_key=create_command.idempotency_key,
            trace_id=create_command.trace_id,
        )
        result = SimpleNamespace(
            record=self.record,
            idempotency_key=create_command.idempotency_key,
            newly_created=True,
        )
        self.create_receipts[create_command.idempotency_key] = SimpleNamespace(
            record=self.record,
            idempotency_key=create_command.idempotency_key,
            newly_created=False,
        )
        return result

    def list_owner(self, query: object) -> SimpleNamespace:
        self.calls.append(("list_owner", query))
        return SimpleNamespace(records=(self.record,))

    def status(self, query: object) -> SimpleNamespace:
        self.calls.append(("status", query))
        if self.status_error is not None:
            raise self.status_error
        return self.record

    def record_review_decision(self, command: object) -> SimpleNamespace:
        self.calls.append(("record_review_decision", command))
        self.record = _record(
            status="running",
            phase="review",
            temporal_workflow_id=self.record.temporal_workflow_id,
        )
        return SimpleNamespace(record=self.record)

    def resume(self, command: object) -> SimpleNamespace:
        self.calls.append(("resume", command))
        self.record = _record(
            status="running",
            phase="matching",
            temporal_workflow_id=self.record.temporal_workflow_id,
        )
        return SimpleNamespace(record=self.record)

    def cancel(self, command: object) -> SimpleNamespace:
        self.calls.append(("cancel", command))
        self.record = _record(
            status="cancelled",
            phase="cancelled",
            temporal_workflow_id=self.record.temporal_workflow_id,
        )
        return SimpleNamespace(record=self.record)


class FakeHandle:
    def __init__(self) -> None:
        self.signals: list[tuple[str, object]] = []
        self.fail_signal = False

    async def signal(self, signal: str, arg: object) -> None:
        if self.fail_signal:
            raise RuntimeError("temporal down")
        self.signals.append((signal, arg))


class FakeTemporalClient:
    def __init__(self) -> None:
        self.started: list[tuple[str, object, str]] = []
        self.handle = FakeHandle()
        self.fail_start = False
        self.already_started = False
        self.start_options: list[tuple[WorkflowIDReusePolicy, WorkflowIDConflictPolicy]] = []

    async def start_workflow(
        self,
        workflow: str,
        arg: object,
        *,
        id: str,
        task_queue: str,
        id_reuse_policy: WorkflowIDReusePolicy,
        id_conflict_policy: WorkflowIDConflictPolicy,
    ) -> None:
        self.start_options.append((id_reuse_policy, id_conflict_policy))
        if self.fail_start:
            raise RuntimeError("temporal down")
        if self.already_started:
            raise WorkflowAlreadyStartedError()
        self.started.append((workflow, arg, f"{id}:{task_queue}"))

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        assert workflow_id == self.handle_workflow_id
        return self.handle

    @property
    def handle_workflow_id(self) -> str:
        return f"goal-run:{ACTOR_ID}:{CREATE_COMMAND_ID}"


class WorkflowAlreadyStartedError(Exception):
    pass


@pytest.mark.asyncio
async def test_create_persists_then_starts_workflow_with_command_identity() -> None:
    repository = FakeRepository()
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    response = await provider.create_goal_run(
        actor_id=ACTOR_ID,
        command_id=CREATE_COMMAND_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        now=NOW,
    )

    assert response.status == "accepted"
    assert response.goal_run.goal_run_id == UUID("00000000-0000-0000-0000-000000000001")
    assert [call[0] for call in repository.calls] == ["find_create", "create"]
    create_command = cast(Any, repository.calls[1][1])
    assert create_command.actor_id == ACTOR_ID
    assert create_command.context == {
        "registry_id": str(REGISTRY_ID),
        "max_records": 1000,
        "source_id": "public-ats",
        "mode": "gmail_dispatch",
    }
    assert create_command.registry_id == REGISTRY_ID
    assert create_command.source_id == "public-ats"
    assert create_command.idempotency_key == f"goal-run:{ACTOR_ID}:{CREATE_COMMAND_ID}"
    assert create_command.temporal_workflow_id == f"goal-run:{ACTOR_ID}:{CREATE_COMMAND_ID}"
    assert create_command.trace_id == f"goal-run-command:{CREATE_COMMAND_ID}"
    assert len(temporal.started) == 1
    workflow, workflow_input, identity = temporal.started[0]
    assert workflow == "GoalRunWorkflow"
    assert identity == f"goal-run:{ACTOR_ID}:{CREATE_COMMAND_ID}:careerops-m0"
    assert temporal.start_options == [
        (WorkflowIDReusePolicy.REJECT_DUPLICATE, WorkflowIDConflictPolicy.FAIL)
    ]
    workflow_input = cast(Any, workflow_input)
    assert workflow_input.goal_run_id == response.goal_run.goal_run_id
    assert workflow_input.owner_user_id == ACTOR_ID
    assert workflow_input.registry_id == REGISTRY_ID
    assert workflow_input.source_id == "public-ats"
    assert workflow_input.fencing_token == FENCING_TOKEN
    assert response.goal_run.temporal_workflow_id == f"goal-run:{ACTOR_ID}:{CREATE_COMMAND_ID}"


@pytest.mark.asyncio
async def test_create_persists_reviewed_matching_and_gmail_dispatch_context() -> None:
    repository = FakeRepository()
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    response = await provider.create_goal_run(
        actor_id=ACTOR_ID,
        command_id=CREATE_COMMAND_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        match_config=GoalRunMatchConfig(
            include_keywords=("python", "agent", "backend"),
            exclude_keywords=("senior manager",),
            min_score=0.72,
        ),
        gmail_dispatch=GoalRunReviewedGmailDispatchConfig(
            candidate_id=UUID("00000000-0000-0000-0000-000000000e01"),
            account_id=UUID("00000000-0000-0000-0000-000000000e02"),
            campaign_id=UUID("00000000-0000-0000-0000-000000000e03"),
            grant_version_id=UUID("00000000-0000-0000-0000-000000000e04"),
            release_qualification_id=UUID("00000000-0000-0000-0000-000000000e05"),
            sender="candidate@example.com",
            recipient="recruiting@example.com",
            subject="Application for backend role",
            text_body="Reviewed exact body.",
            attachment_refs=(
                GoalRunGmailAttachmentRef(
                    object_key="materials/resume.pdf",
                    filename="resume.pdf",
                    content_type="application/pdf",
                    size_bytes=1024,
                    sha256="d" * 64,
                ),
            ),
            draft_expires_at=NOW,
            authorization_expires_at=NOW,
        ),
        now=NOW,
    )

    create_command = cast(Any, next(call[1] for call in repository.calls if call[0] == "create"))
    assert create_command.context == {
        "registry_id": str(REGISTRY_ID),
        "max_records": 1000,
        "source_id": "public-ats",
        "mode": "gmail_dispatch",
        "match_config": {
            "include_keywords": ["python", "agent", "backend"],
            "exclude_keywords": ["senior manager"],
            "min_score": 0.72,
            "max_applications": 1,
        },
        "gmail_dispatch": {
            "candidate_id": "00000000-0000-0000-0000-000000000e01",
            "account_id": "00000000-0000-0000-0000-000000000e02",
            "campaign_id": "00000000-0000-0000-0000-000000000e03",
            "grant_version_id": "00000000-0000-0000-0000-000000000e04",
            "release_qualification_id": "00000000-0000-0000-0000-000000000e05",
            "sender": "candidate@example.com",
            "recipient": "recruiting@example.com",
            "subject": "Application for backend role",
            "text_body": "Reviewed exact body.",
            "attachment_refs": [
                {
                    "object_key": "materials/resume.pdf",
                    "filename": "resume.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 1024,
                    "sha256": "d" * 64,
                }
            ],
            "draft_expires_at": "2026-07-20T12:00:00Z",
            "authorization_expires_at": "2026-07-20T12:00:00Z",
        },
    }
    assert response.goal_run.source_id == "public-ats"


@pytest.mark.asyncio
async def test_summary_projects_review_without_credential_handles() -> None:
    repository = FakeRepository(
        _record(
            review_kind="gmail_dispatch",
            review_payload={
                "candidate": "候选岗位",
                "credential_handle": "opaque-secret",
                "nested": {"oauth_token": "secret", "recipient": "recruiting@example.com"},
            },
        )
    )
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    response = await provider.goal_run_status(actor_id=ACTOR_ID, goal_run_id=GOAL_RUN_ID)

    assert response.goal_run.pending_review is not None
    assert response.goal_run.pending_review.review_id == REVIEW_ID
    assert response.goal_run.pending_review.kind == "gmail_dispatch"
    assert response.goal_run.pending_review.payload == {
        "candidate": "候选岗位",
        "nested": {"recipient": "recruiting@example.com"},
    }
    assert [action.label_zh for action in response.goal_run.pending_review.actions] == [
        "批准进入受控 Gmail 出站链路",
        "拒绝",
    ]


@pytest.mark.asyncio
async def test_distinct_creates_same_source_get_distinct_idempotency_and_workflow_ids() -> None:
    repository = FakeRepository()
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    first = await provider.create_goal_run(
        actor_id=ACTOR_ID,
        command_id=CREATE_COMMAND_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        now=NOW,
    )
    second = await provider.create_goal_run(
        actor_id=ACTOR_ID,
        command_id=SECOND_CREATE_COMMAND_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        now=NOW,
    )

    create_commands = [cast(Any, call[1]) for call in repository.calls if call[0] == "create"]
    assert create_commands[0].idempotency_key != create_commands[1].idempotency_key
    assert create_commands[0].temporal_workflow_id == f"goal-run:{ACTOR_ID}:{CREATE_COMMAND_ID}"
    assert create_commands[1].temporal_workflow_id == (
        f"goal-run:{ACTOR_ID}:{SECOND_CREATE_COMMAND_ID}"
    )
    assert first.goal_run.goal_run_id != second.goal_run.goal_run_id
    assert [identity for _workflow, _input, identity in temporal.started] == [
        f"goal-run:{ACTOR_ID}:{CREATE_COMMAND_ID}:careerops-m0",
        f"goal-run:{ACTOR_ID}:{SECOND_CREATE_COMMAND_ID}:careerops-m0",
    ]


@pytest.mark.asyncio
async def test_same_create_command_retry_reattaches_existing_workflow() -> None:
    repository = FakeRepository()
    temporal = FakeTemporalClient()
    temporal.already_started = True
    provider = _provider(repository, temporal)

    response = await provider.create_goal_run(
        actor_id=ACTOR_ID,
        command_id=CREATE_COMMAND_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        now=NOW,
    )

    assert response.status == "accepted"
    assert response.goal_run.temporal_workflow_id == f"goal-run:{ACTOR_ID}:{CREATE_COMMAND_ID}"


@pytest.mark.asyncio
async def test_preapplication_create_loads_exact_active_approved_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository()
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)
    candidate_record = CandidateProfileSnapshotRecord(
        owner_user_id=ACTOR_ID,
        candidate_id=GOAL_RUN_ID,
        profile_version_id=PROFILE_VERSION_ID,
        profile_version=1,
        material_bundle_id=UUID("00000000-0000-0000-0000-000000000c0d"),
        material_bundle_version=1,
        profile={
            "skills": ["LLM", "TypeScript"],
            "role_titles": ["AI Application Engineer"],
            "years_experience": 6,
            "work_authorization": "unknown",
            "requires_sponsorship": None,
        },
        preferences={
            "target_titles": ["AI Application Engineer"],
            "required_skills": [],
            "preferred_skills": ["LLM", "TypeScript"],
            "excluded_skills": ["model training"],
            "required_keywords": ["production"],
            "preferred_keywords": ["agent"],
            "excluded_keywords": ["research scientist"],
            "allowed_locations": ["Chengdu"],
            "excluded_locations": [],
            "allowed_companies": ["OpenAI"],
            "allowed_industries": ["Software"],
            "work_modes": ["remote", "hybrid"],
            "employment_types": ["full_time"],
            "seniority_levels": ["senior"],
            "minimum_salary": None,
            "salary_currency": None,
            "sponsorship_allowed": None,
            "excluded_companies": [],
            "excluded_industries": [],
            "minimum_match_score": 0.35,
        },
        materials=(
            {
                "kind": "resume",
                "object_key": f"sha256/aa/bb/{'d' * 64}",
                "filename": "resume.pdf",
                "media_type": "application/pdf",
                "byte_size": 1024,
                "sha256": "d" * 64,
            },
        ),
        material_bundle_sha256="e" * 64,
        snapshot_sha256="f" * 64,
        decision=CandidateProfileDecision.APPROVE,
        newly_created=False,
    )

    class FakeCandidateProfileRepository:
        def __init__(self, _connection: object) -> None:
            pass

        def get_approved(self, query: object) -> CandidateProfileSnapshotRecord:
            assert cast(Any, query).actor_id == ACTOR_ID
            assert cast(Any, query).candidate_id == GOAL_RUN_ID
            return candidate_record

    monkeypatch.setattr(
        goal_run_operator_module,
        "PostgresCandidateProfileRepository",
        FakeCandidateProfileRepository,
    )

    response = await provider.create_goal_run(
        actor_id=ACTOR_ID,
        command_id=CREATE_COMMAND_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        mode=GoalRunMode.PRE_APPLICATION_ONLY,
        candidate_id=GOAL_RUN_ID,
        candidate_profile_version_id=PROFILE_VERSION_ID,
        now=NOW,
    )

    context = cast(
        Any,
        next(call[1] for call in repository.calls if call[0] == "create"),
    ).context
    assert context["mode"] == "pre_application_only"
    assert context["candidate_profile_version_id"] == str(PROFILE_VERSION_ID)
    assert context["candidate_profile_snapshot_sha256"] == "f" * 64
    assert context["candidate_profile"]["required_keywords"] == ["production"]
    assert context["candidate_profile"]["allowed_companies"] == ["OpenAI"]
    assert context["candidate_profile"]["allowed_industries"] == ["Software"]
    assert context["candidate_profile"]["seniority_levels"] == ["senior"]
    assert context["candidate_profile"]["resume"] == {
        "object_key": f"sha256/aa/bb/{'d' * 64}",
        "filename": "resume.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1024,
        "sha256": "d" * 64,
    }
    assert context["match_config"]["include_keywords"] == [
        "agent",
        "AI Application Engineer",
    ]
    assert context["match_config"]["min_score"] == 0.35
    assert cast(Any, temporal.started[0][1]).mode is GoalRunMode.PRE_APPLICATION_ONLY
    assert response.goal_run.mode is GoalRunMode.PRE_APPLICATION_ONLY


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_drift", ["new_version", "expired"])
async def test_preapplication_retry_reattaches_before_current_profile_validation(
    monkeypatch: pytest.MonkeyPatch,
    profile_drift: str,
) -> None:
    repository = FakeRepository()
    temporal = FakeTemporalClient()
    temporal.fail_start = True
    provider = _provider(repository, temporal)
    profile_calls = 0
    drifted = False

    class FakeCandidateProfileRepository:
        def __init__(self, _connection: object) -> None:
            pass

        def get_approved(self, query: object) -> CandidateProfileSnapshotRecord:
            nonlocal profile_calls
            profile_calls += 1
            assert cast(Any, query).actor_id == ACTOR_ID
            assert cast(Any, query).candidate_id == GOAL_RUN_ID
            if drifted and profile_drift == "expired":
                raise CandidateProfileRepositoryError("CANDIDATE_PROFILE_NOT_FOUND")
            if drifted:
                return _candidate_profile_record(profile_version_id=SECOND_PROFILE_VERSION_ID)
            return _candidate_profile_record()

    monkeypatch.setattr(
        goal_run_operator_module,
        "PostgresCandidateProfileRepository",
        FakeCandidateProfileRepository,
    )
    request = {
        "actor_id": ACTOR_ID,
        "command_id": CREATE_COMMAND_ID,
        "registry_id": REGISTRY_ID,
        "source_id": "public-ats",
        "mode": GoalRunMode.PRE_APPLICATION_ONLY,
        "candidate_id": GOAL_RUN_ID,
        "candidate_profile_version_id": PROFILE_VERSION_ID,
        "now": NOW,
    }

    with pytest.raises(GoalRunUnavailable):
        await provider.create_goal_run(**request)
    assert repository.create_count == 1
    assert profile_calls == 1

    drifted = True
    temporal.fail_start = False
    response = await provider.create_goal_run(**request)

    assert response.status == "accepted"
    assert repository.create_count == 1
    assert profile_calls == 1
    assert [call[0] for call in repository.calls] == [
        "find_create",
        "create",
        "find_create",
    ]
    assert len(temporal.started) == 1


@pytest.mark.asyncio
async def test_preapplication_retry_rejects_changed_immutable_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository()
    temporal = FakeTemporalClient()
    temporal.fail_start = True
    provider = _provider(repository, temporal)

    class FakeCandidateProfileRepository:
        def __init__(self, _connection: object) -> None:
            pass

        def get_approved(self, _query: object) -> CandidateProfileSnapshotRecord:
            return _candidate_profile_record()

    monkeypatch.setattr(
        goal_run_operator_module,
        "PostgresCandidateProfileRepository",
        FakeCandidateProfileRepository,
    )
    request = {
        "actor_id": ACTOR_ID,
        "command_id": CREATE_COMMAND_ID,
        "registry_id": REGISTRY_ID,
        "source_id": "public-ats",
        "mode": GoalRunMode.PRE_APPLICATION_ONLY,
        "candidate_id": GOAL_RUN_ID,
        "candidate_profile_version_id": PROFILE_VERSION_ID,
        "now": NOW,
    }
    with pytest.raises(GoalRunUnavailable):
        await provider.create_goal_run(**request)
    temporal.fail_start = False

    with pytest.raises(GoalRunConflict):
        await provider.create_goal_run(**{**request, "source_id": "different-source"})

    assert repository.create_count == 1
    assert temporal.started == []


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_mismatch", ["owner", "candidate"])
async def test_preapplication_create_fails_closed_on_profile_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    identity_mismatch: str,
) -> None:
    repository = FakeRepository()
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)
    returned = _candidate_profile_record(
        owner_user_id=OTHER_ACTOR_ID if identity_mismatch == "owner" else ACTOR_ID,
        candidate_id=UUID(int=999) if identity_mismatch == "candidate" else GOAL_RUN_ID,
    )

    class FakeCandidateProfileRepository:
        def __init__(self, _connection: object) -> None:
            pass

        def get_approved(self, _query: object) -> CandidateProfileSnapshotRecord:
            return returned

    monkeypatch.setattr(
        goal_run_operator_module,
        "PostgresCandidateProfileRepository",
        FakeCandidateProfileRepository,
    )

    with pytest.raises(GoalRunConflict):
        await provider.create_goal_run(
            actor_id=ACTOR_ID,
            command_id=CREATE_COMMAND_ID,
            registry_id=REGISTRY_ID,
            source_id="public-ats",
            mode=GoalRunMode.PRE_APPLICATION_ONLY,
            candidate_id=GOAL_RUN_ID,
            candidate_profile_version_id=PROFILE_VERSION_ID,
            now=NOW,
        )

    assert repository.create_count == 0
    assert temporal.started == []


@pytest.mark.asyncio
async def test_preapplication_approval_revalidates_exact_active_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_record = _candidate_profile_record()
    context = {
        "registry_id": str(REGISTRY_ID),
        "source_id": "public-ats",
        "max_records": 1000,
        "mode": "pre_application_only",
        **goal_run_operator_module._goal_run_profile_context(candidate_record),
    }
    repository = FakeRepository(_record(context=context))
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)
    profile_calls = 0

    class FakeCandidateProfileRepository:
        def __init__(self, _connection: object) -> None:
            pass

        def get_approved(self, query: object) -> CandidateProfileSnapshotRecord:
            nonlocal profile_calls
            profile_calls += 1
            assert cast(Any, query).actor_id == ACTOR_ID
            assert cast(Any, query).candidate_id == GOAL_RUN_ID
            return candidate_record

    monkeypatch.setattr(
        goal_run_operator_module,
        "PostgresCandidateProfileRepository",
        FakeCandidateProfileRepository,
    )

    await provider.submit_review(
        actor_id=ACTOR_ID,
        goal_run_id=GOAL_RUN_ID,
        review_id=REVIEW_ID,
        command_id=REVIEW_COMMAND_ID,
        snapshot_sha256=SNAPSHOT_SHA256,
        decision="approve",
        now=NOW,
    )

    assert profile_calls == 1
    assert [call[0] for call in repository.calls] == ["status", "record_review_decision"]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_drift", ["new_version", "expired"])
async def test_preapplication_approval_blocks_profile_drift_or_expiry(
    monkeypatch: pytest.MonkeyPatch,
    profile_drift: str,
) -> None:
    candidate_record = _candidate_profile_record()
    context = {
        "registry_id": str(REGISTRY_ID),
        "source_id": "public-ats",
        "max_records": 1000,
        "mode": "pre_application_only",
        **goal_run_operator_module._goal_run_profile_context(candidate_record),
    }
    repository = FakeRepository(_record(context=context))
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    class FakeCandidateProfileRepository:
        def __init__(self, _connection: object) -> None:
            pass

        def get_approved(self, _query: object) -> CandidateProfileSnapshotRecord:
            if profile_drift == "expired":
                raise CandidateProfileRepositoryError("CANDIDATE_PROFILE_NOT_FOUND")
            return _candidate_profile_record(profile_version_id=SECOND_PROFILE_VERSION_ID)

    monkeypatch.setattr(
        goal_run_operator_module,
        "PostgresCandidateProfileRepository",
        FakeCandidateProfileRepository,
    )

    with pytest.raises(GoalRunConflict):
        await provider.submit_review(
            actor_id=ACTOR_ID,
            goal_run_id=GOAL_RUN_ID,
            review_id=REVIEW_ID,
            command_id=REVIEW_COMMAND_ID,
            snapshot_sha256=SNAPSHOT_SHA256,
            decision="approve",
            now=NOW,
        )

    assert [call[0] for call in repository.calls] == ["status"]
    assert temporal.handle.signals == []


@pytest.mark.asyncio
async def test_preapplication_rejection_does_not_require_active_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_record = _candidate_profile_record()
    context = {
        "registry_id": str(REGISTRY_ID),
        "source_id": "public-ats",
        "max_records": 1000,
        "mode": "pre_application_only",
        **goal_run_operator_module._goal_run_profile_context(candidate_record),
    }
    repository = FakeRepository(_record(context=context))
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    class UnexpectedCandidateProfileRepository:
        def __init__(self, _connection: object) -> None:
            raise AssertionError("rejection must not load the candidate profile")

    monkeypatch.setattr(
        goal_run_operator_module,
        "PostgresCandidateProfileRepository",
        UnexpectedCandidateProfileRepository,
    )

    await provider.submit_review(
        actor_id=ACTOR_ID,
        goal_run_id=GOAL_RUN_ID,
        review_id=REVIEW_ID,
        command_id=REVIEW_COMMAND_ID,
        snapshot_sha256=SNAPSHOT_SHA256,
        decision="reject",
        now=NOW,
    )

    assert [call[0] for call in repository.calls] == ["status", "record_review_decision"]


@pytest.mark.asyncio
async def test_same_command_id_for_different_owners_has_distinct_workflow_identity() -> None:
    repository = FakeRepository()
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    await provider.create_goal_run(
        actor_id=ACTOR_ID,
        command_id=CREATE_COMMAND_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        now=NOW,
    )
    await provider.create_goal_run(
        actor_id=OTHER_ACTOR_ID,
        command_id=CREATE_COMMAND_ID,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        now=NOW,
    )

    assert [identity for _workflow, _input, identity in temporal.started] == [
        f"goal-run:{ACTOR_ID}:{CREATE_COMMAND_ID}:careerops-m0",
        f"goal-run:{OTHER_ACTOR_ID}:{CREATE_COMMAND_ID}:careerops-m0",
    ]


@pytest.mark.asyncio
async def test_create_translates_repository_conflict() -> None:
    repository = FakeRepository()
    repository.create_error = GoalRunRepositoryError("GOAL_RUN_IDEMPOTENCY_CONFLICT")
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    with pytest.raises(GoalRunConflict):
        await provider.create_goal_run(
            actor_id=ACTOR_ID,
            command_id=CREATE_COMMAND_ID,
            registry_id=REGISTRY_ID,
            source_id="public-ats",
            now=NOW,
        )

    assert temporal.started == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (GoalRunRepositoryError("GOAL_RUN_NOT_FOUND", sqlstate="23503"), GoalRunNotFound),
        (GoalRunRepositoryError("GOAL_RUN_STATE_CONFLICT", sqlstate="23514"), GoalRunConflict),
    ],
)
async def test_create_translates_sqlstate_mapped_repository_errors(
    error: GoalRunRepositoryError,
    expected: type[Exception],
) -> None:
    repository = FakeRepository()
    repository.create_error = error
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    with pytest.raises(expected):
        await provider.create_goal_run(
            actor_id=ACTOR_ID,
            command_id=CREATE_COMMAND_ID,
            registry_id=REGISTRY_ID,
            source_id="public-ats",
            now=NOW,
        )

    assert temporal.started == []


@pytest.mark.asyncio
async def test_create_leaves_durable_record_when_temporal_is_unavailable() -> None:
    repository = FakeRepository()
    temporal = FakeTemporalClient()
    temporal.fail_start = True
    provider = _provider(repository, temporal)

    with pytest.raises(GoalRunUnavailable):
        await provider.create_goal_run(
            actor_id=ACTOR_ID,
            command_id=CREATE_COMMAND_ID,
            registry_id=REGISTRY_ID,
            source_id=None,
            now=NOW,
        )

    assert [call[0] for call in repository.calls] == ["find_create", "create"]
    assert temporal.started == []


@pytest.mark.asyncio
async def test_review_commits_db_command_before_retryable_signal_failure() -> None:
    repository = FakeRepository(_record(review_snapshot_sha256=CALLER_SNAPSHOT_SHA256))
    temporal = FakeTemporalClient()
    temporal.handle.fail_signal = True
    provider = _provider(repository, temporal)

    with pytest.raises(GoalRunUnavailable):
        await provider.submit_review(
            actor_id=ACTOR_ID,
            goal_run_id=GOAL_RUN_ID,
            review_id=REVIEW_ID,
            command_id=REVIEW_COMMAND_ID,
            snapshot_sha256=CALLER_SNAPSHOT_SHA256,
            decision="approve",
            now=NOW,
        )

    assert [call[0] for call in repository.calls] == ["status", "record_review_decision"]
    committed = cast(Any, repository.calls[1][1])
    assert committed.actor_id == ACTOR_ID
    assert committed.expected_version == 7
    assert committed.fencing_token == FENCING_TOKEN
    assert committed.review_item_id == REVIEW_ID
    assert committed.review_snapshot_sha256 == CALLER_SNAPSHOT_SHA256
    assert committed.idempotency_key == f"goal-run:{ACTOR_ID}:{REVIEW_COMMAND_ID}"
    assert committed.trace_id == f"goal-run-command:{REVIEW_COMMAND_ID}"


@pytest.mark.asyncio
async def test_review_uses_caller_snapshot_for_db_command_and_signal() -> None:
    repository = FakeRepository(_record(review_snapshot_sha256=CALLER_SNAPSHOT_SHA256))
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    response = await provider.submit_review(
        actor_id=ACTOR_ID,
        goal_run_id=GOAL_RUN_ID,
        review_id=REVIEW_ID,
        command_id=REVIEW_COMMAND_ID,
        snapshot_sha256=CALLER_SNAPSHOT_SHA256,
        decision="approve",
        now=NOW,
    )

    assert response.status == "submitted"
    committed = cast(Any, repository.calls[1][1])
    assert committed.review_snapshot_sha256 == CALLER_SNAPSHOT_SHA256
    assert temporal.handle.signals == [("review", temporal.handle.signals[0][1])]
    signal_arg = cast(Any, temporal.handle.signals[0][1])
    assert signal_arg.command_id == REVIEW_COMMAND_ID
    assert signal_arg.snapshot_sha256 == CALLER_SNAPSHOT_SHA256


@pytest.mark.asyncio
async def test_cross_owner_not_found_stops_before_temporal() -> None:
    repository = FakeRepository()
    repository.status_error = GoalRunRepositoryError("GOAL_RUN_NOT_FOUND")
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    with pytest.raises(GoalRunNotFound):
        await provider.resume_goal_run(
            actor_id=ACTOR_ID,
            goal_run_id=GOAL_RUN_ID,
            command_id=RESUME_COMMAND_ID,
            now=NOW,
        )

    assert [call[0] for call in repository.calls] == ["status"]
    assert temporal.handle.signals == []


@pytest.mark.asyncio
async def test_resume_and_cancel_commit_before_signaling_workflow() -> None:
    repository = FakeRepository()
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    resume_response = await provider.resume_goal_run(
        actor_id=ACTOR_ID,
        goal_run_id=GOAL_RUN_ID,
        command_id=RESUME_COMMAND_ID,
        now=NOW,
    )
    cancel_response = await provider.cancel_goal_run(
        actor_id=ACTOR_ID,
        goal_run_id=GOAL_RUN_ID,
        command_id=CANCEL_COMMAND_ID,
        reason="operator stop",
        now=NOW,
    )

    assert resume_response.status == "resumed"
    assert cancel_response.status == "cancelled"
    assert [call[0] for call in repository.calls] == ["status", "resume", "status", "cancel"]
    assert [signal for signal, _arg in temporal.handle.signals] == ["resume", "cancel"]


@pytest.mark.asyncio
async def test_distinct_resume_command_ids_do_not_alias() -> None:
    repository = FakeRepository()
    temporal = FakeTemporalClient()
    provider = _provider(repository, temporal)

    await provider.resume_goal_run(
        actor_id=ACTOR_ID,
        goal_run_id=GOAL_RUN_ID,
        command_id=RESUME_COMMAND_ID,
        now=NOW,
    )
    await provider.resume_goal_run(
        actor_id=ACTOR_ID,
        goal_run_id=GOAL_RUN_ID,
        command_id=SECOND_RESUME_COMMAND_ID,
        now=NOW,
    )

    resume_commands = [cast(Any, call[1]) for call in repository.calls if call[0] == "resume"]
    assert [command.idempotency_key for command in resume_commands] == [
        f"goal-run:{ACTOR_ID}:{RESUME_COMMAND_ID}",
        f"goal-run:{ACTOR_ID}:{SECOND_RESUME_COMMAND_ID}",
    ]
    signal_args = [cast(Any, arg) for signal, arg in temporal.handle.signals if signal == "resume"]
    assert [arg.command_id for arg in signal_args] == [RESUME_COMMAND_ID, SECOND_RESUME_COMMAND_ID]


def _provider(
    repository: FakeRepository,
    temporal: FakeTemporalClient,
) -> RuntimeGoalRunOperatorProvider:
    return RuntimeGoalRunOperatorProvider(
        settings=Settings.model_validate(
            {
                "environment": "test",
                "console_allowed_hosts": ("testserver",),
                "console_allowed_origins": ("http://testserver",),
            }
        ),
        transaction_factory=cast(Any, NoopTransaction),
        repository_factory=cast(Any, lambda _connection: repository),
        temporal_client_factory=lambda: temporal,
    )


def _candidate_profile_record(
    *,
    owner_user_id: UUID = ACTOR_ID,
    candidate_id: UUID = GOAL_RUN_ID,
    profile_version_id: UUID = PROFILE_VERSION_ID,
) -> CandidateProfileSnapshotRecord:
    return CandidateProfileSnapshotRecord(
        owner_user_id=owner_user_id,
        candidate_id=candidate_id,
        profile_version_id=profile_version_id,
        profile_version=1 if profile_version_id == PROFILE_VERSION_ID else 2,
        material_bundle_id=UUID("00000000-0000-0000-0000-000000000c0d"),
        material_bundle_version=1,
        profile={
            "skills": ["LLM", "TypeScript"],
            "role_titles": ["AI Application Engineer"],
            "years_experience": 6,
            "work_authorization": "unknown",
            "requires_sponsorship": None,
        },
        preferences={
            "target_titles": ["AI Application Engineer"],
            "required_skills": [],
            "preferred_skills": ["LLM", "TypeScript"],
            "excluded_skills": ["model training"],
            "required_keywords": [],
            "preferred_keywords": ["agent"],
            "excluded_keywords": ["research scientist"],
            "allowed_locations": ["Chengdu"],
            "excluded_locations": [],
            "work_modes": ["remote", "hybrid"],
            "employment_types": ["full_time"],
            "minimum_salary": None,
            "salary_currency": None,
            "sponsorship_allowed": None,
            "excluded_companies": [],
            "excluded_industries": [],
            "minimum_match_score": 0.35,
        },
        materials=(
            {
                "kind": "resume",
                "object_key": f"sha256/aa/bb/{'d' * 64}",
                "filename": "resume.pdf",
                "media_type": "application/pdf",
                "byte_size": 1024,
                "sha256": "d" * 64,
            },
        ),
        material_bundle_sha256="e" * 64,
        snapshot_sha256="f" * 64,
        decision=CandidateProfileDecision.APPROVE,
        newly_created=False,
    )


def _record(
    *,
    goal_run_id: UUID = GOAL_RUN_ID,
    actor_id: UUID = ACTOR_ID,
    status: str = "waiting_review",
    phase: str = "review",
    temporal_workflow_id: str | None = f"goal-run:{ACTOR_ID}:{CREATE_COMMAND_ID}",
    review_snapshot_sha256: str = SNAPSHOT_SHA256,
    review_kind: str = "application_drafts",
    review_payload: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    idempotency_key: str = "goal-run:test",
    trace_id: str = f"goal-run-command:{CREATE_COMMAND_ID}",
) -> SimpleNamespace:
    return SimpleNamespace(
        goal_run_id=goal_run_id,
        actor_id=actor_id,
        owner_user_id=actor_id,
        registry_id=REGISTRY_ID,
        source_id="public-ats",
        max_records=1000,
        goal_kind="job-search",
        goal="Run bounded career goal automation with authenticated operator review.",
        status=status,
        phase=phase,
        version=7,
        fencing_token=FENCING_TOKEN,
        context=context
        or {"registry_id": str(REGISTRY_ID), "source_id": "public-ats", "max_records": 1000},
        checkpoint={},
        review_state="requested",
        review_kind=review_kind,
        review_payload=review_payload or {},
        review_item_id=REVIEW_ID,
        review_snapshot_sha256=review_snapshot_sha256,
        idempotency_key=idempotency_key,
        temporal_workflow_id=temporal_workflow_id,
        trace_id=trace_id,
        created_at=NOW,
        updated_at=NOW,
    )
