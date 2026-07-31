"""Global candidate CRUD routes (auth-rm Task 2).

These routes are the candidate management surface introduced by removing the
console login + multi-candidate path params. They are GLOBAL (no candidate
path prefix) and read ``app.state.candidate_service`` via the ``_service``
dependency. When the service is absent the dependency raises 503, so a
runtime that has not wired the service fails closed rather than silently
returning empty results.

Endpoints:
- ``GET    /api/v1/candidates``          — list candidates (default limit 50)
- ``POST   /api/v1/candidates``          — create a candidate (body ``{display_name}``)
- ``GET    /api/v1/candidates/{id}``     — fetch one candidate by id (404 if missing)
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1/candidates", tags=["candidates"])


class CreateCandidateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)


class CandidateResponse(BaseModel):
    id: str
    display_name: str


class CandidateListResponse(BaseModel):
    items: list[CandidateResponse]


def _service(request: Request):
    """Resolve ``app.state.candidate_service`` or fail closed with 503."""
    svc = getattr(request.app.state, "candidate_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="candidate service not ready")
    return svc


@router.get("", response_model=CandidateListResponse)
async def list_candidates(service=Depends(_service)) -> CandidateListResponse:
    items = [
        CandidateResponse(id=str(c.id), display_name=c.display_name)
        for c in service.list_all()
    ]
    return CandidateListResponse(items=items)


@router.post("", response_model=CandidateResponse, status_code=201)
async def create_candidate(
    body: CreateCandidateRequest, service=Depends(_service)
) -> CandidateResponse:
    c = service.create(body.display_name)
    return CandidateResponse(id=str(c.id), display_name=c.display_name)


@router.get("/{candidate_id}", response_model=CandidateResponse)
async def get_candidate(
    candidate_id: UUID, service=Depends(_service)
) -> CandidateResponse:
    c = service.get(candidate_id)
    if c is None:
        raise HTTPException(status_code=404, detail="candidate not found")
    return CandidateResponse(id=str(c.id), display_name=c.display_name)
