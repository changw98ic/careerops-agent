"""REST API routes for companies and jobs (M1.11)."""

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field

from careerops.api.errors import DependencyNotReadyError, NotFoundError

router = APIRouter(prefix="/api/v1", tags=["jobs"])


class CompanyResponse(BaseModel):
    id: str
    name: str
    normalized_name: str
    official_domains: list[str] = Field(default_factory=list)
    terms_status: str = "unknown"
    created_at: str | None = None


class CompanyListResponse(BaseModel):
    items: list[CompanyResponse] = Field(default_factory=list)
    total: int = 0
    next_cursor: str | None = None
    has_more: bool = False


class SourceInfoResponse(BaseModel):
    type: str
    identifier: str
    base_url: str = ""
    state: str = ""


class JobPostingResponse(BaseModel):
    id: str
    source_id: str
    external_id: str
    canonical_url: str = ""
    source_state: str = "active"
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    source: SourceInfoResponse | None = None


class CanonicalJobResponse(BaseModel):
    id: str
    company_id: str
    canonical_title: str
    aggregate_state: str = "active"
    current_apply_url: str = ""
    aggregate_status: str = "unknown"
    postings: list[JobPostingResponse] = Field(default_factory=list)


class JobListResponse(BaseModel):
    items: list[CanonicalJobResponse] = Field(default_factory=list)
    total: int = 0
    next_cursor: str | None = None
    has_more: bool = False


class JobDetailResponse(BaseModel):
    canonical_job: CanonicalJobResponse
    versions: list[dict[str, Any]] = Field(default_factory=list)
    merge_decisions: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/companies", response_model=CompanyListResponse, summary="List companies")
async def list_companies(
    request: Request,
    response: Response,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> CompanyListResponse:
    response.headers["Cache-Control"] = "no-store"
    repo = _get_job_repository(request)
    if repo is None:
        return CompanyListResponse(items=[], total=0)
    result = repo.list_companies(cursor=cursor, limit=limit)
    items = [
        CompanyResponse(
            id=str(c["id"]),
            name=c["name"],
            normalized_name=c["normalized_name"],
            official_domains=c.get("official_domains", []),
            terms_status=c.get("terms_status", "unknown"),
            created_at=c.get("created_at"),
        )
        for c in result["items"]
    ]
    return CompanyListResponse(
        items=items,
        total=result["total"],
        next_cursor=result.get("next_cursor"),
        has_more=result.get("next_cursor") is not None,
    )


@router.get("/jobs", response_model=JobListResponse, summary="List canonical jobs (Inbox)")
async def list_jobs(
    request: Request,
    response: Response,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    state: str | None = Query(default=None),
    q: str | None = Query(default=None),
) -> JobListResponse:
    response.headers["Cache-Control"] = "no-store"
    repo = _get_job_repository(request)
    if repo is None:
        return JobListResponse(items=[], total=0)
    result = repo.list_canonical_jobs(cursor=cursor, limit=limit, state=state, q=q)
    items = [
        CanonicalJobResponse(
            id=str(j["id"]),
            company_id=str(j["company_id"]),
            canonical_title=j["canonical_title"],
            aggregate_state=j.get("aggregate_state", "active"),
            current_apply_url=j.get("current_apply_url", ""),
            aggregate_status=j.get("aggregate_status", "unknown"),
            postings=[JobPostingResponse(**p) for p in j.get("postings", [])],
        )
        for j in result["items"]
    ]
    return JobListResponse(
        items=items,
        total=result["total"],
        next_cursor=result.get("next_cursor"),
        has_more=result.get("next_cursor") is not None,
    )


@router.get("/jobs/{job_id}", response_model=JobDetailResponse, summary="Job detail")
async def get_job_detail(
    job_id: str,
    request: Request,
    response: Response,
) -> JobDetailResponse:
    response.headers["Cache-Control"] = "no-store"
    repo = _get_job_repository(request)
    if repo is None:
        raise DependencyNotReadyError("Job repository not available")
    detail = repo.get_job_detail(job_id)
    if detail is None:
        raise NotFoundError(f"Job {job_id} not found")
    postings_data = detail.get("postings", [])
    postings = [
        JobPostingResponse(
            id=p["id"],
            source_id=p["source_id"],
            external_id=p["external_id"],
            canonical_url=p["canonical_url"],
            source_state=p.get("source_state", "active"),
            first_seen_at=p.get("first_seen_at"),
            last_seen_at=p.get("last_seen_at"),
            source=SourceInfoResponse(**p["source"]) if p.get("source") else None,
        )
        for p in postings_data
    ]
    return JobDetailResponse(
        canonical_job=CanonicalJobResponse(
            id=str(detail["id"]),
            company_id=str(detail["company_id"]),
            canonical_title=detail["canonical_title"],
            aggregate_state=detail.get("aggregate_state", "active"),
            current_apply_url=detail.get("current_apply_url", ""),
            aggregate_status=detail.get("aggregate_status", "unknown"),
            postings=postings,
        ),
        versions=detail.get("versions", []),
        merge_decisions=detail.get("merge_decisions", []),
    )


def _get_job_repository(request: Request) -> Any:
    return getattr(request.app.state, "job_read_repository", None)
