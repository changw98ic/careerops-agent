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
class Application:
    """A job application with lifecycle state."""

    id: UUID
    candidate_id: UUID
    canonical_job_id: UUID
    state: ApplicationState = ApplicationState.FAVORITED
    apply_url: str = ""
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
