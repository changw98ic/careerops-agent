"""System-managed Gmail send API routes (Section 10, tasks 10.1-10.8, 10.11).

Additive routes under /api/v1 that turn the CareerOps final-confirmation
click into a durable send intent and expose the resulting send-progress /
sent / failed / reconciliation-required state to the UI. The provider is
NEVER called from the request path — confirmation returns ``pending`` and
an isolated side-effect worker drives the provider call (task 10.4).

Routes:
- POST /api/v1/applications/{application_id}/system-send
        — record the final confirmation (durable intent + outbox + pending)
- GET  /api/v1/applications/{application_id}/system-send/{intent_id}
        — idempotent status read (never displays queued as sent)
- POST /api/v1/applications/{application_id}/system-send/{intent_id}/reconcile
        — surface an ambiguous outcome as a reconciliation task (no retry)

Iron rules honored:
- Additive (Iron Rule 8): new paths only; no existing route touched.
- Server-side ownership (Iron Rule 2 + 6): candidate resolved via
  ``require_candidate_id``; not-owned -> 404 (no existence leak).
- Dependency-not-ready (Iron Rule 3/6): missing service/kernel -> 503.
- Default-deny (Iron Rule 7): every route is gated on the
  ``SYSTEM_MANAGED_SEND`` capability, which stays DENIED at the contract
  layer until a separate qualification change releases it. Real Gmail OAuth
  is NOT enabled here (task 17.6).
- Model output is review-only (Iron Rule 2): the request body carries only
  the exact bytes the user reviewed (payload hash, recipient, subject, body);
  the server never lets the model pick the recipient or transition state.
"""

# Repos/services fetched via require_repository are typed as ``object``.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from careerops.api.auth_dependency import require_candidate_id
from careerops.api.capability_dependency import require_capability, require_repository
from careerops.application.system_managed_send import (
    SystemManagedSendService,
    SystemSendDeniedError,
    SystemSendNotFoundError,
)
from careerops.domain.system_send import SystemSendPhase
from careerops.orchestration.capability_resolver import CapabilityKind

router = APIRouter(
    prefix="/api/v1",
    tags=["system-managed-send"],
    # Every system-managed-send path is default-denied at the contract layer:
    # SYSTEM_MANAGED_SEND stays DENIED until a separate qualification change
    # releases it. Iron Rule 7.
    dependencies=[Depends(require_capability(CapabilityKind.SYSTEM_MANAGED_SEND))],
)

_NO_STORE = {"cache_control": "no-store"}


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class SystemSendConfirmRequest(BaseModel):
    """Exact bytes the user reviewed at final confirmation.

    The server recomputes / revalidates against trusted business state; these
    fields are the user-acknowledged payload, not a model-produced recipient.
    """

    account_email: str
    recipient: str
    subject: str
    body: str
    package_version_id: str
    payload_hash: str
    job_version_id: str | None = None
    resume_version_id: str | None = None
    attachment_hashes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class SystemSendStatusResponse(BaseModel):
    application_id: str
    intent_id: str | None = None
    phase: str
    payload_hash: str | None = None
    provider_resource_id: str | None = None
    provider_message_id: str | None = None
    submitted_at: str | None = None
    denial_reasons: list[str] = Field(default_factory=list)
    reconciled: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(request: Request) -> SystemManagedSendService:
    return require_repository(request, "system_managed_send_service")  # type: ignore[return-value]


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, SystemSendNotFoundError):
        return HTTPException(status_code=404, detail="Send intent not found")
    if isinstance(exc, SystemSendDeniedError):
        return HTTPException(
            status_code=403,
            detail={
                "code": "DENIED_POLICY",
                "reason_codes": list(exc.reason_codes),
            },
        )
    return HTTPException(status_code=400, detail=str(exc))


def _to_status_response(status: object) -> SystemSendStatusResponse:
    submitted_at = status.submitted_at  # type: ignore[attr-defined]
    intent_id = status.intent_id  # type: ignore[attr-defined]
    return SystemSendStatusResponse(
        application_id=str(status.application_id),  # type: ignore[attr-defined]
        intent_id=str(intent_id) if intent_id else None,
        phase=status.phase.value,  # type: ignore[attr-defined]
        payload_hash=status.payload_hash,  # type: ignore[attr-defined]
        provider_resource_id=status.provider_resource_id,  # type: ignore[attr-defined]
        provider_message_id=status.provider_message_id,  # type: ignore[attr-defined]
        submitted_at=submitted_at.isoformat() if submitted_at else None,
        denial_reasons=list(status.denial_reasons),  # type: ignore[attr-defined]
        reconciled=bool(status.reconciled),  # type: ignore[attr-defined]
    )


def _to_request(
    application_id: UUID, candidate_id: UUID, body: SystemSendConfirmRequest
) -> object:
    from careerops.domain.system_send import SystemSendRequest

    return SystemSendRequest(
        application_id=application_id,
        candidate_id=candidate_id,
        account_email=body.account_email,
        recipient=body.recipient,
        subject=body.subject,
        body=body.body,
        package_version_id=UUID(body.package_version_id),
        payload_hash=body.payload_hash,
        job_version_id=UUID(body.job_version_id) if body.job_version_id else None,
        resume_version_id=UUID(body.resume_version_id) if body.resume_version_id else None,
        attachment_hashes=tuple(body.attachment_hashes),
        evidence_refs=tuple(body.evidence_ids),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/applications/{application_id}/system-send")
def confirm_system_send(
    application_id: str,
    body: SystemSendConfirmRequest,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> SystemSendStatusResponse:
    """Record the CareerOps final confirmation as a durable send intent.

    Returns ``phase=pending`` — the provider is NOT called from this path; an
    isolated worker drives execution (task 10.4). Revalidation denials
    (ownership / state / package / capability / recipient / account) surface
    as 403 BEFORE any provider call (task 10.8).
    """
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    try:
        status = service.confirm_send(
            _to_request(UUID(application_id), candidate_id, body),  # type: ignore[arg-type]
            candidate_id=candidate_id,
            now=datetime.now(tz=UTC),
        )
    except Exception as e:
        raise _translate(e) from e
    return _to_status_response(status)


@router.get("/applications/{application_id}/system-send/{intent_id}")
def get_system_send_status(
    application_id: str,
    intent_id: str,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> SystemSendStatusResponse:
    """Return the durable send phase (never displays queued as sent).

    ``pending`` -> in progress, ``sent`` -> provider receipt confirmed +
    application submitted, ``failed`` -> definitive provider failure,
    ``reconciliation_required`` -> ambiguous outcome; blind retry disabled.
    """
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    try:
        status = service.get_status(
            UUID(intent_id),
            candidate_id=candidate_id,
        )
    except Exception as e:
        raise _translate(e) from e
    # Defense-in-depth: the status route never claims a submitted channel the
    # application does not own.
    if str(status.application_id) != application_id:  # type: ignore[attr-defined]
        raise HTTPException(status_code=404, detail="Send intent not found")
    return _to_status_response(status)


@router.post("/applications/{application_id}/system-send/{intent_id}/reconcile")
def escalate_system_send_reconciliation(
    application_id: str,
    intent_id: str,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> SystemSendStatusResponse:
    """Surface an ambiguous send as a user reconciliation task (no retry).

    Blind retry is disabled for ambiguous outcomes (task 10.6). The intent
    stays in ``reconciliation_required`` and the application remains visibly
    unresolved.
    """
    response.headers["Cache-Control"] = "no-store"
    service = _service(request)
    try:
        status = service.escalate_reconciliation(
            UUID(intent_id),
            candidate_id=candidate_id,
            now=datetime.now(tz=UTC),
        )
    except Exception as e:
        raise _translate(e) from e
    if str(status.application_id) != application_id:  # type: ignore[attr-defined]
        raise HTTPException(status_code=404, detail="Send intent not found")
    if status.phase is not SystemSendPhase.RECONCILIATION_REQUIRED:  # type: ignore[attr-defined]
        raise HTTPException(
            status_code=409,
            detail="reconciliation escalation only applies to ambiguous outcomes",
        )
    return _to_status_response(status)
