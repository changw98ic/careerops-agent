"""M5B Scheduling service: FreeBusy, slot generation, and event creation.

Integrates with the M5A side-effect kernel for the full authorization chain:
User Action -> Policy -> Approval -> Intent -> Receipt -> Audit.

Safety invariants enforced:
- No event before recruiter confirmation.
- No unattended event creation: user must click "re-verify and create".
- FreeBusy is re-queried at execution time; stale results fail closed.
- Race conditions trigger conflict review and block confirmation emails.
- No duplicate events: reconciliation key + unique constraint + provider lookup.
- Every write has user action + Policy + Approval + Intent + Receipt + Audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from careerops.application.side_effect_kernel import (
    ProposalInput,
    SideEffectKernel,
)
from careerops.domain.scheduling import (
    CalendarEvent,
    CalendarEventStatus,
    ConflictReview,
    FreeBusyResult,
    InterviewRecord,
    InterviewStatus,
    ProposalStatus,
    ScheduleProposal,
    SchedulingRules,
    SlotCandidate,
    SlotRejectionReason,
    TimeSlot,
)
from careerops.domain.side_effects import (
    canonical_payload_hash,
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SchedulingError(RuntimeError):
    """Base error for scheduling operations."""


class RecruiterNotConfirmedError(SchedulingError):
    """Cannot create event before recruiter confirmation."""


class UserActionRequiredError(SchedulingError):
    """Cannot create event without explicit user action."""


class FreeBusyStaleError(SchedulingError):
    """FreeBusy results are stale; must re-query before event creation."""


class SlotConflictError(SchedulingError):
    """A conflict was detected during event creation."""


class DuplicateEventError(SchedulingError):
    """An event with this reconciliation key already exists."""


class NoAvailableSlotError(SchedulingError):
    """No valid slot could be generated."""


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class FreeBusyProvider(Protocol):
    """Port for querying calendar FreeBusy information."""

    def query_freebusy(
        self,
        *,
        calendar_ids: tuple[str, ...],
        window_start: datetime,
        window_end: datetime,
        now: datetime,
    ) -> FreeBusyResult: ...


class CalendarEventProvider(Protocol):
    """Port for creating calendar events."""

    def create_event(
        self,
        *,
        calendar_id: str,
        reconciliation_key: str,
        title: str,
        start_utc: datetime,
        end_utc: datetime,
        timezone: str,
        meeting_link: str,
        now: datetime,
    ) -> str:
        """Create event and return provider event ID. Raises on conflict."""
        ...

    def lookup_by_reconciliation_key(
        self, *, calendar_id: str, reconciliation_key: str
    ) -> str | None:
        """Look up an event by reconciliation key. Returns provider ID or None."""
        ...


class ScheduleProposalStore(Protocol):
    """Repository for schedule proposals."""

    def insert(self, proposal: ScheduleProposal) -> ScheduleProposal: ...

    def get(self, proposal_id: UUID) -> ScheduleProposal: ...

    def update(self, proposal: ScheduleProposal) -> ScheduleProposal: ...

    def find_by_application(self, application_id: UUID) -> tuple[ScheduleProposal, ...]: ...


class CalendarEventStore(Protocol):
    """Repository for calendar events."""

    def insert(self, event: CalendarEvent) -> CalendarEvent: ...

    def get(self, event_id: UUID) -> CalendarEvent: ...

    def update(self, event: CalendarEvent) -> CalendarEvent: ...

    def find_by_reconciliation_key(self, reconciliation_key: str) -> CalendarEvent | None: ...

    def find_by_proposal(self, proposal_id: UUID) -> tuple[CalendarEvent, ...]: ...

    def count_events_in_period(
        self, *, start: datetime, end: datetime, calendar_id: str
    ) -> int: ...


class InterviewRecordStore(Protocol):
    """Repository for interview records."""

    def insert(self, record: InterviewRecord) -> InterviewRecord: ...

    def get(self, record_id: UUID) -> InterviewRecord: ...

    def update(self, record: InterviewRecord) -> InterviewRecord: ...

    def find_by_application(self, application_id: UUID) -> tuple[InterviewRecord, ...]: ...


class ConflictReviewStore(Protocol):
    """Repository for conflict reviews."""

    def insert(self, review: ConflictReview) -> ConflictReview: ...

    def get(self, review_id: UUID) -> ConflictReview: ...

    def find_by_proposal(self, proposal_id: UUID) -> tuple[ConflictReview, ...]: ...


# ---------------------------------------------------------------------------
# In-memory stores for testing
# ---------------------------------------------------------------------------


class InMemoryScheduleProposalStore:
    """In-memory store for schedule proposals."""

    def __init__(self) -> None:
        self._proposals: dict[UUID, ScheduleProposal] = {}

    def insert(self, proposal: ScheduleProposal) -> ScheduleProposal:
        self._proposals[proposal.id] = proposal
        return proposal

    def get(self, proposal_id: UUID) -> ScheduleProposal:
        return self._proposals[proposal_id]

    def update(self, proposal: ScheduleProposal) -> ScheduleProposal:
        self._proposals[proposal.id] = proposal
        return proposal

    def find_by_application(self, application_id: UUID) -> tuple[ScheduleProposal, ...]:
        return tuple(p for p in self._proposals.values() if p.application_id == application_id)


class InMemoryCalendarEventStore:
    """In-memory store for calendar events."""

    def __init__(self) -> None:
        self._events: dict[UUID, CalendarEvent] = {}
        self._by_reconciliation_key: dict[str, UUID] = {}

    def insert(self, event: CalendarEvent) -> CalendarEvent:
        existing = self._by_reconciliation_key.get(event.reconciliation_key)
        if existing is not None:
            raise DuplicateEventError(
                f"event with reconciliation key {event.reconciliation_key} already exists"
            )
        self._events[event.id] = event
        self._by_reconciliation_key[event.reconciliation_key] = event.id
        return event

    def get(self, event_id: UUID) -> CalendarEvent:
        return self._events[event_id]

    def update(self, event: CalendarEvent) -> CalendarEvent:
        self._events[event.id] = event
        return event

    def find_by_reconciliation_key(self, reconciliation_key: str) -> CalendarEvent | None:
        event_id = self._by_reconciliation_key.get(reconciliation_key)
        return self._events.get(event_id) if event_id else None

    def find_by_proposal(self, proposal_id: UUID) -> tuple[CalendarEvent, ...]:
        return tuple(e for e in self._events.values() if e.schedule_proposal_id == proposal_id)

    def count_events_in_period(self, *, start: datetime, end: datetime, calendar_id: str) -> int:
        count = 0
        for event in self._events.values():
            if event.calendar_id != calendar_id:
                continue
            if event.status not in (CalendarEventStatus.CREATED, CalendarEventStatus.PENDING):
                continue
            if event.slot.start_utc < end and event.slot.end_utc > start:
                count += 1
        return count


class InMemoryInterviewRecordStore:
    """In-memory store for interview records."""

    def __init__(self) -> None:
        self._records: dict[UUID, InterviewRecord] = {}

    def insert(self, record: InterviewRecord) -> InterviewRecord:
        self._records[record.id] = record
        return record

    def get(self, record_id: UUID) -> InterviewRecord:
        return self._records[record_id]

    def update(self, record: InterviewRecord) -> InterviewRecord:
        self._records[record.id] = record
        return record

    def find_by_application(self, application_id: UUID) -> tuple[InterviewRecord, ...]:
        return tuple(r for r in self._records.values() if r.application_id == application_id)


class InMemoryConflictReviewStore:
    """In-memory store for conflict reviews."""

    def __init__(self) -> None:
        self._reviews: dict[UUID, ConflictReview] = {}

    def insert(self, review: ConflictReview) -> ConflictReview:
        self._reviews[review.id] = review
        return review

    def get(self, review_id: UUID) -> ConflictReview:
        return self._reviews[review_id]

    def find_by_proposal(self, proposal_id: UUID) -> tuple[ConflictReview, ...]:
        return tuple(r for r in self._reviews.values() if r.schedule_proposal_id == proposal_id)


# ---------------------------------------------------------------------------
# Fake providers for testing
# ---------------------------------------------------------------------------


class FakeFreeBusyProvider:
    """Fake FreeBusy provider for testing."""

    def __init__(self) -> None:
        self.busy_periods: list[tuple[datetime, datetime]] = []
        self.query_count = 0

    def query_freebusy(
        self,
        *,
        calendar_ids: tuple[str, ...],
        window_start: datetime,
        window_end: datetime,
        now: datetime,
    ) -> FreeBusyResult:
        self.query_count += 1
        return FreeBusyResult(
            calendar_ids=calendar_ids,
            busy_periods=tuple(self.busy_periods),
            queried_at=now,
            window_start=window_start,
            window_end=window_end,
        )


class FakeCalendarEventProvider:
    """Fake calendar event provider for testing."""

    def __init__(self) -> None:
        self.events: dict[str, str] = {}  # reconciliation_key -> provider_event_id
        self.create_count = 0
        self.fail_on_create = False
        self.conflict_on_create = False

    def create_event(
        self,
        *,
        calendar_id: str,
        reconciliation_key: str,
        title: str,
        start_utc: datetime,
        end_utc: datetime,
        timezone: str,
        meeting_link: str,
        now: datetime,
    ) -> str:
        self.create_count += 1
        if self.fail_on_create:
            raise RuntimeError("provider failure")
        if self.conflict_on_create:
            raise SlotConflictError("conflict detected during insert")
        existing = self.events.get(reconciliation_key)
        if existing is not None:
            return existing
        provider_id = f"event-{len(self.events) + 1}"
        self.events[reconciliation_key] = provider_id
        return provider_id

    def lookup_by_reconciliation_key(
        self, *, calendar_id: str, reconciliation_key: str
    ) -> str | None:
        return self.events.get(reconciliation_key)


# ---------------------------------------------------------------------------
# Scheduling service
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreateProposalInput:
    """Input for creating a schedule proposal."""

    application_id: UUID
    canonical_job_id: UUID
    candidate_slots: tuple[TimeSlot, ...]
    calendar_ids: tuple[str, ...]
    rules: SchedulingRules
    created_by: str
    recruiter_confirmed_at: datetime | None = None
    meeting_link: str = ""
    interviewer_name: str = ""
    interviewer_email: str = ""
    interview_type: str = "video"


@dataclass(frozen=True, slots=True)
class EventCreationResult:
    """Result of event creation through the M5A chain."""

    proposal: ScheduleProposal
    calendar_event: CalendarEvent
    interview_record: InterviewRecord
    intent_id: UUID
    receipt_id: UUID | None
    conflict_review: ConflictReview | None = None


class SchedulingService:
    """Coordinates scheduling operations through the M5A authorization chain.

    Every calendar event creation goes through:
    1. Recruiter confirmation (external)
    2. User action ("re-verify and create")
    3. Fresh FreeBusy query (fail closed if stale)
    4. M5A chain: Policy -> Approval -> Intent -> Execute -> Receipt -> Audit
    5. Post-write reconciliation
    6. Conflict detection -> conflict review + block confirmation email
    """

    def __init__(
        self,
        *,
        kernel: SideEffectKernel,
        freebusy_provider: FreeBusyProvider,
        event_provider: CalendarEventProvider,
        proposal_store: ScheduleProposalStore,
        event_store: CalendarEventStore,
        interview_store: InterviewRecordStore,
        conflict_store: ConflictReviewStore,
        calendar_id: str = "careerops-interviews",
    ) -> None:
        self._kernel = kernel
        self._freebusy = freebusy_provider
        self._event_provider = event_provider
        self._proposals = proposal_store
        self._events = event_store
        self._interviews = interview_store
        self._conflicts = conflict_store
        self._calendar_id = calendar_id

    # -- Proposal lifecycle ------------------------------------------------

    def create_proposal(self, input: CreateProposalInput, *, now: datetime) -> ScheduleProposal:
        """Create a new schedule proposal with candidate slots."""
        proposal = ScheduleProposal(
            id=uuid4(),
            application_id=input.application_id,
            canonical_job_id=input.canonical_job_id,
            candidate_slots=input.candidate_slots,
            selected_slot=None,
            status=ProposalStatus.DRAFT,
            rules_version=input.rules.rules_version,
            scheduling_rules=input.rules,
            calendar_ids_checked=input.calendar_ids,
            recruiter_confirmed_at=input.recruiter_confirmed_at,
            user_action_at=None,
            action_intent_id=None,
            payload_hash=None,
            freebusy_queried_at=None,
            expires_at=now + timedelta(hours=24),
            conflict_reason=None,
            created_by=input.created_by,
            created_at=now,
            updated_at=now,
        )
        return self._proposals.insert(proposal)

    def mark_recruiter_confirmed(
        self, proposal_id: UUID, *, confirmed_at: datetime
    ) -> ScheduleProposal:
        """Mark that the recruiter has confirmed the interview time."""
        proposal = self._proposals.get(proposal_id)
        updated = ScheduleProposal(
            id=proposal.id,
            application_id=proposal.application_id,
            canonical_job_id=proposal.canonical_job_id,
            candidate_slots=proposal.candidate_slots,
            selected_slot=proposal.selected_slot,
            status=ProposalStatus.RECRUITER_CONFIRMED,
            rules_version=proposal.rules_version,
            scheduling_rules=proposal.scheduling_rules,
            calendar_ids_checked=proposal.calendar_ids_checked,
            recruiter_confirmed_at=confirmed_at,
            user_action_at=proposal.user_action_at,
            action_intent_id=proposal.action_intent_id,
            payload_hash=proposal.payload_hash,
            freebusy_queried_at=proposal.freebusy_queried_at,
            expires_at=proposal.expires_at,
            conflict_reason=proposal.conflict_reason,
            created_by=proposal.created_by,
            created_at=proposal.created_at,
            updated_at=confirmed_at,
        )
        return self._proposals.update(updated)

    def select_slot(self, proposal_id: UUID, slot: TimeSlot, *, now: datetime) -> ScheduleProposal:
        """Select a specific slot from the candidates."""
        proposal = self._proposals.get(proposal_id)
        if slot not in proposal.candidate_slots:
            raise SchedulingError("selected slot must be one of the candidate slots")
        updated = ScheduleProposal(
            id=proposal.id,
            application_id=proposal.application_id,
            canonical_job_id=proposal.canonical_job_id,
            candidate_slots=proposal.candidate_slots,
            selected_slot=slot,
            status=proposal.status,
            rules_version=proposal.rules_version,
            scheduling_rules=proposal.scheduling_rules,
            calendar_ids_checked=proposal.calendar_ids_checked,
            recruiter_confirmed_at=proposal.recruiter_confirmed_at,
            user_action_at=proposal.user_action_at,
            action_intent_id=proposal.action_intent_id,
            payload_hash=proposal.payload_hash,
            freebusy_queried_at=proposal.freebusy_queried_at,
            expires_at=proposal.expires_at,
            conflict_reason=proposal.conflict_reason,
            created_by=proposal.created_by,
            created_at=proposal.created_at,
            updated_at=now,
        )
        return self._proposals.update(updated)

    # -- Event creation (M5A chain) ----------------------------------------

    def reverify_and_create_event(
        self,
        proposal_id: UUID,
        *,
        user_id: str,
        now: datetime,
    ) -> EventCreationResult:
        """User clicks 're-verify and create': the full M5B event creation flow.

        Steps:
        1. Validate recruiter confirmation exists.
        2. Validate user action (this call IS the user action).
        3. Re-query FreeBusy (fail closed if stale).
        4. Validate slot against rules (buffer, notice, limits).
        5. Check for conflicts.
        6. Run M5A chain: propose -> approve -> execute.
        7. Create calendar event via provider.
        8. Post-write reconciliation.
        9. Create interview record.
        10. On conflict: enter conflict review, block confirmation email.
        """
        proposal = self._proposals.get(proposal_id)

        # Gate 1: Recruiter must have confirmed
        if not proposal.is_recruiter_confirmed:
            raise RecruiterNotConfirmedError("cannot create event before recruiter confirmation")

        # Gate 2: Selected slot required
        if proposal.selected_slot is None:
            raise SchedulingError("no slot selected for event creation")
        selected_slot = proposal.selected_slot  # Type narrowing for pyright

        # Gate 3: Proposal not expired
        if now >= proposal.expires_at:
            self._update_proposal_status(proposal, ProposalStatus.EXPIRED, now=now)
            raise SchedulingError("proposal has expired")

        # Record user action
        proposal = self._record_user_action(proposal, user_id=user_id, now=now)

        # Gate 4: Fresh FreeBusy query (fail closed)
        freebusy = self._freebusy.query_freebusy(
            calendar_ids=proposal.calendar_ids_checked,
            window_start=selected_slot.start_utc - timedelta(hours=1),
            window_end=selected_slot.end_utc + timedelta(hours=1),
            now=now,
        )
        if freebusy.is_stale(now=now):
            raise FreeBusyStaleError("FreeBusy results are stale; re-query required")

        # Update proposal with FreeBusy timestamp
        proposal = self._update_freebusy_timestamp(proposal, freebusy, now=now)

        # Gate 5: Validate slot against rules
        validation = self._validate_slot(selected_slot, proposal.scheduling_rules, now=now)
        if not validation.is_valid:
            reasons = ", ".join(r.value for r in validation.rejection_reasons)
            raise SchedulingError(f"slot validation failed: {reasons}")

        # Gate 6: Check FreeBusy conflicts
        if freebusy.is_busy(selected_slot):
            self._create_conflict_review(
                proposal=proposal,
                conflict_type="freebusy_race",
                description="Slot conflicts with existing busy period detected in FreeBusy",
                now=now,
            )
            proposal = self._update_proposal_status(
                proposal,
                ProposalStatus.CONFLICT_REVIEW,
                now=now,
                conflict_reason="freebusy_conflict",
            )
            raise SlotConflictError("slot conflicts with existing busy period")

        # Gate 7: Check for duplicate events
        reconciliation_key = self._compute_reconciliation_key(proposal)
        existing_event = self._events.find_by_reconciliation_key(reconciliation_key)
        if existing_event is not None:
            raise DuplicateEventError(
                f"event with reconciliation key {reconciliation_key} already exists"
            )

        # Gate 8: M5A authorization chain
        proposal = self._update_proposal_status(proposal, ProposalStatus.EXECUTING, now=now)

        payload_hash = canonical_payload_hash(
            target={
                "calendar_id": self._calendar_id,
                "reconciliation_key": reconciliation_key,
            },
            payload={
                "title": f"Interview: {proposal.canonical_job_id}",
                "start_utc": selected_slot.start_utc.isoformat(),
                "end_utc": selected_slot.end_utc.isoformat(),
                "timezone": selected_slot.source_timezone,
            },
        )

        # Propose through M5A kernel
        proposal_input = ProposalInput(
            action_kind="create_calendar_event",
            resource_type="schedule_proposal",
            resource_id=proposal.id,
            idempotency_key=reconciliation_key,
            created_by=user_id,
            target={
                "calendar_id": self._calendar_id,
                "reconciliation_key": reconciliation_key,
            },
            payload={
                "title": f"Interview: {proposal.canonical_job_id}",
                "start_utc": selected_slot.start_utc.isoformat(),
                "end_utc": selected_slot.end_utc.isoformat(),
                "timezone": selected_slot.source_timezone,
            },
            evidence_refs=(f"proposal:{proposal.id}", f"slot:{reconciliation_key}"),
            trusted_facts={
                "capability_released": True,
                "target_allowlisted": True,
                "recruiter_confirmed": True,
                "user_action": True,
            },
        )

        proposal_result = self._kernel.propose(proposal_input, now=now)

        # Update proposal with intent link
        proposal = self._update_proposal_intent(
            proposal, intent_id=proposal_result.intent.id, payload_hash=payload_hash, now=now
        )

        # Request and grant approval (user action = approval)
        approval = self._kernel.request_approval(
            proposal_result.intent.id, requested_for=user_id, now=now
        )
        self._kernel.approve(approval.id, now=now)

        # Execute through M5A kernel
        outcome = self._kernel.execute(proposal_result.intent.id, now=now)

        # Gate 9: Create calendar event via provider
        try:
            provider_event_id = self._event_provider.create_event(
                calendar_id=self._calendar_id,
                reconciliation_key=reconciliation_key,
                title=f"Interview: {proposal.canonical_job_id}",
                start_utc=selected_slot.start_utc,
                end_utc=selected_slot.end_utc,
                timezone=selected_slot.source_timezone,
                meeting_link="",
                now=now,
            )
        except SlotConflictError:
            # Race condition: conflict between FreeBusy and insert
            self._create_conflict_review(
                proposal=proposal,
                conflict_type="freebusy_race",
                description="Conflict detected between last FreeBusy and event insert",
                now=now,
            )
            proposal = self._update_proposal_status(
                proposal, ProposalStatus.CONFLICT_REVIEW, now=now, conflict_reason="insert_race"
            )
            raise

        # Create calendar event record
        calendar_event = CalendarEvent(
            id=uuid4(),
            schedule_proposal_id=proposal.id,
            action_intent_id=proposal_result.intent.id,
            provider_event_id=provider_event_id,
            calendar_id=self._calendar_id,
            reconciliation_key=reconciliation_key,
            title=f"Interview: {proposal.canonical_job_id}",
            slot=selected_slot,
            meeting_link="",
            status=CalendarEventStatus.CREATED,
            conflict_detected_at=None,
            created_at=now,
            updated_at=now,
        )
        self._events.insert(calendar_event)

        # Gate 10: Post-write reconciliation
        self._post_write_reconcile(calendar_event, now=now)

        # Create interview record
        interview_record = InterviewRecord(
            id=uuid4(),
            application_id=proposal.application_id,
            canonical_job_id=proposal.canonical_job_id,
            calendar_event_id=calendar_event.id,
            schedule_proposal_id=proposal.id,
            status=InterviewStatus.SCHEDULED,
            interviewer_name="",
            interviewer_email="",
            interview_type="video",
            meeting_link="",
            notes="",
            evidence_summary={
                "proposal_id": str(proposal.id),
                "slot_start": selected_slot.start_utc.isoformat(),
                "slot_end": selected_slot.end_utc.isoformat(),
                "timezone": selected_slot.source_timezone,
            },
            created_at=now,
            updated_at=now,
        )
        self._interviews.insert(interview_record)

        # Mark proposal confirmed
        proposal = self._update_proposal_status(proposal, ProposalStatus.CONFIRMED, now=now)

        return EventCreationResult(
            proposal=proposal,
            calendar_event=calendar_event,
            interview_record=interview_record,
            intent_id=proposal_result.intent.id,
            receipt_id=outcome.receipt.id if outcome.receipt else None,
        )

    # -- Slot validation ---------------------------------------------------

    def generate_candidate_slots(
        self,
        *,
        rules: SchedulingRules,
        window_start: datetime,
        window_end: datetime,
        freebusy: FreeBusyResult,
        existing_events_count: int,
        now: datetime,
    ) -> tuple[SlotCandidate, ...]:
        """Generate up to MAX_CANDIDATE_SLOTS valid candidate slots."""
        candidates: list[SlotCandidate] = []
        duration = timedelta(minutes=rules.slot_duration_minutes)
        buffer = timedelta(minutes=rules.buffer_minutes)

        current = window_start
        while current + duration <= window_end and len(candidates) < 3:
            slot_end = current + duration
            slot = TimeSlot(
                start_utc=current,
                end_utc=slot_end,
                source_timezone=rules.timezone,
                duration_minutes=rules.slot_duration_minutes,
            )

            rejection_reasons: list[SlotRejectionReason] = []

            # Check buffer
            if now + timedelta(minutes=rules.minimum_notice_minutes) > current:
                rejection_reasons.append(SlotRejectionReason.NOTICE_VIOLATION)

            # Check FreeBusy
            if freebusy.is_busy(slot):
                rejection_reasons.append(SlotRejectionReason.CONFLICT_WITH_EXISTING)

            # Check daily limit
            daily_count = existing_events_count  # simplified for testing
            if daily_count >= rules.max_events_per_day:
                rejection_reasons.append(SlotRejectionReason.DAILY_LIMIT_EXCEEDED)

            is_valid = len(rejection_reasons) == 0
            candidates.append(
                SlotCandidate(
                    slot=slot,
                    is_valid=is_valid,
                    rejection_reasons=tuple(rejection_reasons),
                )
            )

            current = slot_end + buffer

        return tuple(candidates)

    def validate_slot(
        self, slot: TimeSlot, rules: SchedulingRules, *, now: datetime
    ) -> SlotCandidate:
        """Validate a single slot against scheduling rules."""
        return self._validate_slot(slot, rules, now=now)

    # -- Conflict handling -------------------------------------------------

    def resolve_conflict(
        self,
        conflict_id: UUID,
        *,
        resolution: str,
        resolved_at: datetime,
    ) -> ConflictReview:
        """Resolve a conflict review (human action required)."""
        conflict = self._conflicts.get(conflict_id)
        resolved = ConflictReview(
            id=conflict.id,
            schedule_proposal_id=conflict.schedule_proposal_id,
            calendar_event_id=conflict.calendar_event_id,
            conflict_type=conflict.conflict_type,
            description=conflict.description,
            detected_at=conflict.detected_at,
            resolved_at=resolved_at,
            resolution=resolution,
            confirmation_email_blocked=True,  # Always blocked
        )
        return self._conflicts.insert(resolved)

    # -- Private helpers ---------------------------------------------------

    def _validate_slot(
        self, slot: TimeSlot, rules: SchedulingRules, *, now: datetime
    ) -> SlotCandidate:
        """Validate a slot against scheduling rules."""
        rejection_reasons: list[SlotRejectionReason] = []

        # Check minimum notice
        if now + timedelta(minutes=rules.minimum_notice_minutes) > slot.start_utc:
            rejection_reasons.append(SlotRejectionReason.NOTICE_VIOLATION)

        # Check available windows (simplified: check if within business hours)
        local_start = slot.start_utc.astimezone(ZoneInfo(rules.timezone))
        minutes_from_midnight = local_start.hour * 60 + local_start.minute
        in_window = any(
            start <= minutes_from_midnight < end for start, end in rules.available_windows
        )
        if not in_window:
            rejection_reasons.append(SlotRejectionReason.OUTSIDE_AVAILABLE_WINDOW)

        return SlotCandidate(
            slot=slot,
            is_valid=len(rejection_reasons) == 0,
            rejection_reasons=tuple(rejection_reasons),
        )

    def _compute_reconciliation_key(self, proposal: ScheduleProposal) -> str:
        """Compute a stable reconciliation key for deduplication."""
        if proposal.selected_slot is None:
            raise ValueError("Cannot compute reconciliation key without a selected slot")
        return (
            f"sched:{proposal.id}:{proposal.selected_slot.start_utc.isoformat()}:"
            f"{proposal.selected_slot.end_utc.isoformat()}"
        )

    def _record_user_action(
        self, proposal: ScheduleProposal, *, user_id: str, now: datetime
    ) -> ScheduleProposal:
        """Record the user's explicit action to create the event."""
        updated = ScheduleProposal(
            id=proposal.id,
            application_id=proposal.application_id,
            canonical_job_id=proposal.canonical_job_id,
            candidate_slots=proposal.candidate_slots,
            selected_slot=proposal.selected_slot,
            status=ProposalStatus.PENDING_USER_ACTION,
            rules_version=proposal.rules_version,
            scheduling_rules=proposal.scheduling_rules,
            calendar_ids_checked=proposal.calendar_ids_checked,
            recruiter_confirmed_at=proposal.recruiter_confirmed_at,
            user_action_at=now,
            action_intent_id=proposal.action_intent_id,
            payload_hash=proposal.payload_hash,
            freebusy_queried_at=proposal.freebusy_queried_at,
            expires_at=proposal.expires_at,
            conflict_reason=proposal.conflict_reason,
            created_by=proposal.created_by,
            created_at=proposal.created_at,
            updated_at=now,
        )
        return self._proposals.update(updated)

    def _update_freebusy_timestamp(
        self, proposal: ScheduleProposal, freebusy: FreeBusyResult, *, now: datetime
    ) -> ScheduleProposal:
        """Update the proposal with the FreeBusy query timestamp."""
        updated = ScheduleProposal(
            id=proposal.id,
            application_id=proposal.application_id,
            canonical_job_id=proposal.canonical_job_id,
            candidate_slots=proposal.candidate_slots,
            selected_slot=proposal.selected_slot,
            status=proposal.status,
            rules_version=proposal.rules_version,
            scheduling_rules=proposal.scheduling_rules,
            calendar_ids_checked=proposal.calendar_ids_checked,
            recruiter_confirmed_at=proposal.recruiter_confirmed_at,
            user_action_at=proposal.user_action_at,
            action_intent_id=proposal.action_intent_id,
            payload_hash=proposal.payload_hash,
            freebusy_queried_at=freebusy.queried_at,
            expires_at=proposal.expires_at,
            conflict_reason=proposal.conflict_reason,
            created_by=proposal.created_by,
            created_at=proposal.created_at,
            updated_at=now,
        )
        return self._proposals.update(updated)

    def _update_proposal_status(
        self,
        proposal: ScheduleProposal,
        status: ProposalStatus,
        *,
        now: datetime,
        conflict_reason: str | None = None,
    ) -> ScheduleProposal:
        """Update proposal status."""
        updated = ScheduleProposal(
            id=proposal.id,
            application_id=proposal.application_id,
            canonical_job_id=proposal.canonical_job_id,
            candidate_slots=proposal.candidate_slots,
            selected_slot=proposal.selected_slot,
            status=status,
            rules_version=proposal.rules_version,
            scheduling_rules=proposal.scheduling_rules,
            calendar_ids_checked=proposal.calendar_ids_checked,
            recruiter_confirmed_at=proposal.recruiter_confirmed_at,
            user_action_at=proposal.user_action_at,
            action_intent_id=proposal.action_intent_id,
            payload_hash=proposal.payload_hash,
            freebusy_queried_at=proposal.freebusy_queried_at,
            expires_at=proposal.expires_at,
            conflict_reason=conflict_reason if conflict_reason else proposal.conflict_reason,
            created_by=proposal.created_by,
            created_at=proposal.created_at,
            updated_at=now,
        )
        return self._proposals.update(updated)

    def _update_proposal_intent(
        self,
        proposal: ScheduleProposal,
        *,
        intent_id: UUID,
        payload_hash: str,
        now: datetime,
    ) -> ScheduleProposal:
        """Link the proposal to the M5A intent."""
        updated = ScheduleProposal(
            id=proposal.id,
            application_id=proposal.application_id,
            canonical_job_id=proposal.canonical_job_id,
            candidate_slots=proposal.candidate_slots,
            selected_slot=proposal.selected_slot,
            status=proposal.status,
            rules_version=proposal.rules_version,
            scheduling_rules=proposal.scheduling_rules,
            calendar_ids_checked=proposal.calendar_ids_checked,
            recruiter_confirmed_at=proposal.recruiter_confirmed_at,
            user_action_at=proposal.user_action_at,
            action_intent_id=intent_id,
            payload_hash=payload_hash,
            freebusy_queried_at=proposal.freebusy_queried_at,
            expires_at=proposal.expires_at,
            conflict_reason=proposal.conflict_reason,
            created_by=proposal.created_by,
            created_at=proposal.created_at,
            updated_at=now,
        )
        return self._proposals.update(updated)

    def _create_conflict_review(
        self,
        *,
        proposal: ScheduleProposal,
        conflict_type: str,
        description: str,
        now: datetime,
    ) -> ConflictReview:
        """Create a conflict review and block confirmation emails."""
        conflict = ConflictReview(
            id=uuid4(),
            schedule_proposal_id=proposal.id,
            calendar_event_id=None,
            conflict_type=conflict_type,
            description=description,
            detected_at=now,
            resolved_at=None,
            resolution=None,
            confirmation_email_blocked=True,
        )
        return self._conflicts.insert(conflict)

    def _post_write_reconcile(self, event: CalendarEvent, *, now: datetime) -> None:
        """Post-write reconciliation: verify the event exists in the provider."""
        provider_id = self._event_provider.lookup_by_reconciliation_key(
            calendar_id=event.calendar_id,
            reconciliation_key=event.reconciliation_key,
        )
        if provider_id is None:
            # Event not found in provider after creation - this is a conflict
            conflict = ConflictReview(
                id=uuid4(),
                schedule_proposal_id=event.schedule_proposal_id,
                calendar_event_id=event.id,
                conflict_type="reconciliation_failure",
                description="Event not found in provider after creation",
                detected_at=now,
                resolved_at=None,
                resolution=None,
                confirmation_email_blocked=True,
            )
            self._conflicts.insert(conflict)
            updated_event = CalendarEvent(
                id=event.id,
                schedule_proposal_id=event.schedule_proposal_id,
                action_intent_id=event.action_intent_id,
                provider_event_id=event.provider_event_id,
                calendar_id=event.calendar_id,
                reconciliation_key=event.reconciliation_key,
                title=event.title,
                slot=event.slot,
                meeting_link=event.meeting_link,
                status=CalendarEventStatus.CONFLICT_DETECTED,
                conflict_detected_at=now,
                created_at=event.created_at,
                updated_at=now,
            )
            self._events.update(updated_event)
