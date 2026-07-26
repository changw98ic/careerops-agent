"""Domain models for M4: Gmail read-only sync, classification, internal drafts, approval.

Safety invariants:
- Only gmail.readonly scope is ever requested or stored.
- No plaintext tokens in DB or logs.
- Non-recruitment email body is never persisted.
- Duplicate Pub/Sub delivery is handled via provider message ID uniqueness.
- No gmail.send/gmail.compose scope or adapter exists anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

# ---------------------------------------------------------------------------
# OAuth scope enforcement
# ---------------------------------------------------------------------------

ALLOWED_GMAIL_SCOPES: frozenset[str] = frozenset({"https://www.googleapis.com/auth/gmail.readonly"})

FORBIDDEN_SCOPES: frozenset[str] = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://mail.google.com/",
    }
)


def validate_scopes(scopes: frozenset[str]) -> None:
    """Reject any scope that is not gmail.readonly."""
    forbidden_found = scopes & FORBIDDEN_SCOPES
    if forbidden_found:
        raise ScopeViolationError(f"Forbidden scopes requested: {sorted(forbidden_found)}")
    extra = scopes - ALLOWED_GMAIL_SCOPES
    if extra:
        raise ScopeViolationError(f"Unapproved scopes requested: {sorted(extra)}")


class ScopeViolationError(Exception):
    """Raised when a forbidden or unapproved OAuth scope is requested."""


# ---------------------------------------------------------------------------
# Email account
# ---------------------------------------------------------------------------


class EmailAccountStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class EmailAccount:
    """A dedicated job-search Gmail account connected via OAuth.

    Invariant: only gmail.readonly scope is granted. The credential_reference
    points to an envelope-encrypted token; no plaintext token is stored here.
    """

    id: UUID
    email_address: str
    credential_reference_id: UUID
    status: EmailAccountStatus = EmailAccountStatus.ACTIVE
    history_id: str = ""
    watch_expiration: datetime | None = None
    last_sync_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Email threads and messages
# ---------------------------------------------------------------------------


class EmailCategory(StrEnum):
    """Classification categories for recruitment emails."""

    RECRUITMENT = "recruitment"
    NON_RECRUITMENT = "non_recruitment"
    UNKNOWN = "unknown"


class EmailMessageState(StrEnum):
    SYNCED = "synced"
    CLASSIFIED = "classified"
    EXTRACTED = "extracted"
    DRAFTED = "drafted"
    ARCHIVED = "archived"


class EmailEventProposalState(StrEnum):
    """Lifecycle state of a mail-derived application event proposal.

    Per recruiting-email-intelligence spec: a proposal is review-only model/
    rules output and MUST NOT directly advance ``ApplicationState``. Only user
    acceptance appends an application event through the legal transition table.
    ``STALE`` covers a proposal that can no longer be safely applied because it
    was superseded by a newer message in the thread, the application reached a
    terminal state, or the thread link became unresolved; a stale proposal must
    not be applied after the fact. Repeat decisions on the same proposal return
    the recorded result without a second state transition.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class EmailThread:
    """A Gmail thread linked to an email account."""

    id: UUID
    account_id: UUID
    provider_thread_id: str
    subject: str = ""
    application_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """A Gmail message within a thread.

    Invariant: non-recruitment messages do NOT persist body content.
    Only metadata (headers, sender, subject) is stored for non-recruitment.
    """

    id: UUID
    thread_id: UUID
    account_id: UUID
    provider_message_id: str
    category: EmailCategory = EmailCategory.UNKNOWN
    state: EmailMessageState = EmailMessageState.SYNCED
    sender_email: str = ""
    sender_name: str = ""
    subject: str = ""
    received_at: datetime | None = None
    snippet: str = ""
    body_persisted: bool = False
    application_id: UUID | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Email extractions (structured fields from recruitment emails)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmailExtraction:
    """Structured data extracted from a recruitment email.

    Only created for emails classified as recruitment.
    Model output is a proposal; it cannot directly advance Application state.
    """

    id: UUID
    message_id: UUID
    extraction_type: str = ""
    extracted_data: dict[str, object] = field(default_factory=lambda: {})
    confidence: float = 0.0
    model_version: str = ""
    rules_version: str = ""
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Reply drafts (internal only - never sent via Gmail API in M4)
# ---------------------------------------------------------------------------


class DraftStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ReplyDraft:
    """An internal reply draft stored in CareerOps DB only.

    Invariant: M4 does NOT call Gmail Draft API or Send API.
    Approval freezes the payload but does not trigger any external action.
    """

    id: UUID
    message_id: UUID
    thread_id: UUID
    account_id: UUID
    application_id: UUID | None = None
    status: DraftStatus = DraftStatus.DRAFT
    to_address: str = ""
    subject: str = ""
    body_text: str = ""
    references_header: str = ""
    in_reply_to_header: str = ""
    payload_hash: str = ""
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Attachment quarantine
# ---------------------------------------------------------------------------


class QuarantineStatus(StrEnum):
    QUARANTINED = "quarantined"
    CLEARED = "cleared"
    DENIED = "denied"
    EXPIRED = "expired"


ALLOWED_ATTACHMENT_TYPES: frozenset[str] = frozenset(
    {
        "text/calendar",
        "text/plain",
        "application/pdf",
    }
)


@dataclass(frozen=True, slots=True)
class AttachmentQuarantine:
    """A quarantined attachment pending validation.

    Invariant: declared MIME and magic bytes must both match.
    Only text/calendar, text/plain, and PDF metadata/text are allowed.
    Unknown/macro/executable/archive/password-protected content is denied.
    """

    id: UUID
    message_id: UUID
    filename: str = ""
    declared_mime: str = ""
    detected_mime: str = ""
    byte_size: int = 0
    status: QuarantineStatus = QuarantineStatus.QUARANTINED
    deny_reason: str = ""
    content_hash: str = ""
    created_at: datetime | None = None
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.byte_size < 0:
            raise ValueError("byte_size must be non-negative")


def is_attachment_allowed(declared_mime: str, detected_mime: str) -> bool:
    """Both declared and detected MIME must be in the allowlist and match."""
    if declared_mime not in ALLOWED_ATTACHMENT_TYPES:
        return False
    if detected_mime not in ALLOWED_ATTACHMENT_TYPES:
        return False
    return declared_mime == detected_mime


# ---------------------------------------------------------------------------
# Pub/Sub delivery deduplication
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PubSubReceipt:
    """A persisted Pub/Sub notification receipt for idempotency.

    Duplicate deliveries are detected by ack_id + message_id uniqueness.
    """

    id: UUID
    account_id: UUID
    pubsub_message_id: str
    ack_id: str
    history_id: str = ""
    processed: bool = False
    received_at: datetime | None = None
    processed_at: datetime | None = None
