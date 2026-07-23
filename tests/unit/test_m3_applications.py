"""Unit tests for M3: contacts, application state machine, resume versions, packages, follow-ups."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from careerops.application.applications import (
    ApplicationCreateRequest,
    ApplicationService,
    ApplicationServiceError,
    DuplicateApplicationError,
    DuplicateFollowUpError,
    FollowUpScheduleRequest,
    PackageCreateRequest,
    ResumeVersionCreateRequest,
    StateTransitionRequest,
    compute_follow_up_due_date,
)
from careerops.application.contacts import (
    ContactCreateRequest,
    ContactService,
    ContactValidationError,
)
from careerops.domain.applications import (
    LEGAL_TRANSITIONS,
    Application,
    ApplicationEvent,
    ApplicationEventType,
    ApplicationPackage,
    ApplicationState,
    FollowUpReminder,
    FollowUpState,
    IllegalTransitionError,
    PackageClaim,
    ResumeVersion,
    is_transition_legal,
    validate_transition,
)
from careerops.domain.contacts import (
    ContactAction,
    ContactConfidence,
    ContactSource,
    RecruitingContact,
    compute_allowed_actions,
)

NOW = datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC)
CANDIDATE_ID = uuid4()
JOB_ID = uuid4()
COMPANY_ID = uuid4()


# ---------------------------------------------------------------------------
# In-memory repositories
# ---------------------------------------------------------------------------


class InMemoryContactRepo:
    def __init__(self) -> None:
        self._contacts: list[RecruitingContact] = []

    def find_by_email(self, company_id: UUID, email: str) -> RecruitingContact | None:
        for c in self._contacts:
            if c.company_id == company_id and c.email == email:
                return c
        return None

    def find_by_company(self, company_id: UUID) -> list[RecruitingContact]:
        return [c for c in self._contacts if c.company_id == company_id]

    def save(self, contact: RecruitingContact) -> None:
        self._contacts.append(contact)


class InMemoryApplicationRepo:
    def __init__(self) -> None:
        self._applications: dict[UUID, Application] = {}
        self._events: list[ApplicationEvent] = []

    def find_by_id(self, application_id: UUID) -> Application | None:
        return self._applications.get(application_id)

    def find_by_candidate_and_job(
        self, candidate_id: UUID, canonical_job_id: UUID
    ) -> Application | None:
        for app in self._applications.values():
            if app.candidate_id == candidate_id and app.canonical_job_id == canonical_job_id:
                return app
        return None

    def save(self, application: Application) -> None:
        self._applications[application.id] = application

    def append_event(self, event: ApplicationEvent) -> None:
        self._events.append(event)

    def get_events(self, application_id: UUID) -> list[ApplicationEvent]:
        return [e for e in self._events if e.application_id == application_id]


class InMemoryResumeRepo:
    def __init__(self) -> None:
        self._versions: list[ResumeVersion] = []

    def find_latest_version(self, candidate_id: UUID) -> ResumeVersion | None:
        candidate_versions = [v for v in self._versions if v.candidate_id == candidate_id]
        if not candidate_versions:
            return None
        return max(candidate_versions, key=lambda v: v.version_number)

    def save(self, version: ResumeVersion) -> None:
        self._versions.append(version)


class InMemoryPackageRepo:
    def __init__(self) -> None:
        self._packages: list[ApplicationPackage] = []

    def find_by_application(self, application_id: UUID) -> ApplicationPackage | None:
        for p in self._packages:
            if p.application_id == application_id:
                return p
        return None

    def save(self, package: ApplicationPackage) -> None:
        self._packages.append(package)


class InMemoryFollowUpRepo:
    def __init__(self) -> None:
        self._reminders: dict[UUID, FollowUpReminder] = {}

    def find_by_id(self, reminder_id: UUID) -> FollowUpReminder | None:
        return self._reminders.get(reminder_id)

    def find_active_by_application_and_rule(
        self, application_id: UUID, rule_version: str
    ) -> FollowUpReminder | None:
        for r in self._reminders.values():
            if (
                r.application_id == application_id
                and r.rule_version == rule_version
                and r.state == FollowUpState.ACTIVE
            ):
                return r
        return None

    def save(self, reminder: FollowUpReminder) -> None:
        self._reminders[reminder.id] = reminder


def make_service() -> tuple[
    ApplicationService,
    InMemoryApplicationRepo,
    InMemoryResumeRepo,
    InMemoryPackageRepo,
    InMemoryFollowUpRepo,
]:
    app_repo = InMemoryApplicationRepo()
    resume_repo = InMemoryResumeRepo()
    package_repo = InMemoryPackageRepo()
    follow_up_repo = InMemoryFollowUpRepo()
    service = ApplicationService(app_repo, resume_repo, package_repo, follow_up_repo)
    return service, app_repo, resume_repo, package_repo, follow_up_repo


# ---------------------------------------------------------------------------
# Contact Tests
# ---------------------------------------------------------------------------


class TestContactCreation:
    def test_create_valid_contact(self) -> None:
        repo = InMemoryContactRepo()
        service = ContactService(repo)
        request = ContactCreateRequest(
            company_id=COMPANY_ID,
            email="recruiting@acme.com",
            name="Recruiting Team",
            source=ContactSource.CAREERS_PAGE,
            source_url="https://acme.com/careers",
            source_text="Contact us at recruiting@acme.com",
            publicly_listed=True,
            domain_match=True,
            confidence=ContactConfidence.HIGH,
        )
        contact = service.create_contact(request, NOW)
        assert contact.email == "recruiting@acme.com"
        assert contact.source_url == "https://acme.com/careers"
        assert contact.publicly_listed is True
        assert ContactAction.INITIATE_CONTACT in contact.allowed_actions

    def test_reject_missing_source_url(self) -> None:
        repo = InMemoryContactRepo()
        service = ContactService(repo)
        request = ContactCreateRequest(
            company_id=COMPANY_ID,
            email="test@acme.com",
            source_url="",
        )
        with pytest.raises(ContactValidationError, match="source_url"):
            service.create_contact(request, NOW)

    def test_reject_non_public_contact(self) -> None:
        repo = InMemoryContactRepo()
        service = ContactService(repo)
        request = ContactCreateRequest(
            company_id=COMPANY_ID,
            email="employee@acme.com",
            source_url="https://acme.com/team",
            publicly_listed=False,
        )
        with pytest.raises(ContactValidationError, match="publicly listed"):
            service.create_contact(request, NOW)

    def test_reject_invalid_email(self) -> None:
        repo = InMemoryContactRepo()
        service = ContactService(repo)
        request = ContactCreateRequest(
            company_id=COMPANY_ID,
            email="not-an-email",
            source_url="https://acme.com/careers",
        )
        with pytest.raises(ContactValidationError, match="valid email"):
            service.create_contact(request, NOW)

    def test_idempotent_creation(self) -> None:
        repo = InMemoryContactRepo()
        service = ContactService(repo)
        request = ContactCreateRequest(
            company_id=COMPANY_ID,
            email="hr@acme.com",
            source_url="https://acme.com/careers",
        )
        first = service.create_contact(request, NOW)
        second = service.create_contact(request, NOW)
        assert first.id == second.id

    def test_low_confidence_restricts_actions(self) -> None:
        repo = InMemoryContactRepo()
        service = ContactService(repo)
        request = ContactCreateRequest(
            company_id=COMPANY_ID,
            email="maybe@acme.com",
            source_url="https://acme.com/about",
            confidence=ContactConfidence.LOW,
        )
        contact = service.create_contact(request, NOW)
        assert ContactAction.INITIATE_CONTACT not in contact.allowed_actions
        assert ContactAction.DISPLAY in contact.allowed_actions
        assert ContactAction.REVIEW in contact.allowed_actions

    def test_domain_mismatch_restricts_actions(self) -> None:
        repo = InMemoryContactRepo()
        service = ContactService(repo)
        request = ContactCreateRequest(
            company_id=COMPANY_ID,
            email="recruiter@gmail.com",
            source_url="https://acme.com/careers",
            domain_match=False,
            confidence=ContactConfidence.HIGH,
        )
        contact = service.create_contact(request, NOW)
        assert ContactAction.INITIATE_CONTACT not in contact.allowed_actions
        assert not service.can_initiate_contact(contact)


class TestContactDomainModel:
    def test_requires_source_url(self) -> None:
        with pytest.raises(ValueError, match="source_url"):
            RecruitingContact(
                id=uuid4(),
                company_id=COMPANY_ID,
                email="test@acme.com",
                source_url="",
            )

    def test_requires_publicly_listed(self) -> None:
        with pytest.raises(ValueError, match="publicly listed"):
            RecruitingContact(
                id=uuid4(),
                company_id=COMPANY_ID,
                email="test@acme.com",
                source_url="https://acme.com",
                publicly_listed=False,
            )


class TestComputeAllowedActions:
    def test_high_confidence_domain_match(self) -> None:
        actions = compute_allowed_actions(ContactConfidence.HIGH, domain_match=True)
        assert ContactAction.INITIATE_CONTACT in actions

    def test_medium_confidence(self) -> None:
        actions = compute_allowed_actions(ContactConfidence.MEDIUM, domain_match=True)
        assert ContactAction.DRAFT_REPLY in actions
        assert ContactAction.INITIATE_CONTACT not in actions

    def test_low_confidence(self) -> None:
        actions = compute_allowed_actions(ContactConfidence.LOW, domain_match=True)
        assert actions == (ContactAction.DISPLAY, ContactAction.REVIEW)

    def test_domain_mismatch(self) -> None:
        actions = compute_allowed_actions(ContactConfidence.HIGH, domain_match=False)
        assert actions == (ContactAction.DISPLAY, ContactAction.REVIEW)


# ---------------------------------------------------------------------------
# Application State Machine Tests
# ---------------------------------------------------------------------------


class TestApplicationStateMachine:
    def test_all_legal_transitions_defined(self) -> None:
        for state in ApplicationState:
            assert state in LEGAL_TRANSITIONS

    def test_terminal_states_have_no_transitions(self) -> None:
        assert LEGAL_TRANSITIONS[ApplicationState.REJECTED] == frozenset()
        assert LEGAL_TRANSITIONS[ApplicationState.WITHDRAWN] == frozenset()

    def test_validate_legal_transition(self) -> None:
        validate_transition(ApplicationState.FAVORITED, ApplicationState.PREPARING)

    def test_validate_illegal_transition_raises(self) -> None:
        with pytest.raises(IllegalTransitionError):
            validate_transition(ApplicationState.FAVORITED, ApplicationState.SUBMITTED)

    def test_is_transition_legal(self) -> None:
        assert is_transition_legal(ApplicationState.PREPARING, ApplicationState.SUBMITTED)
        assert not is_transition_legal(ApplicationState.REJECTED, ApplicationState.SUBMITTED)

    def test_illegal_transition_error_message(self) -> None:
        err = IllegalTransitionError(ApplicationState.FAVORITED, ApplicationState.OFFER)
        assert "favorited" in str(err)
        assert "offer" in str(err)
        assert err.from_state == ApplicationState.FAVORITED
        assert err.to_state == ApplicationState.OFFER


class TestApplicationService:
    def test_create_application(self) -> None:
        service, _, _, _, _ = make_service()
        request = ApplicationCreateRequest(
            candidate_id=CANDIDATE_ID,
            canonical_job_id=JOB_ID,
            apply_url="https://acme.com/apply/123",
        )
        app = service.create_application(request, NOW)
        assert app.state == ApplicationState.FAVORITED
        assert app.version == 1
        assert app.apply_url == "https://acme.com/apply/123"

    def test_create_application_records_event(self) -> None:
        service, _, _, _, _ = make_service()
        request = ApplicationCreateRequest(candidate_id=CANDIDATE_ID, canonical_job_id=JOB_ID)
        app = service.create_application(request, NOW)
        events = service.get_event_history(app.id)
        assert len(events) == 1
        assert events[0].event_type == ApplicationEventType.CREATED
        assert events[0].to_state == ApplicationState.FAVORITED

    def test_duplicate_application_rejected(self) -> None:
        service, _, _, _, _ = make_service()
        request = ApplicationCreateRequest(candidate_id=CANDIDATE_ID, canonical_job_id=JOB_ID)
        service.create_application(request, NOW)
        with pytest.raises(DuplicateApplicationError):
            service.create_application(request, NOW)

    def test_legal_transition_updates_state(self) -> None:
        service, _, _, _, _ = make_service()
        app = service.create_application(
            ApplicationCreateRequest(candidate_id=CANDIDATE_ID, canonical_job_id=JOB_ID),
            NOW,
        )
        updated = service.transition_state(
            StateTransitionRequest(
                application_id=app.id,
                to_state=ApplicationState.PREPARING,
            ),
            NOW,
        )
        assert updated.state == ApplicationState.PREPARING
        assert updated.version == 2

    def test_illegal_transition_rejected(self) -> None:
        service, _, _, _, _ = make_service()
        app = service.create_application(
            ApplicationCreateRequest(candidate_id=CANDIDATE_ID, canonical_job_id=JOB_ID),
            NOW,
        )
        with pytest.raises(IllegalTransitionError):
            service.transition_state(
                StateTransitionRequest(
                    application_id=app.id,
                    to_state=ApplicationState.OFFER,
                ),
                NOW,
            )

    def test_event_history_complete(self) -> None:
        service, _, _, _, _ = make_service()
        app = service.create_application(
            ApplicationCreateRequest(candidate_id=CANDIDATE_ID, canonical_job_id=JOB_ID),
            NOW,
        )
        service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.PREPARING),
            NOW,
        )
        service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.SUBMITTED),
            NOW + timedelta(hours=1),
        )
        events = service.get_event_history(app.id)
        assert len(events) == 3
        assert events[0].event_type == ApplicationEventType.CREATED
        assert events[1].event_type == ApplicationEventType.STATE_CHANGED
        assert events[1].from_state == ApplicationState.FAVORITED
        assert events[1].to_state == ApplicationState.PREPARING
        assert events[2].event_type == ApplicationEventType.STATE_CHANGED
        assert events[2].from_state == ApplicationState.PREPARING
        assert events[2].to_state == ApplicationState.SUBMITTED

    def test_manual_submission(self) -> None:
        service, _, _, _, _ = make_service()
        app = service.create_application(
            ApplicationCreateRequest(candidate_id=CANDIDATE_ID, canonical_job_id=JOB_ID),
            NOW,
        )
        service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.PREPARING),
            NOW,
        )
        submitted = service.record_manual_submission(app.id, "user", NOW + timedelta(hours=2))
        assert submitted.state == ApplicationState.SUBMITTED
        assert submitted.submitted_at == NOW + timedelta(hours=2)
        events = service.get_event_history(app.id)
        submit_events = [
            e for e in events if e.event_type == ApplicationEventType.SUBMITTED_MANUALLY
        ]
        assert len(submit_events) == 1
        assert "no submit adapter" in submit_events[0].note

    def test_manual_submission_from_wrong_state(self) -> None:
        service, _, _, _, _ = make_service()
        app = service.create_application(
            ApplicationCreateRequest(candidate_id=CANDIDATE_ID, canonical_job_id=JOB_ID),
            NOW,
        )
        with pytest.raises(IllegalTransitionError):
            service.record_manual_submission(app.id, "user", NOW)

    def test_full_lifecycle(self) -> None:
        service, _, _, _, _ = make_service()
        app = service.create_application(
            ApplicationCreateRequest(candidate_id=CANDIDATE_ID, canonical_job_id=JOB_ID),
            NOW,
        )
        # favorited -> preparing -> submitted -> interviewing -> offer
        service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.PREPARING),
            NOW,
        )
        service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.SUBMITTED),
            NOW,
        )
        service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.INTERVIEWING),
            NOW,
        )
        final = service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.OFFER),
            NOW,
        )
        assert final.state == ApplicationState.OFFER
        events = service.get_event_history(app.id)
        assert len(events) == 5  # created + 4 transitions


# ---------------------------------------------------------------------------
# Resume Version Tests
# ---------------------------------------------------------------------------


class TestResumeVersions:
    def test_register_first_version(self) -> None:
        service, _, _, _, _ = make_service()
        version = service.register_resume_version(
            ResumeVersionCreateRequest(
                candidate_id=CANDIDATE_ID,
                file_reference="sha256/ab/cd/abcdef1234567890",
                content_hash="a" * 64,
                target_type="general",
                human_confirmed=True,
            ),
            NOW,
        )
        assert version.version_number == 1
        assert version.human_confirmed is True

    def test_register_increments_version(self) -> None:
        service, _, _, _, _ = make_service()
        v1 = service.register_resume_version(
            ResumeVersionCreateRequest(
                candidate_id=CANDIDATE_ID,
                file_reference="ref1",
                content_hash="a" * 64,
            ),
            NOW,
        )
        v2 = service.register_resume_version(
            ResumeVersionCreateRequest(
                candidate_id=CANDIDATE_ID,
                file_reference="ref2",
                content_hash="b" * 64,
            ),
            NOW,
        )
        assert v1.version_number == 1
        assert v2.version_number == 2


# ---------------------------------------------------------------------------
# Application Package Tests
# ---------------------------------------------------------------------------


class TestApplicationPackages:
    def test_create_package_with_claims(self) -> None:
        service, _, _, _, _ = make_service()
        evidence_id = uuid4()
        package = service.create_package(
            PackageCreateRequest(
                application_id=uuid4(),
                resume_version_id=uuid4(),
                cover_letter_text="Dear Hiring Manager...",
                claims=(
                    PackageClaim(
                        claim_text="5 years Python experience",
                        evidence_ids=(evidence_id,),
                    ),
                ),
            ),
            NOW,
        )
        assert len(package.claims) == 1
        assert package.claims[0].evidence_ids == (evidence_id,)

    def test_claim_without_evidence_rejected(self) -> None:
        with pytest.raises(ValueError, match="traceable to evidence"):
            PackageClaim(claim_text="Expert in Python", evidence_ids=())

    def test_package_claims_traceable_to_evidence(self) -> None:
        evidence_id_1 = uuid4()
        evidence_id_2 = uuid4()
        claim = PackageClaim(
            claim_text="Built distributed systems",
            evidence_ids=(evidence_id_1, evidence_id_2),
        )
        assert len(claim.evidence_ids) == 2
        assert evidence_id_1 in claim.evidence_ids
        assert evidence_id_2 in claim.evidence_ids


# ---------------------------------------------------------------------------
# Follow-up Tests
# ---------------------------------------------------------------------------


class TestFollowUps:
    def _create_submitted_app(self, service: ApplicationService) -> Application:
        app = service.create_application(
            ApplicationCreateRequest(candidate_id=CANDIDATE_ID, canonical_job_id=JOB_ID),
            NOW,
        )
        service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.PREPARING),
            NOW,
        )
        return service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.SUBMITTED),
            NOW,
        )

    def test_schedule_follow_up(self) -> None:
        service, _, _, _, _ = make_service()
        app = self._create_submitted_app(service)
        reminder = service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id, business_days=5),
            NOW,
        )
        assert reminder.state == FollowUpState.ACTIVE
        assert reminder.due_at is not None

    def test_duplicate_follow_up_rejected(self) -> None:
        service, _, _, _, _ = make_service()
        app = self._create_submitted_app(service)
        service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id),
            NOW,
        )
        with pytest.raises(DuplicateFollowUpError):
            service.schedule_follow_up(
                FollowUpScheduleRequest(application_id=app.id),
                NOW,
            )

    def test_follow_up_cancelled_on_rejection(self) -> None:
        service, _, _, _, follow_up_repo = make_service()
        app = self._create_submitted_app(service)
        service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id),
            NOW,
        )
        # Transition to rejected should cancel follow-up
        service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.REJECTED),
            NOW + timedelta(days=1),
        )
        # Verify no active follow-up remains
        active = follow_up_repo.find_active_by_application_and_rule(app.id, "m3-followup-v1")
        assert active is None

    def test_follow_up_cancelled_on_withdrawn(self) -> None:
        service, _, _, _, follow_up_repo = make_service()
        app = self._create_submitted_app(service)
        service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id),
            NOW,
        )
        service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.WITHDRAWN),
            NOW + timedelta(days=1),
        )
        active = follow_up_repo.find_active_by_application_and_rule(app.id, "m3-followup-v1")
        assert active is None

    def test_follow_up_cancelled_on_offer(self) -> None:
        service, _, _, _, follow_up_repo = make_service()
        app = self._create_submitted_app(service)
        service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id),
            NOW,
        )
        service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.OFFER),
            NOW + timedelta(days=1),
        )
        active = follow_up_repo.find_active_by_application_and_rule(app.id, "m3-followup-v1")
        assert active is None

    def test_snooze_follow_up(self) -> None:
        service, _, _, _, _ = make_service()
        app = self._create_submitted_app(service)
        reminder = service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id),
            NOW,
        )
        snoozed_until = NOW + timedelta(days=3)
        snoozed = service.snooze_follow_up(reminder.id, snoozed_until, NOW)
        assert snoozed.state == FollowUpState.SNOOZED
        assert snoozed.snoozed_until == snoozed_until

    def test_snooze_non_active_raises(self) -> None:
        service, _, _, _, _ = make_service()
        app = self._create_submitted_app(service)
        reminder = service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id),
            NOW,
        )
        service.cancel_follow_up(reminder.id, "test cancel", NOW)
        with pytest.raises(ApplicationServiceError, match="Cannot snooze"):
            service.snooze_follow_up(reminder.id, NOW + timedelta(days=1), NOW)

    def test_cancel_follow_up(self) -> None:
        service, _, _, _, _ = make_service()
        app = self._create_submitted_app(service)
        reminder = service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id),
            NOW,
        )
        cancelled = service.cancel_follow_up(reminder.id, "no longer needed", NOW)
        assert cancelled.state == FollowUpState.CANCELLED
        assert cancelled.cancelled_reason == "no longer needed"

    def test_cancel_already_cancelled_raises(self) -> None:
        service, _, _, _, _ = make_service()
        app = self._create_submitted_app(service)
        reminder = service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id),
            NOW,
        )
        service.cancel_follow_up(reminder.id, "first cancel", NOW)
        with pytest.raises(ApplicationServiceError, match="Cannot cancel"):
            service.cancel_follow_up(reminder.id, "second cancel", NOW)

    def test_reschedule_follow_up(self) -> None:
        service, _, _, _, _ = make_service()
        app = self._create_submitted_app(service)
        reminder = service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id),
            NOW,
        )
        new_due = NOW + timedelta(days=10)
        rescheduled = service.reschedule_follow_up(reminder.id, new_due, NOW)
        assert rescheduled.state == FollowUpState.ACTIVE
        assert rescheduled.due_at == new_due

    def test_reschedule_cancelled_raises(self) -> None:
        service, _, _, _, _ = make_service()
        app = self._create_submitted_app(service)
        reminder = service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id),
            NOW,
        )
        service.cancel_follow_up(reminder.id, "done", NOW)
        with pytest.raises(ApplicationServiceError, match="Cannot reschedule"):
            service.reschedule_follow_up(reminder.id, NOW + timedelta(days=5), NOW)

    def test_snooze_records_event(self) -> None:
        service, _, _, _, _ = make_service()
        app = self._create_submitted_app(service)
        reminder = service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id),
            NOW,
        )
        service.snooze_follow_up(reminder.id, NOW + timedelta(days=2), NOW)
        events = service.get_event_history(app.id)
        snooze_events = [
            e for e in events if e.event_type == ApplicationEventType.FOLLOW_UP_SNOOZED
        ]
        assert len(snooze_events) == 1

    def test_reschedule_records_event(self) -> None:
        service, _, _, _, _ = make_service()
        app = self._create_submitted_app(service)
        reminder = service.schedule_follow_up(
            FollowUpScheduleRequest(application_id=app.id),
            NOW,
        )
        service.reschedule_follow_up(reminder.id, NOW + timedelta(days=7), NOW)
        events = service.get_event_history(app.id)
        reschedule_events = [
            e for e in events if e.event_type == ApplicationEventType.FOLLOW_UP_RESCHEDULED
        ]
        assert len(reschedule_events) == 1

    def test_find_nonexistent_reminder_raises(self) -> None:
        service, _, _, _, _ = make_service()
        with pytest.raises(ApplicationServiceError, match="not found"):
            service.snooze_follow_up(uuid4(), NOW + timedelta(days=1), NOW)


class TestBusinessDayCalculation:
    def test_weekday_calculation(self) -> None:
        # Wednesday 2026-07-15
        wednesday = datetime(2026, 7, 15, 10, 0, 0, tzinfo=UTC)
        due = compute_follow_up_due_date(wednesday, business_days=3)
        # Wed + 1 = Thu, + 2 = Fri, + 3 = Mon (skip weekend)
        assert due.weekday() == 0  # Monday
        assert due.day == 20

    def test_friday_skips_weekend(self) -> None:
        # Friday 2026-07-17
        friday = datetime(2026, 7, 17, 10, 0, 0, tzinfo=UTC)
        due = compute_follow_up_due_date(friday, business_days=1)
        # Fri + 1 business day = Monday
        assert due.weekday() == 0  # Monday
        assert due.day == 20

    def test_one_business_day(self) -> None:
        # Monday 2026-07-13
        monday = datetime(2026, 7, 13, 10, 0, 0, tzinfo=UTC)
        due = compute_follow_up_due_date(monday, business_days=1)
        assert due.weekday() == 1  # Tuesday
        assert due.day == 14

    def test_five_business_days_from_monday(self) -> None:
        # Monday 2026-07-13
        monday = datetime(2026, 7, 13, 10, 0, 0, tzinfo=UTC)
        due = compute_follow_up_due_date(monday, business_days=5)
        # Mon+5 business days = next Monday
        assert due.weekday() == 0  # Monday
        assert due.day == 20

    def test_zero_business_days(self) -> None:
        monday = datetime(2026, 7, 13, 10, 0, 0, tzinfo=UTC)
        due = compute_follow_up_due_date(monday, business_days=0)
        assert due == monday


# ---------------------------------------------------------------------------
# No Submit Adapter Test
# ---------------------------------------------------------------------------


class TestNoSubmitAdapter:
    def test_no_submit_adapter_exists(self) -> None:
        """Verify that no submit adapter exists in the codebase.

        The system can only open the official application page and record
        manual submission. There is no automated form submission.
        """
        import pkgutil

        import careerops.adapters as adapters_pkg

        adapter_names = [name for _, name, _ in pkgutil.iter_modules(adapters_pkg.__path__)]
        # No adapter should contain 'submit' or 'apply' in its name
        for name in adapter_names:
            assert "submit" not in name.lower(), f"Submit adapter found: {name}"
            assert "apply_form" not in name.lower(), f"Apply form adapter found: {name}"

    def test_manual_submission_note_documents_no_adapter(self) -> None:
        service, _, _, _, _ = make_service()
        app = service.create_application(
            ApplicationCreateRequest(candidate_id=CANDIDATE_ID, canonical_job_id=JOB_ID),
            NOW,
        )
        service.transition_state(
            StateTransitionRequest(application_id=app.id, to_state=ApplicationState.PREPARING),
            NOW,
        )
        service.record_manual_submission(app.id, "user", NOW)
        events = service.get_event_history(app.id)
        submit_event = next(
            e for e in events if e.event_type == ApplicationEventType.SUBMITTED_MANUALLY
        )
        assert "no submit adapter" in submit_event.note.lower()
