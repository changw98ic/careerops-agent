# pyright: reportUnusedFunction=false

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from careerops.auth.contracts import AuthenticatedPrincipal, AuthError
from careerops.web.routes import ConsoleAuthServicePort
from careerops.web.security import ConsoleWebSettings, OriginHostValidator, RequestOriginRejected
from careerops.workflows.goal_run_contracts import GoalRunMode

BoundedKey = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._:@/-]+$")]
EmailAddress = Annotated[
    str,
    Field(
        min_length=3,
        max_length=320,
        pattern=r"^[^@\s]{1,160}@[^@\s]{1,160}\.[^@\s]{2,40}$",
    ),
]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

GoalRunPhaseName = Literal[
    "initializing",
    "selecting_source",
    "claiming_source",
    "discovery",
    "complete_source",
    "canonical_ingest",
    "matching",
    "draft_preparation",
    "review",
    "blocked",
    "cancelled",
    "rejected",
    "dispatch",
    "reconciliation",
    "completed",
    "failed",
]


class GoalRunContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GoalRunMatchConfig(GoalRunContract):
    """Bounded matching configuration for one reviewed application candidate."""

    include_keywords: tuple[str, ...] = Field(min_length=1, max_length=50)
    exclude_keywords: tuple[str, ...] = Field(default=(), max_length=50)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    max_applications: Literal[1] = 1


class GoalRunMaterialRef(GoalRunContract):
    object_key: BoundedKey
    filename: str = Field(min_length=1, max_length=180)
    content_type: str = Field(min_length=3, max_length=160)
    size_bytes: int = Field(ge=0, le=25 * 1024 * 1024)
    sha256: Sha256Hex


# Compatibility name for the existing reviewed-Gmail contract. New pre-application
# contracts use the provider-neutral material name above.
GoalRunGmailAttachmentRef = GoalRunMaterialRef


class GoalRunReviewedGmailDispatchConfig(GoalRunContract):
    """Review-required Gmail send intent; credential handles are intentionally absent."""

    candidate_id: UUID
    account_id: UUID
    campaign_id: UUID
    grant_version_id: UUID
    release_qualification_id: UUID
    sender: EmailAddress
    recipient: EmailAddress
    subject: str = Field(min_length=1, max_length=320)
    text_body: str = Field(min_length=1, max_length=20_000)
    attachment_refs: tuple[GoalRunGmailAttachmentRef, ...] = Field(min_length=1, max_length=10)
    draft_expires_at: datetime
    authorization_expires_at: datetime


class GoalRunCandidateProfileSnapshot(GoalRunContract):
    """Immutable, review-safe candidate facts used by a pre-application GoalRun.

    Contact details and arbitrary resume text are deliberately absent.  The source and
    optional resume are bound by SHA-256 so the review packet can identify the exact
    candidate inputs without granting a provider side effect.
    """

    candidate_id: UUID
    profile_version: BoundedKey
    source_sha256: Sha256Hex
    headline: str = Field(min_length=1, max_length=500)
    desired_titles: tuple[str, ...] = Field(min_length=1, max_length=30)
    skills: tuple[str, ...] = Field(min_length=1, max_length=100)
    required_skills: tuple[str, ...] = Field(default=(), max_length=50)
    required_keywords: tuple[str, ...] = Field(default=(), max_length=50)
    locations: tuple[str, ...] = Field(default=(), max_length=30)
    excluded_locations: tuple[str, ...] = Field(default=(), max_length=30)
    allowed_companies: tuple[str, ...] = Field(default=(), max_length=50)
    allowed_industries: tuple[str, ...] = Field(default=(), max_length=50)
    remote_preference: Literal[
        "required",
        "preferred",
        "acceptable",
        "onsite",
        "unspecified",
    ] = "unspecified"
    years_experience: float | None = Field(default=None, ge=0, le=80)
    excluded_terms: tuple[str, ...] = Field(default=(), max_length=50)
    work_modes: tuple[Literal["remote", "hybrid", "onsite"], ...] = Field(default=(), max_length=3)
    employment_types: tuple[
        Literal["full_time", "part_time", "contract", "internship", "temporary"], ...
    ] = Field(default=(), max_length=5)
    seniority_levels: tuple[
        Literal["unknown", "intern", "entry", "mid", "senior", "staff", "principal", "executive"],
        ...,
    ] = Field(default=(), max_length=8)
    work_authorization: Literal["unknown", "authorized", "restricted", "requires_sponsorship"] = (
        "unknown"
    )
    requires_sponsorship: bool | None = None
    sponsorship_allowed: bool | None = None
    minimum_salary: int | None = Field(default=None, ge=0, le=100_000_000)
    salary_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    resume: GoalRunMaterialRef | None = None

    @model_validator(mode="after")
    def validate_salary_pair(self) -> "GoalRunCandidateProfileSnapshot":
        if (self.minimum_salary is None) != (self.salary_currency is None):
            raise ValueError("minimum_salary and salary_currency must be provided together")
        return self


class GoalRunReviewActionLabel(GoalRunContract):
    action: Literal["approve", "reject"]
    label_zh: str


class GoalRunReviewProjection(GoalRunContract):
    review_id: UUID
    kind: str = Field(min_length=1, max_length=128)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, object]
    actions: tuple[GoalRunReviewActionLabel, ...] = (
        GoalRunReviewActionLabel(action="approve", label_zh="批准"),
        GoalRunReviewActionLabel(action="reject", label_zh="拒绝"),
    )


class CreateGoalRunRequest(GoalRunContract):
    command_id: UUID
    registry_id: UUID
    source_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    mode: GoalRunMode = GoalRunMode.GMAIL_DISPATCH
    match_config: GoalRunMatchConfig | None = None
    candidate_id: UUID | None = None
    candidate_profile_version_id: UUID | None = None
    gmail_dispatch: GoalRunReviewedGmailDispatchConfig | None = None

    @model_validator(mode="after")
    def validate_execution_boundary(self) -> "CreateGoalRunRequest":
        if self.mode is GoalRunMode.PRE_APPLICATION_ONLY:
            if self.gmail_dispatch is not None:
                raise ValueError("pre-application mode cannot include a Gmail dispatch envelope")
            if self.match_config is not None:
                raise ValueError("pre-application matching comes from the approved profile")
            if self.candidate_id is None or self.candidate_profile_version_id is None:
                raise ValueError(
                    "pre-application mode requires candidate_id and candidate_profile_version_id"
                )
        if self.mode is GoalRunMode.GMAIL_DISPATCH and (
            self.candidate_id is not None or self.candidate_profile_version_id is not None
        ):
            raise ValueError("Gmail mode cannot include a pre-application profile reference")
        return self


class GoalRunSummary(GoalRunContract):
    goal_run_id: UUID
    owner_user_id: UUID
    registry_id: UUID
    source_id: str | None = None
    mode: GoalRunMode = GoalRunMode.GMAIL_DISPATCH
    completion_kind: str | None = Field(default=None, max_length=128)
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
    ]
    phase: GoalRunPhaseName
    created_at: datetime
    updated_at: datetime
    status_message: str | None = None
    pending_review_id: UUID | None = None
    pending_review: GoalRunReviewProjection | None = None
    temporal_workflow_id: str | None = Field(default=None, max_length=160)


class CreateGoalRunResponse(GoalRunContract):
    status: Literal["accepted"] = "accepted"
    goal_run: GoalRunSummary


class ListGoalRunsResponse(GoalRunContract):
    status: Literal["listed"] = "listed"
    goal_runs: tuple[GoalRunSummary, ...]


class GoalRunStatusResponse(GoalRunContract):
    status: Literal["found"] = "found"
    goal_run: GoalRunSummary


class SubmitGoalRunReviewRequest(GoalRunContract):
    command_id: UUID
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]


class GoalRunCommandResponse(GoalRunContract):
    status: Literal["submitted", "resumed", "cancelled"]
    goal_run: GoalRunSummary


class CancelGoalRunRequest(GoalRunContract):
    command_id: UUID
    reason: str | None = Field(default=None, min_length=1, max_length=500)


class ResumeGoalRunRequest(GoalRunContract):
    command_id: UUID


class GoalRunNotFound(LookupError):
    """Goal run is missing or is not visible to the authenticated owner."""


class GoalRunConflict(RuntimeError):
    """Goal run exists but its current state cannot accept the requested command."""


class GoalRunUnavailable(RuntimeError):
    """Goal run runtime is unavailable."""


class GoalRunOperatorProvider(Protocol):
    async def create_goal_run(  # pyright: ignore[reportUnusedFunction]
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
    ) -> CreateGoalRunResponse: ...

    async def list_goal_runs(  # pyright: ignore[reportUnusedFunction]
        self,
        *,
        actor_id: UUID,
        limit: int,
    ) -> ListGoalRunsResponse: ...

    async def goal_run_status(  # pyright: ignore[reportUnusedFunction]
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
    ) -> GoalRunStatusResponse: ...

    async def submit_review(  # pyright: ignore[reportUnusedFunction]
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
        review_id: UUID,
        command_id: UUID,
        snapshot_sha256: str,
        decision: Literal["approve", "reject"],
        now: datetime,
    ) -> GoalRunCommandResponse: ...

    async def resume_goal_run(  # pyright: ignore[reportUnusedFunction]
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
        command_id: UUID,
        now: datetime,
    ) -> GoalRunCommandResponse: ...

    async def cancel_goal_run(  # pyright: ignore[reportUnusedFunction]
        self,
        *,
        actor_id: UUID,
        goal_run_id: UUID,
        command_id: UUID,
        reason: str | None,
        now: datetime,
    ) -> GoalRunCommandResponse: ...


class DisabledGoalRunOperatorProvider:
    async def create_goal_run(self, **_kwargs: object) -> CreateGoalRunResponse:
        raise GoalRunUnavailable("goal run operator runtime is not configured")

    async def list_goal_runs(self, **_kwargs: object) -> ListGoalRunsResponse:
        raise GoalRunUnavailable("goal run operator runtime is not configured")

    async def goal_run_status(self, **_kwargs: object) -> GoalRunStatusResponse:
        raise GoalRunUnavailable("goal run operator runtime is not configured")

    async def submit_review(self, **_kwargs: object) -> GoalRunCommandResponse:
        raise GoalRunUnavailable("goal run operator runtime is not configured")

    async def resume_goal_run(self, **_kwargs: object) -> GoalRunCommandResponse:
        raise GoalRunUnavailable("goal run operator runtime is not configured")

    async def cancel_goal_run(self, **_kwargs: object) -> GoalRunCommandResponse:
        raise GoalRunUnavailable("goal run operator runtime is not configured")


def install_goal_run_operator_api(
    app: FastAPI,
    *,
    auth_service: ConsoleAuthServicePort,
    settings: ConsoleWebSettings,
    provider: GoalRunOperatorProvider,
    now_provider: Callable[[], datetime] | None = None,
) -> None:
    app.include_router(
        create_goal_run_operator_router(
            auth_service=auth_service,
            settings=settings,
            provider=provider,
            now_provider=now_provider,
        )
    )


def create_goal_run_operator_router(
    *,
    auth_service: ConsoleAuthServicePort,
    settings: ConsoleWebSettings,
    provider: GoalRunOperatorProvider,
    now_provider: Callable[[], datetime] | None = None,
) -> APIRouter:
    clock = now_provider or (lambda: datetime.now(UTC))
    origin_validator = OriginHostValidator(settings)
    router = APIRouter(prefix="/api/v1/internal/goal-runs", tags=["goal-run-operator"])

    def require_principal(request: Request) -> AuthenticatedPrincipal:
        try:
            origin_validator.validate_host(request)
        except RequestOriginRejected:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
        raw_session = request.cookies.get(settings.session_cookie_name, "")
        raw_csrf = request.cookies.get(settings.csrf_cookie_name, "")
        try:
            principal = auth_service.authenticate(raw_session, now=clock())
            auth_service.validate_csrf(principal, raw_csrf)
        except (AuthError, ValueError):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from None
        return principal

    def require_mutation_csrf(
        request: Request,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_principal)],
        x_csrf_token: Annotated[str, Header(min_length=1, max_length=512)],
    ) -> AuthenticatedPrincipal:
        try:
            origin_validator.validate_mutation(request)
        except RequestOriginRejected:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
        try:
            auth_service.validate_csrf(principal, x_csrf_token)
        except (AuthError, ValueError):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from None
        return principal

    @router.post("", response_model=CreateGoalRunResponse, status_code=status.HTTP_202_ACCEPTED)
    async def create_goal_run(
        body: CreateGoalRunRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_mutation_csrf)],
    ) -> CreateGoalRunResponse:
        return await _translate_goal_run_errors(
            provider.create_goal_run(
                actor_id=principal.user_id,
                command_id=body.command_id,
                registry_id=body.registry_id,
                source_id=body.source_id,
                mode=body.mode,
                match_config=body.match_config,
                candidate_id=body.candidate_id,
                candidate_profile_version_id=body.candidate_profile_version_id,
                gmail_dispatch=body.gmail_dispatch,
                now=clock(),
            )
        )

    @router.get("", response_model=ListGoalRunsResponse)
    async def list_goal_runs(
        principal: Annotated[AuthenticatedPrincipal, Depends(require_principal)],
        limit: int = 100,
    ) -> ListGoalRunsResponse:
        _require_limit(limit)
        return await _translate_goal_run_errors(
            provider.list_goal_runs(actor_id=principal.user_id, limit=limit)
        )

    @router.get("/{goal_run_id}", response_model=GoalRunStatusResponse)
    async def goal_run_status(
        goal_run_id: UUID,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_principal)],
    ) -> GoalRunStatusResponse:
        return await _translate_goal_run_errors(
            provider.goal_run_status(actor_id=principal.user_id, goal_run_id=goal_run_id)
        )

    @router.post(
        "/{goal_run_id}/reviews/{review_id}",
        response_model=GoalRunCommandResponse,
    )
    async def submit_review(
        goal_run_id: UUID,
        review_id: UUID,
        body: SubmitGoalRunReviewRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_mutation_csrf)],
    ) -> GoalRunCommandResponse:
        return await _translate_goal_run_errors(
            provider.submit_review(
                actor_id=principal.user_id,
                goal_run_id=goal_run_id,
                review_id=review_id,
                command_id=body.command_id,
                snapshot_sha256=body.snapshot_sha256,
                decision=body.decision,
                now=clock(),
            )
        )

    @router.post("/{goal_run_id}/resume", response_model=GoalRunCommandResponse)
    async def resume_goal_run(
        goal_run_id: UUID,
        body: ResumeGoalRunRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_mutation_csrf)],
    ) -> GoalRunCommandResponse:
        return await _translate_goal_run_errors(
            provider.resume_goal_run(
                actor_id=principal.user_id,
                goal_run_id=goal_run_id,
                command_id=body.command_id,
                now=clock(),
            )
        )

    @router.post("/{goal_run_id}/cancel", response_model=GoalRunCommandResponse)
    async def cancel_goal_run(
        goal_run_id: UUID,
        body: CancelGoalRunRequest,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_mutation_csrf)],
    ) -> GoalRunCommandResponse:
        return await _translate_goal_run_errors(
            provider.cancel_goal_run(
                actor_id=principal.user_id,
                goal_run_id=goal_run_id,
                command_id=body.command_id,
                reason=body.reason,
                now=clock(),
            )
        )

    return router


async def _translate_goal_run_errors[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except GoalRunUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from None
    except GoalRunConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Request could not be processed",
        ) from None
    except GoalRunNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


def _require_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be between 1 and 500",
        )


__all__: Sequence[str] = (
    "CancelGoalRunRequest",
    "CreateGoalRunRequest",
    "CreateGoalRunResponse",
    "DisabledGoalRunOperatorProvider",
    "GoalRunCommandResponse",
    "GoalRunConflict",
    "GoalRunContract",
    "GoalRunGmailAttachmentRef",
    "GoalRunMatchConfig",
    "GoalRunMaterialRef",
    "GoalRunMode",
    "GoalRunNotFound",
    "GoalRunOperatorProvider",
    "GoalRunPhaseName",
    "GoalRunReviewActionLabel",
    "GoalRunReviewProjection",
    "GoalRunReviewedGmailDispatchConfig",
    "GoalRunStatusResponse",
    "GoalRunSummary",
    "GoalRunUnavailable",
    "ListGoalRunsResponse",
    "ResumeGoalRunRequest",
    "SubmitGoalRunReviewRequest",
    "create_goal_run_operator_router",
    "install_goal_run_operator_api",
)
