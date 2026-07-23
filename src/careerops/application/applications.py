"""M3 application service: application lifecycle, resume versions, packages, follow-ups."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from careerops.domain.applications import (
    FOLLOW_UP_CANCEL_STATES,
    Application,
    ApplicationEvent,
    ApplicationEventSource,
    ApplicationEventType,
    ApplicationPackage,
    ApplicationState,
    FollowUpReminder,
    FollowUpState,
    IllegalTransitionError,
    PackageApprovalState,
    PackageClaim,
    ResumeVersion,
    validate_transition,
)

FOLLOW_UP_RULE_VERSION = "m3-followup-v1"
DEFAULT_FOLLOW_UP_BUSINESS_DAYS = 5


@dataclass(frozen=True, slots=True)
class ApplicationCreateRequest:
    candidate_id: UUID
    canonical_job_id: UUID
    apply_url: str = ""


@dataclass(frozen=True, slots=True)
class StateTransitionRequest:
    application_id: UUID
    to_state: ApplicationState
    source: ApplicationEventSource = ApplicationEventSource.USER
    actor_id: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class ResumeVersionCreateRequest:
    candidate_id: UUID
    file_reference: str
    content_hash: str
    target_type: str = "general"
    human_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class PackageCreateRequest:
    application_id: UUID
    resume_version_id: UUID
    cover_letter_text: str = ""
    notes: str = ""
    answers: dict[str, str] | None = None
    claims: tuple[PackageClaim, ...] = ()


@dataclass(frozen=True, slots=True)
class FollowUpScheduleRequest:
    application_id: UUID
    business_days: int = DEFAULT_FOLLOW_UP_BUSINESS_DAYS
    rule_version: str = FOLLOW_UP_RULE_VERSION


class ApplicationRepository(Protocol):
    def find_by_id(self, application_id: UUID) -> Application | None: ...

    def find_by_candidate_and_job(
        self, candidate_id: UUID, canonical_job_id: UUID
    ) -> Application | None: ...

    def save(self, application: Application) -> None: ...

    def append_event(self, event: ApplicationEvent) -> None: ...

    def get_events(self, application_id: UUID) -> list[ApplicationEvent]: ...


class ResumeRepository(Protocol):
    def find_latest_version(self, candidate_id: UUID) -> ResumeVersion | None: ...

    def save(self, version: ResumeVersion) -> None: ...


class PackageRepository(Protocol):
    def find_by_application(self, application_id: UUID) -> ApplicationPackage | None: ...

    def save(self, package: ApplicationPackage) -> None: ...


class FollowUpRepository(Protocol):
    def find_by_id(self, reminder_id: UUID) -> FollowUpReminder | None: ...

    def find_active_by_application_and_rule(
        self, application_id: UUID, rule_version: str
    ) -> FollowUpReminder | None: ...

    def save(self, reminder: FollowUpReminder) -> None: ...


class ApplicationServiceError(Exception):
    """Base error for application service operations."""


class DuplicateApplicationError(ApplicationServiceError):
    def __init__(self, candidate_id: UUID, canonical_job_id: UUID) -> None:
        super().__init__(
            f"Application already exists for candidate={candidate_id} job={canonical_job_id}"
        )


class DuplicateFollowUpError(ApplicationServiceError):
    def __init__(self, application_id: UUID, rule_version: str) -> None:
        super().__init__(
            f"Active follow-up already exists for application={application_id} rule={rule_version}"
        )


def compute_follow_up_due_date(
    submitted_at: datetime, business_days: int = DEFAULT_FOLLOW_UP_BUSINESS_DAYS
) -> datetime:
    """Compute follow-up due date using business days (Mon-Fri).

    Skips weekends. Does not account for holidays (MVP simplification).
    """
    current = submitted_at
    days_added = 0
    while days_added < business_days:
        current += timedelta(days=1)
        # Monday=0 ... Sunday=6; skip Saturday(5) and Sunday(6)
        if current.weekday() < 5:
            days_added += 1
    return current


class ApplicationService:
    """Manages application lifecycle with strict state machine enforcement.

    Invariants:
    - Illegal transitions are rejected
    - Event history is complete (every state change produces an event)
    - No submit adapter exists (only manual submission recording)
    - One active follow-up per application per rule version
    """

    def __init__(
        self,
        application_repo: ApplicationRepository,
        resume_repo: ResumeRepository,
        package_repo: PackageRepository,
        follow_up_repo: FollowUpRepository,
    ) -> None:
        self._applications = application_repo
        self._resumes = resume_repo
        self._packages = package_repo
        self._follow_ups = follow_up_repo

    def create_application(self, request: ApplicationCreateRequest, now: datetime) -> Application:
        """Create a new application in FAVORITED state."""
        existing = self._applications.find_by_candidate_and_job(
            request.candidate_id, request.canonical_job_id
        )
        if existing is not None:
            raise DuplicateApplicationError(request.candidate_id, request.canonical_job_id)

        app = Application(
            id=uuid4(),
            candidate_id=request.candidate_id,
            canonical_job_id=request.canonical_job_id,
            state=ApplicationState.FAVORITED,
            apply_url=request.apply_url,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._applications.save(app)
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=app.id,
                event_type=ApplicationEventType.CREATED,
                to_state=ApplicationState.FAVORITED,
                source=ApplicationEventSource.USER,
                occurred_at=now,
                created_at=now,
            )
        )
        return app

    def transition_state(self, request: StateTransitionRequest, now: datetime) -> Application:
        """Transition application state with validation and event recording.

        Raises IllegalTransitionError if the transition is not allowed.
        """
        app = self._applications.find_by_id(request.application_id)
        if app is None:
            raise ApplicationServiceError(f"Application not found: {request.application_id}")

        # Validate transition (raises IllegalTransitionError if illegal)
        validate_transition(app.state, request.to_state)

        new_submitted_at = app.submitted_at
        if request.to_state == ApplicationState.SUBMITTED and app.submitted_at is None:
            new_submitted_at = now

        updated = Application(
            id=app.id,
            candidate_id=app.candidate_id,
            canonical_job_id=app.canonical_job_id,
            state=request.to_state,
            apply_url=app.apply_url,
            submitted_at=new_submitted_at,
            follow_up_due_at=app.follow_up_due_at,
            version=app.version + 1,
            created_at=app.created_at,
            updated_at=now,
        )
        self._applications.save(updated)
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=app.id,
                event_type=ApplicationEventType.STATE_CHANGED,
                from_state=app.state,
                to_state=request.to_state,
                source=request.source,
                actor_id=request.actor_id,
                note=request.note,
                occurred_at=now,
                created_at=now,
            )
        )

        # Cancel follow-ups on terminal/relevant states
        if request.to_state in FOLLOW_UP_CANCEL_STATES:
            self._cancel_active_follow_ups(
                app.id, f"state_changed_to_{request.to_state.value}", now
            )

        return updated

    def record_manual_submission(
        self, application_id: UUID, actor_id: str, now: datetime
    ) -> Application:
        """Record that the user manually submitted the application.

        There is NO submit adapter. The system only opens the official
        application page and records the human submission.
        """
        app = self._applications.find_by_id(application_id)
        if app is None:
            raise ApplicationServiceError(f"Application not found: {application_id}")

        if app.state != ApplicationState.PREPARING:
            raise IllegalTransitionError(app.state, ApplicationState.SUBMITTED)

        updated = Application(
            id=app.id,
            candidate_id=app.candidate_id,
            canonical_job_id=app.canonical_job_id,
            state=ApplicationState.SUBMITTED,
            apply_url=app.apply_url,
            submitted_at=now,
            follow_up_due_at=app.follow_up_due_at,
            version=app.version + 1,
            created_at=app.created_at,
            updated_at=now,
        )
        self._applications.save(updated)
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=application_id,
                event_type=ApplicationEventType.SUBMITTED_MANUALLY,
                from_state=ApplicationState.PREPARING,
                to_state=ApplicationState.SUBMITTED,
                source=ApplicationEventSource.USER,
                actor_id=actor_id,
                note="Manual submission recorded (no submit adapter exists)",
                occurred_at=now,
                created_at=now,
            )
        )
        return updated

    def register_resume_version(
        self, request: ResumeVersionCreateRequest, now: datetime
    ) -> ResumeVersion:
        """Register a new resume version. No generation or auto-rewrite."""
        latest = self._resumes.find_latest_version(request.candidate_id)
        next_version = (latest.version_number + 1) if latest else 1

        version = ResumeVersion(
            id=uuid4(),
            candidate_id=request.candidate_id,
            version_number=next_version,
            file_reference=request.file_reference,
            content_hash=request.content_hash,
            target_type=request.target_type,
            human_confirmed=request.human_confirmed,
            created_at=now,
        )
        self._resumes.save(version)
        return version

    def create_package(self, request: PackageCreateRequest, now: datetime) -> ApplicationPackage:
        """Create an application package with evidence-bound claims.

        All positive claims must be traceable to candidate evidence.
        """
        package = ApplicationPackage(
            id=uuid4(),
            application_id=request.application_id,
            resume_version_id=request.resume_version_id,
            cover_letter_text=request.cover_letter_text,
            notes=request.notes,
            answers=request.answers or {},
            claims=request.claims,
            approval_state=PackageApprovalState.DRAFT,
            created_at=now,
            updated_at=now,
        )
        self._packages.save(package)
        return package

    def schedule_follow_up(
        self, request: FollowUpScheduleRequest, now: datetime
    ) -> FollowUpReminder:
        """Schedule a follow-up reminder. One active per application/rule.

        Raises DuplicateFollowUpError if an active follow-up already exists.
        """
        existing = self._follow_ups.find_active_by_application_and_rule(
            request.application_id, request.rule_version
        )
        if existing is not None:
            raise DuplicateFollowUpError(request.application_id, request.rule_version)

        app = self._applications.find_by_id(request.application_id)
        if app is None:
            raise ApplicationServiceError(f"Application not found: {request.application_id}")

        base_time = app.submitted_at or now
        due_at = compute_follow_up_due_date(base_time, request.business_days)

        reminder = FollowUpReminder(
            id=uuid4(),
            application_id=request.application_id,
            rule_version=request.rule_version,
            state=FollowUpState.ACTIVE,
            due_at=due_at,
            created_at=now,
            updated_at=now,
        )
        self._follow_ups.save(reminder)
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=request.application_id,
                event_type=ApplicationEventType.FOLLOW_UP_SCHEDULED,
                source=ApplicationEventSource.SYSTEM,
                event_data={"due_at": due_at.isoformat(), "rule_version": request.rule_version},
                occurred_at=now,
                created_at=now,
            )
        )
        return reminder

    def snooze_follow_up(
        self, reminder_id: UUID, snoozed_until: datetime, now: datetime
    ) -> FollowUpReminder:
        """Snooze an active follow-up reminder."""
        reminder = self._find_reminder_by_id(reminder_id)
        if reminder.state != FollowUpState.ACTIVE:
            raise ApplicationServiceError(
                f"Cannot snooze follow-up in state: {reminder.state.value}"
            )

        updated = FollowUpReminder(
            id=reminder.id,
            application_id=reminder.application_id,
            rule_version=reminder.rule_version,
            state=FollowUpState.SNOOZED,
            due_at=reminder.due_at,
            snoozed_until=snoozed_until,
            created_at=reminder.created_at,
            updated_at=now,
        )
        self._follow_ups.save(updated)
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=reminder.application_id,
                event_type=ApplicationEventType.FOLLOW_UP_SNOOZED,
                source=ApplicationEventSource.USER,
                event_data={"snoozed_until": snoozed_until.isoformat()},
                occurred_at=now,
                created_at=now,
            )
        )
        return updated

    def cancel_follow_up(self, reminder_id: UUID, reason: str, now: datetime) -> FollowUpReminder:
        """Cancel an active or snoozed follow-up reminder."""
        reminder = self._find_reminder_by_id(reminder_id)
        if reminder.state in (FollowUpState.CANCELLED, FollowUpState.COMPLETED):
            raise ApplicationServiceError(
                f"Cannot cancel follow-up in state: {reminder.state.value}"
            )

        updated = FollowUpReminder(
            id=reminder.id,
            application_id=reminder.application_id,
            rule_version=reminder.rule_version,
            state=FollowUpState.CANCELLED,
            due_at=reminder.due_at,
            snoozed_until=reminder.snoozed_until,
            cancelled_reason=reason,
            created_at=reminder.created_at,
            updated_at=now,
        )
        self._follow_ups.save(updated)
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=reminder.application_id,
                event_type=ApplicationEventType.FOLLOW_UP_CANCELLED,
                source=ApplicationEventSource.SYSTEM,
                event_data={"reason": reason},
                occurred_at=now,
                created_at=now,
            )
        )
        return updated

    def reschedule_follow_up(
        self, reminder_id: UUID, new_due_at: datetime, now: datetime
    ) -> FollowUpReminder:
        """Reschedule an active or snoozed follow-up reminder to a new due date."""
        reminder = self._find_reminder_by_id(reminder_id)
        if reminder.state in (FollowUpState.CANCELLED, FollowUpState.COMPLETED):
            raise ApplicationServiceError(
                f"Cannot reschedule follow-up in state: {reminder.state.value}"
            )

        updated = FollowUpReminder(
            id=reminder.id,
            application_id=reminder.application_id,
            rule_version=reminder.rule_version,
            state=FollowUpState.ACTIVE,
            due_at=new_due_at,
            snoozed_until=None,
            cancelled_reason="",
            created_at=reminder.created_at,
            updated_at=now,
        )
        self._follow_ups.save(updated)
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=reminder.application_id,
                event_type=ApplicationEventType.FOLLOW_UP_RESCHEDULED,
                source=ApplicationEventSource.USER,
                event_data={"new_due_at": new_due_at.isoformat()},
                occurred_at=now,
                created_at=now,
            )
        )
        return updated

    def get_event_history(self, application_id: UUID) -> list[ApplicationEvent]:
        """Get complete event history for an application."""
        return self._applications.get_events(application_id)

    def _cancel_active_follow_ups(self, application_id: UUID, reason: str, now: datetime) -> None:
        """Cancel all active follow-ups for an application."""
        reminder = self._follow_ups.find_active_by_application_and_rule(
            application_id, FOLLOW_UP_RULE_VERSION
        )
        if reminder is not None:
            updated = FollowUpReminder(
                id=reminder.id,
                application_id=reminder.application_id,
                rule_version=reminder.rule_version,
                state=FollowUpState.CANCELLED,
                due_at=reminder.due_at,
                snoozed_until=reminder.snoozed_until,
                cancelled_reason=reason,
                created_at=reminder.created_at,
                updated_at=now,
            )
            self._follow_ups.save(updated)
            self._applications.append_event(
                ApplicationEvent(
                    id=uuid4(),
                    application_id=application_id,
                    event_type=ApplicationEventType.FOLLOW_UP_CANCELLED,
                    source=ApplicationEventSource.SYSTEM,
                    event_data={"reason": reason},
                    occurred_at=now,
                    created_at=now,
                )
            )

    def _find_reminder_by_id(self, reminder_id: UUID) -> FollowUpReminder:
        """Find a reminder by ID using the follow-up repository."""
        reminder = self._follow_ups.find_by_id(reminder_id)
        if reminder is None:
            raise ApplicationServiceError(f"Follow-up reminder not found: {reminder_id}")
        return reminder
