"""Candidate-facing, review-only Agent routes."""

# Dynamic structured result mappings are bounded by AgentRuntime and the model
# JSON schemas; keep route mapping annotations compact.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from careerops.api.auth_dependency import require_candidate_id
from careerops.api.capability_dependency import require_repository
from careerops.application.agent_services import (
    AgentStartInput,
)
from careerops.domain.agent_runs import AgentCapability, AgentReviewDecision, AgentRun

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


class AgentStartRequest(BaseModel):
    resume_version_id: UUID
    canonical_job_id: UUID
    job_version_id: UUID
    profile_version_id: UUID | None = None
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=50)
    user_context: str = Field(default="", max_length=2_000)


class AgentReviewRequest(BaseModel):
    decision: AgentReviewDecision
    note: str = Field(default="", max_length=1_000)
    edited_result: dict[str, object] = Field(default_factory=dict)


class AgentRunResponse(BaseModel):
    id: str
    candidate_id: str
    capability: str
    state: str
    idempotency_key: str
    input_hash: str
    input_identities: dict[str, str]
    evidence_ids: list[str]
    schema_version: str
    prompt_version: str
    model_id: str
    trace_id: str
    result: dict[str, object]
    error_category: str
    input_tokens: int
    output_tokens: int
    review_decision: str | None = None
    reviewed_by: str = ""
    review_note: str = ""
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    reviewed_at: str | None = None


class AgentRunListResponse(BaseModel):
    items: list[AgentRunResponse] = Field(default_factory=list)
    total: int = 0


def _start_input(body: AgentStartRequest) -> AgentStartInput:
    return AgentStartInput(
        resume_version_id=body.resume_version_id,
        canonical_job_id=body.canonical_job_id,
        job_version_id=body.job_version_id,
        profile_version_id=body.profile_version_id,
        evidence_ids=tuple(body.evidence_ids),
        user_context=body.user_context,
    )


@router.post("/resume-review")
def start_resume_review(
    body: AgentStartRequest,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    request: Request,
) -> AgentRunResponse:
    service = require_repository(request, "resume_review_service")
    run = service.start(candidate_id, _start_input(body))  # type: ignore[attr-defined]
    return _to_response(run)


@router.post("/interview-preparation")
def start_interview_preparation(
    body: AgentStartRequest,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    request: Request,
) -> AgentRunResponse:
    service = require_repository(request, "interview_preparation_service")
    run = service.start(candidate_id, _start_input(body))  # type: ignore[attr-defined]
    return _to_response(run)


@router.get("/runs")
def list_agent_runs(
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    capability: AgentCapability | None = None,
    limit: int = 50,
) -> AgentRunListResponse:
    runtime = require_repository(request, "agent_runtime")
    items = runtime.list(candidate_id, capability=capability, limit=limit)  # type: ignore[attr-defined]
    return AgentRunListResponse(items=[_to_response(item) for item in items], total=len(items))


@router.get("/runs/{run_id}")
def get_agent_run(
    run_id: UUID,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> AgentRunResponse:
    runtime = require_repository(request, "agent_runtime")
    return _to_response(runtime.get(candidate_id, run_id))  # type: ignore[attr-defined]


@router.post("/runs/{run_id}/review")
def review_agent_run(
    run_id: UUID,
    body: AgentReviewRequest,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> AgentRunResponse:
    runtime = require_repository(request, "agent_runtime")
    run = runtime.review(  # type: ignore[attr-defined]
        candidate_id,
        run_id,
        decision=body.decision,
        actor_id=str(candidate_id),
        note=body.note,
        edited_result=body.edited_result,
    )
    return _to_response(run)


@router.get("/runs/{run_id}/reviews")
def list_agent_reviews(
    run_id: UUID,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> dict[str, object]:
    runtime = require_repository(request, "agent_runtime")
    reviews = runtime.reviews(candidate_id, run_id)  # type: ignore[attr-defined]
    return {
        "items": [
            {
                "id": str(review.id),
                "run_id": str(review.run_id),
                "candidate_id": str(review.candidate_id),
                "decision": review.decision.value,
                "actor_id": review.actor_id,
                "note": review.note,
                "edited_result": dict(review.edited_result),
                "created_at": review.created_at.isoformat() if review.created_at else None,
            }
            for review in reviews
        ],
        "total": len(reviews),
    }


def _to_response(run: AgentRun) -> AgentRunResponse:
    return AgentRunResponse(
        id=str(run.id),
        candidate_id=str(run.candidate_id),
        capability=run.capability.value,
        state=run.state.value,
        idempotency_key=run.idempotency_key,
        input_hash=run.input_hash,
        input_identities=dict(run.input_identities),
        evidence_ids=[str(item) for item in run.evidence_ids],
        schema_version=run.schema_version,
        prompt_version=run.prompt_version,
        model_id=run.model_id,
        trace_id=run.trace_id,
        result=dict(run.result),
        error_category=run.error_category,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        review_decision=run.review_decision.value if run.review_decision else None,
        reviewed_by=run.reviewed_by,
        review_note=run.review_note,
        created_at=run.created_at.isoformat() if run.created_at else None,
        started_at=run.started_at.isoformat() if run.started_at else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        reviewed_at=run.reviewed_at.isoformat() if run.reviewed_at else None,
    )
