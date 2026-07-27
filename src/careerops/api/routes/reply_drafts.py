"""Reply drafting + follow-up API routes (Section 13, tasks 13.7 / 13.10).

Additive routes under /api/v1 that expose the reply-draft review surface and
the follow-up reminder actions. A reply draft is created review-only; the user
approves or rejects it, and ONLY an approved low-risk reply may be sent through
the system-managed delivery chain (reused from Section 10). High-risk
categories are permanently denied system send; auto-send is permanently denied
for every reply (Iron Rule 2 / 7).

Routes:

- POST /api/v1/reply/drafts                       — assemble context + create draft
- GET  /api/v1/reply/drafts                       — list drafts (cursor, no-store)
- GET  /api/v1/reply/drafts/{draft_id}            — draft detail (ownership)
- POST /api/v1/reply/drafts/{draft_id}/edit       — body edit -> new version
- POST /api/v1/reply/drafts/{draft_id}/approve    — approve (ownership, CSRF)
- POST /api/v1/reply/drafts/{draft_id}/reject     — reject (ownership, idempotent)
- POST /api/v1/reply/drafts/{draft_id}/send       — user-confirmed send (13.6)
- GET  /api/v1/reply/drafts/{draft_id}/send-status— idempotent send status
- POST /api/v1/applications/{app_id}/follow-ups   — schedule trigger-aware reminder
- GET  /api/v1/applications/{app_id}/follow-ups   — list active reminder
- POST /api/v1/follow-ups/{reminder_id}/{action}  — snooze/reschedule/cancel/complete

Iron rules honored:
- Additive (Iron Rule 8): new paths only; no existing route touched.
- Server-side ownership (Iron Rule 2 + 6): candidate resolved via
  ``require_candidate_id``; not-owned -> 404 (no existence leak).
- Dependency-not-ready (Iron Rule 3/6): missing service -> 503.
- Default-deny (Iron Rule 7): the router is gated on the released-by-default
  CRAWL_PLAN_MANAGEMENT capability (reply work is downstream of crawl + mail
  provenance and performs NO external writes itself — the send delegates to
  the Section 10 chain, separately gated).
- Model review-only (Iron Rule 2): the recipient / intent are user-sourced;
  the draft is review-only DATA until the user approves.
"""

# Repos/services fetched via require_repository are typed as ``object``.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from careerops.api.auth_dependency import require_candidate_id
from careerops.api.capability_dependency import require_capability, require_repository
from careerops.application.reply_draft_service import (
    DuplicateFollowUpError,
    FollowUpNotOwnedError,
    FollowUpService,
    ReplyDraftService,
    ReplySendOutcome,
)
from careerops.domain.reply_draft import (
    DEFAULT_FOLLOW_UP_WAITING_PERIODS,
    ApplicationFact,
    IllegalReplyTransitionError,
    MessageNotLinkedError,
    RecipientMutationError,
    ReplyDraftAlreadyDecidedError,
    ReplyDraftApprovalState,
    ReplyDraftClaim,
    ReplyDraftError,
    ReplyDraftNotOwnedError,
    ReplyIntent,
    ReplySendDeniedError,
    UnsupportedClaimError,
)
from careerops.orchestration.capability_resolver import CapabilityKind

router = APIRouter(
    prefix="/api/v1",
    tags=["reply-draft-follow-up"],
    # Reply work is downstream of crawl + mail provenance and performs NO
    # external writes itself (the send delegates to the Section 10 chain).
    # Gate on the released-by-default CRAWL_PLAN_MANAGEMENT capability.
    dependencies=[Depends(require_capability(CapabilityKind.CRAWL_PLAN_MANAGEMENT))],
)

_NO_STORE = {"cache_control": "no-store"}

_TRIGGER_PATTERN = "^(submitted|awaiting_response|interview|assessment|user)$"


# ---------------------------------------------------------------------------
# Response / request schemas
# ---------------------------------------------------------------------------


class ApplicationFactInput(BaseModel):
    label: str
    value: str
    source: str = "application"


class DraftClaimInput(BaseModel):
    claim_text: str
    evidence_ids: list[str] = Field(default_factory=list)


class DraftCreateRequest(BaseModel):
    message_id: str
    thread_id: str
    application_id: str | None = None
    account_id: str | None = None
    account_email: str = ""
    intent: str = "acknowledge"
    mail_category: str = ""
    recipient: str
    subject: str = ""
    body: str = ""
    in_reply_to: str = ""
    references_header: str = ""
    excerpt: str | None = None
    application_facts: list[ApplicationFactInput] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    claims: list[DraftClaimInput] = Field(default_factory=list)


class DraftEditRequest(BaseModel):
    subject: str | None = None
    body: str | None = None
    claims: list[DraftClaimInput] | None = None


class DraftSendRequest(BaseModel):
    account_email: str


class FollowUpScheduleRequest(BaseModel):
    trigger: str
    business_days: int | None = None
    rule_version: str | None = None
    base_at: datetime | None = None


class FollowUpSnoozeRequest(BaseModel):
    snoozed_until: datetime


class FollowUpRescheduleRequest(BaseModel):
    new_due_at: datetime


class FollowUpCancelRequest(BaseModel):
    reason: str = ""


class ApplicationFactItem(BaseModel):
    label: str
    value: str
    source: str = "application"


class DraftClaimItem(BaseModel):
    claim_text: str
    evidence_ids: list[str] = Field(default_factory=list)
    supported: bool = True


class DraftContextItem(BaseModel):
    message_id: str
    thread_id: str
    application_id: str | None = None
    thread_excerpt: str = ""
    application_facts: list[ApplicationFactItem] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    intent: str = "acknowledge"
    risk_category: str = "low_risk"
    mail_category: str = ""


class DraftResponse(BaseModel):
    id: str
    candidate_id: str
    message_id: str
    thread_id: str
    account_id: str | None = None
    application_id: str | None = None
    version_number: int = 1
    recipient: str
    in_reply_to: str = ""
    references_header: str = ""
    subject: str = ""
    body: str = ""
    intent: str = "acknowledge"
    risk_category: str = "low_risk"
    context: DraftContextItem
    claims: list[DraftClaimItem] = Field(default_factory=list)
    validation_issues: list[str] = Field(default_factory=list)
    payload_hash: str = ""
    approval_state: str = "draft"
    decided_at: str | None = None
    decided_by: str = ""
    send_intent_id: str | None = None
    send_phase: str = ""
    created_at: str | None = None
    updated_at: str | None = None


class DraftListResponse(BaseModel):
    items: list[DraftResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class SendStatusResponse(BaseModel):
    draft_id: str
    phase: str
    intent_id: str | None = None
    payload_hash: str | None = None
    provider_resource_id: str | None = None
    provider_message_id: str | None = None
    denial_reasons: list[str] = Field(default_factory=list)
    sent_at: str | None = None


class FollowUpResponse(BaseModel):
    id: str
    application_id: str
    rule_version: str
    state: str = "active"
    due_at: str | None = None
    snoozed_until: str | None = None
    cancelled_reason: str = ""
    created_at: str | None = None
    updated_at: str | None = None


class FollowUpListResponse(BaseModel):
    items: list[FollowUpResponse] = Field(default_factory=list)


class TriggerPeriodItem(BaseModel):
    trigger: str
    business_days: int


class FollowUpRulesResponse(BaseModel):
    rule_version: str
    default_periods: list[TriggerPeriodItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _draft_service(request: Request) -> ReplyDraftService:
    return require_repository(request, "reply_draft_service")  # type: ignore[return-value]


def _follow_up_service(request: Request) -> FollowUpService:
    return require_repository(request, "follow_up_service")  # type: ignore[return-value]


def _iso_or_none(value: object) -> str | None:
    if value is None:
        return None
    return value.isoformat()  # type: ignore[attr-defined]


def _fact_to_item(fact: object) -> ApplicationFactItem:
    return ApplicationFactItem(
        label=getattr(fact, "label", ""),
        value=getattr(fact, "value", ""),
        source=getattr(fact, "source", "application"),
    )


def _context_to_item(ctx: object) -> DraftContextItem:
    return DraftContextItem(
        message_id=str(getattr(ctx, "message_id", "")),
        thread_id=str(getattr(ctx, "thread_id", "")),
        application_id=str(ctx.application_id) if getattr(ctx, "application_id", None) else None,
        thread_excerpt=getattr(ctx, "thread_excerpt", ""),
        application_facts=[_fact_to_item(f) for f in getattr(ctx, "application_facts", ())],
        evidence_refs=[str(e) for e in getattr(ctx, "evidence_refs", ())],
        intent=getattr(ctx, "intent", "acknowledge"),
        risk_category=getattr(ctx, "risk_category", "low_risk"),
        mail_category=getattr(ctx, "mail_category", ""),
    )


def _draft_to_response(d: object) -> DraftResponse:
    return DraftResponse(
        id=str(d.id),  # type: ignore[attr-defined]
        candidate_id=str(d.candidate_id),  # type: ignore[attr-defined]
        message_id=str(d.message_id),  # type: ignore[attr-defined]
        thread_id=str(d.thread_id),  # type: ignore[attr-defined]
        account_id=str(d.account_id) if getattr(d, "account_id", None) else None,  # type: ignore[attr-defined]
        application_id=str(d.application_id) if getattr(d, "application_id", None) else None,
        version_number=d.version_number,  # type: ignore[attr-defined]
        recipient=d.recipient,  # type: ignore[attr-defined]
        in_reply_to=d.in_reply_to,  # type: ignore[attr-defined]
        references_header=d.references_header,  # type: ignore[attr-defined]
        subject=d.subject,  # type: ignore[attr-defined]
        body=d.body,  # type: ignore[attr-defined]
        intent=d.intent.value,  # type: ignore[attr-defined]
        risk_category=d.risk_category.value,  # type: ignore[attr-defined]
        context=_context_to_item(d.context),  # type: ignore[attr-defined]
        claims=[
            DraftClaimItem(
                claim_text=c.claim_text,
                evidence_ids=[str(e) for e in c.evidence_ids],
                supported=c.supported,
            )
            for c in getattr(d, "claims", ())
        ],
        validation_issues=list(getattr(d, "validation_issues", ())),
        payload_hash=getattr(d, "payload_hash", ""),
        approval_state=d.approval_state.value,  # type: ignore[attr-defined]
        decided_at=_iso_or_none(getattr(d, "decided_at", None)),
        decided_by=getattr(d, "decided_by", ""),
        send_intent_id=str(d.send_intent_id) if getattr(d, "send_intent_id", None) else None,
        send_phase=getattr(d, "send_phase", ""),
        created_at=_iso_or_none(getattr(d, "created_at", None)),
        updated_at=_iso_or_none(getattr(d, "updated_at", None)),
    )


def _reminder_to_response(r: object) -> FollowUpResponse:
    return FollowUpResponse(
        id=str(r.id),  # type: ignore[attr-defined]
        application_id=str(r.application_id),  # type: ignore[attr-defined]
        rule_version=r.rule_version,  # type: ignore[attr-defined]
        state=r.state.value,  # type: ignore[attr-defined]
        due_at=_iso_or_none(getattr(r, "due_at", None)),
        snoozed_until=_iso_or_none(getattr(r, "snoozed_until", None)),
        cancelled_reason=getattr(r, "cancelled_reason", ""),
        created_at=_iso_or_none(getattr(r, "created_at", None)),
        updated_at=_iso_or_none(getattr(r, "updated_at", None)),
    )


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, (ReplyDraftNotOwnedError, FollowUpNotOwnedError)):
        return HTTPException(status_code=404, detail="Reply resource not found")
    if isinstance(exc, ReplyDraftAlreadyDecidedError):
        return HTTPException(
            status_code=409, detail=f"Reply draft already decided: {exc.state.value}"
        )
    if isinstance(exc, IllegalReplyTransitionError):
        return HTTPException(
            status_code=409,
            detail=(f"Illegal reply transition: {exc.from_state.value} -> {exc.to_state.value}"),
        )
    if isinstance(exc, RecipientMutationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, UnsupportedClaimError):
        return HTTPException(status_code=422, detail="; ".join(exc.issues))
    if isinstance(exc, DuplicateFollowUpError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ReplySendDeniedError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, MessageNotLinkedError):
        return HTTPException(
            status_code=422,
            detail="Reply draft has no resolved application/thread link",
        )
    if isinstance(exc, (ReplyDraftError,)):
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


def _send_outcome_to_response(draft_id: UUID, outcome: ReplySendOutcome) -> SendStatusResponse:
    return SendStatusResponse(
        draft_id=str(draft_id),
        phase=outcome.phase,
        intent_id=str(outcome.intent_id) if outcome.intent_id else None,
        payload_hash=outcome.payload_hash,
        provider_resource_id=outcome.provider_resource_id,
        provider_message_id=outcome.provider_message_id,
        denial_reasons=list(outcome.denial_reasons),
        sent_at=_iso_or_none(outcome.sent_at),
    )


# ---------------------------------------------------------------------------
# Draft routes (tasks 13.7 / 13.10)
# ---------------------------------------------------------------------------


@router.get("/reply/drafts")
def list_drafts(
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
    application_id: str | None = None,
    state: str | None = Query(
        None, pattern="^(draft|pending_review|approved|rejected|expired|superseded)$"
    ),
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> DraftListResponse:
    """List reply drafts for the candidate (cursor pagination, no-store)."""
    service = _draft_service(request)
    state_filter = ReplyDraftApprovalState(state) if state else None
    app_id = UUID(application_id) if application_id else None
    decoded = _decode_cursor(cursor)
    items, next_cursor = service.list_drafts(
        candidate_id=candidate_id,
        application_id=app_id,
        state=state_filter,
        limit=limit,
        cursor=decoded,
    )
    response.headers["cache-control"] = "no-store"
    return DraftListResponse(
        items=[_draft_to_response(d) for d in items],
        next_cursor=_encode_cursor(next_cursor),
    )


@router.get("/reply/drafts/{draft_id}")
def get_draft(
    draft_id: str,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> DraftResponse:
    """Return a reply-draft detail (ownership-scoped)."""
    service = _draft_service(request)
    try:
        draft = service.get_draft(draft_id=UUID(draft_id), candidate_id=candidate_id)
    except ReplyDraftError as e:
        raise _translate(e) from e
    return _draft_to_response(draft)


@router.post("/reply/drafts")
def create_draft(
    body: DraftCreateRequest,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> DraftResponse:
    """Assemble the constrained context + create a DRAFT reply (tasks 13.3/13.4).

    The recipient + thread headers are frozen on the draft; the deterministic
    unsupported-claim validation flags salary / deadline / authorization
    assertions that block approval until corrected.
    """
    service = _draft_service(request)
    try:
        intent = ReplyIntent(body.intent)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Unknown intent: {body.intent}") from e
    try:
        context = service.assemble_context(
            candidate_id=candidate_id,
            message_id=UUID(body.message_id),
            thread_id=UUID(body.thread_id),
            application_id=UUID(body.application_id) if body.application_id else None,
            intent=intent,
            mail_category=body.mail_category,
            application_facts=tuple(
                ApplicationFact(label=f.label, value=f.value, source=f.source)
                for f in body.application_facts
            ),
            evidence_refs=tuple(UUID(e) for e in body.evidence_ids),
            excerpt_override=body.excerpt,
        )
        claims = tuple(
            ReplyDraftClaim(
                claim_text=c.claim_text,
                evidence_ids=tuple(UUID(e) for e in c.evidence_ids),
            )
            for c in body.claims
        )
        draft = service.create_draft(
            candidate_id=candidate_id,
            context=context,
            recipient=body.recipient,
            account_email=body.account_email,
            subject=body.subject,
            body=body.body,
            in_reply_to=body.in_reply_to,
            references_header=body.references_header,
            claims=claims,
            account_id=UUID(body.account_id) if body.account_id else None,
            now=datetime.now(tz=UTC),
        )
    except ReplyDraftError as e:
        raise _translate(e) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    response.headers["cache-control"] = "no-store"
    return _draft_to_response(draft)


@router.post("/reply/drafts/{draft_id}/edit")
def edit_draft(
    draft_id: str,
    body: DraftEditRequest,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> DraftResponse:
    """Apply a body/subject/claims edit as a NEW version (task 13.5).

    The recipient + thread headers are immutable. A body edit produces a fresh
    payload hash; the new version re-enters validation + approval.
    """
    service = _draft_service(request)
    claims = None
    if body.claims is not None:
        claims = tuple(
            ReplyDraftClaim(
                claim_text=c.claim_text,
                evidence_ids=tuple(UUID(e) for e in c.evidence_ids),
            )
            for c in body.claims
        )
    try:
        draft = service.edit_draft(
            draft_id=UUID(draft_id),
            candidate_id=candidate_id,
            subject=body.subject,
            body=body.body,
            claims=claims,
            now=datetime.now(tz=UTC),
        )
    except ReplyDraftError as e:
        raise _translate(e) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    response.headers["cache-control"] = "no-store"
    return _draft_to_response(draft)


@router.post("/reply/drafts/{draft_id}/approve")
def approve_draft(
    draft_id: str,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> DraftResponse:
    """Approve a reply draft after validating preconditions (task 13.5).

    Blocks when the draft carries unsupported claims (13.4). Idempotent: a
    repeat approve returns the recorded version.
    """
    service = _draft_service(request)
    try:
        draft = service.approve(
            draft_id=UUID(draft_id), candidate_id=candidate_id, now=datetime.now(tz=UTC)
        )
    except ReplyDraftError as e:
        raise _translate(e) from e
    response.headers["cache-control"] = "no-store"
    return _draft_to_response(draft)


@router.post("/reply/drafts/{draft_id}/reject")
def reject_draft(
    draft_id: str,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> DraftResponse:
    """Reject a reply draft (task 13.5). Idempotent; no provider write."""
    service = _draft_service(request)
    try:
        draft = service.reject(
            draft_id=UUID(draft_id), candidate_id=candidate_id, now=datetime.now(tz=UTC)
        )
    except ReplyDraftError as e:
        raise _translate(e) from e
    response.headers["cache-control"] = "no-store"
    return _draft_to_response(draft)


@router.post("/reply/drafts/{draft_id}/send")
def send_draft(
    draft_id: str,
    body: DraftSendRequest,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> SendStatusResponse:
    """Send an approved reply through Section 10's delivery chain (task 13.6).

    Auto-send is permanently denied; high-risk categories are permanently
    denied system send (the send refuses before any provider call). Idempotent:
    a repeat send returns the in-flight outcome.
    """
    service = _draft_service(request)
    try:
        outcome = service.send_approved(
            draft_id=UUID(draft_id),
            candidate_id=candidate_id,
            account_email=body.account_email,
            now=datetime.now(tz=UTC),
        )
    except ReplyDraftError as e:
        raise _translate(e) from e
    response.headers["cache-control"] = "no-store"
    return _send_outcome_to_response(UUID(draft_id), outcome)


@router.get("/reply/drafts/{draft_id}/send-status")
def get_send_status(
    draft_id: str,
    request: Request,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> SendStatusResponse:
    """Idempotent read of the reply send phase (task 13.6 / 13.10)."""
    service = _draft_service(request)
    try:
        outcome = service.get_send_status(draft_id=UUID(draft_id), candidate_id=candidate_id)
    except ReplyDraftError as e:
        raise _translate(e) from e
    return _send_outcome_to_response(UUID(draft_id), outcome)


# ---------------------------------------------------------------------------
# Follow-up routes (tasks 13.1 / 13.2 / 13.7)
# ---------------------------------------------------------------------------


@router.get("/reply/follow-up-rules")
def get_follow_up_rules(
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> FollowUpRulesResponse:
    """Return the active follow-up rule version + default waiting periods."""
    from careerops.domain.reply_draft import FOLLOW_UP_RULE_VERSION_S13

    return FollowUpRulesResponse(
        rule_version=FOLLOW_UP_RULE_VERSION_S13,
        default_periods=[
            TriggerPeriodItem(trigger=trigger.value, business_days=days)
            for trigger, days in DEFAULT_FOLLOW_UP_WAITING_PERIODS.items()
        ],
    )


@router.get("/applications/{application_id}/follow-ups")
def list_follow_ups(
    application_id: str,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> FollowUpListResponse:
    """List follow-up reminders for the application (ownership-scoped)."""
    service = _follow_up_service(request)
    try:
        reminders = service.list_for_application(
            application_id=UUID(application_id),
            candidate_id=candidate_id,
        )
    except FollowUpNotOwnedError as e:
        raise _translate(e) from e
    response.headers["cache-control"] = "no-store"
    return FollowUpListResponse(items=[_reminder_to_response(r) for r in reminders])


@router.post("/applications/{application_id}/follow-ups")
def schedule_follow_up(
    application_id: str,
    body: FollowUpScheduleRequest,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> FollowUpResponse:
    """Schedule a trigger-aware follow-up reminder (tasks 13.1 / 13.2).

    Dedups one-active-per-(application, rule). The trigger selects the default
    business-day waiting period (submitted/awaiting_response/interview/
    assessment/user).
    """
    service = _follow_up_service(request)
    now = datetime.now(tz=UTC)
    try:
        reminder = service.schedule_follow_up(
            application_id=UUID(application_id),
            candidate_id=candidate_id,
            trigger=body.trigger,
            business_days=body.business_days,
            rule_version=body.rule_version,
            base_at=body.base_at,
            now=now,
        )
    except DuplicateFollowUpError as e:
        raise _translate(e) from e
    except FollowUpNotOwnedError as e:
        raise _translate(e) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    response.headers["cache-control"] = "no-store"
    return _reminder_to_response(reminder)


@router.post("/follow-ups/{reminder_id}/snooze")
def snooze_follow_up(
    reminder_id: str,
    body: FollowUpSnoozeRequest,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> FollowUpResponse:
    """Snooze an active follow-up reminder (task 13.2)."""
    service = _follow_up_service(request)
    try:
        reminder = service.snooze_follow_up(
            reminder_id=UUID(reminder_id),
            candidate_id=candidate_id,
            snoozed_until=body.snoozed_until,
            now=datetime.now(tz=UTC),
        )
    except FollowUpNotOwnedError as e:
        raise _translate(e) from e
    except Exception as e:
        raise _translate(e) from e
    response.headers["cache-control"] = "no-store"
    return _reminder_to_response(reminder)


@router.post("/follow-ups/{reminder_id}/reschedule")
def reschedule_follow_up(
    reminder_id: str,
    body: FollowUpRescheduleRequest,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> FollowUpResponse:
    """Reschedule an active/snoozed follow-up reminder (task 13.2)."""
    service = _follow_up_service(request)
    try:
        reminder = service.reschedule_follow_up(
            reminder_id=UUID(reminder_id),
            candidate_id=candidate_id,
            new_due_at=body.new_due_at,
            now=datetime.now(tz=UTC),
        )
    except FollowUpNotOwnedError as e:
        raise _translate(e) from e
    except Exception as e:
        raise _translate(e) from e
    response.headers["cache-control"] = "no-store"
    return _reminder_to_response(reminder)


@router.post("/follow-ups/{reminder_id}/cancel")
def cancel_follow_up(
    reminder_id: str,
    body: FollowUpCancelRequest,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> FollowUpResponse:
    """Cancel an active/snoozed follow-up reminder (task 13.2)."""
    service = _follow_up_service(request)
    try:
        reminder = service.cancel_follow_up(
            reminder_id=UUID(reminder_id),
            candidate_id=candidate_id,
            reason=body.reason,
            now=datetime.now(tz=UTC),
        )
    except FollowUpNotOwnedError as e:
        raise _translate(e) from e
    except Exception as e:
        raise _translate(e) from e
    response.headers["cache-control"] = "no-store"
    return _reminder_to_response(reminder)


@router.post("/follow-ups/{reminder_id}/complete")
def complete_follow_up(
    reminder_id: str,
    request: Request,
    response: Response,
    candidate_id: Annotated[UUID, Depends(require_candidate_id)],
) -> FollowUpResponse:
    """Mark an active/snoozed follow-up reminder completed (task 13.2)."""
    service = _follow_up_service(request)
    try:
        reminder = service.complete_follow_up(
            reminder_id=UUID(reminder_id),
            candidate_id=candidate_id,
            now=datetime.now(tz=UTC),
        )
    except FollowUpNotOwnedError as e:
        raise _translate(e) from e
    except Exception as e:
        raise _translate(e) from e
    response.headers["cache-control"] = "no-store"
    return _reminder_to_response(reminder)
