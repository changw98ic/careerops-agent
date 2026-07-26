"""Domain models for M3: applications, state machine, resume versions, packages, follow-ups."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ApplicationState(StrEnum):
    """Application lifecycle states."""

    FAVORITED = "favorited"
    IGNORED = "ignored"
    PREPARING = "preparing"
    SUBMITTED = "submitted"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    ON_HOLD = "on_hold"


class ApplicationEventType(StrEnum):
    """Types of application events (append-only history)."""

    CREATED = "created"
    STATE_CHANGED = "state_changed"
    SUBMITTED_MANUALLY = "submitted_manually"
    NOTE_ADDED = "note_added"
    PACKAGE_ATTACHED = "package_attached"
    FOLLOW_UP_SCHEDULED = "follow_up_scheduled"
    FOLLOW_UP_CANCELLED = "follow_up_cancelled"
    FOLLOW_UP_SNOOZED = "follow_up_snoozed"
    FOLLOW_UP_RESCHEDULED = "follow_up_rescheduled"


class ApplicationEventSource(StrEnum):
    """Source of an application event."""

    USER = "user"
    SYSTEM = "system"
    WORKFLOW = "workflow"


class PackageApprovalState(StrEnum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class FollowUpState(StrEnum):
    ACTIVE = "active"
    SNOOZED = "snoozed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class ResumeParseStatus(StrEnum):
    """Deterministic parse lifecycle of a registered resume version.

    A resume may only enter an application package once parsing succeeded
    (``PARSED``) and the user confirmed the extracted content (``confirmation_status = CONFIRMED``).
    Model tailoring never changes this status directly; it is driven by the
    parse/confirmation services.
    """

    PENDING = "pending"
    PARSED = "parsed"
    FAILED = "failed"


class ConfirmationStatus(StrEnum):
    """User confirmation state for a resume version or an evidence item.

    Shared by ``resume_versions.confirmation_status`` and
    ``evidence_items.confirmation_status``. ``UNCONFIRMED`` is the default for
    newly extracted content; only ``CONFIRMED`` content is eligible for
    approved application packages. ``REJECTED`` content is retained for audit
    but excluded from trusted claims.
    """

    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class SubmissionChannel(StrEnum):
    """How an application submission is delivered to the employer.

    Per application-workspace spec the channel is derived from trusted job/source
    evidence and user choice, never from model output. ``EMAIL`` requires a
    verified recruiting contact (evidence-bound); ``EXTERNAL_FORM`` requires a
    trusted official apply URL; ``MANUAL`` covers user-recorded submissions
    outside the system-managed delivery chain. Model output cannot select or
    invent a channel, and the workspace must show why a channel is eligible or
    unavailable before preparation.
    """

    EMAIL = "email"
    EXTERNAL_FORM = "external_form"
    MANUAL = "manual"


# Legal state transitions: from_state -> set of allowed to_states
LEGAL_TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    ApplicationState.FAVORITED: frozenset(
        {
            ApplicationState.PREPARING,
            ApplicationState.IGNORED,
            ApplicationState.ON_HOLD,
        }
    ),
    ApplicationState.IGNORED: frozenset(
        {
            ApplicationState.FAVORITED,
            ApplicationState.PREPARING,
        }
    ),
    ApplicationState.PREPARING: frozenset(
        {
            ApplicationState.SUBMITTED,
            ApplicationState.ON_HOLD,
            ApplicationState.WITHDRAWN,
            ApplicationState.IGNORED,
        }
    ),
    ApplicationState.SUBMITTED: frozenset(
        {
            ApplicationState.INTERVIEWING,
            ApplicationState.REJECTED,
            ApplicationState.WITHDRAWN,
            ApplicationState.ON_HOLD,
            ApplicationState.OFFER,
        }
    ),
    ApplicationState.INTERVIEWING: frozenset(
        {
            ApplicationState.OFFER,
            ApplicationState.REJECTED,
            ApplicationState.WITHDRAWN,
            ApplicationState.ON_HOLD,
        }
    ),
    ApplicationState.OFFER: frozenset(
        {
            ApplicationState.WITHDRAWN,
        }
    ),
    ApplicationState.REJECTED: frozenset(),
    ApplicationState.WITHDRAWN: frozenset(),
    ApplicationState.ON_HOLD: frozenset(
        {
            ApplicationState.PREPARING,
            ApplicationState.SUBMITTED,
            ApplicationState.INTERVIEWING,
            ApplicationState.WITHDRAWN,
        }
    ),
}

# Canonical contract name for the application state transition table.
# ``LEGAL_TRANSITIONS`` above is the historical name retained for compatibility;
# the two are the same mapping. Enforcement (validate_transition /
# is_transition_legal / IllegalTransitionError) is already provided below and is
# the contract a later gate consumes; illegal transitions MUST be rejected.
ALLOWED_TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = LEGAL_TRANSITIONS

# Terminal states where follow-ups should be cancelled
FOLLOW_UP_CANCEL_STATES: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.REJECTED,
        ApplicationState.WITHDRAWN,
        ApplicationState.OFFER,
    }
)


class IllegalTransitionError(Exception):
    """Raised when an illegal state transition is attempted."""

    def __init__(self, from_state: ApplicationState, to_state: ApplicationState) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"Illegal transition: {from_state.value} -> {to_state.value}")


def validate_transition(from_state: ApplicationState, to_state: ApplicationState) -> None:
    """Validate a state transition, raising IllegalTransitionError if illegal."""
    allowed = LEGAL_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise IllegalTransitionError(from_state, to_state)


def is_transition_legal(from_state: ApplicationState, to_state: ApplicationState) -> bool:
    """Check if a state transition is legal without raising."""
    allowed = LEGAL_TRANSITIONS.get(from_state, frozenset())
    return to_state in allowed


@dataclass(frozen=True, slots=True)
class ApplicationCycle:
    """One application cycle for a (candidate, canonical_job) pair.

    At most one active cycle exists per pair at a time (DB partial unique
    index). An explicit re-application opens a NEW cycle linked to the prior
    one via ``prior_cycle_id`` and closes the old one, preserving full history
    (design Decision 8). ``active`` and ``closed_at`` move together: an active
    cycle has no closed_at; an inactive cycle must be closed.
    """

    id: UUID
    candidate_id: UUID
    canonical_job_id: UUID
    active: bool = True
    prior_cycle_id: UUID | None = None
    reason: str = ""
    created_at: datetime | None = None
    closed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Application:
    """A job application with lifecycle state."""

    id: UUID
    candidate_id: UUID
    canonical_job_id: UUID
    state: ApplicationState = ApplicationState.FAVORITED
    apply_url: str = ""
    # 2.6: link to the application cycle (nullable for backfill; required at
    # the service layer for new rows in a later stage).
    cycle_id: UUID | None = None
    # 2.7: submission channel (Phase 0 SubmissionChannel), approved package
    # version binding, payload hash, and provider receipt linkage. All
    # nullable/additive. payload_hash binds the approved package; any mutation
    # invalidates the prior approval (design Decision 4/5).
    submission_channel: SubmissionChannel | None = None
    package_version_id: UUID | None = None
    payload_hash: str | None = None
    provider_kind: str | None = None
    provider_message_id: str | None = None
    submitted_at: datetime | None = None
    follow_up_due_at: datetime | None = None
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    """An immutable event in the application history (append-only)."""

    id: UUID
    application_id: UUID
    event_type: ApplicationEventType
    from_state: ApplicationState | None = None
    to_state: ApplicationState | None = None
    source: ApplicationEventSource = ApplicationEventSource.USER
    actor_id: str = ""
    note: str = ""
    event_data: dict[str, object] = field(default_factory=lambda: {})
    occurred_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResumeVersion:
    """A registered resume version. No generation or auto-rewrite.

    Only imports, hashes, selects, and versionizes existing resumes.
    """

    id: UUID
    candidate_id: UUID
    version_number: int
    file_reference: str
    content_hash: str
    target_type: str = "general"
    human_confirmed: bool = False
    # 2.4 lifecycle fields (additive; defaults keep existing callers valid).
    # A resume is eligible for an application package only when
    # ``parse_status == PARSED`` and ``confirmation_status == CONFIRMED``.
    parse_status: ResumeParseStatus = ResumeParseStatus.PENDING
    confirmation_status: ConfirmationStatus = ConfirmationStatus.UNCONFIRMED
    source_reference: str = ""
    parsed_at: datetime | None = None
    confirmed_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PackageClaim:
    """A capability claim in an application package, bound to evidence."""

    claim_text: str
    evidence_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if not self.evidence_ids:
            raise ValueError("every positive claim must be traceable to evidence")


@dataclass(frozen=True, slots=True)
class ApplicationPackage:
    """A package of materials for a specific application.

    Contains human-confirmed content only. No auto-generation.
    All positive claims must be traceable to candidate evidence.
    """

    id: UUID
    application_id: UUID
    resume_version_id: UUID
    cover_letter_text: str = ""
    notes: str = ""
    answers: dict[str, str] = field(default_factory=lambda: {})
    claims: tuple[PackageClaim, ...] = ()
    approval_state: PackageApprovalState = PackageApprovalState.DRAFT
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FollowUpReminder:
    """A follow-up reminder for an application.

    Invariant: only one active follow-up per application per rule version.
    """

    id: UUID
    application_id: UUID
    rule_version: str
    state: FollowUpState = FollowUpState.ACTIVE
    due_at: datetime | None = None
    snoozed_until: datetime | None = None
    cancelled_reason: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
