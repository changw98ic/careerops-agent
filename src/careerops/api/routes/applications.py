"""REST API routes for M3: contacts, applications, resume versions, packages, follow-ups."""

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

import contextlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from careerops.api.auth_dependency import require_api_auth
from careerops.auth.contracts import AuthenticatedPrincipal

router = APIRouter(prefix="/api/v1", tags=["applications"])


# ---------------------------------------------------------------------------
# Contact schemas
# ---------------------------------------------------------------------------


class ContactResponse(BaseModel):
    id: str
    company_id: str
    email: str
    name: str = ""
    role: str = ""
    source: str = ""
    source_url: str = ""
    publicly_listed: bool = True
    domain_match: bool = True
    confidence: str = "high"
    allowed_actions: list[str] = Field(default_factory=list)


class ContactListResponse(BaseModel):
    items: list[ContactResponse] = Field(default_factory=list)
    total: int = 0


class ContactCreateRequest(BaseModel):
    company_id: str
    email: str
    name: str = ""
    role: str = ""
    source: str = "job_page"
    source_url: str
    source_text: str = ""
    publicly_listed: bool = True
    domain_match: bool = True
    confidence: str = "high"


# ---------------------------------------------------------------------------
# Application schemas
# ---------------------------------------------------------------------------


class ApplicationResponse(BaseModel):
    id: str
    candidate_id: str
    canonical_job_id: str
    state: str
    apply_url: str = ""
    submitted_at: str | None = None
    follow_up_due_at: str | None = None
    version: int = 1


class ApplicationListResponse(BaseModel):
    items: list[ApplicationResponse] = Field(default_factory=list)
    total: int = 0
    next_cursor: str | None = None
    has_more: bool = False


class ApplicationCreateRequest(BaseModel):
    canonical_job_id: str
    apply_url: str = ""


class StateTransitionRequest(BaseModel):
    to_state: str
    note: str = ""


class EmailDraftResponse(BaseModel):
    subject: str
    body: str
    to: str = ""
    company: str = ""
    title: str = ""


class SendEmailRequest(BaseModel):
    to: str
    subject: str
    body: str


class ApplicationEventResponse(BaseModel):
    id: str
    application_id: str
    event_type: str
    from_state: str | None = None
    to_state: str | None = None
    source: str = ""
    actor_id: str = ""
    note: str = ""
    occurred_at: str | None = None


class ApplicationEventListResponse(BaseModel):
    items: list[ApplicationEventResponse] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# Resume version schemas
# ---------------------------------------------------------------------------


class ResumeVersionResponse(BaseModel):
    id: str
    candidate_id: str
    version_number: int
    file_reference: str
    content_hash: str
    target_type: str = "general"
    human_confirmed: bool = False


class ResumeVersionCreateRequest(BaseModel):
    candidate_id: str
    file_reference: str
    content_hash: str
    target_type: str = "general"
    human_confirmed: bool = False


# ---------------------------------------------------------------------------
# Application package schemas
# ---------------------------------------------------------------------------


class PackageClaimResponse(BaseModel):
    claim_text: str
    evidence_ids: list[str] = Field(default_factory=list)


class ApplicationPackageResponse(BaseModel):
    id: str
    application_id: str
    resume_version_id: str
    cover_letter_text: str = ""
    notes: str = ""
    answers: dict[str, str] = Field(default_factory=dict)
    claims: list[PackageClaimResponse] = Field(default_factory=list)
    approval_state: str = "draft"


class PackageClaimInput(BaseModel):
    claim_text: str
    evidence_ids: list[str] = Field(default_factory=list)


class ApplicationPackageCreateRequest(BaseModel):
    application_id: str
    resume_version_id: str
    cover_letter_text: str = ""
    notes: str = ""
    answers: dict[str, str] = Field(default_factory=dict)
    claims: list[PackageClaimInput] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Follow-up schemas
# ---------------------------------------------------------------------------


class FollowUpResponse(BaseModel):
    id: str
    application_id: str
    rule_version: str
    state: str
    due_at: str | None = None
    snoozed_until: str | None = None
    cancelled_reason: str = ""


class FollowUpScheduleRequest(BaseModel):
    application_id: str
    business_days: int = 5


# ---------------------------------------------------------------------------
# Contact endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/companies/{company_id}/contacts",
    response_model=ContactListResponse,
    summary="List recruiting contacts for a company",
)
async def list_contacts(
    company_id: str,
    request: Request,
    response: Response,
) -> ContactListResponse:
    response.headers["Cache-Control"] = "no-store"
    repo = _get_contact_repository(request)
    if repo is None:
        return ContactListResponse(items=[], total=0)
    contacts = repo.list_contacts(company_id)
    items = [
        ContactResponse(
            id=str(c["id"]),
            company_id=str(c["company_id"]),
            email=c["email"],
            name=c.get("name", ""),
            role=c.get("role", ""),
            source=c.get("source", ""),
            source_url=c.get("source_url", ""),
            publicly_listed=c.get("publicly_listed", True),
            domain_match=c.get("domain_match", True),
            confidence=c.get("confidence", "high"),
            allowed_actions=c.get("allowed_actions", ["display"]),
        )
        for c in contacts
    ]
    return ContactListResponse(items=items, total=len(items))


@router.post(
    "/contacts",
    response_model=ContactResponse,
    status_code=201,
    summary="Create a recruiting contact",
)
async def create_contact(
    body: ContactCreateRequest,
    request: Request,
    response: Response,
) -> ContactResponse:
    response.headers["Cache-Control"] = "no-store"
    service = _get_contact_service(request)
    if service is None:
        response.status_code = 503
        return ContactResponse(id="", company_id=body.company_id, email=body.email)

    from careerops.application.contacts import ContactCreateRequest as ServiceRequest
    from careerops.domain.contacts import ContactConfidence, ContactSource

    result = service.create_contact(
        ServiceRequest(
            company_id=_parse_uuid(body.company_id),
            email=body.email,
            name=body.name,
            role=body.role,
            source=ContactSource(body.source),
            source_url=body.source_url,
            source_text=body.source_text,
            publicly_listed=body.publicly_listed,
            domain_match=body.domain_match,
            confidence=ContactConfidence(body.confidence),
        ),
        now=_now(),
    )
    response.status_code = 201
    return ContactResponse(
        id=str(result.id),
        company_id=str(result.company_id),
        email=result.email,
        name=result.name,
        role=result.role,
        source=result.source.value,
        source_url=result.source_url,
        publicly_listed=result.publicly_listed,
        domain_match=result.domain_match,
        confidence=result.confidence.value,
        allowed_actions=[a.value for a in result.allowed_actions],
    )


# ---------------------------------------------------------------------------
# Application endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/applications",
    response_model=ApplicationListResponse,
    summary="List applications",
)
async def list_applications(
    request: Request,
    response: Response,
    candidate_id: str | None = Query(default=None),
    state: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> ApplicationListResponse:
    response.headers["Cache-Control"] = "no-store"
    repo = _get_application_repository(request)
    if repo is None:
        return ApplicationListResponse(items=[], total=0)
    result = repo.list_applications(
        candidate_id=candidate_id, state=state, cursor=cursor, limit=limit
    )
    items = [
        ApplicationResponse(
            id=str(a["id"]),
            candidate_id=str(a["candidate_id"]),
            canonical_job_id=str(a["canonical_job_id"]),
            state=a["state"],
            apply_url=a.get("apply_url", ""),
            submitted_at=a.get("submitted_at"),
            follow_up_due_at=a.get("follow_up_due_at"),
            version=a.get("version", 1),
        )
        for a in result["items"]
    ]
    return ApplicationListResponse(
        items=items,
        total=result["total"],
        next_cursor=result.get("next_cursor"),
        has_more=result.get("next_cursor") is not None,
    )


@router.post(
    "/applications",
    response_model=ApplicationResponse,
    summary="Create an application (idempotent)",
)
async def create_application(
    body: ApplicationCreateRequest,
    request: Request,
    response: Response,
    principal: Annotated[AuthenticatedPrincipal | None, Depends(require_api_auth)] = None,
) -> ApplicationResponse | dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    service = _get_application_service(request)
    if service is None:
        response.status_code = 503
        return {"error": "service_unavailable"}

    # Resolve candidate_id from authenticated session.
    if principal is None or not principal.candidate_id:
        response.status_code = 409
        return {"error": "CANDIDATE_PROFILE_REQUIRED"}

    candidate_id = principal.candidate_id
    canonical_job_id = _parse_uuid(body.canonical_job_id)

    # Idempotency: return existing application if (candidate, job) already tracked.
    repo = _get_application_repository(request)
    if repo is not None:
        existing = repo.find_by_candidate_and_job(candidate_id, canonical_job_id)
        if existing is not None:
            response.status_code = 200
            return _app_to_response(existing)

    from careerops.application.applications import ApplicationCreateRequest as ServiceRequest

    result = service.create_application(
        ServiceRequest(
            candidate_id=candidate_id,
            canonical_job_id=canonical_job_id,
            apply_url=body.apply_url,
        ),
        now=_now(),
    )
    response.status_code = 201
    return _app_to_response(result)


@router.post(
    "/applications/{application_id}/transition",
    response_model=ApplicationResponse,
    summary="Transition application state",
)
async def transition_application(
    application_id: str,
    body: StateTransitionRequest,
    request: Request,
    response: Response,
) -> ApplicationResponse | dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    service = _get_application_service(request)
    if service is None:
        response.status_code = 503
        return {"error": "service_unavailable"}

    from careerops.application.applications import StateTransitionRequest as ServiceRequest
    from careerops.domain.applications import (
        ApplicationEventSource,
        ApplicationState,
        IllegalTransitionError,
    )

    try:
        result = service.transition_state(
            ServiceRequest(
                application_id=_parse_uuid(application_id),
                to_state=ApplicationState(body.to_state),
                source=ApplicationEventSource.USER,
                note=body.note,
            ),
            now=_now(),
        )
    except IllegalTransitionError as e:
        response.status_code = 422
        return {"error": f"illegal_transition: {e}"}
    return _app_to_response(result)


@router.post(
    "/applications/{application_id}/submit",
    response_model=ApplicationResponse,
    summary="Record manual submission (no submit adapter exists)",
)
async def record_submission(
    application_id: str,
    request: Request,
    response: Response,
) -> ApplicationResponse | dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    service = _get_application_service(request)
    if service is None:
        response.status_code = 503
        return {"error": "service_unavailable"}

    from careerops.domain.applications import IllegalTransitionError

    try:
        result = service.record_manual_submission(
            application_id=_parse_uuid(application_id),
            actor_id="user",
            now=_now(),
        )
    except IllegalTransitionError as e:
        response.status_code = 422
        return {"error": f"illegal_transition: {e}"}
    return _app_to_response(result)


@router.get(
    "/applications/{application_id}/email-draft",
    response_model=EmailDraftResponse,
    summary="Generate email draft for application",
)
async def get_email_draft(
    application_id: str,
    request: Request,
    response: Response,
) -> EmailDraftResponse | dict[str, str]:
    response.headers["Cache-Control"] = "no-store"

    app_repo = _get_application_repository(request)
    if app_repo is None:
        response.status_code = 503
        return {"error": "service_unavailable"}

    app = app_repo.find_by_id(_parse_uuid(application_id))
    if app is None:
        response.status_code = 404
        return {"error": "NOT_FOUND"}

    # Get job detail for email generation
    job_repo = getattr(request.app.state, "job_read_repository", None)
    if job_repo is None:
        response.status_code = 503
        return {"error": "service_unavailable"}

    job_detail = job_repo.get_job_detail(str(app.canonical_job_id))
    if job_detail is None:
        response.status_code = 404
        return {"error": "JOB_NOT_FOUND"}

    # Build job dict for email_drafting.generate_body
    version_data: dict[str, Any] = {}
    if job_detail.get("versions"):
        version_data = job_detail["versions"][0].get("structured_data", {})

    job_dict: dict[str, Any] = {
        "title": job_detail.get("canonical_title", ""),
        "company": "",
        "raw_data": version_data,
    }
    # Get company name from postings
    postings = job_detail.get("postings", [])
    if postings and postings[0].get("source"):
        job_dict["company"] = postings[0]["source"].get("identifier", "")

    # Generate email draft
    from careerops.application.email_drafting import generate_body

    resume_text = ""  # TODO: load from candidate's resume version
    subject, body = generate_body(job_dict, resume_text)

    # Look up recruiting contact from contacts table
    to_email = ""
    company_id = job_detail.get("company_id")
    if company_id:
        contact_repo = _get_contact_repository(request)
        if contact_repo:
            contacts = contact_repo.find_by_company(company_id)
            if contacts:
                contact = contacts[0]
                to_email = contact.email if hasattr(contact, "email") else contact.get("email", "")

    return EmailDraftResponse(
        subject=subject,
        body=body,
        to=to_email,
        company=job_dict["company"],
        title=job_dict["title"],
    )


@router.post(
    "/applications/{application_id}/send-email",
    response_model=ApplicationResponse,
    summary="Send application email via Gmail",
)
async def send_application_email(
    application_id: str,
    body: SendEmailRequest,
    request: Request,
    response: Response,
) -> ApplicationResponse | dict[str, str]:
    response.headers["Cache-Control"] = "no-store"

    app_repo = _get_application_repository(request)
    if app_repo is None:
        response.status_code = 503
        return {"error": "service_unavailable"}

    app = app_repo.find_by_id(_parse_uuid(application_id))
    if app is None:
        response.status_code = 404
        return {"error": "NOT_FOUND"}

    if not body.to or "@" not in body.to:
        response.status_code = 422
        return {"error": "INVALID_RECIPIENT"}

    # Send email via GmailSender
    import json
    from pathlib import Path

    from careerops.integrations.gmail_sender import (
        GmailSender,
        OutgoingEmail,
        refresh_access_token,
    )

    token_file = (
        Path(__file__).resolve().parent.parent.parent.parent / "secrets" / "gmail_send_token.json"
    )
    if not token_file.exists():
        response.status_code = 503
        return {"error": "GMAIL_NOT_CONFIGURED"}

    tok = json.loads(token_file.read_text())
    if "gmail.send" not in tok.get("scope", ""):
        response.status_code = 503
        return {"error": "GMAIL_SEND_SCOPE_MISSING"}

    access_token = tok["access_token"]
    if tok.get("refresh_token"):
        with contextlib.suppress(Exception):
            access_token = refresh_access_token(
                client_id=tok["client_id"],
                client_secret=tok["client_secret"],
                refresh_token=tok["refresh_token"],
            )  # use stored token on failure

    sender = GmailSender(access_token)
    email = OutgoingEmail(
        to=body.to,
        subject=body.subject,
        body=body.body,
    )

    try:
        sender.send(email)
    except Exception as e:
        response.status_code = 502
        return {"error": f"SEND_FAILED: {e}"}

    # Record submission
    from careerops.domain.applications import IllegalTransitionError

    app_service = _get_application_service(request)
    if app_service is not None:
        with contextlib.suppress(IllegalTransitionError):
            app_service.record_manual_submission(
                application_id=_parse_uuid(application_id),
                actor_id="email_send",
                now=_now(),
            )

    return _app_to_response(app)


@router.get(
    "/applications/{application_id}/events",
    response_model=ApplicationEventListResponse,
    summary="Get application event history",
)
async def get_application_events(
    application_id: str,
    request: Request,
    response: Response,
) -> ApplicationEventListResponse:
    response.headers["Cache-Control"] = "no-store"
    service = _get_application_service(request)
    if service is None:
        return ApplicationEventListResponse(items=[], total=0)
    events = service.get_event_history(_parse_uuid(application_id))
    items = [
        ApplicationEventResponse(
            id=str(e.id),
            application_id=str(e.application_id),
            event_type=e.event_type.value,
            from_state=e.from_state.value if e.from_state else None,
            to_state=e.to_state.value if e.to_state else None,
            source=e.source.value,
            actor_id=e.actor_id,
            note=e.note,
            occurred_at=e.occurred_at.isoformat() if e.occurred_at else None,
        )
        for e in events
    ]
    return ApplicationEventListResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# Resume version endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/resume-versions",
    response_model=ResumeVersionResponse,
    status_code=201,
    summary="Register a resume version",
)
async def create_resume_version(
    body: ResumeVersionCreateRequest,
    request: Request,
    response: Response,
) -> ResumeVersionResponse:
    response.headers["Cache-Control"] = "no-store"
    service = _get_application_service(request)
    if service is None:
        response.status_code = 503
        return ResumeVersionResponse(
            id="",
            candidate_id=body.candidate_id,
            version_number=0,
            file_reference="",
            content_hash="",
        )

    from careerops.application.applications import ResumeVersionCreateRequest as ServiceRequest

    result = service.register_resume_version(
        ServiceRequest(
            candidate_id=_parse_uuid(body.candidate_id),
            file_reference=body.file_reference,
            content_hash=body.content_hash,
            target_type=body.target_type,
            human_confirmed=body.human_confirmed,
        ),
        now=_now(),
    )
    response.status_code = 201
    return ResumeVersionResponse(
        id=str(result.id),
        candidate_id=str(result.candidate_id),
        version_number=result.version_number,
        file_reference=result.file_reference,
        content_hash=result.content_hash,
        target_type=result.target_type,
        human_confirmed=result.human_confirmed,
    )


# ---------------------------------------------------------------------------
# Application package endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/application-packages",
    response_model=ApplicationPackageResponse,
    status_code=201,
    summary="Create an application package",
)
async def create_package(
    body: ApplicationPackageCreateRequest,
    request: Request,
    response: Response,
) -> ApplicationPackageResponse:
    response.headers["Cache-Control"] = "no-store"
    service = _get_application_service(request)
    if service is None:
        response.status_code = 503
        return ApplicationPackageResponse(
            id="", application_id=body.application_id, resume_version_id=body.resume_version_id
        )

    from careerops.application.applications import PackageCreateRequest as ServiceRequest
    from careerops.domain.applications import PackageClaim

    claims = tuple(
        PackageClaim(
            claim_text=c.claim_text,
            evidence_ids=tuple(_parse_uuid(eid) for eid in c.evidence_ids),
        )
        for c in body.claims
    )
    result = service.create_package(
        ServiceRequest(
            application_id=_parse_uuid(body.application_id),
            resume_version_id=_parse_uuid(body.resume_version_id),
            cover_letter_text=body.cover_letter_text,
            notes=body.notes,
            answers=body.answers,
            claims=claims,
        ),
        now=_now(),
    )
    response.status_code = 201
    return ApplicationPackageResponse(
        id=str(result.id),
        application_id=str(result.application_id),
        resume_version_id=str(result.resume_version_id),
        cover_letter_text=result.cover_letter_text,
        notes=result.notes,
        answers=result.answers,
        claims=[
            PackageClaimResponse(
                claim_text=c.claim_text,
                evidence_ids=[str(eid) for eid in c.evidence_ids],
            )
            for c in result.claims
        ],
        approval_state=result.approval_state.value,
    )


# ---------------------------------------------------------------------------
# Follow-up endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/follow-ups",
    response_model=FollowUpResponse,
    status_code=201,
    summary="Schedule a follow-up reminder",
)
async def schedule_follow_up(
    body: FollowUpScheduleRequest,
    request: Request,
    response: Response,
) -> FollowUpResponse | dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    service = _get_application_service(request)
    if service is None:
        response.status_code = 503
        return {"error": "service_unavailable"}

    from careerops.application.applications import (
        DuplicateFollowUpError,
    )
    from careerops.application.applications import (
        FollowUpScheduleRequest as ServiceRequest,
    )

    try:
        result = service.schedule_follow_up(
            ServiceRequest(
                application_id=_parse_uuid(body.application_id),
                business_days=body.business_days,
            ),
            now=_now(),
        )
    except DuplicateFollowUpError as e:
        response.status_code = 409
        return {"error": str(e)}
    response.status_code = 201
    return FollowUpResponse(
        id=str(result.id),
        application_id=str(result.application_id),
        rule_version=result.rule_version,
        state=result.state.value,
        due_at=result.due_at.isoformat() if result.due_at else None,
        snoozed_until=result.snoozed_until.isoformat() if result.snoozed_until else None,
        cancelled_reason=result.cancelled_reason,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_contact_repository(request: Request) -> Any:
    return getattr(request.app.state, "contact_repository", None)


def _get_contact_service(request: Request) -> Any:
    return getattr(request.app.state, "contact_service", None)


def _get_application_repository(request: Request) -> Any:
    return getattr(request.app.state, "application_repository", None)


def _get_application_service(request: Request) -> Any:
    return getattr(request.app.state, "application_service", None)


def _parse_uuid(value: str) -> Any:
    from uuid import UUID

    return UUID(value)


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC)


def _app_to_response(app: Any) -> ApplicationResponse:
    return ApplicationResponse(
        id=str(app.id),
        candidate_id=str(app.candidate_id),
        canonical_job_id=str(app.canonical_job_id),
        state=app.state.value,
        apply_url=app.apply_url,
        submitted_at=app.submitted_at.isoformat() if app.submitted_at else None,
        follow_up_due_at=app.follow_up_due_at.isoformat() if app.follow_up_due_at else None,
        version=app.version,
    )
