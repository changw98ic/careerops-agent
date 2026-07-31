"""Review-only smart form preview/apply API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Request, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from careerops.api.errors import (
    DependencyNotReadyError,
    SmartIntakeDisabledError,
)
from careerops.application.smart_intake import (
    ApplyDecisionInput,
    SmartIntakeRequest,
    SmartIntakeService,
    SmartPreviewResult,
)
from careerops.orchestration.capability_resolver import CapabilityKind

# Audit actor for the local single-user console (post login-removal). There is
# no session principal; the smart-intake audit trail records this constant.
AUDIT_ACTOR = "local"

router = APIRouter(
    prefix="/api/v1/candidates/{candidate_id}/smart-intake", tags=["smart-intake"]
)


def _empty_uuid_list() -> list[UUID]:
    return []


class SmartIntakeTextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["text"]
    text: str = Field(min_length=1, max_length=12_000)


class SmartIntakeContextRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_version_id: UUID | None = None


class SmartIntakeInterviewRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_job_id: UUID
    job_version_id: UUID
    resume_version_id: UUID
    profile_version_id: UUID | None = None
    evidence_ids: list[UUID] = Field(default_factory=_empty_uuid_list, max_length=50)


class SmartIntakePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["profile", "interview_context"]
    input: SmartIntakeTextInput
    idempotency_key: str = Field(min_length=1, max_length=128)
    context_refs: SmartIntakeContextRefs | None = None
    interview_refs: SmartIntakeInterviewRefs | None = None

    @model_validator(mode="after")
    def validate_context(self) -> SmartIntakePreviewRequest:
        if self.target == "profile":
            if self.interview_refs is not None:
                raise ValueError("profile smart intake does not accept interview references")
        else:
            if self.interview_refs is None:
                raise ValueError("interview_context smart intake requires interview_refs")
            if len(self.input.text) > 2_000:
                raise ValueError("interview_context text must be at most 2000 characters")
            if (
                self.context_refs is not None
                and self.context_refs.profile_version_id != self.interview_refs.profile_version_id
            ):
                raise ValueError("profile version references must agree")
        return self


class SmartIntakeSourceRefResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_offset: int = Field(ge=0, le=12_000)
    end_offset: int = Field(ge=0, le=12_000)


def _empty_source_ref_list() -> list[SmartIntakeSourceRefResponse]:
    return []


class SmartIntakeFieldResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    value: StrictStr | StrictInt | StrictFloat | StrictBool | None = None
    value_type: Literal["string", "string_list", "integer", "number", "boolean"]
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["proposed", "unknown", "blocked"]
    reason: str = Field(default="", max_length=240)
    source_refs: list[SmartIntakeSourceRefResponse] = Field(
        default_factory=_empty_source_ref_list, max_length=3
    )


class SmartIntakeDraftPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, StrictStr | StrictInt | StrictFloat | StrictBool | None] = Field(
        default_factory=dict, max_length=80
    )


class SmartIntakePreviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: UUID
    candidate_id: UUID
    target: Literal["profile", "interview_context"]
    state: Literal["ready", "unavailable", "abstained", "invalid", "stale", "expired", "revoked"]
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fields: list[SmartIntakeFieldResponse] = Field(max_length=80)
    expires_at: datetime
    model_id: str
    prompt_version: str
    draft_patch: SmartIntakeDraftPatch | None = None
    decision_set_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SmartIntakeCapabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    released: bool
    provider_enabled: bool
    manual_fallback: Literal[True] = True


SmartIntakeDecisionValue = StrictStr | StrictInt | StrictFloat | StrictBool | None


class SmartIntakeDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=80)
    decision: Literal["accept", "edit", "reject", "unknown"]
    value: SmartIntakeDecisionValue = None
    reason: str = Field(default="", max_length=240)


class SmartIntakeApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply_idempotency_key: str = Field(min_length=1, max_length=128)
    context_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    decision_set_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    decisions: list[SmartIntakeDecisionRequest] = Field(max_length=80)


@router.get("/capability", response_model=SmartIntakeCapabilityResponse)
async def get_capability(
    request: Request,
    response: Response,
    candidate_id: UUID,
) -> SmartIntakeCapabilityResponse:
    del candidate_id
    resolver = getattr(request.app.state, "capability_resolver", None)
    if resolver is None:
        raise DependencyNotReadyError("capability resolver is not wired")
    service = _service(request)
    decision = resolver.decide(CapabilityKind.SMART_INTAKE)
    response.headers["Cache-Control"] = "no-store"
    return SmartIntakeCapabilityResponse(
        released=bool(decision.released),
        provider_enabled=service.provider_enabled,
    )


@router.post("/previews", response_model=SmartIntakePreviewResponse)
async def create_preview(
    body: SmartIntakePreviewRequest,
    request: Request,
    response: Response,
    candidate_id: UUID,
) -> SmartIntakePreviewResponse:
    _authorize(request, candidate_id, consume_rate_limit=True)
    service = _service(request)
    result = service.create_preview(
        candidate_id,
        SmartIntakeRequest(
            target=body.target,
            text=body.input.text,
            idempotency_key=body.idempotency_key,
            profile_version_id=(
                body.context_refs.profile_version_id
                if body.target == "profile" and body.context_refs is not None
                else body.interview_refs.profile_version_id
                if body.interview_refs is not None
                else None
            ),
            canonical_job_id=(
                body.interview_refs.canonical_job_id if body.interview_refs is not None else None
            ),
            job_version_id=(
                body.interview_refs.job_version_id if body.interview_refs is not None else None
            ),
            resume_version_id=(
                body.interview_refs.resume_version_id if body.interview_refs is not None else None
            ),
            evidence_ids=(
                tuple(body.interview_refs.evidence_ids) if body.interview_refs is not None else ()
            ),
        ),
        actor_id=AUDIT_ACTOR,
    )
    response.headers["Cache-Control"] = "no-store"
    return _to_response(result)


@router.get("/previews/{preview_id}", response_model=SmartIntakePreviewResponse)
async def get_preview(
    preview_id: UUID,
    request: Request,
    response: Response,
    candidate_id: UUID,
) -> SmartIntakePreviewResponse:
    # Cross-candidate access is resolved by the service.
    result = _service(request).get_preview(candidate_id, preview_id)
    response.headers["Cache-Control"] = "no-store"
    return _to_response(result)


@router.post("/previews/{preview_id}/apply", response_model=SmartIntakePreviewResponse)
async def apply_preview(
    preview_id: UUID,
    body: SmartIntakeApplyRequest,
    request: Request,
    response: Response,
    candidate_id: UUID,
) -> SmartIntakePreviewResponse:
    _authorize(request, candidate_id, consume_rate_limit=False)
    decisions = tuple(
        ApplyDecisionInput(
            path=item.path,
            decision=item.decision,
            value=item.value,
            reason=item.reason,
        )
        for item in body.decisions
    )
    result = _service(request).apply_preview(
        candidate_id,
        preview_id,
        apply_idempotency_key=body.apply_idempotency_key,
        context_digest=body.context_digest,
        decision_set_hash=body.decision_set_hash,
        decisions=decisions,
        actor_id=AUDIT_ACTOR,
    )
    response.headers["Cache-Control"] = "no-store"
    return _to_response(result)


def _service(request: Request) -> SmartIntakeService:
    service = getattr(request.app.state, "smart_intake_service", None)
    if not isinstance(service, SmartIntakeService):
        raise DependencyNotReadyError("smart intake service is not wired")
    return service


def _authorize(request: Request, candidate_id: UUID, *, consume_rate_limit: bool) -> None:
    resolver = getattr(request.app.state, "capability_resolver", None)
    if resolver is None:
        raise DependencyNotReadyError("capability resolver is not wired")
    decision = resolver.decide(CapabilityKind.SMART_INTAKE)
    if not decision.released:
        raise SmartIntakeDisabledError()
    if consume_rate_limit:
        limiter = getattr(request.app.state, "smart_intake_rate_limiter", None)
        if limiter is None:
            raise DependencyNotReadyError("smart intake rate limiter is not wired")


def _to_response(result: SmartPreviewResult) -> SmartIntakePreviewResponse:
    return SmartIntakePreviewResponse(
        preview_id=result.id,
        candidate_id=result.candidate_id,
        target=cast(Literal["profile", "interview_context"], result.target),
        state=cast(
            Literal["ready", "unavailable", "abstained", "invalid", "stale", "expired", "revoked"],
            result.state,
        ),
        input_digest=result.input_digest,
        context_digest=result.context_digest,
        fields=[SmartIntakeFieldResponse.model_validate(item) for item in result.fields],
        expires_at=result.expires_at,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        draft_patch=(
            SmartIntakeDraftPatch.model_validate(result.draft_patch)
            if result.draft_patch is not None
            else None
        ),
        decision_set_hash=result.decision_set_hash,
    )
