"""Evidence API routes (Section 3, task 3.6).

Additive routes under ``/api/v1/candidates/{candidate_id}/evidence`` backed by
:class:`EvidenceService`. Confirm/reject is idempotent and records an
append-only audit event on the actual transition (Iron Rule 7). The actor id
is a fixed local actor (the console login was removed; there is no session
principal) and is never taken from the body. Candidate identity comes from the
URL path parameter.
"""

# Pydantic ``Field(default_factory=list)`` and the ``object``-typed response
# mappers produce ``reportUnknown*`` reports that obscure the real mapping
# logic; the existing applications router suppresses them the same way.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from careerops.api.capability_dependency import require_repository
from careerops.application.evidence_service import EvidenceReviewRequest, EvidenceService

# Audit actor for the local single-user console (post login-removal). The
# audit trail records who decided; with no session there is exactly one local
# operator, so the actor is a constant.
AUDIT_ACTOR = "local"

router = APIRouter(prefix="/api/v1/candidates/{candidate_id}/evidence", tags=["evidence"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class EvidenceItemResponse(BaseModel):
    id: str
    kind: str
    name: str
    description: str = ""
    repository: str = ""
    extractor_version: str = ""
    source_span: str = ""
    confirmation_status: str = "unconfirmed"
    evidence_hash: str = ""
    resume_version_id: str | None = None
    verified: bool = False


class EvidenceListResponse(BaseModel):
    items: list[EvidenceItemResponse] = Field(default_factory=list)
    total: int = 0


class EvidenceDecisionResponse(BaseModel):
    evidence: EvidenceItemResponse
    status: str
    was_change: bool


class EvidenceReviewBody(BaseModel):
    """Optional body for confirm/reject. Carries NO actor_id (server-resolved).

    ``source_reference`` is an optional free-form note the user may attach
    (e.g. "confirmed against section 2 of the resume"); it is recorded in the
    audit trail.
    """

    source_reference: str = ""


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _evidence_service(request: Request) -> EvidenceService:
    return require_repository(request, "evidence_service")  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_evidence(
    candidate_id: UUID,
    service: Annotated[EvidenceService, Depends(_evidence_service)],
    response: Response,
    status: Annotated[str | None, Query(description="Filter by confirmation status")] = None,
    limit: int = 200,
) -> EvidenceListResponse:
    from careerops.domain.applications import ConfirmationStatus

    status_filter: ConfirmationStatus | None = None
    if status:
        try:
            status_filter = ConfirmationStatus(status)
        except ValueError:
            from careerops.api.errors import InvalidStateError

            raise InvalidStateError(
                f"unknown confirmation status '{status}'; "
                "expected one of: unconfirmed, confirmed, rejected"
            ) from None
    items = service.list_for_candidate(candidate_id, status=status_filter, limit=limit)
    response.headers["Cache-Control"] = "no-store"
    return EvidenceListResponse(
        items=[_to_response(i) for i in items],
        total=len(items),
    )


@router.get("/{evidence_id}")
def get_evidence(
    evidence_id: UUID,
    candidate_id: UUID,
    service: Annotated[EvidenceService, Depends(_evidence_service)],
    response: Response,
) -> EvidenceItemResponse:
    item = service.get(candidate_id, evidence_id)
    response.headers["Cache-Control"] = "no-store"
    return _to_response(item)


@router.post("/{evidence_id}/confirm")
def confirm_evidence(
    evidence_id: UUID,
    candidate_id: UUID,
    service: Annotated[EvidenceService, Depends(_evidence_service)],
    body: EvidenceReviewBody | None = None,
) -> EvidenceDecisionResponse:
    """Idempotently mark evidence CONFIRMED and append an audit event.

    Repeat confirmations return the existing state (``was_change=False``) and
    record NO new audit event (Iron Rule 7).
    """
    decision = service.confirm(
        candidate_id,
        EvidenceReviewRequest(
            evidence_id=evidence_id,
            actor_id=AUDIT_ACTOR,
            source_reference=(body.source_reference if body else ""),
        ),
    )
    return _decision_to_response(decision)


@router.post("/{evidence_id}/reject")
def reject_evidence(
    evidence_id: UUID,
    candidate_id: UUID,
    service: Annotated[EvidenceService, Depends(_evidence_service)],
    body: EvidenceReviewBody | None = None,
) -> EvidenceDecisionResponse:
    """Idempotently mark evidence REJECTED and append an audit event."""
    decision = service.reject(
        candidate_id,
        EvidenceReviewRequest(
            evidence_id=evidence_id,
            actor_id=AUDIT_ACTOR,
            source_reference=(body.source_reference if body else ""),
        ),
    )
    return _decision_to_response(decision)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_response(item: object) -> EvidenceItemResponse:
    return EvidenceItemResponse(
        id=str(item.id),  # type: ignore[attr-defined]
        kind=item.kind.value,  # type: ignore[attr-defined]
        name=item.name,  # type: ignore[attr-defined]
        description=item.description,  # type: ignore[attr-defined]
        repository=item.repository,  # type: ignore[attr-defined]
        extractor_version=item.extractor_version,  # type: ignore[attr-defined]
        source_span=item.source_span,  # type: ignore[attr-defined]
        confirmation_status=item.confirmation_status.value,  # type: ignore[attr-defined]
        evidence_hash=item.evidence_hash,  # type: ignore[attr-defined]
        resume_version_id=(
            str(item.resume_version_id) if item.resume_version_id else None  # type: ignore[attr-defined]
        ),
        verified=bool(item.verified),  # type: ignore[attr-defined]
    )


def _decision_to_response(decision: object) -> EvidenceDecisionResponse:
    return EvidenceDecisionResponse(
        evidence=_to_response(decision.evidence),  # type: ignore[attr-defined]
        status=decision.status.value,  # type: ignore[attr-defined]
        was_change=decision.was_change,  # type: ignore[attr-defined]
    )
