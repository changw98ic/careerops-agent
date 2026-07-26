"""Application workspace API routes (Section 7, task 7.8).

Additive routes under /api/v1 for the application workspace — the
preparation/lifecycle surface defined by the application-workspace spec. All
candidate ownership is resolved server-side via ``require_candidate_id``;
client-supplied candidate ids are never honored.

Routes:
- GET   /api/v1/applications/{application_id}                       — detail
- POST  /api/v1/applications/{application_id}/prepare                — FAVORITED → PREPARING
- GET   /api/v1/applications/{application_id}/channels               — channel eligibility
- POST  /api/v1/applications/{application_id}/channel                — select a channel
- POST  /api/v1/applications/{application_id}/package                — bind approved package (7.5)
- GET   /api/v1/applications/{application_id}/timeline               — append-only timeline
- POST  /api/v1/applications/{application_id}/confirm-external-submission — external/manual (7.7)
- POST  /api/v1/applications/{application_id}/state                  — guarded USER transition

Iron rules honored:
- Additive (Iron Rule 8): new paths only; the M3 /transition, /submit, /events
  routes are untouched.
- Server-side ownership (Iron Rule 2 + 6): candidate resolved server-side;
  not-owned → 404 (no existence leak).
- Dependency-not-ready (Iron Rule 3/6): missing service → 503.
- Model cannot transition state (Iron Rule 2): every mutating route records a
  USER-sourced event; submission requires evidence.
- Default-deny (Iron Rule 7): CRAWL_PLAN_MANAGEMENT gate; no external writes.
"""

# Repos/services fetched via require_repository are typed as ``object``.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from careerops.api.auth_dependency import require_candidate_id
from careerops.api.capability_dependency import require_capability, require_repository
from careerops.application.application_workspace import (
    ApplicationNotOwnedError,
    ApplicationWorkspaceService,
    ChannelUnavailableError,
    ExternalFormEvidenceError,
    WorkspaceError,
)
from careerops.domain.applications import (
    IllegalTransitionError,
    SubmissionChannel,
)
from careerops.orchestration.capability_resolver import CapabilityKind

router = APIRouter(
    prefix="/api/v1",
    tags=["application-workspace"],
    # The workspace is downstream of crawl-plan provenance (same as the inbox)
    # and performs no external writes; gate on the released-by-default
    # CRAWL_PLAN_MANAGEMENT capability.
    dependencies=[Depends(require_capability(CapabilityKind.CRAWL_PLAN_MANAGEMENT))],
)


# ---------------------------------------------------------------------------
# Response / request schemas
# ---------------------------------------------------------------------------


class ApplicationDetailResponse(BaseModel):
    id: str
    canonical_job_id: str
    state: str
    apply_url: str = ""
    submission_channel: str | None = None
    cycle_id: str | None = None
    package_version_id: str | None = None
    payload_hash: str | None = None
    submitted_at: str | None = None
    version: int = 1


class ChannelEligibilityItem(BaseModel):
    channel: str
    eligible: bool
    reason: str = ""
    evidence_refs: dict[str, str] = Field(default_factory=dict)


class ChannelEligibilityResponse(BaseModel):
    candidate_id: str
    canonical_job_id: str
    channels: list[ChannelEligibilityItem] = Field(default_factory=list)


class ChannelSelectRequest(BaseModel):
    channel: str


class PackageBindRequest(BaseModel):
    package_version_id: str
    payload_hash: str | None = None
    job_version_id: str | None = None
    resume_version_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class StateChangeRequest(BaseModel):
    to_state: str
    note: str = ""


class ConfirmExternalSubmissionRequest(BaseModel):
    apply_url: str = ""
    submitted_at: datetime | None = None
    note: str = ""
    channel: str = "external_form"


class TimelineEntryItem(BaseModel):
    id: str
    kind: str
    occurred_at: str
    title: str
    description: str = ""
    source: str = "user"
    status: str = "confirmed"
    from_state: str | None = None
    to_state: str | None = None
    evidence_refs: dict[str, str] = Field(default_factory=dict)


class TimelineResponse(BaseModel):
    application_id: str
    items: list[TimelineEntryItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(request: Request) -> ApplicationWorkspaceService:
    return require_repository(request, "application_workspace_service")  # type: ignore[return-value]


def _to_detail(app: object) -> ApplicationDetailResponse:
    return ApplicationDetailResponse(
        id=str(app.id),  # type: ignore[attr-defined]
        canonical_job_id=str(app.canonical_job_id),  # type: ignore[attr-defined]
        state=app.state.value,  # type: ignore[attr-defined]
        apply_url=app.apply_url,  # type: ignore[attr-defined]
        submission_channel=app.submission_channel.value  # type: ignore[attr-defined]
        if app.submission_channel is not None  # type: ignore[attr-defined]
        else None,
        cycle_id=str(app.cycle_id) if app.cycle_id is not None else None,  # type: ignore[attr-defined]
        package_version_id=str(app.package_version_id)  # type: ignore[attr-defined]
        if app.package_version_id is not None  # type: ignore[attr-defined]
        else None,
        payload_hash=app.payload_hash,  # type: ignore[attr-defined]
        submitted_at=app.submitted_at.isoformat()  # type: ignore[attr-defined]
        if app.submitted_at is not None  # type: ignore[attr-defined]
        else None,
        version=app.version,  # type: ignore[attr-defined]
    )


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, ApplicationNotOwnedError):
        return HTTPException(status_code=404, detail="Application not found")
    if isinstance(exc, IllegalTransitionError):
        transition = f"{exc.from_state.value} -> {exc.to_state.value}"
        return HTTPException(status_code=422, detail=f"Illegal transition: {transition}")
    if isinstance(exc, ChannelUnavailableError):
        return HTTPException(
            status_code=422, detail=f"Channel {exc.channel.value} unavailable: {exc.reason}"
        )
    if isinstance(exc, ExternalFormEvidenceError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, WorkspaceError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/applications/{application_id}")
def get_application(
    application_id: str,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> ApplicationDetailResponse:
    """Return the application detail (state, channel, binding, timestamps)."""
    service = _service(request)
    try:
        app = service.get_application(
            application_id=UUID(application_id), candidate_id=candidate_id
        )
    except Exception as e:
        raise _translate(e) from e
    return _to_detail(app)


@router.post("/applications/{application_id}/prepare")
def prepare_application(
    application_id: str,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> ApplicationDetailResponse:
    """Move the application from FAVORITED to PREPARING (user action)."""
    service = _service(request)
    try:
        app = service.prepare_application(
            application_id=UUID(application_id),
            candidate_id=candidate_id,
            now=datetime.now(tz=UTC),
        )
    except Exception as e:
        raise _translate(e) from e
    return _to_detail(app)


@router.get("/applications/{application_id}/channels")
def list_channels(
    application_id: str,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> ChannelEligibilityResponse:
    """Return the channel-eligibility snapshot (why each channel is/isn't available)."""
    service = _service(request)
    try:
        snapshot = service.evaluate_channels(
            application_id=UUID(application_id), candidate_id=candidate_id
        )
    except Exception as e:
        raise _translate(e) from e
    return ChannelEligibilityResponse(
        candidate_id=str(snapshot.candidate_id),
        canonical_job_id=str(snapshot.canonical_job_id),
        channels=[
            ChannelEligibilityItem(
                channel=c.channel.value,
                eligible=c.eligible,
                reason=c.reason,
                evidence_refs=dict(c.evidence_refs),
            )
            for c in snapshot.channels
        ],
    )


@router.post("/applications/{application_id}/channel")
def select_channel(
    application_id: str,
    body: ChannelSelectRequest,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> ApplicationDetailResponse:
    """Select a submission channel (blocks unavailable channels)."""
    service = _service(request)
    try:
        channel = SubmissionChannel(body.channel)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Unknown channel: {body.channel}") from e
    try:
        app = service.select_channel(
            application_id=UUID(application_id),
            candidate_id=candidate_id,
            channel=channel,
            now=datetime.now(tz=UTC),
        )
    except Exception as e:
        raise _translate(e) from e
    return _to_detail(app)


@router.post("/applications/{application_id}/package")
def bind_package(
    application_id: str,
    body: PackageBindRequest,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> ApplicationDetailResponse:
    """Bind an approved package version to the application (task 7.5).

    The package itself is built/approved by the Section 8 package service; this
    route records the binding (package version + payload hash + input
    identities) on the application so the workspace can show the exact payload
    and so any later input mutation invalidates the approval.
    """
    service = _service(request)
    evidence_refs = tuple(UUID(eid) for eid in body.evidence_ids)
    try:
        app = service.bind_package(
            application_id=UUID(application_id),
            candidate_id=candidate_id,
            package_version_id=UUID(body.package_version_id),
            payload_hash=body.payload_hash,
            job_version_id=UUID(body.job_version_id) if body.job_version_id else None,
            resume_version_id=UUID(body.resume_version_id) if body.resume_version_id else None,
            evidence_refs=evidence_refs,
            now=datetime.now(tz=UTC),
        )
    except Exception as e:
        raise _translate(e) from e
    return _to_detail(app)


@router.get("/applications/{application_id}/timeline")
def get_timeline(
    application_id: str,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> TimelineResponse:
    """Return the chronological application timeline projection."""
    service = _service(request)
    try:
        entries = service.get_timeline(
            application_id=UUID(application_id), candidate_id=candidate_id
        )
    except Exception as e:
        raise _translate(e) from e
    return TimelineResponse(
        application_id=application_id,
        items=[
            TimelineEntryItem(
                id=str(e.id),
                kind=e.kind.value,
                occurred_at=e.occurred_at.isoformat(),
                title=e.title,
                description=e.description,
                source=e.source,
                status=e.status.value,
                from_state=e.from_state,
                to_state=e.to_state,
                evidence_refs=dict(e.evidence_refs),
            )
            for e in entries
        ],
    )


@router.post("/applications/{application_id}/confirm-external-submission")
def confirm_external_submission(
    application_id: str,
    body: ConfirmExternalSubmissionRequest,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> ApplicationDetailResponse:
    """Confirm an external-form / manual submission with evidence (task 7.7).

    Requires PREPARING and, for EXTERNAL_FORM, the official apply URL. Records
    a manually-confirmed submitted event. No automatic submitted claim: an
    abandoned form leaves the application in PREPARING.
    """
    service = _service(request)
    try:
        channel = SubmissionChannel(body.channel)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Unknown channel: {body.channel}") from e
    now = datetime.now(tz=UTC)
    try:
        app = service.confirm_external_submission(
            application_id=UUID(application_id),
            candidate_id=candidate_id,
            apply_url=body.apply_url,
            submitted_at=body.submitted_at or now,
            note=body.note,
            channel=channel,
            now=now,
        )
    except Exception as e:
        raise _translate(e) from e
    return _to_detail(app)


@router.post("/applications/{application_id}/state")
def change_state(
    application_id: str,
    body: StateChangeRequest,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> ApplicationDetailResponse:
    """Apply a guarded USER state transition (ON_HOLD, WITHDRAWN, etc.).

    Submission (→ SUBMITTED) is NOT permitted here; it must go through
    /confirm-external-submission (Gate B) or the system-managed send path
    (Section 10), both evidence-bound.
    """
    from careerops.domain.applications import ApplicationState

    service = _service(request)
    try:
        to_state = ApplicationState(body.to_state)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Unknown state: {body.to_state}") from e
    try:
        app = service.apply_user_transition(
            application_id=UUID(application_id),
            candidate_id=candidate_id,
            to_state=to_state,
            note=body.note,
            now=datetime.now(tz=UTC),
        )
    except Exception as e:
        raise _translate(e) from e
    return _to_detail(app)

