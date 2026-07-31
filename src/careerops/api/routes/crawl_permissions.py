"""Crawl-permission API routes (real-autonomous-career-loop Phase 6.3).

Ownership-scoped CRUD for source-specific crawl permission requests.
Every route takes the candidate from the URL path (Iron Rule 2) and pulls the
``CrawlPermissionService`` from ``app.state``.

Endpoints:
- GET    /api/v1/candidates/{candidate_id}/crawl-permissions                     -- list ownership-scoped requests
- GET    /api/v1/candidates/{candidate_id}/crawl-permissions/{permission_id}     -- get one permission
- GET    /api/v1/candidates/{candidate_id}/crawl-sources/{source_id}/permissions  -- list permissions for a source
- POST   /api/v1/candidates/{candidate_id}/crawl-sources/{source_id}/permissions  -- request permission (pauses source)
- POST   /api/v1/candidates/{candidate_id}/crawl-permissions/{permission_id}/grant  -- grant
- POST   /api/v1/candidates/{candidate_id}/crawl-permissions/{permission_id}/deny   -- deny
- POST   /api/v1/candidates/{candidate_id}/crawl-permissions/{permission_id}/revoke -- revoke
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from careerops.api.capability_dependency import require_repository
from careerops.api.errors import InvalidStateError, NotFoundError
from careerops.application.crawl_permission_service import CrawlPermissionService
from careerops.application.crawl_plan_service import CrawlSourceService
from careerops.domain.crawl_attempts import CrawlPermissionState

router = APIRouter(
    prefix="/api/v1/candidates/{candidate_id}",
    tags=["crawl-permissions"],
)
_LOGIN_TOOLS: dict[str, object] = {}


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class PermissionResponse(BaseModel):
    id: str
    source_id: str
    state: str
    domain_scope: str = ""
    disclosed_terms: dict[str, object] = Field(default_factory=dict)
    requested_at: str | None = None
    granted_at: str | None = None
    denied_at: str | None = None
    revoked_at: str | None = None
    expired_at: str | None = None
    expires_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    session_ref: str | None = None


class PermissionListResponse(BaseModel):
    items: list[PermissionResponse] = Field(
        default_factory=lambda: list[PermissionResponse]()
    )
    total: int = 0


class PermissionRequest(BaseModel):
    """Body for requesting a new crawl permission."""

    login_evidence: str = ""
    domain_scope: str = ""
    disclosed_terms: dict[str, object] = Field(default_factory=dict)


class DecisionRequest(BaseModel):
    """Body for grant / deny / revoke decisions."""

    reason: str = ""
    session_ref: str = ""


class LoginSessionResponse(BaseModel):
    opened: bool
    session_ref: str


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


def _service(request: Request) -> CrawlPermissionService:
    svc = require_repository(request, "crawl_permission_service")
    return svc  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Routes -- permission-scoped
# ---------------------------------------------------------------------------


@router.get("/crawl-permissions", response_model=PermissionListResponse)
def list_all_permissions(
    candidate_id: UUID,
    service: Annotated[CrawlPermissionService, Depends(_service)],
    limit: int = Query(default=50, ge=1, le=200),
) -> PermissionListResponse:
    """List all permission requests across all sources for the owner.

    This is a convenience endpoint; the ownership scope is the caller's
    server-resolved candidate_id.
    """
    # We iterate the owner's sources and collect permissions from each.
    # In the single-user runtime this is the full set.
    from careerops.domain.crawl_plans import CrawlSourceRepository

    source_repo: CrawlSourceRepository = service._sources  # type: ignore[attr-defined]
    sources = source_repo.list_for(candidate_id, limit=limit)
    all_perms: list[object] = []
    for src in sources:
        perms = service.list_for_source(candidate_id, src.id, limit=limit)
        all_perms.extend(perms)
    # Sort by created_at descending.
    all_perms.sort(key=lambda p: p.created_at or datetime.min, reverse=True)  # type: ignore[attr-defined]
    return PermissionListResponse(
        items=[_to_response(p) for p in all_perms[:limit]],
        total=len(all_perms),
    )


@router.get("/crawl-permissions/{permission_id}", response_model=PermissionResponse)
def get_permission(
    permission_id: UUID,
    candidate_id: UUID,
    service: Annotated[CrawlPermissionService, Depends(_service)],
) -> PermissionResponse:
    try:
        perm = service.get_permission(candidate_id, permission_id)
    except Exception as exc:
        raise NotFoundError("crawl permission not found") from exc
    return _to_response(perm)


@router.post(
    "/crawl-permissions/{permission_id}/grant",
    response_model=PermissionResponse,
)
def grant_permission(
    permission_id: UUID,
    candidate_id: UUID,
    service: Annotated[CrawlPermissionService, Depends(_service)],
    body: DecisionRequest | None = None,
) -> PermissionResponse:
    try:
        session_ref = body.session_ref if body is not None else ""
        perm = service.grant(candidate_id, permission_id, session_ref=session_ref)
    except ValueError as exc:
        raise InvalidStateError(str(exc)) from exc
    except Exception as exc:
        raise NotFoundError("crawl permission not found") from exc
    return _to_response(perm)


@router.post(
    "/crawl-permissions/{permission_id}/open-login",
    response_model=LoginSessionResponse,
)
def open_login_session(
    permission_id: UUID,
    request: Request,
    candidate_id: UUID,
    service: Annotated[CrawlPermissionService, Depends(_service)],
) -> LoginSessionResponse:
    """Open the approved source in its isolated persistent browser session."""
    perm = service.get_permission(candidate_id, permission_id)
    if perm.state is not CrawlPermissionState.GRANTED or not perm.session_ref:
        raise InvalidStateError("grant permission before opening a login session")
    source_service = cast(
        "CrawlSourceService",
        require_repository(request, "crawl_source_service"),
    )
    source = source_service.get_source(candidate_id, perm.source_id)

    from careerops.infrastructure.playwright_tool import PlaywrightTool

    existing = _LOGIN_TOOLS.get(perm.session_ref)
    if existing is None:
        try:
            tool = PlaywrightTool(headless=False, session_ref=perm.session_ref)
            tool.navigate(source.base_url, wait_s=1.0)
        except Exception as exc:
            raise InvalidStateError(f"cannot open login browser: {exc}") from exc
        _LOGIN_TOOLS[perm.session_ref] = tool
    return LoginSessionResponse(opened=True, session_ref=perm.session_ref)


@router.post(
    "/crawl-permissions/{permission_id}/deny",
    response_model=PermissionResponse,
)
def deny_permission(
    permission_id: UUID,
    body: DecisionRequest,
    candidate_id: UUID,
    service: Annotated[CrawlPermissionService, Depends(_service)],
) -> PermissionResponse:
    try:
        perm = service.deny(candidate_id, permission_id, reason=body.reason)
    except ValueError as exc:
        raise InvalidStateError(str(exc)) from exc
    except Exception as exc:
        raise NotFoundError("crawl permission not found") from exc
    return _to_response(perm)


@router.post(
    "/crawl-permissions/{permission_id}/revoke",
    response_model=PermissionResponse,
)
def revoke_permission(
    permission_id: UUID,
    body: DecisionRequest,
    candidate_id: UUID,
    service: Annotated[CrawlPermissionService, Depends(_service)],
) -> PermissionResponse:
    try:
        perm = service.revoke(candidate_id, permission_id, reason=body.reason)
    except ValueError as exc:
        raise InvalidStateError(str(exc)) from exc
    except Exception as exc:
        raise NotFoundError("crawl permission not found") from exc
    return _to_response(perm)


# ---------------------------------------------------------------------------
# Routes -- source-scoped
# ---------------------------------------------------------------------------


@router.get(
    "/crawl-sources/{source_id}/permissions",
    response_model=PermissionListResponse,
)
def list_permissions_for_source(
    source_id: UUID,
    candidate_id: UUID,
    service: Annotated[CrawlPermissionService, Depends(_service)],
    limit: int = Query(default=50, ge=1, le=200),
) -> PermissionListResponse:
    perms = service.list_for_source(candidate_id, source_id, limit=limit)
    return PermissionListResponse(
        items=[_to_response(p) for p in perms],
        total=len(perms),
    )


@router.post(
    "/crawl-sources/{source_id}/permissions",
    response_model=PermissionResponse,
    status_code=201,
)
def request_permission(
    source_id: UUID,
    body: PermissionRequest,
    candidate_id: UUID,
    service: Annotated[CrawlPermissionService, Depends(_service)],
) -> PermissionResponse:
    """Request crawl permission for a source.

    Pauses the source and creates a pending request.  If a pending request
    already exists, returns it (idempotent).
    """
    try:
        perm = service.request_permission(
            candidate_id,
            source_id,
            login_evidence=body.login_evidence,
            domain_scope=body.domain_scope,
            disclosed_terms=body.disclosed_terms,
        )
    except Exception as exc:
        raise NotFoundError("crawl source not found") from exc
    return _to_response(perm)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _to_response(perm: object) -> PermissionResponse:
    return PermissionResponse(
        id=str(perm.id),  # type: ignore[attr-defined]
        source_id=str(perm.source_id),  # type: ignore[attr-defined]
        state=perm.state.value,  # type: ignore[attr-defined]
        domain_scope=perm.domain_scope,  # type: ignore[attr-defined]
        disclosed_terms=dict(perm.disclosed_terms),  # type: ignore[attr-defined]
        requested_at=_iso(perm.requested_at),  # type: ignore[attr-defined]
        granted_at=_iso(perm.granted_at),  # type: ignore[attr-defined]
        denied_at=_iso(perm.denied_at),  # type: ignore[attr-defined]
        revoked_at=_iso(perm.revoked_at),  # type: ignore[attr-defined]
        expired_at=_iso(perm.expired_at),  # type: ignore[attr-defined]
        expires_at=_iso(perm.expires_at),  # type: ignore[attr-defined]
        created_at=_iso(perm.created_at),  # type: ignore[attr-defined]
        updated_at=_iso(perm.updated_at),  # type: ignore[attr-defined]
        session_ref=getattr(perm, "session_ref", None),
    )


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
