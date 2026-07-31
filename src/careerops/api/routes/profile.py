"""Profile API routes (Section 3, task 3.2).

Additive routes under ``/api/v1/candidates/{candidate_id}/profile`` backed by
:class:`ProfileService`. The candidate identity is supplied by the URL path
parameter (post-auth single-tenant console) and the service is pulled through
:func:`require_repository` so a missing service surfaces as
``DependencyNotReadyError`` (503) rather than a silent empty response
(Iron Rules 1 + 2).

The path-supplied ``candidate_id`` always scopes ownership; a body never
carries one.
"""

# Pydantic ``Field(default_factory=list)`` and the ``object``-typed response
# mappers produce ``reportUnknown*`` reports that obscure the real mapping
# logic; the existing applications router suppresses them the same way.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from careerops.api.capability_dependency import require_repository
from careerops.api.errors import NotFoundError
from careerops.application.profile_service import ProfilePreferences, ProfileService
from careerops.domain.profiles import (
    Authorization,
    CompensationPreference,
    HardExclusions,
    LocationKind,
    LocationPreference,
    RemoteRules,
    TargetRole,
)

router = APIRouter(prefix="/api/v1/candidates/{candidate_id}/profile", tags=["profile"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class TargetRoleInput(BaseModel):
    title: str
    seniority: str = ""
    notes: str = ""


class LocationInput(BaseModel):
    name: str
    kind: str = "preferred"
    radius_km: int | None = None


class RemoteRulesInput(BaseModel):
    remote_allowed: bool = False
    hybrid_allowed: bool = False
    onsite_required: bool = False
    timezone: str = ""


class CompensationInput(BaseModel):
    currency: str = ""
    amount_min: int | None = None
    amount_max: int | None = None
    period: str = ""
    equity: bool = False


class AuthorizationInput(BaseModel):
    work_authorization: str = ""
    visa_sponsorship_required: bool = False
    locale_restrictions: list[str] = Field(default_factory=list)


class HardExclusionsInput(BaseModel):
    companies: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class ProfileWriteRequest(BaseModel):
    """Body for create/update. Carries NO candidate_id (server-resolved)."""

    target_roles: list[TargetRoleInput] = Field(default_factory=list)
    locations: list[LocationInput] = Field(default_factory=list)
    remote_rules: RemoteRulesInput = Field(default_factory=RemoteRulesInput)
    compensation: CompensationInput = Field(default_factory=CompensationInput)
    seniority: str = ""
    authorization: AuthorizationInput = Field(default_factory=AuthorizationInput)
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    hard_exclusions: HardExclusionsInput = Field(default_factory=HardExclusionsInput)
    activate: bool = True


class TargetRoleOut(BaseModel):
    title: str
    seniority: str = ""
    notes: str = ""


class LocationOut(BaseModel):
    name: str
    kind: str = "preferred"
    radius_km: int | None = None


class RemoteRulesOut(BaseModel):
    remote_allowed: bool = False
    hybrid_allowed: bool = False
    onsite_required: bool = False
    timezone: str = ""


class CompensationOut(BaseModel):
    currency: str = ""
    amount_min: int | None = None
    amount_max: int | None = None
    period: str = ""
    equity: bool = False


class AuthorizationOut(BaseModel):
    work_authorization: str = ""
    visa_sponsorship_required: bool = False
    locale_restrictions: list[str] = Field(default_factory=list)


class HardExclusionsOut(BaseModel):
    companies: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class ProfileVersionResponse(BaseModel):
    id: str
    candidate_id: str
    version: int
    is_active: bool
    target_roles: list[TargetRoleOut] = Field(default_factory=list)
    locations: list[LocationOut] = Field(default_factory=list)
    remote_rules: RemoteRulesOut = Field(default_factory=RemoteRulesOut)
    compensation: CompensationOut = Field(default_factory=CompensationOut)
    seniority: str = ""
    authorization: AuthorizationOut = Field(default_factory=AuthorizationOut)
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    hard_exclusions: HardExclusionsOut = Field(default_factory=HardExclusionsOut)
    rules_version: str = ""


class ProfileVersionListResponse(BaseModel):
    items: list[ProfileVersionResponse] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


def _profile_service(request: Request) -> ProfileService:
    service = require_repository(request, "profile_service")
    return service  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=None)
def get_active_profile(
    candidate_id: UUID,
    service: Annotated[ProfileService, Depends(_profile_service)],
) -> ProfileVersionResponse:
    """Return the active profile version for the authenticated candidate.

    200 with the active version, or 404 (standard error envelope) when no
    profile exists yet.
    """
    active = service.get_active(candidate_id)
    if active is None:
        raise NotFoundError("no active profile version for candidate")
    return _to_response(active)


@router.get("/versions")
def list_profile_versions(
    candidate_id: UUID,
    service: Annotated[ProfileService, Depends(_profile_service)],
    limit: int = 50,
) -> ProfileVersionListResponse:
    versions = service.list_versions(candidate_id, limit=limit)
    return ProfileVersionListResponse(
        items=[_to_response(v) for v in versions],
        total=len(versions),
    )


@router.get("/versions/{version_id}")
def get_profile_version(
    version_id: UUID,
    candidate_id: UUID,
    service: Annotated[ProfileService, Depends(_profile_service)],
) -> ProfileVersionResponse:
    version = service.get_version(candidate_id, version_id)
    return _to_response(version)


@router.post("", status_code=201)
def create_profile_version(
    body: ProfileWriteRequest,
    candidate_id: UUID,
    service: Annotated[ProfileService, Depends(_profile_service)],
) -> ProfileVersionResponse:
    """Validate, persist, and (by default) activate a new profile version.

    Invalid preferences raise ``InvalidStateError`` (409) and never displace
    the prior active version. The body carries no ``candidate_id``; the
    server-resolved id always owns the new version.
    """
    preferences = _to_preferences(body)
    version = service.create_version(candidate_id, preferences, activate=body.activate)
    return _to_response(version)


@router.post("/versions/{version_id}/activate")
def activate_profile_version(
    version_id: UUID,
    candidate_id: UUID,
    service: Annotated[ProfileService, Depends(_profile_service)],
) -> ProfileVersionResponse:
    """Activate an existing version (re-validated against current rules)."""
    version = service.activate_version(candidate_id, version_id)
    return _to_response(version)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


def _to_preferences(body: ProfileWriteRequest) -> ProfilePreferences:
    roles = tuple(
        TargetRole(title=r.title, seniority=r.seniority, notes=r.notes) for r in body.target_roles
    )
    locations = tuple(_location_to_domain(loc) for loc in body.locations)
    remote = RemoteRules(
        remote_allowed=body.remote_rules.remote_allowed,
        hybrid_allowed=body.remote_rules.hybrid_allowed,
        onsite_required=body.remote_rules.onsite_required,
        timezone=body.remote_rules.timezone,
    )
    compensation = CompensationPreference(
        currency=body.compensation.currency,
        amount_min=body.compensation.amount_min,
        amount_max=body.compensation.amount_max,
        period=body.compensation.period,
        equity=body.compensation.equity,
    )
    authorization = Authorization(
        work_authorization=body.authorization.work_authorization,
        visa_sponsorship_required=body.authorization.visa_sponsorship_required,
        locale_restrictions=tuple(body.authorization.locale_restrictions),
    )
    hard_exclusions = HardExclusions(
        companies=tuple(body.hard_exclusions.companies),
        titles=tuple(body.hard_exclusions.titles),
        keywords=tuple(body.hard_exclusions.keywords),
    )
    return ProfilePreferences(
        target_roles=roles,
        locations=locations,
        remote_rules=remote,
        compensation=compensation,
        seniority=body.seniority,
        authorization=authorization,
        include_keywords=tuple(body.include_keywords),
        exclude_keywords=tuple(body.exclude_keywords),
        hard_exclusions=hard_exclusions,
    )


def _location_to_domain(loc: LocationInput) -> LocationPreference:
    try:
        kind = LocationKind(loc.kind)
    except ValueError:
        kind = LocationKind.PREFERRED
    return LocationPreference(name=loc.name, kind=kind, radius_km=loc.radius_km)


def _to_response(version: object) -> ProfileVersionResponse:
    return ProfileVersionResponse(
        id=str(version.id),  # type: ignore[attr-defined]
        candidate_id=str(version.candidate_id),  # type: ignore[attr-defined]
        version=version.version,  # type: ignore[attr-defined]
        is_active=version.is_active,  # type: ignore[attr-defined]
        target_roles=[
            TargetRoleOut(title=r.title, seniority=r.seniority, notes=r.notes)  # type: ignore[attr-defined]
            for r in version.target_roles  # type: ignore[attr-defined]
        ],
        locations=[
            LocationOut(name=loc.name, kind=loc.kind.value, radius_km=loc.radius_km)  # type: ignore[attr-defined]
            for loc in version.locations  # type: ignore[attr-defined]
        ],
        remote_rules=RemoteRulesOut(
            remote_allowed=version.remote_rules.remote_allowed,  # type: ignore[attr-defined]
            hybrid_allowed=version.remote_rules.hybrid_allowed,  # type: ignore[attr-defined]
            onsite_required=version.remote_rules.onsite_required,  # type: ignore[attr-defined]
            timezone=version.remote_rules.timezone,  # type: ignore[attr-defined]
        ),
        compensation=CompensationOut(
            currency=version.compensation.currency,  # type: ignore[attr-defined]
            amount_min=version.compensation.amount_min,  # type: ignore[attr-defined]
            amount_max=version.compensation.amount_max,  # type: ignore[attr-defined]
            period=getattr(version.compensation.period, "value", str(version.compensation.period)),  # type: ignore[attr-defined]
            equity=version.compensation.equity,  # type: ignore[attr-defined]
        ),
        seniority=version.seniority,  # type: ignore[attr-defined]
        authorization=AuthorizationOut(
            work_authorization=version.authorization.work_authorization,  # type: ignore[attr-defined]
            visa_sponsorship_required=version.authorization.visa_sponsorship_required,  # type: ignore[attr-defined]
            locale_restrictions=list(version.authorization.locale_restrictions),  # type: ignore[attr-defined]
        ),
        include_keywords=list(version.include_keywords),  # type: ignore[attr-defined]
        exclude_keywords=list(version.exclude_keywords),  # type: ignore[attr-defined]
        hard_exclusions=HardExclusionsOut(
            companies=list(version.hard_exclusions.companies),  # type: ignore[attr-defined]
            titles=list(version.hard_exclusions.titles),  # type: ignore[attr-defined]
            keywords=list(version.hard_exclusions.keywords),  # type: ignore[attr-defined]
        ),
        rules_version=version.rules_version,  # type: ignore[attr-defined]
    )
