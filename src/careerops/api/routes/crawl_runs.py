"""Crawl-run API routes (Section 4, task 4.7).

Additive routes under ``/api/v1/crawl-runs`` backed by :class:`CrawlRunService`.
Read-only inspection of run history: list runs for the owner, list runs bound
to a plan version, and fetch a single run detail. Section 4 records run
identity + terminal counters; the actual posting/version ingest lives in
Section 5. Same Iron-rule posture as the other crawl routers: server-side
candidate ownership, ``require_repository`` (503) on missing wiring, and the
``CRAWL_PLAN_MANAGEMENT`` capability gate (released by default).
"""

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from careerops.api.auth_dependency import require_candidate_id
from careerops.api.capability_dependency import require_capability, require_repository
from careerops.application.crawl_plan_service import CrawlRunService
from careerops.orchestration.capability_resolver import CapabilityKind

router = APIRouter(
    prefix="/api/v1/crawl-runs",
    tags=["crawl-runs"],
    dependencies=[Depends(require_capability(CapabilityKind.CRAWL_PLAN_MANAGEMENT))],
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class PerRunLimitsOut(BaseModel):
    max_postings_per_source: int | None = None
    max_sources: int | None = None
    timeout_seconds: int | None = None


class RunCountersOut(BaseModel):
    discovered: int = 0
    updated: int = 0
    closed: int = 0
    failed: int = 0


class RunResponse(BaseModel):
    id: str
    plan_version_id: str
    run_identity: str
    state: str
    source_set: list[str] = Field(default_factory=list)
    limits: PerRunLimitsOut = Field(default_factory=PerRunLimitsOut)
    counters: RunCountersOut = Field(default_factory=RunCountersOut)
    error_category: str = ""
    created_at: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    next_eligible_at: str | None = None


class RunListResponse(BaseModel):
    items: list[RunResponse] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


def _service(request: Request) -> CrawlRunService:
    return require_repository(request, "crawl_run_service")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_runs(
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlRunService, Depends(_service)],
    limit: int = Query(default=50, ge=1, le=200),
    plan_version_id: UUID | None = None,
) -> RunListResponse:
    """Return the owner's runs, newest first.

    Without ``plan_version_id`` the list spans all of the owner's plan
    versions. With it, the list is scoped to one plan version's run history
    (ownership of the plan version is verified first so a client-supplied id
    for another owner leaks nothing — Iron Rule 2).
    """
    if plan_version_id is not None:
        runs = service.list_runs_for_plan(candidate_id, plan_version_id, limit=limit)
    else:
        runs = service.list_runs(candidate_id, limit=limit)
    return RunListResponse(items=[_to_response(r) for r in runs], total=len(runs))


@router.get("/{run_id}")
def get_run(
    run_id: UUID,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlRunService, Depends(_service)],
) -> RunResponse:
    """Return one run, scoped transitively via its plan version's owner. A run
    whose plan version belongs to a different candidate raises ``NOT_FOUND``
    (Iron Rule 2 — ownership substitution never leaks a row)."""
    return _to_response(service.get_run(candidate_id, run_id))


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _to_response(run: object) -> RunResponse:
    return RunResponse(
        id=str(run.id),  # type: ignore[attr-defined]
        plan_version_id=str(run.plan_version_id),  # type: ignore[attr-defined]
        run_identity=run.run_identity,  # type: ignore[attr-defined]
        state=run.state.value,  # type: ignore[attr-defined]
        source_set=[str(s) for s in run.source_set],  # type: ignore[attr-defined]
        limits=PerRunLimitsOut(
            max_postings_per_source=run.limits.max_postings_per_source,  # type: ignore[attr-defined]
            max_sources=run.limits.max_sources,  # type: ignore[attr-defined]
            timeout_seconds=run.limits.timeout_seconds,  # type: ignore[attr-defined]
        ),
        counters=RunCountersOut(
            discovered=run.counters.discovered,  # type: ignore[attr-defined]
            updated=run.counters.updated,  # type: ignore[attr-defined]
            closed=run.counters.closed,  # type: ignore[attr-defined]
            failed=run.counters.failed,  # type: ignore[attr-defined]
        ),
        error_category=run.error_category,  # type: ignore[attr-defined]
        created_at=_iso(run.created_at),  # type: ignore[attr-defined]
        started_at=_iso(run.started_at),  # type: ignore[attr-defined]
        ended_at=_iso(run.ended_at),  # type: ignore[attr-defined]
        next_eligible_at=_iso(run.next_eligible_at),  # type: ignore[attr-defined]
    )


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
