"""Domain models for M5B: Calendar scheduling and interview management.

Safety invariants:
- Calendar events are ONLY created after recruiter confirmation AND user action.
- No unattended event creation: every write requires user action + Policy + Approval
  + Intent + Receipt + Audit through the M5A authorization chain.
- FreeBusy is re-queried at execution time; stale results (>10s) fail closed.
- Race conditions (conflict between last FreeBusy and insert) trigger conflict review
  and block confirmation emails.
- No duplicate events: stable reconciliation key + DB unique constraint + provider lookup.
- Events are created only in the dedicated CareerOps Interviews calendar, no external
  attendees, no update notifications.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
FREEBUSY_MAX_AGE_SECONDS = 10
MAX_CANDIDATE_SLOTS = 3


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ProposalStatus(StrEnum):
    DRAFT = "draft"
    PENDING_RECRUITER = "pending_recruiter"
    RECRUITER_CONFIRMED = "recruiter_confirmed"
    PENDING_USER_ACTION = "pending_user_action"
    EXECUTING = "executing"
    CONFIRMED = "confirmed"
    CONFLICT_REVIEW = "conflict_review"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CalendarEventStatus(StrEnum):
    PENDING = "pending"
    CREATED = "created"
    CONFLICT_DETECTED = "conflict_detected"
    CANCELLED = "cancelled"


class InterviewStatus(StrEnum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    RESCHEDULED = "rescheduled"


class SlotRejectionReason(StrEnum):
    BUFFER_VIOLATION = "buffer_violation"
    NOTICE_VIOLATION = "notice_violation"
    DAILY_LIMIT_EXCEEDED = "daily_limit_exceeded"
    WEEKLY_LIMIT_EXCEEDED = "weekly_limit_exceeded"
    OUTSIDE_AVAILABLE_WINDOW = "outside_available_window"
    TIMEZONE_AMBIGUOUS = "timezone_ambiguous"
    CONFLICT_WITH_EXISTING = "conflict_with_existing"


# ---------------------------------------------------------------------------
# Timezone and slot models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimeSlot:
    """A time slot with explicit IANA timezone.

    Ambiguous timezone abbreviations (e.g. CST) are rejected at parse time.
    All times are stored as UTC with the source timezone recorded for display.
    """

    start_utc: datetime
    end_utc: datetime
    source_timezone: str  # IANA timezone name, e.g. "America/New_York"
    duration_minutes: int

    def __post_init__(self) -> None:
        if self.start_utc >= self.end_utc:
            raise ValueError("slot start must be before end")
        if self.duration_minutes <= 0:
            raise ValueError("duration must be positive")
        expected_duration = self.end_utc - self.start_utc
        if expected_duration != timedelta(minutes=self.duration_minutes):
            raise ValueError("duration_minutes does not match start/end difference")
        # Validate IANA timezone
        try:
            ZoneInfo(self.source_timezone)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"invalid IANA timezone: {self.source_timezone}") from exc

    @property
    def start_local(self) -> datetime:
        return self.start_utc.astimezone(ZoneInfo(self.source_timezone))

    @property
    def start_display(self) -> datetime:
        """Start time in the display timezone (Asia/Shanghai)."""
        return self.start_utc.astimezone(DISPLAY_TIMEZONE)

    @property
    def end_display(self) -> datetime:
        """End time in the display timezone (Asia/Shanghai)."""
        return self.end_utc.astimezone(DISPLAY_TIMEZONE)


@dataclass(frozen=True, slots=True)
class SchedulingRules:
    """Rules governing slot generation and validation."""

    buffer_minutes: int = 15
    minimum_notice_minutes: int = 60
    max_events_per_day: int = 3
    max_events_per_week: int = 10
    available_windows: tuple[tuple[int, int], ...] = ((9 * 60, 17 * 60),)  # minutes from midnight
    slot_duration_minutes: int = 60
    timezone: str = "Asia/Shanghai"
    rules_version: str = "m5b.scheduling.v1"

    def __post_init__(self) -> None:
        if self.buffer_minutes < 0:
            raise ValueError("buffer_minutes must be non-negative")
        if self.minimum_notice_minutes < 0:
            raise ValueError("minimum_notice_minutes must be non-negative")
        if self.max_events_per_day < 1:
            raise ValueError("max_events_per_day must be at least 1")
        if self.max_events_per_week < 1:
            raise ValueError("max_events_per_week must be at least 1")
        if self.slot_duration_minutes < 15:
            raise ValueError("slot_duration_minutes must be at least 15")


@dataclass(frozen=True, slots=True)
class SlotCandidate:
    """A candidate slot with validation status."""

    slot: TimeSlot
    is_valid: bool
    rejection_reasons: tuple[SlotRejectionReason, ...] = ()


@dataclass(frozen=True, slots=True)
class FreeBusyResult:
    """Result of a FreeBusy query against configured calendars."""

    calendar_ids: tuple[str, ...]
    busy_periods: tuple[tuple[datetime, datetime], ...]  # UTC start/end pairs
    queried_at: datetime
    window_start: datetime
    window_end: datetime

    def is_stale(self, *, now: datetime) -> bool:
        """FreeBusy results older than FREEBUSY_MAX_AGE_SECONDS are stale."""
        return (now - self.queried_at).total_seconds() > FREEBUSY_MAX_AGE_SECONDS

    def is_busy(self, slot: TimeSlot) -> bool:
        """Check if a slot overlaps with any busy period."""
        for busy_start, busy_end in self.busy_periods:
            if slot.start_utc < busy_end and slot.end_utc > busy_start:
                return True
        return False


# ---------------------------------------------------------------------------
# Schedule proposal
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduleProposal:
    """A scheduling proposal binding slots, rules, and the M5A authorization chain.

    A proposal is created when the user wants to schedule an interview. It contains
    candidate slots and must go through:
    1. Recruiter confirmation (external, tracked via email/thread)
    2. User action ("re-verify and create")
    3. M5A authorization chain (Policy -> Approval -> Intent -> Receipt -> Audit)
    """

    id: UUID
    application_id: UUID
    canonical_job_id: UUID
    candidate_slots: tuple[TimeSlot, ...]
    selected_slot: TimeSlot | None
    status: ProposalStatus
    rules_version: str
    scheduling_rules: SchedulingRules
    calendar_ids_checked: tuple[str, ...]
    recruiter_confirmed_at: datetime | None
    user_action_at: datetime | None
    action_intent_id: UUID | None  # Links to M5A chain
    payload_hash: str | None  # Bound to M5A payload version
    freebusy_queried_at: datetime | None
    expires_at: datetime
    conflict_reason: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if len(self.candidate_slots) > MAX_CANDIDATE_SLOTS:
            raise ValueError(f"at most {MAX_CANDIDATE_SLOTS} candidate slots allowed")

    @property
    def is_recruiter_confirmed(self) -> bool:
        return self.recruiter_confirmed_at is not None

    @property
    def is_user_actioned(self) -> bool:
        return self.user_action_at is not None

    @property
    def can_create_event(self) -> bool:
        """Event creation requires recruiter confirmation + user action + selected slot."""
        return (
            self.is_recruiter_confirmed
            and self.is_user_actioned
            and self.selected_slot is not None
            and self.status in (ProposalStatus.PENDING_USER_ACTION, ProposalStatus.EXECUTING)
        )


# ---------------------------------------------------------------------------
# Calendar event
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """A calendar event created in the dedicated CareerOps Interviews calendar.

    Invariants:
    - No external attendees (MVP).
    - No update notifications sent.
    - Created only in the dedicated calendar.
    - Reconciliation key prevents duplicates.
    """

    id: UUID
    schedule_proposal_id: UUID
    action_intent_id: UUID
    provider_event_id: str | None
    calendar_id: str
    reconciliation_key: str
    title: str
    slot: TimeSlot
    meeting_link: str
    status: CalendarEventStatus
    conflict_detected_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if len(self.reconciliation_key) < 1:
            raise ValueError("reconciliation_key must not be empty")


# ---------------------------------------------------------------------------
# Interview record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InterviewRecord:
    """Deterministic interview record with company/role/evidence summary.

    No generative Prep Pack in MVP; only deterministic factual summary.
    """

    id: UUID
    application_id: UUID
    canonical_job_id: UUID
    calendar_event_id: UUID | None
    schedule_proposal_id: UUID
    status: InterviewStatus
    interviewer_name: str
    interviewer_email: str
    interview_type: str  # e.g. "phone", "video", "onsite"
    meeting_link: str
    notes: str
    evidence_summary: dict[str, object] = field(default_factory=lambda: {})
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=ZoneInfo("UTC")))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=ZoneInfo("UTC")))


# ---------------------------------------------------------------------------
# Conflict review
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConflictReview:
    """A conflict detected between FreeBusy and event creation.

    When a race condition is detected (external write between last FreeBusy and insert),
    the system enters conflict review, blocks confirmation emails, and requires
    human resolution.
    """

    id: UUID
    schedule_proposal_id: UUID
    calendar_event_id: UUID | None
    conflict_type: str  # "freebusy_race", "duplicate_event", "buffer_violation"
    description: str
    detected_at: datetime
    resolved_at: datetime | None
    resolution: str | None  # "user_resolved", "auto_cancelled", "escalated"
    confirmation_email_blocked: bool = True

    def __post_init__(self) -> None:
        # Safety: confirmation email is ALWAYS blocked on conflict
        if not self.confirmation_email_blocked:
            raise ValueError("confirmation_email_blocked must be True for conflict reviews")
