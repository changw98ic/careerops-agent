from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from fastapi.testclient import TestClient

from careerops.api.app import create_app
from careerops.api.goal_runs import (
    CreateGoalRunResponse,
    GoalRunCommandResponse,
    GoalRunConflict,
    GoalRunGmailAttachmentRef,
    GoalRunMatchConfig,
    GoalRunMode,
    GoalRunNotFound,
    GoalRunOperatorProvider,
    GoalRunReviewedGmailDispatchConfig,
    GoalRunReviewProjection,
    GoalRunStatusResponse,
    GoalRunSummary,
    GoalRunUnavailable,
    ListGoalRunsResponse,
)
from careerops.application.dashboard import DashboardSnapshot, DashboardSystemStatus
from careerops.application.ports.readiness import ReadinessReport, ReadinessState
from careerops.auth.contracts import AuthenticatedPrincipal, AuthRequestContext, SessionSecrets
from careerops.auth.service import ConsoleAuthService
from careerops.config import RuntimeEnvironment, Settings

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000a01")
GOAL_RUN_ID = UUID("00000000-0000-0000-0000-000000000a02")
REGISTRY_ID = UUID("00000000-0000-0000-0000-000000000a03")
REVIEW_ID = UUID("00000000-0000-0000-0000-000000000a04")
CREATE_COMMAND_ID = UUID("00000000-0000-0000-0000-000000000a06")
SECOND_CREATE_COMMAND_ID = UUID("00000000-0000-0000-0000-000000000a0b")
APPROVE_COMMAND_ID = UUID("00000000-0000-0000-0000-000000000a07")
REJECT_COMMAND_ID = UUID("00000000-0000-0000-0000-000000000a08")
RESUME_COMMAND_ID = UUID("00000000-0000-0000-0000-000000000a09")
CANCEL_COMMAND_ID = UUID("00000000-0000-0000-0000-000000000a0a")
SNAPSHOT_SHA256 = "a" * 64
MUTATION_HEADERS = {
    "Origin": "http://testserver",
    "X-CSRF-Token": "authenticated-csrf",
}


class FixedReadinessProbe:
    async def check(self) -> ReadinessReport:
        return ReadinessReport(
            checks={
                "database": ReadinessState.OK,
                "redis": ReadinessState.OK,
                "temporal": ReadinessState.OK,
                "storage": ReadinessState.OK,
            }
        )

    async def close(self) -> None:
        return None


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
            session_id=UUID("00000000-0000-0000-0000-000000000a05"),
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
            session_id=UUID("00000000-0000-0000-0000-000000000a05"),
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


class RecordingGoalRunProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def create_goal_run(
        self,
        *,
        actor_id: UUID,
        command_id: UUID,
        registry_id: UUID,
        source_id: str | None,
        mode: GoalRunMode,
        match_config: GoalRunMatchConfig | None,
        candidate_id: UUID | None,
        candidate_profile_version_id: UUID | None,
        gmail_dispatch: GoalRunReviewedGmailDispatchConfig | None,
        now: datetime,
    ) -> CreateGoalRunResponse:
        self.calls.append(
            (
                "create",
                (
                    actor_id,
                    command_id,
                    registry_id,
                    source_id,
                    mode,
                    match_config,
                    candidate_id,
                    candidate_profile_version_id,
                    gmail_dispatch,
                    now,
                ),
            )
        )
        return CreateGoalRunResponse(goal_run=_summary(actor_id=actor_id, source_id=source_id))

    async def list_goal_runs(
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListGoalRunsResponse:
        self.calls.append(("list", (actor_id, limit)))
        return ListGoalRunsResponse(goal_runs=(_summary(actor_id=actor_id),))

    async def goal_run_status(
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
    ) -> GoalRunStatusResponse:
        self.calls.append(("status", (actor_id, goal_run_id)))
        return GoalRunStatusResponse(goal_run=_summary(actor_id=actor_id, goal_run_id=goal_run_id))

    async def submit_review(
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
        review_id: UUID,
        command_id: UUID,
        snapshot_sha256: str,
        decision: Literal["approve", "reject"],
        now: datetime,
    ) -> GoalRunCommandResponse:
        self.calls.append(
            (
                "review",
                (actor_id, goal_run_id, review_id, command_id, snapshot_sha256, decision, now),
            )
        )
        return GoalRunCommandResponse(
            status="submitted",
            goal_run=_summary(actor_id=actor_id, goal_run_id=goal_run_id),
        )

    async def resume_goal_run(
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
        command_id: UUID,
        now: datetime,
    ) -> GoalRunCommandResponse:
        self.calls.append(("resume", (actor_id, goal_run_id, command_id, now)))
        return GoalRunCommandResponse(
            status="resumed",
            goal_run=_summary(actor_id=actor_id, goal_run_id=goal_run_id),
        )

    async def cancel_goal_run(
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
        command_id: UUID,
        reason: str | None,
        now: datetime,
    ) -> GoalRunCommandResponse:
        self.calls.append(("cancel", (actor_id, goal_run_id, command_id, reason, now)))
        return GoalRunCommandResponse(
            status="cancelled",
            goal_run=_summary(actor_id=actor_id, goal_run_id=goal_run_id, status="cancelled"),
        )


class ErroringGoalRunProvider(RecordingGoalRunProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def goal_run_status(self, *, actor_id: UUID, goal_run_id: UUID) -> GoalRunStatusResponse:
        self.calls.append(("status", (actor_id, goal_run_id)))
        raise self._error


def test_goal_run_operator_routes_are_injected_and_internal() -> None:
    client, _provider = _client(RecordingGoalRunProvider())

    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/internal/goal-runs" in paths
    assert "/api/v1/internal/goal-runs/{goal_run_id}/reviews/{review_id}" in paths
    assert "/api/v1/internal/goal-runs/{goal_run_id}/resume" in paths
    assert "/api/v1/internal/goal-runs/{goal_run_id}/cancel" in paths


def test_goal_run_operator_requires_authenticated_session() -> None:
    provider = RecordingGoalRunProvider()
    app = _app(provider)

    response = TestClient(app).get("/api/v1/internal/goal-runs")

    assert response.status_code == 401
    assert provider.calls == []


def test_goal_run_operator_rejects_read_with_bad_host() -> None:
    client, provider = _client(RecordingGoalRunProvider())

    response = client.get("/api/v1/internal/goal-runs", headers={"Host": "attacker.example"})

    assert response.status_code == 403
    assert provider.calls == []


def test_goal_run_operator_forwards_actor_and_never_accepts_runtime_authority() -> None:
    client, provider = _client(RecordingGoalRunProvider())

    response = client.post(
        "/api/v1/internal/goal-runs",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(CREATE_COMMAND_ID),
            "registry_id": str(REGISTRY_ID),
            "source_id": "public-ats",
        },
    )
    rejected = client.post(
        "/api/v1/internal/goal-runs",
        headers=MUTATION_HEADERS,
        json={
            "registry_id": str(REGISTRY_ID),
            "command_id": str(CREATE_COMMAND_ID),
            "source_id": "public-ats",
            "worker_id": "caller-controlled",
            "workspace_root": "/tmp/workspace",
            "task_queue": "caller-queue",
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert rejected.status_code == 422
    assert provider.calls[0][0] == "create"
    (
        actor_id,
        command_id,
        registry_id,
        source_id,
        mode,
        match_config,
        candidate_id,
        candidate_profile_version_id,
        gmail_dispatch,
        observed_now,
    ) = cast(
        tuple[
            UUID,
            UUID,
            UUID,
            str | None,
            GoalRunMode,
            GoalRunMatchConfig | None,
            UUID | None,
            UUID | None,
            GoalRunReviewedGmailDispatchConfig | None,
            datetime,
        ],
        provider.calls[0][1],
    )
    assert actor_id == ACTOR_ID
    assert command_id == CREATE_COMMAND_ID
    assert registry_id == REGISTRY_ID
    assert source_id == "public-ats"
    assert mode is GoalRunMode.GMAIL_DISPATCH
    assert match_config is None
    assert candidate_id is None
    assert candidate_profile_version_id is None
    assert gmail_dispatch is None
    assert observed_now.tzinfo is not None
    assert len(provider.calls) == 1


def test_goal_run_operator_accepts_reviewed_automation_config_without_runtime_handles() -> None:
    client, provider = _client(RecordingGoalRunProvider())

    response = client.post(
        "/api/v1/internal/goal-runs",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(CREATE_COMMAND_ID),
            "registry_id": str(REGISTRY_ID),
            "source_id": "public-ats",
            "match_config": {
                "include_keywords": ["python", "agent", "backend"],
                "exclude_keywords": ["senior manager"],
                "min_score": 0.72,
                "max_applications": 1,
            },
            "gmail_dispatch": {
                "candidate_id": "00000000-0000-0000-0000-000000000b01",
                "account_id": "00000000-0000-0000-0000-000000000b02",
                "campaign_id": "00000000-0000-0000-0000-000000000b03",
                "grant_version_id": "00000000-0000-0000-0000-000000000b04",
                "release_qualification_id": "00000000-0000-0000-0000-000000000b05",
                "sender": "candidate@example.com",
                "recipient": "recruiting@example.com",
                "subject": "Application for backend role",
                "text_body": "Reviewed exact outbound message.",
                "attachment_refs": [
                    {
                        "object_key": "materials/resume-v1",
                        "filename": "resume.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": 12345,
                        "sha256": "b" * 64,
                    }
                ],
                "draft_expires_at": "2026-07-21T12:00:00Z",
                "authorization_expires_at": "2026-07-21T13:00:00Z",
            },
        },
    )
    rejected = client.post(
        "/api/v1/internal/goal-runs",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(SECOND_CREATE_COMMAND_ID),
            "registry_id": str(REGISTRY_ID),
            "match_config": {"max_applications": 2},
            "mode": "pre_application_only",
            "credential_handle": "caller-controlled",
        },
    )

    assert response.status_code == 202
    assert rejected.status_code == 422
    create_call = cast(tuple[object, ...], provider.calls[0][1])
    assert create_call[4] == GoalRunMode.GMAIL_DISPATCH
    match_config = cast(GoalRunMatchConfig, create_call[5])
    assert create_call[6] is None
    assert create_call[7] is None
    gmail_dispatch = cast(GoalRunReviewedGmailDispatchConfig, create_call[8])
    assert match_config.include_keywords == ("python", "agent", "backend")
    assert match_config.exclude_keywords == ("senior manager",)
    assert match_config.min_score == 0.72
    assert match_config.max_applications == 1
    assert gmail_dispatch.sender == "candidate@example.com"
    assert gmail_dispatch.recipient == "recruiting@example.com"
    assert gmail_dispatch.attachment_refs == (
        GoalRunGmailAttachmentRef(
            object_key="materials/resume-v1",
            filename="resume.pdf",
            content_type="application/pdf",
            size_bytes=12345,
            sha256="b" * 64,
        ),
    )
    assert len(provider.calls) == 1


def test_goal_run_operator_accepts_explicit_local_preapplication_mode() -> None:
    client, provider = _client(RecordingGoalRunProvider())
    candidate_id = "00000000-0000-0000-0000-000000000b01"
    profile_version_id = "00000000-0000-0000-0000-000000000b06"
    body = {
        "command_id": str(CREATE_COMMAND_ID),
        "registry_id": str(REGISTRY_ID),
        "source_id": "public-ats",
        "mode": "pre_application_only",
        "candidate_id": candidate_id,
        "candidate_profile_version_id": profile_version_id,
    }

    response = client.post(
        "/api/v1/internal/goal-runs",
        headers=MUTATION_HEADERS,
        json=body,
    )
    with_gmail = client.post(
        "/api/v1/internal/goal-runs",
        headers=MUTATION_HEADERS,
        json={
            **body,
            "command_id": str(SECOND_CREATE_COMMAND_ID),
            "gmail_dispatch": {
                "candidate_id": "00000000-0000-0000-0000-000000000b01",
                "account_id": "00000000-0000-0000-0000-000000000b02",
                "campaign_id": "00000000-0000-0000-0000-000000000b03",
                "grant_version_id": "00000000-0000-0000-0000-000000000b04",
                "release_qualification_id": "00000000-0000-0000-0000-000000000b05",
                "sender": "candidate@example.com",
                "recipient": "recruiting@example.com",
                "subject": "Reviewed application",
                "text_body": "Exact reviewed body.",
                "attachment_refs": [
                    {
                        "object_key": "materials/resume-v1",
                        "filename": "resume.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": 12345,
                        "sha256": "b" * 64,
                    }
                ],
                "draft_expires_at": "2026-07-21T12:00:00Z",
                "authorization_expires_at": "2026-07-21T13:00:00Z",
            },
        },
    )
    unknown_mode = client.post(
        "/api/v1/internal/goal-runs",
        headers=MUTATION_HEADERS,
        json={**body, "command_id": str(SECOND_CREATE_COMMAND_ID), "mode": "wild_write"},
    )

    assert response.status_code == 202
    assert with_gmail.status_code == 422
    assert unknown_mode.status_code == 422
    create_call = cast(tuple[object, ...], provider.calls[0][1])
    assert create_call[4] is GoalRunMode.PRE_APPLICATION_ONLY
    assert create_call[6] == UUID(candidate_id)
    assert create_call[7] == UUID(profile_version_id)
    assert create_call[8] is None
    assert len(provider.calls) == 1


def test_goal_run_operator_forwards_list_status_review_resume_and_cancel() -> None:
    client, provider = _client(RecordingGoalRunProvider())

    list_response = client.get("/api/v1/internal/goal-runs?limit=5")
    status_response = client.get(f"/api/v1/internal/goal-runs/{GOAL_RUN_ID}")
    approve_response = client.post(
        f"/api/v1/internal/goal-runs/{GOAL_RUN_ID}/reviews/{REVIEW_ID}",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(APPROVE_COMMAND_ID),
            "snapshot_sha256": SNAPSHOT_SHA256,
            "decision": "approve",
        },
    )
    reject_response = client.post(
        f"/api/v1/internal/goal-runs/{GOAL_RUN_ID}/reviews/{REVIEW_ID}",
        headers=MUTATION_HEADERS,
        json={
            "command_id": str(REJECT_COMMAND_ID),
            "snapshot_sha256": SNAPSHOT_SHA256,
            "decision": "reject",
        },
    )
    resume_response = client.post(
        f"/api/v1/internal/goal-runs/{GOAL_RUN_ID}/resume",
        headers=MUTATION_HEADERS,
        json={"command_id": str(RESUME_COMMAND_ID)},
    )
    cancel_response = client.post(
        f"/api/v1/internal/goal-runs/{GOAL_RUN_ID}/cancel",
        headers=MUTATION_HEADERS,
        json={"command_id": str(CANCEL_COMMAND_ID), "reason": "operator stop"},
    )

    assert list_response.status_code == 200
    assert list_response.json()["goal_runs"][0]["owner_user_id"] == str(ACTOR_ID)
    pending_review = list_response.json()["goal_runs"][0]["pending_review"]
    assert pending_review == {
        "review_id": str(REVIEW_ID),
        "kind": "gmail_dispatch",
        "snapshot_sha256": SNAPSHOT_SHA256,
        "payload": {"candidate": "候选岗位", "recipient": "recruiting@example.com"},
        "actions": [
            {"action": "approve", "label_zh": "批准"},
            {"action": "reject", "label_zh": "拒绝"},
        ],
    }
    assert status_response.status_code == 200
    assert status_response.json()["goal_run"]["goal_run_id"] == str(GOAL_RUN_ID)
    assert approve_response.status_code == 200
    assert reject_response.status_code == 200
    assert resume_response.status_code == 200
    assert cancel_response.status_code == 200
    assert [call[0] for call in provider.calls] == [
        "list",
        "status",
        "review",
        "review",
        "resume",
        "cancel",
    ]
    assert cast(tuple[object, ...], provider.calls[2][1])[3] == APPROVE_COMMAND_ID
    assert cast(tuple[object, ...], provider.calls[2][1])[4] == SNAPSHOT_SHA256
    assert cast(tuple[object, ...], provider.calls[2][1])[5] == "approve"
    assert cast(tuple[object, ...], provider.calls[3][1])[3] == REJECT_COMMAND_ID
    assert cast(tuple[object, ...], provider.calls[3][1])[5] == "reject"
    assert cast(tuple[object, ...], provider.calls[4][1])[2] == RESUME_COMMAND_ID
    assert cast(tuple[object, ...], provider.calls[5][1])[2] == CANCEL_COMMAND_ID
    assert cast(tuple[object, ...], provider.calls[5][1])[3] == "operator stop"


def test_goal_run_operator_rejects_mutation_without_csrf_header() -> None:
    client, provider = _client(RecordingGoalRunProvider())

    response = client.post(
        "/api/v1/internal/goal-runs",
        headers={"Origin": "http://testserver"},
        json={
            "command_id": str(CREATE_COMMAND_ID),
            "registry_id": str(REGISTRY_ID),
        },
    )

    assert response.status_code == 422
    assert provider.calls == []


def test_goal_run_operator_rejects_mutation_with_bad_origin() -> None:
    client, provider = _client(RecordingGoalRunProvider())

    response = client.post(
        "/api/v1/internal/goal-runs",
        headers={"Origin": "http://attacker.example", "X-CSRF-Token": "authenticated-csrf"},
        json={
            "command_id": str(CREATE_COMMAND_ID),
            "registry_id": str(REGISTRY_ID),
        },
    )

    assert response.status_code == 403
    assert provider.calls == []


def test_goal_run_operator_maps_not_found_conflict_and_temporal_unavailable() -> None:
    for error, expected_status in (
        (GoalRunNotFound("cross owner"), 404),
        (GoalRunConflict("bad state"), 409),
        (GoalRunUnavailable("temporal unavailable"), 503),
    ):
        client, provider = _client(ErroringGoalRunProvider(error))

        response = client.get(f"/api/v1/internal/goal-runs/{GOAL_RUN_ID}")

        assert response.status_code == expected_status
        assert provider.calls == [("status", (ACTOR_ID, GOAL_RUN_ID))]
        assert "cross owner" not in response.text
        if expected_status == 409:
            assert "bad state" not in response.text


def test_goal_run_disabled_provider_returns_temporal_unavailable() -> None:
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
    )
    client = TestClient(app)
    client.cookies.set("careerops_session", "authenticated-session")
    client.cookies.set("careerops_csrf", "authenticated-csrf")

    response = client.get("/api/v1/internal/goal-runs")

    assert response.status_code == 503


def _summary(
    *,
    actor_id: UUID,
    goal_run_id: UUID = GOAL_RUN_ID,
    source_id: str | None = "public-ats",
    status: Literal[
        "starting",
        "running",
        "waiting_review",
        "blocked",
        "reconciliation_required",
        "completed",
        "failed",
        "cancelled",
        "rejected",
    ] = "running",
) -> GoalRunSummary:
    return GoalRunSummary(
        goal_run_id=goal_run_id,
        owner_user_id=actor_id,
        registry_id=REGISTRY_ID,
        source_id=source_id,
        status=status,
        phase="review",
        created_at=NOW,
        updated_at=NOW,
        status_message="waiting for operator review",
        pending_review_id=REVIEW_ID,
        pending_review=GoalRunReviewProjection(
            review_id=REVIEW_ID,
            kind="gmail_dispatch",
            snapshot_sha256=SNAPSHOT_SHA256,
            payload={"candidate": "候选岗位", "recipient": "recruiting@example.com"},
        ),
    )


def _app(provider: GoalRunOperatorProvider):
    settings = Settings.model_validate(
        {
            "environment": RuntimeEnvironment.TEST,
            "console_allowed_hosts": ("testserver",),
            "console_allowed_origins": ("http://testserver",),
        }
    )
    return create_app(
        settings,
        readiness_probe=FixedReadinessProbe(),
        console_auth_service=cast(ConsoleAuthService, FixedConsoleAuthService()),
        dashboard_provider=FixedDashboardProvider(),
        goal_run_provider=provider,
    )


def _client(provider: GoalRunOperatorProvider) -> tuple[TestClient, RecordingGoalRunProvider]:
    app = _app(provider)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("careerops_session", "authenticated-session")
    client.cookies.set("careerops_csrf", "authenticated-csrf")
    return client, cast(RecordingGoalRunProvider, provider)
