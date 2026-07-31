"""REST API routes for M2: candidates, evidence, matching, compensation."""

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

from datetime import UTC
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field

from careerops.api.errors import DependencyNotReadyError, NotFoundError

router = APIRouter(prefix="/api/v1", tags=["matching"])


class EvidenceItemResponse(BaseModel):
    id: str
    candidate_id: str
    kind: str
    name: str
    description: str = ""
    repository: str = ""
    commit_sha: str = ""
    path: str = ""
    symbol: str = ""
    verified: bool = False


class CandidateResponse(BaseModel):
    id: str
    display_name: str
    evidence_count: int = 0


class CandidateListResponse(BaseModel):
    items: list[CandidateResponse] = Field(default_factory=list)
    total: int = 0


class RequirementMatchResponse(BaseModel):
    requirement_name: str
    level: str
    confidence: float = 0.0
    reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class MatchResultResponse(BaseModel):
    id: str
    candidate_id: str
    canonical_job_id: str
    tier: str
    overall_score: float
    geographic_blocked: bool = False
    remote_verdict: str = "unknown"
    requirement_matches: list[RequirementMatchResponse] = Field(default_factory=list)
    rules_version: str = ""
    input_hash: str = ""
    output_hash: str = ""


class MatchListResponse(BaseModel):
    items: list[MatchResultResponse] = Field(default_factory=list)
    total: int = 0
    cursor: str | None = None


class RemoteEligibilityResponse(BaseModel):
    canonical_job_id: str
    verdict: str
    confidence: float = 0.0
    evidence_spans: list[str] = Field(default_factory=list)
    reason: str = ""
    rules_version: str = ""


class CompensationResponse(BaseModel):
    id: str
    canonical_job_id: str
    currency: str = ""
    amount_min: float | None = None
    amount_max: float | None = None
    period: str = ""
    normalized_amount_min: float | None = None
    normalized_amount_max: float | None = None
    normalized_currency: str = "CNY"
    score: str = "unknown"


class EvidenceImportRequest(BaseModel):
    kind: str
    name: str
    description: str = ""
    repository: str = ""
    commit_sha: str = ""
    path: str = ""
    symbol: str = ""
    source_url: str = ""


class MatchRequest(BaseModel):
    canonical_job_id: str


@router.get("/candidates", response_model=CandidateListResponse, summary="List candidates")
async def list_candidates(
    request: Request,
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
) -> CandidateListResponse:
    """List all candidates (global selector source).

    This is the global candidate list used by the frontend candidate selector.
    It is deliberately NOT scoped to one candidate; post-auth-removal it no
    longer resolves a candidate from the session.
    """
    response.headers["Cache-Control"] = "no-store"
    repo = _get_matching_repository(request)
    if repo is None:
        return CandidateListResponse(items=[], total=0)
    candidates = repo.list_candidates(limit=limit)
    items = [
        CandidateResponse(
            id=str(c["id"]),
            display_name=c["display_name"],
            evidence_count=c.get("evidence_count", 0),
        )
        for c in candidates
    ]
    return CandidateListResponse(items=items, total=len(items))


@router.post(
    "/candidates/{candidate_id}/evidence/import",
    response_model=EvidenceItemResponse,
    status_code=201,
    summary="Import candidate evidence",
)
async def import_evidence(
    candidate_id: UUID,
    body: EvidenceImportRequest,
    request: Request,
    response: Response,
) -> EvidenceItemResponse:
    response.headers["Cache-Control"] = "no-store"
    service = _get_evidence_service(request)
    if service is None:
        response.status_code = 503
        return EvidenceItemResponse(
            id="", candidate_id=str(candidate_id), kind=body.kind, name=body.name
        )
    from careerops.application.matching import EvidenceImportRequest as ServiceRequest
    from careerops.domain.candidates import EvidenceKind

    result = service.import_evidence(
        ServiceRequest(
            candidate_id=candidate_id,
            kind=EvidenceKind(body.kind),
            name=body.name,
            description=body.description,
            repository=body.repository,
            commit_sha=body.commit_sha,
            path=body.path,
            symbol=body.symbol,
            source_url=body.source_url,
        )
    )
    response.status_code = 201
    return EvidenceItemResponse(
        id=str(result.id),
        candidate_id=str(result.candidate_id),
        kind=result.kind.value,
        name=result.name,
        description=result.description,
        repository=result.repository,
        commit_sha=result.commit_sha,
        path=result.path,
        symbol=result.symbol,
        verified=result.verified,
    )


@router.get(
    "/jobs/{job_id}/remote-eligibility",
    response_model=RemoteEligibilityResponse,
    summary="Get remote eligibility for a job",
)
async def get_remote_eligibility(
    job_id: str,
    request: Request,
    response: Response,
) -> RemoteEligibilityResponse:
    response.headers["Cache-Control"] = "no-store"
    repo = _get_matching_repository(request)
    if repo is None:
        raise DependencyNotReadyError("Matching repository not available")
    eligibility = repo.get_remote_eligibility(job_id)
    if eligibility is None:
        raise NotFoundError(f"Remote eligibility for job {job_id} not found")
    return RemoteEligibilityResponse(
        canonical_job_id=str(eligibility["canonical_job_id"]),
        verdict=eligibility["verdict"],
        confidence=eligibility.get("confidence", 0.0),
        evidence_spans=eligibility.get("evidence_spans", []),
        reason=eligibility.get("reason", ""),
        rules_version=eligibility.get("rules_version", ""),
    )


@router.get(
    "/candidates/{candidate_id}/matches",
    response_model=MatchListResponse,
    summary="List match results",
)
async def list_matches(
    candidate_id: UUID,
    request: Request,
    response: Response,
    canonical_job_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> MatchListResponse:
    response.headers["Cache-Control"] = "no-store"
    repo = _get_matching_repository(request)
    if repo is None:
        return MatchListResponse(items=[], total=0)
    matches = repo.list_matches(
        candidate_id=str(candidate_id),
        canonical_job_id=canonical_job_id,
        cursor=cursor,
        limit=limit,
    )
    items = [
        MatchResultResponse(
            id=str(m["id"]),
            candidate_id=str(m["candidate_id"]),
            canonical_job_id=str(m["canonical_job_id"]),
            tier=m["tier"],
            overall_score=m["overall_score"],
            geographic_blocked=m.get("geographic_blocked", False),
            remote_verdict=m.get("remote_verdict", "unknown"),
            requirement_matches=[
                RequirementMatchResponse(
                    requirement_name=rm["requirement_name"],
                    level=rm["level"],
                    confidence=rm.get("confidence", 0.0),
                    reason=rm.get("reason", ""),
                    evidence_ids=rm.get("evidence_ids", []),
                )
                for rm in m.get("requirement_matches", [])
            ],
            rules_version=m.get("rules_version", ""),
            input_hash=m.get("input_hash", ""),
            output_hash=m.get("output_hash", ""),
        )
        for m in matches
    ]
    return MatchListResponse(items=items, total=len(items))


@router.post(
    "/candidates/{candidate_id}/matches/run",
    response_model=MatchResultResponse,
    status_code=201,
    summary="Run matching for a candidate against a job",
)
async def run_match(
    candidate_id: UUID,
    body: MatchRequest,
    request: Request,
    response: Response,
) -> MatchResultResponse:
    response.headers["Cache-Control"] = "no-store"
    orchestrator = _get_match_orchestrator(request)
    if orchestrator is None:
        response.status_code = 503
        return MatchResultResponse(
            id="",
            candidate_id=str(candidate_id),
            canonical_job_id=body.canonical_job_id,
            tier="not_recommended",
            overall_score=0.0,
        )
    from datetime import datetime

    result = orchestrator.run_match_for_request(
        candidate_id=candidate_id,
        canonical_job_id=_parse_uuid(body.canonical_job_id),
        now=datetime.now(tz=UTC),
    )
    response.status_code = 201
    return MatchResultResponse(
        id=str(result.id),
        candidate_id=str(result.candidate_id),
        canonical_job_id=str(result.canonical_job_id),
        tier=result.tier.value,
        overall_score=result.overall_score,
        geographic_blocked=result.geographic_blocked,
        remote_verdict=result.remote_verdict.value,
        requirement_matches=[
            RequirementMatchResponse(
                requirement_name=m.requirement_name,
                level=m.level.value,
                confidence=m.confidence,
                reason=m.reason,
                evidence_ids=[str(eid) for eid in m.evidence_ids],
            )
            for m in result.requirement_matches
        ],
        rules_version=result.rules_version,
        input_hash=result.input_hash,
        output_hash=result.output_hash,
    )


@router.get(
    "/jobs/{job_id}/compensation",
    response_model=CompensationResponse,
    summary="Get compensation for a job",
)
async def get_compensation(
    job_id: str,
    request: Request,
    response: Response,
) -> CompensationResponse:
    response.headers["Cache-Control"] = "no-store"
    repo = _get_matching_repository(request)
    if repo is None:
        raise DependencyNotReadyError("Matching repository not available")
    comp = repo.get_compensation(job_id)
    if comp is None:
        raise NotFoundError(f"Compensation for job {job_id} not found")
    return CompensationResponse(
        id=str(comp["id"]),
        canonical_job_id=str(comp["canonical_job_id"]),
        currency=comp.get("currency", ""),
        amount_min=comp.get("amount_min"),
        amount_max=comp.get("amount_max"),
        period=comp.get("period", ""),
        normalized_amount_min=comp.get("normalized_amount_min"),
        normalized_amount_max=comp.get("normalized_amount_max"),
        normalized_currency=comp.get("normalized_currency", "CNY"),
        score=comp.get("score", "unknown"),
    )


def _get_matching_repository(request: Request) -> Any:
    return getattr(request.app.state, "matching_repository", None)


def _get_evidence_service(request: Request) -> Any:
    return getattr(request.app.state, "evidence_import_service", None)


def _get_match_orchestrator(request: Request) -> Any:
    return getattr(request.app.state, "match_orchestrator", None)


def _parse_uuid(value: str) -> Any:
    from uuid import UUID

    return UUID(value)
