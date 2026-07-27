"""Mail intelligence API routes (Section 12, tasks 12.5-12.6).

Additive routes under /api/v1 that expose the EmailEventProposal review
surface. A proposal is created review-only (the deterministic + optional model
extraction never writes application state); the user accepts or rejects it,
and ONLY acceptance — through the existing USER-sourced transition service —
may change application state (Iron Rule 2).

Routes:
- POST /api/v1/mail/messages/{message_id}/proposal
        — extract + propose (idempotent)
- GET  /api/v1/mail/proposals
        — list proposals for the candidate (cursor pagination, no-store)
- GET  /api/v1/mail/proposals/{proposal_id}
        — proposal detail (ownership)
- POST /api/v1/mail/proposals/{proposal_id}/accept
        — accept (ownership, CSRF via require_api_auth, legal transition, rate limit)
- POST /api/v1/mail/proposals/{proposal_id}/reject
        — reject (ownership, CSRF, idempotent, rate limit)

Iron rules honored:
- Additive (Iron Rule 8): new paths only; no existing route touched.
- Server-side ownership (Iron Rule 2 + 6): candidate resolved via
  ``require_candidate_id``; not-owned -> 404 (no existence leak).
- Dependency-not-ready (Iron Rule 3/6): missing service -> 503.
- Default-deny (Iron Rule 7): the router is gated on the released-by-default
  CRAWL_PLAN_MANAGEMENT capability (mail intelligence is downstream of crawl
  provenance and performs NO external writes). Live Gmail sync stays gated on
  GMAIL_READ (denied) in Section 11.
- Model review-only (Iron Rule 2): extraction is review-only DATA; acceptance
  delegates to the USER-sourced transition path.
"""

# Repos/services fetched via require_repository are typed as ``object``.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from careerops.api.auth_dependency import require_candidate_id
from careerops.api.capability_dependency import require_capability, require_repository
from careerops.application.mail_intelligence_service import (
    MailIntelligenceService,
    ProposalDecisionResult,
)
from careerops.domain.mail_intelligence import (
    EmailEventProposalState,
    IllegalProposalTransitionError,
    MailIntelligenceError,
    MessageNotLinkedError,
    ProposalAlreadyDecidedError,
    ProposalNotOwnedError,
    ProposedStateIllegalError,
)
from careerops.orchestration.capability_resolver import CapabilityKind

router = APIRouter(
    prefix="/api/v1",
    tags=["mail-intelligence"],
    # Mail intelligence is downstream of crawl-plan provenance and performs NO
    # external writes (live Gmail sync is Section 11, gated on the denied
    # GMAIL_READ capability). Gate on the released-by-default
    # CRAWL_PLAN_MANAGEMENT capability, same as the application workspace.
    dependencies=[Depends(require_capability(CapabilityKind.CRAWL_PLAN_MANAGEMENT))],
)

_NO_STORE = {"cache_control": "no-store"}


# ---------------------------------------------------------------------------
# Simple per-candidate rate limiter (task 12.6)
# ---------------------------------------------------------------------------


class _ProposalDecisionRateLimiter:
    """Bounded in-memory sliding-window limiter for proposal decisions.

    Keyed by candidate_id. Deliberately in-process and best-effort: it prevents
    a runaway client from hammering the accept/reject endpoints while the
    shared Redis-backed limiter is reserved for auth. Not authoritative across
    processes; the ownership + idempotency guarantees are the load-bearing
    protections.
    """

    __slots__ = ("_hits", "_limit", "_window")

    def __init__(self, *, window_seconds: float = 60.0, limit: int = 60) -> None:
        self._window = window_seconds
        self._limit = limit
        self._hits: dict[UUID, list[float]] = {}

    def acquire(self, candidate_id: UUID) -> bool:
        now = time.monotonic()
        bucket = self._hits.get(candidate_id)
        if bucket is None:
            bucket = []
            self._hits[candidate_id] = bucket
        cutoff = now - self._window
        bucket[:] = [t for t in bucket if t >= cutoff]
        if len(bucket) >= self._limit:
            return False
        bucket.append(now)
        return True


_RATE_LIMITER = _ProposalDecisionRateLimiter()


def _require_decision_quota(candidate_id: UUID) -> None:
    if not _RATE_LIMITER.acquire(candidate_id):
        from careerops.api.errors import RateLimitedError

        raise RateLimitedError()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class EvidenceSpanItem(BaseModel):
    label: str
    text: str
    start: int = 0
    end: int = 0


class ExtractionItem(BaseModel):
    category: str
    outcome: str
    sender_email: str = ""
    sender_name: str = ""
    sender_domain: str = ""
    interview_at: str | None = None
    timezone: str = ""
    deadline: str | None = None
    requested_materials: list[str] = Field(default_factory=list)
    compensation: str = ""
    summary: str = ""
    confidence: float = 0.0
    evidence_spans: list[EvidenceSpanItem] = Field(default_factory=list)
    source: str = "rules"
    rules_version: str = ""
    model_version: str = ""
    high_risk: bool = False
    review_required: bool = True
    prompt_injection_detected: bool = False


class ProposalResponse(BaseModel):
    id: str
    message_id: str
    thread_id: str | None = None
    account_id: str | None = None
    candidate_id: str
    application_id: str | None = None
    extraction: ExtractionItem
    proposed_state: str | None = None
    idempotency_key: str
    state: str = "pending"
    decided_at: str | None = None
    decided_by: str = ""
    created_at: str | None = None
    updated_at: str | None = None


class ProposalListResponse(BaseModel):
    items: list[ProposalResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class ProposalDecisionResponse(BaseModel):
    proposal: ProposalResponse
    application_id: str | None = None
    application_state: str | None = None
    already_decided: bool = False


class ProposalCreateRequest(BaseModel):
    application_id: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(request: Request) -> MailIntelligenceService:
    return require_repository(request, "mail_intelligence_service")  # type: ignore[return-value]


def _extraction_to_item(extraction: object) -> ExtractionItem:
    def _iso(value: object) -> str | None:
        if value is None:
            return None
        return value.isoformat()  # type: ignore[attr-defined]

    return ExtractionItem(
        category=extraction.category.value,
        outcome=extraction.outcome.value,
        sender_email=getattr(extraction, "sender_email", ""),
        sender_name=getattr(extraction, "sender_name", ""),
        sender_domain=getattr(extraction, "sender_domain", ""),
        interview_at=_iso(getattr(extraction, "interview_at", None)),
        timezone=getattr(extraction, "timezone", ""),
        deadline=_iso(getattr(extraction, "deadline", None)),
        requested_materials=list(getattr(extraction, "requested_materials", ())),
        compensation=getattr(extraction, "compensation", ""),
        summary=getattr(extraction, "summary", ""),
        confidence=getattr(extraction, "confidence", 0.0),
        evidence_spans=[
            EvidenceSpanItem(label=s.label, text=s.text, start=s.start, end=s.end)
            for s in getattr(extraction, "evidence_spans", ())
        ],
        source=extraction.source.value,
        rules_version=getattr(extraction, "rules_version", ""),
        model_version=getattr(extraction, "model_version", ""),
        high_risk=getattr(extraction, "high_risk", False),
        review_required=getattr(extraction, "review_required", True),
        prompt_injection_detected=getattr(extraction, "prompt_injection_detected", False),
    )


def _proposal_to_response(p: object) -> ProposalResponse:
    decided_at = getattr(p, "decided_at", None)
    created_at = getattr(p, "created_at", None)
    updated_at = getattr(p, "updated_at", None)
    return ProposalResponse(
        id=str(p.id),
        message_id=str(p.message_id),
        thread_id=str(p.thread_id) if p.thread_id else None,
        account_id=str(p.account_id) if p.account_id else None,
        candidate_id=str(p.candidate_id),
        application_id=str(p.application_id) if p.application_id else None,
        extraction=_extraction_to_item(p.extraction),
        proposed_state=getattr(p, "proposed_state", None),
        idempotency_key=getattr(p, "idempotency_key", ""),
        state=p.state.value,
        decided_at=decided_at.isoformat() if decided_at else None,
        decided_by=getattr(p, "decided_by", ""),
        created_at=created_at.isoformat() if created_at else None,
        updated_at=updated_at.isoformat() if updated_at else None,
    )


def _translate_decision_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (ProposalNotOwnedError,)):
        return HTTPException(status_code=404, detail="Proposal not found")
    if isinstance(exc, ProposedStateIllegalError):
        return HTTPException(
            status_code=422,
            detail=(f"Illegal transition: {exc.current_state} -> {exc.proposed_state}"),
        )
    if isinstance(exc, ProposalAlreadyDecidedError):
        # Repeat decision: surface as 409 with the recorded state.
        return HTTPException(
            status_code=409,
            detail=f"Proposal already decided: {exc.state.value}",
        )
    if isinstance(exc, MessageNotLinkedError):
        return HTTPException(
            status_code=422,
            detail=(
                "Proposal has no resolved application link; confirm the "
                "application before accepting"
            ),
        )
    if isinstance(exc, IllegalProposalTransitionError):
        return HTTPException(
            status_code=409,
            detail=f"Illegal proposal transition: {exc.from_state.value} -> {exc.to_state.value}",
        )
    if isinstance(exc, MailIntelligenceError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _decode_cursor(raw: str | None) -> tuple[datetime, UUID] | None:
    if not raw:
        return None
    try:
        ts_str, uid_str = raw.split(":", 1)
        return datetime.fromisoformat(ts_str), UUID(uid_str)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid cursor") from None


def _encode_cursor(cursor: tuple[datetime, UUID] | None) -> str | None:
    if cursor is None:
        return None
    ts, uid = cursor
    return f"{ts.isoformat()}:{uid}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/mail/messages/{message_id}/proposal")
def create_proposal(
    message_id: str,
    body: ProposalCreateRequest | None,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> ProposalResponse:
    """Extract a message and create/reuse a proposal (task 12.5).

    Idempotent on ``(message_id, extraction_hash)``: a re-run returns the
    existing proposal. High-risk / low-confidence / unknown / injection-flagged
    proposals are created ``review_required`` and NEVER auto-apply.
    """
    service = _service(request)
    application_id = UUID(body.application_id) if body and body.application_id else None
    try:
        proposal = service.extract_and_propose(
            message_id=UUID(message_id),
            candidate_id=candidate_id,
            application_id=application_id,
            now=datetime.now(tz=UTC),
        )
    except MailIntelligenceError as e:
        raise _translate_decision_error(e) from e
    return _proposal_to_response(proposal)


@router.get("/mail/proposals")
def list_proposals(
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    state: str | None = Query(None, pattern="^(pending|accepted|rejected|superseded)$"),
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> ProposalListResponse:
    """List proposals for the candidate (cursor pagination, no-store)."""
    service = _service(request)
    state_filter = EmailEventProposalState(state) if state else None
    decoded = _decode_cursor(cursor)
    items, next_cursor = service.list_proposals(
        candidate_id=candidate_id,
        state=state_filter,
        limit=limit,
        cursor=decoded,
    )
    response.headers["cache-control"] = "no-store"
    return ProposalListResponse(
        items=[_proposal_to_response(p) for p in items],
        next_cursor=_encode_cursor(next_cursor),
    )


@router.get("/mail/proposals/{proposal_id}")
def get_proposal(
    proposal_id: str,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> ProposalResponse:
    """Return a proposal detail (ownership-scoped)."""
    service = _service(request)
    try:
        proposal = service.get_proposal(proposal_id=UUID(proposal_id), candidate_id=candidate_id)
    except MailIntelligenceError as e:
        raise _translate_decision_error(e) from e
    return _proposal_to_response(proposal)


@router.post("/mail/proposals/{proposal_id}/accept")
def accept_proposal(
    proposal_id: str,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> ProposalDecisionResponse:
    """Accept a proposal (task 12.6 + 12.7).

    Validates ownership + legal proposal transition + legal application
    transition; delegates the state change to the USER-sourced transition
    service (the proposal never writes state directly). Appends a mail-derived
    timeline note and schedules a follow-up where policy permits. Idempotent: a
    repeat accept returns the recorded result.
    """
    _require_decision_quota(candidate_id)
    service = _service(request)
    try:
        result = service.accept_proposal(
            proposal_id=UUID(proposal_id),
            candidate_id=candidate_id,
            now=datetime.now(tz=UTC),
        )
    except MailIntelligenceError as e:
        raise _translate_decision_error(e) from e
    response.headers["cache-control"] = "no-store"
    return _decision_response(result)


@router.post("/mail/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: str,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> ProposalDecisionResponse:
    """Reject a proposal (task 12.6).

    Records the rejection, leaves application state unchanged, blocks the same
    proposal from being accepted later. Idempotent.
    """
    _require_decision_quota(candidate_id)
    service = _service(request)
    try:
        result = service.reject_proposal(
            proposal_id=UUID(proposal_id),
            candidate_id=candidate_id,
            now=datetime.now(tz=UTC),
        )
    except MailIntelligenceError as e:
        raise _translate_decision_error(e) from e
    response.headers["cache-control"] = "no-store"
    return _decision_response(result)


def _decision_response(result: ProposalDecisionResult) -> ProposalDecisionResponse:
    application = result.application
    return ProposalDecisionResponse(
        proposal=_proposal_to_response(result.proposal),
        application_id=str(application.id) if application else None,
        application_state=application.state.value if application else None,
        already_decided=result.already_decided,
    )
