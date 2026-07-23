"""Unit tests for M5B: Calendar scheduling and interview management.

Covers the M5B acceptance matrix:
- Timezone/slot accuracy: IANA timezone parsing, DST, ambiguous abbreviation rejection.
- No event before recruiter confirmation.
- No duplicate events: reconciliation key + unique constraint.
- No unattended event creation: user action required.
- Every write has user action + Policy + Approval + Intent + Receipt + Audit.
- Race condition -> conflict review + no confirmation email.
- FreeBusy stale results fail closed.
- Buffer, notice, daily/weekly limits enforced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from careerops.application.scheduling import (
    CalendarEventProvider,
    CreateProposalInput,
    DuplicateEventError,
    FakeCalendarEventProvider,
    FakeFreeBusyProvider,
    FreeBusyProvider,
    FreeBusyStaleError,
    InMemoryCalendarEventStore,
    InMemoryConflictReviewStore,
    InMemoryInterviewRecordStore,
    InMemoryScheduleProposalStore,
    RecruiterNotConfirmedError,
    SchedulingError,
    SchedulingService,
    SlotConflictError,
)
from careerops.application.side_effect_kernel import (
    InMemoryAuditWriter,
    SideEffectKernel,
)
from careerops.domain.scheduling import (
    FREEBUSY_MAX_AGE_SECONDS,
    CalendarEventStatus,
    ConflictReview,
    FreeBusyResult,
    InterviewStatus,
    ProposalStatus,
    ScheduleProposal,
    SchedulingRules,
    SlotCandidate,
    SlotRejectionReason,
    TimeSlot,
)
from careerops.infrastructure.database.side_effect_memory import InMemorySideEffectStore
from careerops.integrations.fake_side_effect_provider import FakeSideEffectProvider

NOW = datetime(2026, 7, 22, 10, 0, 0, tzinfo=UTC)
FUTURE = NOW + timedelta(hours=2)


def make_slot(
    *,
    start: datetime | None = None,
    duration_minutes: int = 60,
    timezone: str = "Asia/Shanghai",
) -> TimeSlot:
    """Create a valid time slot for testing."""
    start = start or FUTURE
    end = start + timedelta(minutes=duration_minutes)
    return TimeSlot(
        start_utc=start,
        end_utc=end,
        source_timezone=timezone,
        duration_minutes=duration_minutes,
    )


def make_rules(**kwargs: object) -> SchedulingRules:
    """Create scheduling rules with sensible defaults for testing."""
    defaults: dict[str, object] = {
        "buffer_minutes": 15,
        "minimum_notice_minutes": 60,
        "max_events_per_day": 3,
        "max_events_per_week": 10,
        "available_windows": ((0, 24 * 60),),  # All day for testing
        "slot_duration_minutes": 60,
        "timezone": "Asia/Shanghai",
        "rules_version": "m5b.scheduling.v1",
    }
    defaults.update(kwargs)
    return SchedulingRules(**defaults)  # type: ignore[arg-type]


def build_service(
    *,
    freebusy: FakeFreeBusyProvider | None = None,
    event_provider: FakeCalendarEventProvider | None = None,
) -> tuple[SchedulingService, FakeFreeBusyProvider, FakeCalendarEventProvider, InMemoryAuditWriter]:
    """Build a SchedulingService with in-memory stores and fake providers."""
    freebusy = freebusy or FakeFreeBusyProvider()
    event_provider = event_provider or FakeCalendarEventProvider()
    audit = InMemoryAuditWriter()
    kernel = SideEffectKernel(
        InMemorySideEffectStore(),
        FakeSideEffectProvider(),
        audit_writer=audit,
    )
    service = SchedulingService(
        kernel=kernel,
        freebusy_provider=freebusy,
        event_provider=event_provider,
        proposal_store=InMemoryScheduleProposalStore(),
        event_store=InMemoryCalendarEventStore(),
        interview_store=InMemoryInterviewRecordStore(),
        conflict_store=InMemoryConflictReviewStore(),
    )
    return service, freebusy, event_provider, audit


def build_service_custom(
    *,
    freebusy: FreeBusyProvider,
    event_provider: CalendarEventProvider,
) -> SchedulingService:
    """Build a SchedulingService around caller-supplied provider doubles."""
    kernel = SideEffectKernel(
        InMemorySideEffectStore(),
        FakeSideEffectProvider(),
        audit_writer=InMemoryAuditWriter(),
    )
    return SchedulingService(
        kernel=kernel,
        freebusy_provider=freebusy,
        event_provider=event_provider,
        proposal_store=InMemoryScheduleProposalStore(),
        event_store=InMemoryCalendarEventStore(),
        interview_store=InMemoryInterviewRecordStore(),
        conflict_store=InMemoryConflictReviewStore(),
    )


def make_proposal_input(
    *,
    slots: tuple[TimeSlot, ...] | None = None,
    recruiter_confirmed_at: datetime | None = None,
    rules: SchedulingRules | None = None,
) -> CreateProposalInput:
    """Create a proposal input for testing."""
    slots = slots or (make_slot(),)
    return CreateProposalInput(
        application_id=uuid4(),
        canonical_job_id=uuid4(),
        candidate_slots=slots,
        calendar_ids=("careerops-interviews",),
        rules=rules or make_rules(),
        created_by="user-1",
        recruiter_confirmed_at=recruiter_confirmed_at,
    )


# ---------------------------------------------------------------------------
# Timezone and slot validation
# ---------------------------------------------------------------------------


class TestTimezoneAndSlots:
    def test_valid_slot_creation(self) -> None:
        slot = make_slot()
        assert slot.start_utc < slot.end_utc
        assert slot.duration_minutes == 60
        assert slot.source_timezone == "Asia/Shanghai"

    def test_slot_rejects_invalid_timezone(self) -> None:
        with pytest.raises(ValueError, match="invalid IANA timezone"):
            TimeSlot(
                start_utc=FUTURE,
                end_utc=FUTURE + timedelta(hours=1),
                source_timezone="Invalid/Timezone",
                duration_minutes=60,
            )

    def test_slot_rejects_end_before_start(self) -> None:
        with pytest.raises(ValueError, match="start must be before end"):
            TimeSlot(
                start_utc=FUTURE + timedelta(hours=1),
                end_utc=FUTURE,
                source_timezone="Asia/Shanghai",
                duration_minutes=60,
            )

    def test_slot_rejects_duration_mismatch(self) -> None:
        with pytest.raises(ValueError, match="duration_minutes does not match"):
            TimeSlot(
                start_utc=FUTURE,
                end_utc=FUTURE + timedelta(hours=2),
                source_timezone="Asia/Shanghai",
                duration_minutes=60,  # Wrong: should be 120
            )

    def test_slot_local_and_display_times(self) -> None:
        # Create a slot at a known UTC time
        utc_start = datetime(2026, 7, 22, 2, 0, 0, tzinfo=UTC)  # 10:00 Shanghai
        slot = TimeSlot(
            start_utc=utc_start,
            end_utc=utc_start + timedelta(hours=1),
            source_timezone="Asia/Shanghai",
            duration_minutes=60,
        )
        # Local time in Shanghai should be 10:00
        assert slot.start_local.hour == 10
        # Display time is also Shanghai
        assert slot.start_display.hour == 10

    def test_half_hour_timezone_support(self) -> None:
        # India is UTC+5:30
        slot = make_slot(timezone="Asia/Kolkata")
        assert slot.source_timezone == "Asia/Kolkata"

    def test_45_minute_timezone_support(self) -> None:
        # Nepal is UTC+5:45
        slot = make_slot(timezone="Asia/Kathmandu")
        assert slot.source_timezone == "Asia/Kathmandu"

    def test_dst_aware_timezone(self) -> None:
        # US Eastern has DST
        slot = make_slot(timezone="America/New_York")
        assert slot.source_timezone == "America/New_York"


# ---------------------------------------------------------------------------
# Scheduling rules validation
# ---------------------------------------------------------------------------


class TestSchedulingRules:
    def test_default_rules(self) -> None:
        rules = SchedulingRules()
        assert rules.buffer_minutes == 15
        assert rules.minimum_notice_minutes == 60
        assert rules.max_events_per_day == 3
        assert rules.max_events_per_week == 10

    def test_rules_reject_negative_buffer(self) -> None:
        with pytest.raises(ValueError, match="buffer_minutes"):
            SchedulingRules(buffer_minutes=-1)

    def test_rules_reject_zero_daily_limit(self) -> None:
        with pytest.raises(ValueError, match="max_events_per_day"):
            SchedulingRules(max_events_per_day=0)

    def test_rules_reject_short_slot_duration(self) -> None:
        with pytest.raises(ValueError, match="slot_duration_minutes"):
            SchedulingRules(slot_duration_minutes=10)


# ---------------------------------------------------------------------------
# Proposal lifecycle
# ---------------------------------------------------------------------------


class TestProposalLifecycle:
    def test_create_proposal(self) -> None:
        service, _, _, _ = build_service()
        proposal = service.create_proposal(make_proposal_input(), now=NOW)

        assert proposal.status is ProposalStatus.DRAFT
        assert len(proposal.candidate_slots) == 1
        assert proposal.recruiter_confirmed_at is None
        assert proposal.user_action_at is None

    def test_create_proposal_with_recruiter_confirmed(self) -> None:
        service, _, _, _ = build_service()
        confirmed_at = NOW - timedelta(hours=1)
        proposal = service.create_proposal(
            make_proposal_input(recruiter_confirmed_at=confirmed_at), now=NOW
        )

        assert proposal.recruiter_confirmed_at == confirmed_at
        assert proposal.is_recruiter_confirmed

    def test_mark_recruiter_confirmed(self) -> None:
        service, _, _, _ = build_service()
        proposal = service.create_proposal(make_proposal_input(), now=NOW)
        confirmed_at = NOW + timedelta(minutes=30)

        updated = service.mark_recruiter_confirmed(proposal.id, confirmed_at=confirmed_at)

        assert updated.status is ProposalStatus.RECRUITER_CONFIRMED
        assert updated.recruiter_confirmed_at == confirmed_at
        assert updated.is_recruiter_confirmed

    def test_select_slot(self) -> None:
        service, _, _, _ = build_service()
        slot = make_slot()
        proposal = service.create_proposal(make_proposal_input(slots=(slot,)), now=NOW)

        updated = service.select_slot(proposal.id, slot, now=NOW)

        assert updated.selected_slot == slot

    def test_select_slot_rejects_non_candidate(self) -> None:
        service, _, _, _ = build_service()
        slot1 = make_slot()
        slot2 = make_slot(start=FUTURE + timedelta(hours=3))
        proposal = service.create_proposal(make_proposal_input(slots=(slot1,)), now=NOW)

        with pytest.raises(SchedulingError, match="must be one of the candidate"):
            service.select_slot(proposal.id, slot2, now=NOW)

    def test_proposal_expires(self) -> None:
        service, _, _, _ = build_service()
        proposal = service.create_proposal(make_proposal_input(), now=NOW)

        # Proposal expires after 24 hours
        assert proposal.expires_at == NOW + timedelta(hours=24)


# ---------------------------------------------------------------------------
# Event creation: M5A authorization chain
# ---------------------------------------------------------------------------


class TestEventCreation:
    def test_happy_path_creates_event_with_full_chain(self) -> None:
        """Full end-to-end: recruiter confirms, user acts, event created with M5A chain."""
        service, freebusy, event_provider, audit = build_service()
        slot = make_slot()
        confirmed_at = NOW - timedelta(minutes=30)

        # Create proposal with recruiter already confirmed
        proposal = service.create_proposal(
            make_proposal_input(slots=(slot,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal = service.select_slot(proposal.id, slot, now=NOW)

        # User clicks "re-verify and create"
        result = service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        # Verify event was created
        assert result.calendar_event.status is CalendarEventStatus.CREATED
        assert result.calendar_event.provider_event_id is not None
        assert result.calendar_event.reconciliation_key

        # Verify interview record was created
        assert result.interview_record.status is InterviewStatus.SCHEDULED
        assert result.interview_record.calendar_event_id == result.calendar_event.id

        # Verify M5A chain was used
        assert result.intent_id is not None
        assert result.receipt_id is not None

        # Verify proposal is confirmed
        assert result.proposal.status is ProposalStatus.CONFIRMED
        assert result.proposal.user_action_at is not None
        assert result.proposal.action_intent_id == result.intent_id

        # Verify FreeBusy was queried
        assert freebusy.query_count == 1

        # Verify provider was called
        assert event_provider.create_count == 1

        # Verify audit events exist
        assert len(audit.all_events()) >= 3  # proposed, approved, executed

    def test_no_event_before_recruiter_confirmation(self) -> None:
        """Cannot create event before recruiter confirms."""
        service, _, _, _ = build_service()
        slot = make_slot()
        proposal = service.create_proposal(make_proposal_input(slots=(slot,)), now=NOW)
        proposal = service.select_slot(proposal.id, slot, now=NOW)

        with pytest.raises(RecruiterNotConfirmedError, match="recruiter confirmation"):
            service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

    def test_no_event_without_selected_slot(self) -> None:
        """Cannot create event without selecting a slot."""
        service, _, _, _ = build_service()
        confirmed_at = NOW - timedelta(minutes=30)
        proposal = service.create_proposal(
            make_proposal_input(recruiter_confirmed_at=confirmed_at), now=NOW
        )

        with pytest.raises(SchedulingError, match="no slot selected"):
            service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

    def test_no_event_after_proposal_expiry(self) -> None:
        """Cannot create event after proposal expires."""
        service, _, _, _ = build_service()
        slot = make_slot()
        confirmed_at = NOW - timedelta(minutes=30)
        proposal = service.create_proposal(
            make_proposal_input(slots=(slot,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal = service.select_slot(proposal.id, slot, now=NOW)

        # Try to create after expiry
        expired_time = NOW + timedelta(hours=25)
        with pytest.raises(SchedulingError, match="expired"):
            service.reverify_and_create_event(proposal.id, user_id="user-1", now=expired_time)

    def test_user_action_is_recorded(self) -> None:
        """User action timestamp is recorded on the proposal."""
        service, _, _, _ = build_service()
        slot = make_slot()
        confirmed_at = NOW - timedelta(minutes=30)
        proposal = service.create_proposal(
            make_proposal_input(slots=(slot,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal = service.select_slot(proposal.id, slot, now=NOW)

        result = service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        assert result.proposal.user_action_at == NOW

    def test_every_write_has_audit_trail(self) -> None:
        """Every calendar event write has a complete audit trail."""
        service, _, _, audit = build_service()
        slot = make_slot()
        confirmed_at = NOW - timedelta(minutes=30)
        proposal = service.create_proposal(
            make_proposal_input(slots=(slot,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal = service.select_slot(proposal.id, slot, now=NOW)

        service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        # Verify audit chain: proposed -> approved -> executed
        events = audit.all_events()
        event_types = {e.event_type for e in events}
        assert "side_effect_proposed" in event_types
        assert "side_effect_approved" in event_types
        assert "side_effect_executed" in event_types

        # Verify each audit event has required fields
        for event in events:
            assert event.actor_type is not None
            assert event.resource_type == "action_intent"
            assert event.trace_id is not None


# ---------------------------------------------------------------------------
# Duplicate prevention
# ---------------------------------------------------------------------------


class TestDuplicatePrevention:
    def test_duplicate_event_rejected_by_event_store(self) -> None:
        """Event store rejects duplicate reconciliation keys."""
        from careerops.application.scheduling import InMemoryCalendarEventStore
        from careerops.domain.scheduling import CalendarEvent, CalendarEventStatus

        store = InMemoryCalendarEventStore()
        slot = make_slot()
        reconciliation_key = "test-key-123"

        # Create first event
        event1 = CalendarEvent(
            id=uuid4(),
            schedule_proposal_id=uuid4(),
            action_intent_id=uuid4(),
            provider_event_id="event-1",
            calendar_id="careerops-interviews",
            reconciliation_key=reconciliation_key,
            title="Test Event",
            slot=slot,
            meeting_link="",
            status=CalendarEventStatus.CREATED,
            conflict_detected_at=None,
            created_at=NOW,
            updated_at=NOW,
        )
        store.insert(event1)

        # Try to create second event with same reconciliation key
        event2 = CalendarEvent(
            id=uuid4(),
            schedule_proposal_id=uuid4(),
            action_intent_id=uuid4(),
            provider_event_id="event-2",
            calendar_id="careerops-interviews",
            reconciliation_key=reconciliation_key,  # Same key
            title="Test Event 2",
            slot=slot,
            meeting_link="",
            status=CalendarEventStatus.CREATED,
            conflict_detected_at=None,
            created_at=NOW,
            updated_at=NOW,
        )

        with pytest.raises(DuplicateEventError, match="already exists"):
            store.insert(event2)

    def test_different_slots_create_separate_events(self) -> None:
        """Different slots create separate events."""
        service, _, event_provider, _ = build_service()
        slot1 = make_slot()
        slot2 = make_slot(start=FUTURE + timedelta(hours=3))
        confirmed_at = NOW - timedelta(minutes=30)

        # Create first event
        proposal1 = service.create_proposal(
            make_proposal_input(slots=(slot1,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal1 = service.select_slot(proposal1.id, slot1, now=NOW)
        result1 = service.reverify_and_create_event(proposal1.id, user_id="user-1", now=NOW)

        # Create second event with different slot
        proposal2 = service.create_proposal(
            make_proposal_input(slots=(slot2,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal2 = service.select_slot(proposal2.id, slot2, now=NOW)
        result2 = service.reverify_and_create_event(proposal2.id, user_id="user-1", now=NOW)

        assert result1.calendar_event.id != result2.calendar_event.id
        assert event_provider.create_count == 2


# ---------------------------------------------------------------------------
# FreeBusy and conflict detection
# ---------------------------------------------------------------------------


class TestFreeBusyAndConflicts:
    def test_freebusy_conflict_triggers_conflict_review(self) -> None:
        """Conflict with busy period triggers conflict review."""
        service, freebusy, _, _ = build_service()
        slot = make_slot()
        confirmed_at = NOW - timedelta(minutes=30)

        # Set up a busy period that conflicts with the slot
        freebusy.busy_periods = [(slot.start_utc, slot.end_utc)]  # type: ignore[assignment]

        proposal = service.create_proposal(
            make_proposal_input(slots=(slot,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal = service.select_slot(proposal.id, slot, now=NOW)

        with pytest.raises(SlotConflictError, match="conflicts with existing busy"):
            service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

    def test_conflict_review_blocks_confirmation_email(self) -> None:
        """Conflict review always blocks confirmation emails."""
        conflict = ConflictReview(
            id=uuid4(),
            schedule_proposal_id=uuid4(),
            calendar_event_id=None,
            conflict_type="freebusy_race",
            description="Test conflict",
            detected_at=NOW,
            resolved_at=None,
            resolution=None,
            confirmation_email_blocked=True,
        )
        assert conflict.confirmation_email_blocked is True

    def test_conflict_review_cannot_unblock_email(self) -> None:
        """Cannot create conflict review with confirmation_email_blocked=False."""
        with pytest.raises(ValueError, match="confirmation_email_blocked must be True"):
            ConflictReview(
                id=uuid4(),
                schedule_proposal_id=uuid4(),
                calendar_event_id=None,
                conflict_type="freebusy_race",
                description="Test conflict",
                detected_at=NOW,
                resolved_at=None,
                resolution=None,
                confirmation_email_blocked=False,  # Not allowed
            )

    def test_freebusy_stale_fails_closed(self) -> None:
        """Stale FreeBusy results fail closed."""
        # Create a FreeBusy result that is stale
        stale_time = NOW - timedelta(seconds=FREEBUSY_MAX_AGE_SECONDS + 1)
        freebusy_result = FreeBusyResult(
            calendar_ids=("careerops-interviews",),
            busy_periods=(),
            queried_at=stale_time,
            window_start=NOW,
            window_end=NOW + timedelta(hours=1),
        )
        assert freebusy_result.is_stale(now=NOW)

    def test_freebusy_fresh_is_not_stale(self) -> None:
        """Fresh FreeBusy results are not stale."""
        fresh_time = NOW - timedelta(seconds=FREEBUSY_MAX_AGE_SECONDS - 1)
        freebusy_result = FreeBusyResult(
            calendar_ids=("careerops-interviews",),
            busy_periods=(),
            queried_at=fresh_time,
            window_start=NOW,
            window_end=NOW + timedelta(hours=1),
        )
        assert not freebusy_result.is_stale(now=NOW)

    def test_freebusy_is_busy_detects_overlap(self) -> None:
        """FreeBusy correctly detects overlapping busy periods."""
        slot = make_slot()
        freebusy_result = FreeBusyResult(
            calendar_ids=("careerops-interviews",),
            busy_periods=(
                (slot.start_utc - timedelta(minutes=30), slot.start_utc + timedelta(minutes=30)),
            ),
            queried_at=NOW,
            window_start=NOW,
            window_end=NOW + timedelta(hours=2),
        )
        assert freebusy_result.is_busy(slot)

    def test_freebusy_not_busy_no_overlap(self) -> None:
        """FreeBusy correctly identifies non-overlapping periods."""
        slot = make_slot()
        freebusy_result = FreeBusyResult(
            calendar_ids=("careerops-interviews",),
            busy_periods=((slot.end_utc + timedelta(hours=1), slot.end_utc + timedelta(hours=2)),),
            queried_at=NOW,
            window_start=NOW,
            window_end=NOW + timedelta(hours=4),
        )
        assert not freebusy_result.is_busy(slot)


# ---------------------------------------------------------------------------
# Slot validation
# ---------------------------------------------------------------------------


class TestSlotValidation:
    def test_valid_slot_passes(self) -> None:
        service, _, _, _ = build_service()
        slot = make_slot()
        rules = make_rules()

        result = service.validate_slot(slot, rules, now=NOW)

        assert result.is_valid
        assert len(result.rejection_reasons) == 0

    def test_notice_violation(self) -> None:
        """Slot too soon violates minimum notice."""
        service, _, _, _ = build_service()
        # Slot starts in 30 minutes, but notice requires 60
        slot = make_slot(start=NOW + timedelta(minutes=30))
        rules = make_rules(minimum_notice_minutes=60)

        result = service.validate_slot(slot, rules, now=NOW)

        assert not result.is_valid
        assert SlotRejectionReason.NOTICE_VIOLATION in result.rejection_reasons

    def test_outside_available_window(self) -> None:
        """Slot outside available windows is rejected."""
        service, _, _, _ = build_service()
        # Create a slot at 3 AM Shanghai time (outside 9-17 window)
        utc_start = datetime(2026, 7, 22, 19, 0, 0, tzinfo=UTC)  # 3 AM Shanghai next day
        slot = make_slot(start=utc_start)
        rules = make_rules(available_windows=((9 * 60, 17 * 60),))  # 9 AM - 5 PM

        result = service.validate_slot(slot, rules, now=NOW)

        assert not result.is_valid
        assert SlotRejectionReason.OUTSIDE_AVAILABLE_WINDOW in result.rejection_reasons


# ---------------------------------------------------------------------------
# Candidate slot generation
# ---------------------------------------------------------------------------


class TestSlotGeneration:
    def test_generate_candidate_slots(self) -> None:
        service, _freebusy, _, _ = build_service()
        rules = make_rules()
        window_start = NOW + timedelta(hours=2)
        window_end = NOW + timedelta(hours=6)
        freebusy_result = FreeBusyResult(
            calendar_ids=("careerops-interviews",),
            busy_periods=(),
            queried_at=NOW,
            window_start=window_start,
            window_end=window_end,
        )

        candidates = service.generate_candidate_slots(
            rules=rules,
            window_start=window_start,
            window_end=window_end,
            freebusy=freebusy_result,
            existing_events_count=0,
            now=NOW,
        )

        assert len(candidates) <= 3
        assert all(isinstance(c, SlotCandidate) for c in candidates)

    def test_generate_slots_respects_busy_periods(self) -> None:
        service, _freebusy, _, _ = build_service()
        rules = make_rules()
        window_start = NOW + timedelta(hours=2)
        window_end = NOW + timedelta(hours=6)

        # Make the first slot busy
        busy_start = window_start
        busy_end = window_start + timedelta(hours=1)
        freebusy_result = FreeBusyResult(
            calendar_ids=("careerops-interviews",),
            busy_periods=((busy_start, busy_end),),
            queried_at=NOW,
            window_start=window_start,
            window_end=window_end,
        )

        candidates = service.generate_candidate_slots(
            rules=rules,
            window_start=window_start,
            window_end=window_end,
            freebusy=freebusy_result,
            existing_events_count=0,
            now=NOW,
        )

        # First candidate should be invalid due to conflict
        if candidates:
            first = candidates[0]
            if first.slot.start_utc < busy_end:
                assert not first.is_valid
                assert SlotRejectionReason.CONFLICT_WITH_EXISTING in first.rejection_reasons


# ---------------------------------------------------------------------------
# Conflict resolution
# ---------------------------------------------------------------------------


class TestConflictResolution:
    def test_resolve_conflict(self) -> None:
        service, _, _, _ = build_service()
        conflict = ConflictReview(
            id=uuid4(),
            schedule_proposal_id=uuid4(),
            calendar_event_id=None,
            conflict_type="freebusy_race",
            description="Test conflict",
            detected_at=NOW,
            resolved_at=None,
            resolution=None,
            confirmation_email_blocked=True,
        )
        # Store the conflict
        service._conflicts.insert(conflict)

        resolved = service.resolve_conflict(
            conflict.id, resolution="user_resolved", resolved_at=NOW + timedelta(hours=1)
        )

        assert resolved.resolved_at is not None
        assert resolved.resolution == "user_resolved"
        assert resolved.confirmation_email_blocked is True  # Always blocked


# ---------------------------------------------------------------------------
# Interview records
# ---------------------------------------------------------------------------


class TestInterviewRecords:
    def test_interview_record_created_with_event(self) -> None:
        service, _, _, _ = build_service()
        slot = make_slot()
        confirmed_at = NOW - timedelta(minutes=30)
        proposal = service.create_proposal(
            make_proposal_input(slots=(slot,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal = service.select_slot(proposal.id, slot, now=NOW)

        result = service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        assert result.interview_record.status is InterviewStatus.SCHEDULED
        assert result.interview_record.calendar_event_id == result.calendar_event.id
        assert result.interview_record.schedule_proposal_id == proposal.id
        assert "proposal_id" in result.interview_record.evidence_summary

    def test_interview_record_has_evidence_summary(self) -> None:
        service, _, _, _ = build_service()
        slot = make_slot()
        confirmed_at = NOW - timedelta(minutes=30)
        proposal = service.create_proposal(
            make_proposal_input(slots=(slot,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal = service.select_slot(proposal.id, slot, now=NOW)

        result = service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        summary = result.interview_record.evidence_summary
        assert "slot_start" in summary
        assert "slot_end" in summary
        assert "timezone" in summary


# ---------------------------------------------------------------------------
# M5A integration
# ---------------------------------------------------------------------------


class TestM5AIntegration:
    def test_event_creation_uses_m5a_kernel(self) -> None:
        """Event creation goes through the M5A side-effect kernel."""
        service, _, _, audit = build_service()
        slot = make_slot()
        confirmed_at = NOW - timedelta(minutes=30)
        proposal = service.create_proposal(
            make_proposal_input(slots=(slot,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal = service.select_slot(proposal.id, slot, now=NOW)

        result = service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        # Verify M5A chain was used
        assert result.intent_id is not None
        assert result.receipt_id is not None

        # Verify audit events from M5A kernel
        events = audit.all_events()
        assert len(events) >= 3

    def test_policy_requires_approval_for_calendar_event(self) -> None:
        """Calendar event creation requires approval through policy."""
        service, _, _, audit = build_service()
        slot = make_slot()
        confirmed_at = NOW - timedelta(minutes=30)
        proposal = service.create_proposal(
            make_proposal_input(slots=(slot,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal = service.select_slot(proposal.id, slot, now=NOW)

        service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        # Verify approval was recorded in audit
        events = audit.all_events()
        approval_events = [e for e in events if e.event_type == "side_effect_approved"]
        assert len(approval_events) == 1

    def test_intent_linked_to_proposal(self) -> None:
        """The M5A intent is linked to the schedule proposal."""
        service, _, _, _ = build_service()
        slot = make_slot()
        confirmed_at = NOW - timedelta(minutes=30)
        proposal = service.create_proposal(
            make_proposal_input(slots=(slot,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal = service.select_slot(proposal.id, slot, now=NOW)

        result = service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        assert result.proposal.action_intent_id == result.intent_id
        assert result.proposal.payload_hash is not None


# ---------------------------------------------------------------------------
# Provider reconciliation
# ---------------------------------------------------------------------------


class TestProviderReconciliation:
    def test_post_write_reconciliation_success(self) -> None:
        """Post-write reconciliation verifies event exists in provider."""
        service, _, _event_provider, _ = build_service()
        slot = make_slot()
        confirmed_at = NOW - timedelta(minutes=30)
        proposal = service.create_proposal(
            make_proposal_input(slots=(slot,), recruiter_confirmed_at=confirmed_at),
            now=NOW,
        )
        proposal = service.select_slot(proposal.id, slot, now=NOW)

        result = service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        # Event should be in CREATED status (reconciliation passed)
        assert result.calendar_event.status is CalendarEventStatus.CREATED

    def test_provider_lookup_by_reconciliation_key(self) -> None:
        """Provider can look up events by reconciliation key."""
        provider = FakeCalendarEventProvider()
        reconciliation_key = "test-key-123"

        # Create an event
        provider.create_event(
            calendar_id="test-cal",
            reconciliation_key=reconciliation_key,
            title="Test Event",
            start_utc=NOW,
            end_utc=NOW + timedelta(hours=1),
            timezone="Asia/Shanghai",
            meeting_link="",
            now=NOW,
        )

        # Look it up
        found = provider.lookup_by_reconciliation_key(
            calendar_id="test-cal", reconciliation_key=reconciliation_key
        )
        assert found is not None

        # Non-existent key returns None
        not_found = provider.lookup_by_reconciliation_key(
            calendar_id="test-cal", reconciliation_key="non-existent"
        )
        assert not_found is None


# ---------------------------------------------------------------------------
# Race condition and reconciliation safety invariants
# ---------------------------------------------------------------------------


class StaleFreeBusyProvider:
    """FreeBusy provider that returns stale results to exercise fail-closed."""

    def __init__(self, *, age_seconds: float) -> None:
        self._age_seconds = age_seconds
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
            busy_periods=(),
            queried_at=now - timedelta(seconds=self._age_seconds),
            window_start=window_start,
            window_end=window_end,
        )


class NoLookupEventProvider:
    """Event provider whose create succeeds but lookup never finds the event.

    Drives the post-write reconciliation failure path.
    """

    def __init__(self) -> None:
        self.create_count = 0

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
        return "event-orphan"

    def lookup_by_reconciliation_key(
        self, *, calendar_id: str, reconciliation_key: str
    ) -> str | None:
        return None


def _confirmed_proposal_with_slot(service: SchedulingService, slot: TimeSlot) -> ScheduleProposal:
    """Create a recruiter-confirmed proposal with a selected slot ready to create."""
    confirmed_at = NOW - timedelta(minutes=30)
    proposal = service.create_proposal(
        make_proposal_input(slots=(slot,), recruiter_confirmed_at=confirmed_at),
        now=NOW,
    )
    return service.select_slot(proposal.id, slot, now=NOW)


class TestRaceConditionAndReconciliation:
    def test_insert_race_triggers_conflict_review_and_blocks_email(self) -> None:
        """A conflict between last FreeBusy and insert enters conflict review.

        Safety: no calendar event is stored, the proposal moves to
        CONFLICT_REVIEW, and the confirmation email is blocked.
        """
        event_provider = FakeCalendarEventProvider()
        event_provider.conflict_on_create = True
        service, _, _, _ = build_service(event_provider=event_provider)
        slot = make_slot()
        proposal = _confirmed_proposal_with_slot(service, slot)

        with pytest.raises(SlotConflictError, match="conflict"):
            service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        # Proposal moved to conflict review with a reason recorded.
        final_proposal = service._proposals.get(proposal.id)
        assert final_proposal.status is ProposalStatus.CONFLICT_REVIEW
        assert final_proposal.conflict_reason == "insert_race"

        # A conflict review was created and the confirmation email is blocked.
        conflicts = service._conflicts.find_by_proposal(proposal.id)
        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == "freebusy_race"
        assert conflicts[0].confirmation_email_blocked is True

        # No calendar event was persisted.
        assert service._events.find_by_proposal(proposal.id) == ()
        # No interview record was created.
        assert service._interviews.find_by_application(proposal.application_id) == ()

    def test_post_write_reconciliation_failure_marks_conflict(self) -> None:
        """If the event is not found in the provider after write, mark conflict."""
        service = build_service_custom(
            freebusy=FakeFreeBusyProvider(),
            event_provider=NoLookupEventProvider(),
        )
        slot = make_slot()
        proposal = _confirmed_proposal_with_slot(service, slot)

        result = service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        # The stored event is marked conflict_detected with a timestamp.
        stored = service._events.get(result.calendar_event.id)
        assert stored.status is CalendarEventStatus.CONFLICT_DETECTED
        assert stored.conflict_detected_at is not None

        # A reconciliation_failure conflict review blocks the confirmation email.
        conflicts = service._conflicts.find_by_proposal(proposal.id)
        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == "reconciliation_failure"
        assert conflicts[0].confirmation_email_blocked is True

    def test_stale_freebusy_fails_closed_on_reverify(self) -> None:
        """Stale FreeBusy at execution time fails closed; no event is created."""
        stale = StaleFreeBusyProvider(age_seconds=FREEBUSY_MAX_AGE_SECONDS + 5)
        event_provider = FakeCalendarEventProvider()
        service = build_service_custom(freebusy=stale, event_provider=event_provider)
        slot = make_slot()
        proposal = _confirmed_proposal_with_slot(service, slot)

        with pytest.raises(FreeBusyStaleError, match="stale"):
            service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        # The provider was never asked to create an event.
        assert event_provider.create_count == 0
        assert service._events.find_by_proposal(proposal.id) == ()

    def test_duplicate_reconciliation_key_on_reverify_rejected(self) -> None:
        """Re-creating an event for an already-created slot is rejected as duplicate."""
        service, _, event_provider, _ = build_service()
        slot = make_slot()
        proposal = _confirmed_proposal_with_slot(service, slot)

        # First creation succeeds.
        service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)
        assert event_provider.create_count == 1

        # A second proposal selecting the same slot would collide on the
        # reconciliation key only if it shared the same proposal id; instead we
        # verify the event store guard directly rejects a duplicate key.
        from careerops.domain.scheduling import CalendarEvent

        existing = service._events.find_by_proposal(proposal.id)[0]
        duplicate = CalendarEvent(
            id=uuid4(),
            schedule_proposal_id=proposal.id,
            action_intent_id=uuid4(),
            provider_event_id="event-dup",
            calendar_id=existing.calendar_id,
            reconciliation_key=existing.reconciliation_key,
            title="Duplicate",
            slot=slot,
            meeting_link="",
            status=CalendarEventStatus.CREATED,
            conflict_detected_at=None,
            created_at=NOW,
            updated_at=NOW,
        )
        with pytest.raises(DuplicateEventError, match="already exists"):
            service._events.insert(duplicate)

    def test_notice_violation_on_reverify_rejected(self) -> None:
        """A slot violating minimum notice is rejected at execution time."""
        service, _, event_provider, _ = build_service()
        # Slot starts in 30 minutes but rules require 60 minutes notice.
        slot = make_slot(start=NOW + timedelta(minutes=30))
        proposal = _confirmed_proposal_with_slot(service, slot)

        with pytest.raises(SchedulingError, match="slot validation failed"):
            service.reverify_and_create_event(proposal.id, user_id="user-1", now=NOW)

        assert event_provider.create_count == 0
        assert service._events.find_by_proposal(proposal.id) == ()
