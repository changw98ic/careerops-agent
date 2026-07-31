"""Email payload API routes (Section 9, task 9.6).

Additive routes under /api/v1/candidates/{candidate_id} for the trusted-contact +
initial-application-email-payload preview surface:

- GET  /api/v1/candidates/{candidate_id}/applications/{application_id}/recruiting-contacts
       — list trusted contact evidence for the application (9.1)
- POST /api/v1/candidates/{candidate_id}/applications/{application_id}/submission-preview
       — return the exact sendable representation + validation errors with
         NO provider side effects (9.6)

Iron rules honored:
- Additive API (Iron Rule 8): new paths only; no existing route broken.
- Server-side ownership (Iron Rule 2 + 6): candidate supplied by the path;
  not-owned → 404 (no existence leak).
- Dependency-not-ready (Iron Rule 3/6): missing service → 503.
- Default-deny (Iron Rule 7): CRAWL_PLAN_MANAGEMENT gate; NO provider side
  effects (the preview performs no external write — that is Section 10).
- Cache-Control: no-store on every response (preview data must never be
  cached, it is the live exact representation).
- Model cannot pick the recipient (Iron Rule 2): the recipient is validated
  against trusted resolver evidence; model-only recipients are denied.
"""

# Repos/services fetched via require_repository are typed as ``object``.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from careerops.api.capability_dependency import require_capability, require_repository
from careerops.application.email_payload_service import (
    ApplicationNotOwnedError,
    EmailPayloadService,
)
from careerops.orchestration.capability_resolver import CapabilityKind

router = APIRouter(
    prefix="/api/v1/candidates/{candidate_id}",
    tags=["email-payloads"],
    # The preview surface is downstream of crawl-plan provenance (same as the
    # workspace + inbox) and performs NO external writes; gate on the
    # released-by-default CRAWL_PLAN_MANAGEMENT capability. SYSTEM_MANAGED_SEND
    # stays denied — it gates the actual send (Section 10), not the preview.
    dependencies=[Depends(require_capability(CapabilityKind.CRAWL_PLAN_MANAGEMENT))],
)

# Every response from this router carries Cache-Control: no-store — the
# preview is the live exact representation and must never be served from a
# cache (task 9.6 + design Decision 9). The header is set per-handler via the
# injected FastAPI ``Response`` object.


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class RecruitingContactEvidenceItem(BaseModel):
    email: str
    company_domain: str
    contact_type: str
    confidence: str
    source_evidence_url: str
    source_evidence_text: str = ""
    domain_match: bool = True


class RecruitingContactListResponse(BaseModel):
    application_id: str
    canonical_job_id: str
    contacts: list[RecruitingContactEvidenceItem] = Field(default_factory=list)


class SubmissionPreviewRequest(BaseModel):
    account_id: str
    recipient_email: str
    subject: str
    body: str = ""
    attachment_hashes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    in_reply_to: str = ""
    references_header: str = ""
    message_id_header: str = ""


class AttachmentPreviewItem(BaseModel):
    name: str
    content_hash: str
    media_type: str
    size_bytes: int
    retention_state: str = "retained"


class RecipientVerdictItem(BaseModel):
    email: str
    company_domain: str
    contact_type: str
    confidence: str
    source_evidence_url: str
    eligible: bool
    denial_reasons: list[str] = Field(default_factory=list)


class PayloadPreviewItem(BaseModel):
    application_id: str
    package_version_id: str
    account_id: str
    account_email: str
    recipient_email: str
    subject: str
    body: str
    attachments: list[AttachmentPreviewItem] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    in_reply_to: str = ""
    references_header: str = ""
    message_id_header: str = ""
    payload_hash: str
    idempotency_key: str
    reconciliation_key: str


class SubmissionPreviewResponse(BaseModel):
    application_id: str
    sendable: bool
    payload: PayloadPreviewItem | None = None
    recipient_verdict: RecipientVerdictItem | None = None
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(request: Request) -> EmailPayloadService:
    return require_repository(request, "email_payload_service")  # type: ignore[return-value]


def _workspace(request: Request) -> object:
    return require_repository(request, "application_workspace_service")


def _require_owned_app(request: Request, application_id: UUID, candidate_id: UUID) -> object:
    """Enforce candidate ownership and return the application record."""
    ws = _workspace(request)
    try:
        return ws.get_application(  # type: ignore[attr-defined]
            application_id=application_id, candidate_id=candidate_id
        )
    except ApplicationNotOwnedError as exc:
        raise HTTPException(status_code=404, detail="Application not found") from exc
    except Exception as exc:
        if "not found" in str(exc).lower() or "not owned" in str(exc).lower():
            raise HTTPException(status_code=404, detail="Application not found") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/applications/{application_id}/recruiting-contacts")
def list_recruiting_contacts(
    application_id: str,
    request: Request,
    response: Response,
    candidate_id: UUID,
) -> RecruitingContactListResponse:
    """List trusted recruiting-contact evidence for the application (9.1).

    Returns the raw, resolver-supplied contact evidence so the UI can show
    *why* a recipient is or is not available before the user picks one. The
    list is empty when no source-backed contact evidence exists (the EMAIL
    channel is then not eligible). No provider side effects; Cache-Control
    no-store.
    """
    svc = _service(request)
    app = _require_owned_app(request, UUID(application_id), candidate_id)
    canonical_job_id: UUID = app.canonical_job_id  # type: ignore[attr-defined]
    response.headers["Cache-Control"] = "no-store"
    candidates = svc.list_contact_candidates(
        candidate_id=candidate_id,
        application_id=UUID(application_id),
        canonical_job_id=canonical_job_id,
    )
    return RecruitingContactListResponse(
        application_id=application_id,
        canonical_job_id=str(canonical_job_id),
        contacts=[
            RecruitingContactEvidenceItem(
                email=c.email,
                company_domain=c.company_domain,
                contact_type=c.contact_type,
                confidence=c.confidence,
                source_evidence_url=c.source_evidence_url,
                source_evidence_text=c.source_evidence_text,
                domain_match=c.domain_match,
            )
            for c in candidates
        ],
    )


@router.post("/applications/{application_id}/submission-preview")
def submission_preview(
    application_id: str,
    body: SubmissionPreviewRequest,
    request: Request,
    response: Response,
    candidate_id: UUID,
) -> SubmissionPreviewResponse:
    """Return the exact sendable representation + validation errors (9.6).

    Performs NO provider side effects and persists NO send intent — that is
    Section 10. Validation errors are returned (not raised) so the UI can
    render them. When ``sendable`` is True, ``payload`` is byte-for-byte what
    the provider would receive after the user's final confirmation
    (preview/send parity, task 9.8). Cache-Control: no-store.
    """
    svc = _service(request)
    app = _require_owned_app(request, UUID(application_id), candidate_id)
    canonical_job_id: UUID = app.canonical_job_id  # type: ignore[attr-defined]
    response.headers["Cache-Control"] = "no-store"
    preview = svc.preview_submission(
        candidate_id=candidate_id,
        application_id=UUID(application_id),
        canonical_job_id=canonical_job_id,
        account_id=UUID(body.account_id),
        recipient_email=body.recipient_email,
        subject=body.subject,
        body=body.body,
        attachment_hashes=tuple(body.attachment_hashes),
        evidence_refs=tuple(UUID(e) for e in body.evidence_ids),
        in_reply_to=body.in_reply_to,
        references_header=body.references_header,
        message_id_header=body.message_id_header,
        now=datetime.now(tz=UTC),
    )
    payload_item: PayloadPreviewItem | None = None
    if preview.payload is not None:
        p = preview.payload
        payload_item = PayloadPreviewItem(
            application_id=str(p.application_id),
            package_version_id=str(p.package_version_id),
            account_id=str(p.account.account_id),
            account_email=p.account.email_address,
            recipient_email=p.recipient.email,
            subject=p.subject,
            body=p.body,
            attachments=[
                AttachmentPreviewItem(
                    name=a.name,
                    content_hash=a.content_hash,
                    media_type=a.media_type,
                    size_bytes=a.size_bytes,
                    retention_state=a.retention_state.value,
                )
                for a in p.attachments
            ],
            evidence_refs=[str(e) for e in p.evidence_refs],
            in_reply_to=p.in_reply_to,
            references_header=p.references_header,
            message_id_header=p.message_id_header,
            payload_hash=p.payload_hash,
            idempotency_key=p.idempotency_key,
            reconciliation_key=p.reconciliation_key,
        )
    verdict_item: RecipientVerdictItem | None = None
    if preview.recipient_verdict is not None:
        v = preview.recipient_verdict
        verdict_item = RecipientVerdictItem(
            email=v.email,
            company_domain=v.company_domain,
            contact_type=v.contact_type,
            confidence=v.confidence,
            source_evidence_url=v.source_evidence_url,
            eligible=v.eligible,
            denial_reasons=[r.value for r in v.denial_reasons],
        )
    # Cache-Control: no-store is set on the injected ``response`` above so the
    # preview (the live exact representation) is never served from a cache.
    return SubmissionPreviewResponse(
        application_id=application_id,
        sendable=preview.sendable,
        payload=payload_item,
        recipient_verdict=verdict_item,
        errors=list(preview.errors),
    )
