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
from careerops.application.package_service import (
    PackageApprovalValidationError,
    PackageNotFoundError,
    PackageService,
    ResumeNotEligibleError,
)
from careerops.domain.application_packages import (
    PackageAttachment,
    PackageClaimVersion,
    PackageDiffEntry,
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


def _package_service(request: Request) -> PackageService:
    return require_repository(request, "package_service")  # type: ignore[return-value]


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
    if isinstance(exc, PackageNotFoundError):
        return HTTPException(status_code=404, detail="Package version not found")
    if isinstance(exc, IllegalTransitionError):
        transition = f"{exc.from_state.value} -> {exc.to_state.value}"
        return HTTPException(status_code=422, detail=f"Illegal transition: {transition}")
    if isinstance(exc, ChannelUnavailableError):
        return HTTPException(
            status_code=422, detail=f"Channel {exc.channel.value} unavailable: {exc.reason}"
        )
    if isinstance(exc, ExternalFormEvidenceError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ResumeNotEligibleError):
        return HTTPException(status_code=422, detail=f"Resume not eligible: {exc.reason}")
    if isinstance(exc, PackageApprovalValidationError):
        return HTTPException(status_code=422, detail="; ".join(exc.reasons))
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


# ===========================================================================
# Section 8 — job-specific application package routes (task 8.8)
# ===========================================================================


class PackageClaimInput(BaseModel):
    claim_text: str
    evidence_ids: list[str] = Field(default_factory=list)


class PackageAttachmentInput(BaseModel):
    name: str
    content_hash: str
    media_type: str = ""
    size_bytes: int = 0


class PackageDraftRequest(BaseModel):
    resume_version_id: str
    job_version_id: str | None = None
    profile_version_id: str | None = None
    cover_letter_text: str = ""
    notes: str = ""
    answers: dict[str, str] = Field(default_factory=dict)
    claims: list[PackageClaimInput] = Field(default_factory=list)
    attachments: list[PackageAttachmentInput] = Field(default_factory=list)


class PackageEditEntryInput(BaseModel):
    section: str
    original_text: str = ""
    proposed_text: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    source: str = "user"


class PackageEditRequest(BaseModel):
    edits: list[PackageEditEntryInput] = Field(default_factory=list)


class PackageClaimItem(BaseModel):
    claim_text: str
    evidence_ids: list[str] = Field(default_factory=list)


class PackageAttachmentItem(BaseModel):
    name: str
    content_hash: str
    media_type: str = ""
    size_bytes: int = 0


class RequirementGapItem(BaseModel):
    requirement_name: str
    match_level: str
    reason: str = ""
    rules_version: str = ""


class PackageDiffItem(BaseModel):
    section: str
    original_text: str = ""
    proposed_text: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    source: str = "user"


class PackageVersionResponse(BaseModel):
    id: str
    application_id: str
    version_number: int
    resume_version_id: str
    job_version_id: str | None = None
    profile_version_id: str | None = None
    cover_letter_text: str = ""
    notes: str = ""
    answers: dict[str, str] = Field(default_factory=dict)
    claims: list[PackageClaimItem] = Field(default_factory=list)
    attachments: list[PackageAttachmentItem] = Field(default_factory=list)
    diff: list[PackageDiffItem] = Field(default_factory=list)
    requirement_gaps: list[RequirementGapItem] = Field(default_factory=list)
    payload_hash: str | None = None
    approval_state: str = "draft"
    approved_at: str | None = None
    approved_by: str = ""


class PackageListResponse(BaseModel):
    items: list[PackageVersionResponse] = Field(default_factory=list)
    latest_id: str | None = None


def _iso_or_none(value: object) -> str | None:
    """ISO-format a datetime-or-None, returning None for falsy values."""
    if value is None:
        return None
    return value.isoformat()  # type: ignore[attr-defined]


def _version_to_response(v: object) -> PackageVersionResponse:
    approval = getattr(v, "approval_state", None)
    return PackageVersionResponse(
        id=str(v.id),  # type: ignore[attr-defined]
        application_id=str(v.application_id),  # type: ignore[attr-defined]
        version_number=v.version_number,  # type: ignore[attr-defined]
        resume_version_id=str(v.resume_version_id),  # type: ignore[attr-defined]
        job_version_id=str(v.job_version_id) if getattr(v, "job_version_id", None) else None,
        profile_version_id=str(v.profile_version_id)
        if getattr(v, "profile_version_id", None)
        else None,
        cover_letter_text=v.cover_letter_text,  # type: ignore[attr-defined]
        notes=v.notes,  # type: ignore[attr-defined]
        answers=dict(getattr(v, "answers", {})),
        claims=[
            PackageClaimItem(claim_text=c.claim_text, evidence_ids=[str(e) for e in c.evidence_ids])
            for c in getattr(v, "claims", ())
        ],
        attachments=[
            PackageAttachmentItem(
                name=a.name,
                content_hash=a.content_hash,
                media_type=a.media_type,
                size_bytes=a.size_bytes,
            )
            for a in getattr(v, "attachments", ())
        ],
        diff=[
            PackageDiffItem(
                section=d.section,
                original_text=d.original_text,
                proposed_text=d.proposed_text,
                evidence_ids=[str(e) for e in d.evidence_ids],
                source=d.source,
            )
            for d in getattr(v, "diff", ())
        ],
        requirement_gaps=[
            RequirementGapItem(
                requirement_name=g.requirement_name,
                match_level=g.match_level,
                reason=g.reason,
                rules_version=g.rules_version,
            )
            for g in getattr(v, "requirement_gaps", ())
        ],
        payload_hash=getattr(v, "payload_hash", None),
        approval_state=getattr(approval, "value", "draft"),
        approved_at=_iso_or_none(getattr(v, "approved_at", None)),
        approved_by=getattr(v, "approved_by", ""),
    )


def _require_owned_app(
    service: ApplicationWorkspaceService, application_id: UUID, candidate_id: UUID
) -> object:
    """Enforce candidate ownership before any package mutation."""
    try:
        return service.get_application(application_id=application_id, candidate_id=candidate_id)
    except Exception as e:
        raise _translate(e) from e


@router.post("/applications/{application_id}/packages")
def create_package_draft(
    application_id: str,
    body: PackageDraftRequest,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> PackageVersionResponse:
    """Create a DRAFT package version bound to the exact inputs (task 8.2).

    Gaps are computed when job/evidence context is available; the claim→evidence
    binding is enforced (a claim with no evidence is a 422). Requires the bound
    resume to be parsed+confirmed.
    """
    ws = _service(request)
    pkg = _package_service(request)
    app = _require_owned_app(ws, UUID(application_id), candidate_id)
    claims = tuple(
        PackageClaimVersion(
            claim_text=c.claim_text,
            evidence_ids=tuple(UUID(e) for e in c.evidence_ids),
        )
        for c in body.claims
    )
    attachments = tuple(
        PackageAttachment(
            name=a.name,
            content_hash=a.content_hash,
            media_type=a.media_type,
            size_bytes=a.size_bytes,
        )
        for a in body.attachments
    )
    # Best-effort gap context: confirmed evidence + job structured data.
    confirmed_evidence = None
    job_data = None
    evidence_repo = getattr(request.app.state, "evidence_repository", None)
    if evidence_repo is not None:
        confirmed_evidence = evidence_repo.list_confirmed_for(candidate_id)
    job_data_repo = getattr(request.app.state, "matching_repository", None)
    if job_data_repo is not None:
        job_data = job_data_repo.get_job_structured_data(app.canonical_job_id)  # type: ignore[attr-defined]
    try:
        version = pkg.create_draft(
            application_id=UUID(application_id),
            candidate_id=candidate_id,
            resume_version_id=UUID(body.resume_version_id),
            job_version_id=UUID(body.job_version_id) if body.job_version_id else None,
            profile_version_id=UUID(body.profile_version_id) if body.profile_version_id else None,
            claims=claims,
            cover_letter_text=body.cover_letter_text,
            notes=body.notes,
            answers=body.answers,
            attachments=attachments,
            job_structured_data=job_data,
            confirmed_evidence=confirmed_evidence,
            now=datetime.now(tz=UTC),
        )
    except Exception as e:
        raise _translate(e) from e
    return _version_to_response(version)


@router.get("/applications/{application_id}/packages")
def list_packages(
    application_id: str,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> PackageListResponse:
    """List package versions (newest first) for the application."""
    ws = _service(request)
    pkg = _package_service(request)
    _require_owned_app(ws, UUID(application_id), candidate_id)
    versions = pkg.list_versions(UUID(application_id))
    items = [_version_to_response(v) for v in versions]
    return PackageListResponse(items=items, latest_id=items[0].id if items else None)


@router.get("/applications/{application_id}/packages/latest")
def get_latest_package(
    application_id: str,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> PackageVersionResponse:
    """Return the latest package version (404 if none)."""
    ws = _service(request)
    pkg = _package_service(request)
    _require_owned_app(ws, UUID(application_id), candidate_id)
    latest = pkg.get_latest(UUID(application_id))
    if latest is None:
        raise HTTPException(status_code=404, detail="No package version found")
    return _version_to_response(latest)


@router.post("/applications/{application_id}/packages/{version_id}/approve")
def approve_package(
    application_id: str,
    version_id: str,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> PackageVersionResponse:
    """Approve a package version after validating all preconditions (8.7).

    Rejects when any claim lacks evidence, the resume is unconfirmed, or the
    source is stale (8.6). Freezes payload_hash + approver + timestamp.
    """
    ws = _service(request)
    pkg = _package_service(request)
    _require_owned_app(ws, UUID(application_id), candidate_id)
    now = datetime.now(tz=UTC)
    try:
        version = pkg.approve(
            application_id=UUID(application_id),
            candidate_id=candidate_id,
            version_id=UUID(version_id),
            actor_id=str(candidate_id),
            now=now,
        )
        # Bind the approved version to the application so the workspace binding,
        # timeline (PACKAGE_ATTACHED) and Application.package_version_id /
        # payload_hash all reflect the approval. The PackageServiceBindingStore
        # reads the latest version independently; this keeps the Application
        # record coherent for the submission gate (Section 10).
        ws.bind_package(
            application_id=UUID(application_id),
            candidate_id=candidate_id,
            package_version_id=version.id,
            payload_hash=version.payload_hash,
            job_version_id=version.job_version_id,
            resume_version_id=version.resume_version_id,
            now=now,
        )
    except Exception as e:
        raise _translate(e) from e
    return _version_to_response(version)


@router.post("/applications/{application_id}/packages/{version_id}/edits")
def apply_package_edits(
    application_id: str,
    version_id: str,
    body: PackageEditRequest,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> PackageVersionResponse:
    """Apply edits as a NEW package version (copy-on-write, task 8.5).

    The base version is immutable; this always produces a fresh version. Any
    prior approval is superseded (the latest version becomes a draft again).
    """
    ws = _service(request)
    pkg = _package_service(request)
    _require_owned_app(ws, UUID(application_id), candidate_id)
    edits = tuple(
        PackageDiffEntry(
            section=e.section,
            original_text=e.original_text,
            proposed_text=e.proposed_text,
            evidence_ids=tuple(UUID(x) for x in e.evidence_ids),
            source=e.source,
        )
        for e in body.edits
    )
    try:
        version = pkg.apply_edits(
            application_id=UUID(application_id),
            candidate_id=candidate_id,
            base_version_id=UUID(version_id),
            edits=edits,
            now=datetime.now(tz=UTC),
        )
    except Exception as e:
        raise _translate(e) from e
    return _version_to_response(version)
