"""Crawl-source API routes (Section 4, task 4.7).

Additive routes under ``/api/v1/crawl-sources`` backed by
:class:`CrawlSourceService`. Every route resolves the candidate SERVER-SIDE via
:func:`require_candidate_id`, pulls the service through
:func:`require_repository` (503 on missing wiring — Iron Rule 7), and passes
through the Phase-0 ``CRAWL_PLAN_MANAGEMENT`` capability gate (released by
default; Iron Rule 8). A client-supplied ``candidate_id`` is NEVER honored for
ownership scoping.

Sources DECLARE SCOPE ONLY (Iron Rule 6): recording trust / terms / robots
status here does not bypass the crawl policy layer
(:mod:`careerops.application.crawl_policy`), which stays authoritative and
fails closed on ``blocked`` / unknown.
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
from careerops.api.errors import InvalidStateError
from careerops.application.crawl_plan_service import CrawlSourceService
from careerops.domain.crawl_plans import CrawlPolicyStatus, CrawlSourceState
from careerops.orchestration.capability_resolver import CapabilityKind

router = APIRouter(
    prefix="/api/v1/crawl-sources",
    tags=["crawl-sources"],
    dependencies=[Depends(require_capability(CapabilityKind.CRAWL_PLAN_MANAGEMENT))],
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class CrawlSourceWriteRequest(BaseModel):
    """Body for register / update. Carries NO candidate_id (server-resolved)."""

    company_id: str
    source_type: str
    source_identifier: str
    base_url: str
    enabled: bool = False
    adapter_version: str = ""
    trust_status: str = "unknown"
    terms_status: str = "unknown"
    robots_status: str = "unknown"
    state: str | None = None  # optional override for the update path only


class CrawlSourceUpdateRequest(BaseModel):
    """Partial update body. All fields optional; omitted fields are preserved."""

    base_url: str | None = None
    source_identifier: str | None = None
    adapter_version: str | None = None
    trust_status: str | None = None
    terms_status: str | None = None
    robots_status: str | None = None
    state: str | None = None


class CrawlSourceResponse(BaseModel):
    id: str
    company_id: str
    source_type: str
    source_identifier: str
    base_url: str
    state: str
    trust_status: str
    terms_status: str
    robots_status: str
    adapter_version: str = ""
    enabled: bool = False
    verified_at: str | None = None
    last_discovery_at: str | None = None
    last_run_at: str | None = None
    last_run_metadata: dict[str, object] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


class CrawlSourceListResponse(BaseModel):
    items: list[CrawlSourceResponse] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


def _service(request: Request) -> CrawlSourceService:
    service = require_repository(request, "crawl_source_service")
    return service  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_sources(
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlSourceService, Depends(_service)],
    limit: int = Query(default=50, ge=1, le=200),
) -> CrawlSourceListResponse:
    sources = service.list_sources(candidate_id, limit=limit)
    return CrawlSourceListResponse(items=[_to_response(s) for s in sources], total=len(sources))


@router.post("", status_code=201)
def register_source(
    body: CrawlSourceWriteRequest,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlSourceService, Depends(_service)],
) -> CrawlSourceResponse:
    """Register a trusted ATS / official source.

    Raises ``INVALID_STATE`` (409) for an unsupported source type, empty
    identifier, or empty URL. The crawl policy layer stays authoritative —
    recording ``unknown`` trust / terms / robots does not bypass it.
    """
    source = service.register(
        candidate_id,
        company_id=UUID(body.company_id),
        source_type=body.source_type,
        source_identifier=body.source_identifier,
        base_url=body.base_url,
        enabled=body.enabled,
        adapter_version=body.adapter_version,
        trust_status=_policy_status(body.trust_status),
        terms_status=_policy_status(body.terms_status),
        robots_status=_policy_status(body.robots_status),
    )
    return _to_response(source)


@router.get("/{source_id}")
def get_source(
    source_id: UUID,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlSourceService, Depends(_service)],
) -> CrawlSourceResponse:
    return _to_response(service.get_source(candidate_id, source_id))


@router.patch("/{source_id}")
def update_source(
    source_id: UUID,
    body: CrawlSourceUpdateRequest,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlSourceService, Depends(_service)],
) -> CrawlSourceResponse:
    source = service.update(
        candidate_id,
        source_id,
        base_url=body.base_url,
        source_identifier=body.source_identifier,
        adapter_version=body.adapter_version,
        trust_status=_policy_status(body.trust_status) if body.trust_status else None,
        terms_status=_policy_status(body.terms_status) if body.terms_status else None,
        robots_status=_policy_status(body.robots_status) if body.robots_status else None,
        state=_source_state(body.state) if body.state else None,
    )
    return _to_response(source)


@router.delete("/{source_id}", status_code=204)
def remove_source(
    source_id: UUID,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlSourceService, Depends(_service)],
) -> None:
    service.remove(candidate_id, source_id)


@router.post("/{source_id}/pause")
def pause_source(
    source_id: UUID,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlSourceService, Depends(_service)],
) -> CrawlSourceResponse:
    """Idempotent: flip ``enabled`` off. Retains prior postings / evidence /
    run history; only future runs are prevented."""
    return _to_response(service.pause(candidate_id, source_id))


@router.post("/{source_id}/resume")
def resume_source(
    source_id: UUID,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    service: Annotated[CrawlSourceService, Depends(_service)],
) -> CrawlSourceResponse:
    """Idempotent: flip ``enabled`` on and return a PAUSED source to ACTIVE.
    A policy-BLOCKED source cannot be resumed (raises ``INVALID_STATE``)."""
    return _to_response(service.resume(candidate_id, source_id))


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _policy_status(value: str) -> CrawlPolicyStatus:
    try:
        return CrawlPolicyStatus(value)
    except ValueError:
        return CrawlPolicyStatus.UNKNOWN


def _source_state(value: str) -> CrawlSourceState:
    try:
        return CrawlSourceState(value)
    except ValueError as err:
        raise InvalidStateError(f"unsupported crawl source state '{value}'") from err


def _to_response(source: object) -> CrawlSourceResponse:
    return CrawlSourceResponse(
        id=str(source.id),  # type: ignore[attr-defined]
        company_id=str(source.company_id),  # type: ignore[attr-defined]
        source_type=source.source_type.value,  # type: ignore[attr-defined]
        source_identifier=source.source_identifier,  # type: ignore[attr-defined]
        base_url=source.base_url,  # type: ignore[attr-defined]
        state=source.state.value,  # type: ignore[attr-defined]
        trust_status=source.trust_status.value,  # type: ignore[attr-defined]
        terms_status=source.terms_status.value,  # type: ignore[attr-defined]
        robots_status=source.robots_status.value,  # type: ignore[attr-defined]
        adapter_version=source.adapter_version,  # type: ignore[attr-defined]
        enabled=source.enabled,  # type: ignore[attr-defined]
        verified_at=_iso(source.verified_at),  # type: ignore[attr-defined]
        last_discovery_at=_iso(source.last_discovery_at),  # type: ignore[attr-defined]
        last_run_at=_iso(source.last_run_at),  # type: ignore[attr-defined]
        last_run_metadata=dict(source.last_run_metadata),  # type: ignore[attr-defined]
        created_at=_iso(source.created_at),  # type: ignore[attr-defined]
        updated_at=_iso(source.updated_at),  # type: ignore[attr-defined]
    )


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
