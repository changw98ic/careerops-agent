"""Application service for M4: email sync, classification, drafts, approval, retention.

Safety invariants enforced here:
- Non-recruitment email body is never persisted.
- Duplicate Pub/Sub delivery is handled via provider_message_id uniqueness.
- Drafts are internal only; no Gmail send/compose adapter exists.
- Classification model output is a proposal; it cannot advance Application state directly.
- Approval freezes payload but does not trigger external action in M4.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from careerops.domain.applications import ApplicationState
from careerops.domain.email import (
    DraftStatus,
    EmailCategory,
    EmailMessage,
    EmailMessageState,
    ReplyDraft,
    is_attachment_allowed,
)

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Result of email classification. Model output is a proposal only."""

    category: EmailCategory
    confidence: float
    rules_version: str
    model_version: str = ""


def classify_email(
    *,
    sender_email: str,
    subject: str,
    snippet: str,
    rules_version: str = "m4.classify.v1",
) -> ClassificationResult:
    """Deterministic rule-based classification for recruitment relevance.

    This is the primary classifier; LLM is only a low-priority fallback
    and its output is always a proposal, never a direct state change.
    """
    sender_lower = sender_email.lower()
    subject_lower = subject.lower()
    snippet_lower = snippet.lower()

    recruitment_signals = (
        "interview",
        "application",
        "position",
        "role",
        "hiring",
        "recruiter",
        "job",
        "offer",
        "candidate",
        "resume",
        "screening",
        "assessment",
        "onboarding",
        "compensation",
        "salary",
        "招聘",
        "面试",
        "职位",
        "简历",
        "录用",
        "offer",
    )

    combined = f"{sender_lower} {subject_lower} {snippet_lower}"
    signal_count = sum(1 for signal in recruitment_signals if signal in combined)

    if signal_count >= 2:
        return ClassificationResult(
            category=EmailCategory.RECRUITMENT,
            confidence=min(0.5 + signal_count * 0.1, 1.0),
            rules_version=rules_version,
        )
    if signal_count == 1:
        return ClassificationResult(
            category=EmailCategory.UNKNOWN,
            confidence=0.4,
            rules_version=rules_version,
        )
    return ClassificationResult(
        category=EmailCategory.NON_RECRUITMENT,
        confidence=0.8,
        rules_version=rules_version,
    )


# ---------------------------------------------------------------------------
# Message ingestion with privacy filtering
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestedMessage:
    """A message after ingestion with privacy filtering applied."""

    message: EmailMessage
    body_stored: bool
    body_redacted: bool


def ingest_message(
    *,
    account_id: UUID,
    thread_id: UUID,
    provider_message_id: str,
    sender_email: str,
    sender_name: str,
    subject: str,
    snippet: str,
    body_text: str,
    received_at: datetime,
    existing_provider_ids: frozenset[str],
    now: datetime | None = None,
) -> IngestedMessage | None:
    """Ingest a message with deduplication and privacy filtering.

    Returns None if the message is a duplicate delivery.
    Non-recruitment messages: body is NOT persisted (body_text discarded).
    Recruitment/unknown messages: body may be persisted for extraction.
    """
    current = now or datetime.now(UTC)

    # Duplicate delivery check
    if provider_message_id in existing_provider_ids:
        return None

    classification = classify_email(
        sender_email=sender_email,
        subject=subject,
        snippet=snippet,
    )

    is_recruitment = classification.category is EmailCategory.RECRUITMENT
    body_persisted = is_recruitment

    message = EmailMessage(
        id=uuid4(),
        thread_id=thread_id,
        account_id=account_id,
        provider_message_id=provider_message_id,
        category=classification.category,
        state=EmailMessageState.CLASSIFIED,
        sender_email=sender_email,
        sender_name=sender_name,
        subject=subject,
        received_at=received_at,
        snippet=snippet if is_recruitment else "",
        body_persisted=body_persisted,
        created_at=current,
    )

    return IngestedMessage(
        message=message,
        body_stored=body_persisted,
        body_redacted=not body_persisted,
    )


# ---------------------------------------------------------------------------
# Draft creation (internal only)
# ---------------------------------------------------------------------------


def compute_payload_hash(
    *,
    to_address: str,
    subject: str,
    body_text: str,
    references: str,
    in_reply_to: str,
) -> str:
    """Compute a stable hash of the draft payload for approval binding."""
    content = f"{to_address}\x00{subject}\x00{body_text}\x00{references}\x00{in_reply_to}"
    return hashlib.sha256(content.encode()).hexdigest()


def create_reply_draft(
    *,
    message_id: UUID,
    thread_id: UUID,
    account_id: UUID,
    to_address: str,
    subject: str,
    body_text: str,
    references_header: str = "",
    in_reply_to_header: str = "",
    application_id: UUID | None = None,
    now: datetime | None = None,
) -> ReplyDraft:
    """Create an internal reply draft. Does NOT call any Gmail API.

    The draft is stored in CareerOps DB only. Approval freezes the payload
    but does not trigger any external send action in M4.
    """
    current = now or datetime.now(UTC)
    payload_hash = compute_payload_hash(
        to_address=to_address,
        subject=subject,
        body_text=body_text,
        references=references_header,
        in_reply_to=in_reply_to_header,
    )

    return ReplyDraft(
        id=uuid4(),
        message_id=message_id,
        thread_id=thread_id,
        account_id=account_id,
        application_id=application_id,
        status=DraftStatus.DRAFT,
        to_address=to_address,
        subject=subject,
        body_text=body_text,
        references_header=references_header,
        in_reply_to_header=in_reply_to_header,
        payload_hash=payload_hash,
        created_at=current,
        updated_at=current,
    )


# ---------------------------------------------------------------------------
# Draft approval (freezes payload, no external action)
# ---------------------------------------------------------------------------


def approve_draft(
    draft: ReplyDraft,
    *,
    now: datetime | None = None,
) -> ReplyDraft:
    """Approve a draft, freezing its payload. Does NOT send.

    In M4, approval only means the payload is frozen and recorded.
    No Gmail send/compose adapter exists.
    """
    current = now or datetime.now(UTC)
    if draft.status is not DraftStatus.PENDING_APPROVAL:
        raise DraftApprovalError(
            f"Cannot approve draft in state {draft.status.value}; must be pending_approval"
        )
    return ReplyDraft(
        id=draft.id,
        message_id=draft.message_id,
        thread_id=draft.thread_id,
        account_id=draft.account_id,
        application_id=draft.application_id,
        status=DraftStatus.APPROVED,
        to_address=draft.to_address,
        subject=draft.subject,
        body_text=draft.body_text,
        references_header=draft.references_header,
        in_reply_to_header=draft.in_reply_to_header,
        payload_hash=draft.payload_hash,
        approved_at=current,
        expires_at=draft.expires_at,
        created_at=draft.created_at,
        updated_at=current,
    )


def submit_draft_for_approval(
    draft: ReplyDraft,
    *,
    now: datetime | None = None,
) -> ReplyDraft:
    """Submit a draft for approval review."""
    current = now or datetime.now(UTC)
    if draft.status is not DraftStatus.DRAFT:
        raise DraftApprovalError(
            f"Cannot submit draft in state {draft.status.value}; must be draft"
        )
    return ReplyDraft(
        id=draft.id,
        message_id=draft.message_id,
        thread_id=draft.thread_id,
        account_id=draft.account_id,
        application_id=draft.application_id,
        status=DraftStatus.PENDING_APPROVAL,
        to_address=draft.to_address,
        subject=draft.subject,
        body_text=draft.body_text,
        references_header=draft.references_header,
        in_reply_to_header=draft.in_reply_to_header,
        payload_hash=draft.payload_hash,
        approved_at=None,
        expires_at=draft.expires_at,
        created_at=draft.created_at,
        updated_at=current,
    )


class DraftApprovalError(Exception):
    """Raised when a draft approval operation is invalid."""


# ---------------------------------------------------------------------------
# Attachment quarantine
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuarantineDecision:
    """Result of attachment quarantine validation."""

    allowed: bool
    deny_reason: str = ""


def validate_attachment(
    *,
    declared_mime: str,
    detected_mime: str,
    byte_size: int,
    max_size_bytes: int = 10 * 1024 * 1024,
) -> QuarantineDecision:
    """Validate an attachment against the M4 allowlist.

    Only text/calendar, text/plain, and PDF are allowed.
    Declared and detected MIME must match and be in the allowlist.
    """
    if byte_size > max_size_bytes:
        return QuarantineDecision(allowed=False, deny_reason="SIZE_LIMIT_EXCEEDED")
    if byte_size == 0:
        return QuarantineDecision(allowed=False, deny_reason="EMPTY_FILE")
    if not is_attachment_allowed(declared_mime, detected_mime):
        if declared_mime != detected_mime:
            return QuarantineDecision(
                allowed=False,
                deny_reason=f"MIME_MISMATCH: declared={declared_mime} detected={detected_mime}",
            )
        return QuarantineDecision(
            allowed=False,
            deny_reason=f"TYPE_NOT_ALLOWED: {declared_mime}",
        )
    return QuarantineDecision(allowed=True)


# ---------------------------------------------------------------------------
# Email retention / redaction
# ---------------------------------------------------------------------------


def should_persist_body(category: EmailCategory) -> bool:
    """Non-recruitment email body must never be persisted."""
    return category is EmailCategory.RECRUITMENT


def redact_for_logging(
    *,
    sender_email: str,
    subject: str,
    body: str,
    category: EmailCategory,
) -> dict[str, str]:
    """Produce a safe log entry that never contains non-recruitment body content."""
    result: dict[str, str] = {
        "sender_domain": sender_email.split("@")[-1] if "@" in sender_email else "unknown",
        "category": category.value,
    }
    if category is EmailCategory.RECRUITMENT:
        result["subject"] = subject[:100]
    return result


# ---------------------------------------------------------------------------
# M4.14: FollowUpWorkflow integration with Gmail/Application
# ---------------------------------------------------------------------------


class FollowUpEmailDecision(StrEnum):
    """Outcome of processing a due follow-up reminder.

    Invariant: no decision here ever sends email. The only productive outcome
    is an internal draft placed in the approval queue.
    """

    DRAFT_CREATED = "draft_created"
    SKIPPED_TERMINAL_STATE = "skipped_terminal_state"
    SKIPPED_JOB_INACTIVE = "skipped_job_inactive"
    SKIPPED_NEW_INBOUND = "skipped_new_inbound"


# Application states in which a follow-up is never appropriate.
TERMINAL_FOLLOW_UP_STATES: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.REJECTED,
        ApplicationState.WITHDRAWN,
        ApplicationState.OFFER,
        ApplicationState.IGNORED,
    }
)


@dataclass(frozen=True, slots=True)
class FollowUpEmailContext:
    """Inputs required to process a due follow-up reminder.

    All fields are trusted business state supplied by the workflow activity;
    none of it is taken from model output or untrusted email content.
    """

    reminder_id: UUID
    application_id: UUID
    account_id: UUID
    thread_id: UUID
    application_state: ApplicationState
    job_active: bool
    new_inbound_since_last_outbound: bool
    to_address: str
    subject: str
    proposed_body: str
    references_header: str = ""
    in_reply_to_header: str = ""


@dataclass(frozen=True, slots=True)
class FollowUpEmailOutcome:
    """Result of processing a due follow-up.

    ``draft`` is populated only for DRAFT_CREATED and is already in
    ``pending_approval`` state, i.e. sitting in the approval queue. It is never
    sent. ``reason`` explains why a follow-up was skipped otherwise.
    """

    decision: FollowUpEmailDecision
    draft: ReplyDraft | None = None
    reason: str = ""


def process_due_follow_up(
    context: FollowUpEmailContext,
    *,
    now: datetime | None = None,
) -> FollowUpEmailOutcome:
    """Process a due follow-up reminder by optionally creating an internal draft.

    Order of checks (fail-closed, most restrictive first):
    1. Terminal application state -> skip (no draft).
    2. Job no longer active -> skip (no draft).
    3. A new inbound recruitment reply arrived since our last outbound -> skip
       (the recruiter already responded; following up would be wrong).
    4. Otherwise create an internal reply draft and submit it for approval.

    This function NEVER sends email and NEVER calls a Gmail send/compose adapter.
    The draft is stored in CareerOps DB only and enters the approval queue.
    """
    if context.application_state in TERMINAL_FOLLOW_UP_STATES:
        return FollowUpEmailOutcome(
            decision=FollowUpEmailDecision.SKIPPED_TERMINAL_STATE,
            reason=f"application_state={context.application_state.value}",
        )

    if not context.job_active:
        return FollowUpEmailOutcome(
            decision=FollowUpEmailDecision.SKIPPED_JOB_INACTIVE,
            reason="job_not_active",
        )

    if context.new_inbound_since_last_outbound:
        return FollowUpEmailOutcome(
            decision=FollowUpEmailDecision.SKIPPED_NEW_INBOUND,
            reason="new_inbound_reply_received",
        )

    draft = create_reply_draft(
        message_id=uuid4(),
        thread_id=context.thread_id,
        account_id=context.account_id,
        to_address=context.to_address,
        subject=context.subject,
        body_text=context.proposed_body,
        references_header=context.references_header,
        in_reply_to_header=context.in_reply_to_header,
        application_id=context.application_id,
        now=now,
    )
    pending = submit_draft_for_approval(draft, now=now)
    return FollowUpEmailOutcome(
        decision=FollowUpEmailDecision.DRAFT_CREATED,
        draft=pending,
    )
