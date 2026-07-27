"""Application workspace service (Section 7, tasks 7.2-7.7).

Sits on top of the M3 :class:`ApplicationService` / :class:`ApplicationRepository`
and adds the workspace concerns the new contract requires:

- **7.2** get-or-create an application bound to its application cycle, with
  authenticated candidate ownership (no client-supplied candidate substitution).
- **7.3** favorite→preparing and preparing→submission validation gates. Model
  output can NEVER drive these — every mutating method records a USER-sourced
  event and validates the legal transition.
- **7.4** channel-eligibility evaluation + selection (email / external_form /
  manual), derived from trusted job evidence; unavailable channels are blocked.
- **7.5** package-binding read/write (the concrete package implementation is
  consumed from Section 8).
- **7.6** append-only timeline projection (events now; proposals/receipts/
  reminders merge in later gates).
- **7.7** external-form / manual submission confirmation with evidence
  (apply URL + submitted time). No automatic submitted claim — an abandoned
  external form leaves the application in PREPARING.

The service is additive: it composes the existing repos rather than mutating
them, so M3 callers and tests are unaffected. All new dependencies are
optional with safe defaults so the service degrades explicitly (cycle/package
binding simply stay None) instead of failing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from careerops.application.applications import (
    ApplicationRepository,
)
from careerops.application.package_service import PackageService
from careerops.domain.application_workspace import (
    ChannelEligibilityResolver,
    ChannelEligibilitySnapshot,
    JobEvidenceChannelResolver,
    PackageBinding,
    PackageBindingStore,
    TimelineEntry,
    event_to_timeline_entry,
)
from careerops.domain.applications import (
    Application,
    ApplicationCycle,
    ApplicationEvent,
    ApplicationEventSource,
    ApplicationEventType,
    ApplicationState,
    IllegalTransitionError,
    SubmissionChannel,
    validate_transition,
)

__all__ = [
    "ApplicationNotOwnedError",
    "ApplicationWorkspaceService",
    "ChannelUnavailableError",
    "ExternalFormEvidenceError",
    "PackageServiceBindingStore",
]


class WorkspaceError(Exception):
    """Base for application-workspace service errors (translated at the API)."""


class ApplicationNotOwnedError(WorkspaceError):
    """The application does not exist or is not owned by the candidate.

    Raised for BOTH missing and not-owned so the response never leaks that an
    application exists for another candidate.
    """

    def __init__(self, application_id: UUID) -> None:
        super().__init__(f"application not found for candidate: {application_id}")


class ChannelUnavailableError(WorkspaceError):
    """The user attempted to select a channel that is not eligible."""

    def __init__(self, channel: SubmissionChannel, reason: str) -> None:
        self.channel = channel
        self.reason = reason
        super().__init__(f"channel {channel.value} is not eligible: {reason}")


class ExternalFormEvidenceError(WorkspaceError):
    """External-form confirmation is missing required evidence (apply URL)."""

    _DEFAULT_REASON = "external form confirmation requires the official apply URL"

    def __init__(self, reason: str = _DEFAULT_REASON) -> None:
        super().__init__(reason)


class _CycleRepository(Protocol):
    """Minimal cycle-repo surface the workspace needs for get-or-create binding."""

    def get_or_create_active(
        self,
        candidate_id: UUID,
        canonical_job_id: UUID,
        *,
        cycle_id: UUID,
        reason: str = "",
        now: datetime | None = None,
    ) -> ApplicationCycle: ...


class ApplicationWorkspaceService:
    """Workspace lifecycle + timeline + channel + binding service."""

    def __init__(
        self,
        application_repo: ApplicationRepository,
        *,
        cycle_repo: _CycleRepository | None = None,
        channel_resolver: ChannelEligibilityResolver | None = None,
        package_binding_store: PackageBindingStore | None = None,
        contact_lookup: Callable[[UUID, UUID], bool] | None = None,
    ) -> None:
        self._applications = application_repo
        self._cycle_repo = cycle_repo
        self._channel_resolver = channel_resolver or JobEvidenceChannelResolver()
        self._package_binding_store = package_binding_store
        # contact_lookup(candidate_id, canonical_job_id) -> has trusted contact.
        # Section 9 wires a real lookup; until then EMAIL is deferred.
        self._contact_lookup = contact_lookup

    # ------------------------------------------------------------------
    # 7.2 — get-or-create with cycle + ownership
    # ------------------------------------------------------------------

    def get_or_create_application(
        self,
        *,
        candidate_id: UUID,
        canonical_job_id: UUID,
        apply_url: str = "",
        now: datetime,
    ) -> Application:
        """Return the existing application for the pair, or create one in FAVORITED.

        Idempotent: a repeat call for the same (candidate, job) returns the
        existing application unchanged. When a cycle repository is wired, the
        application is bound to its (get-or-created) application cycle so the
        one-active-cycle invariant holds and history is preserved across
        re-application. ``candidate_id`` is always the server-resolved owner.
        """
        existing = self._applications.find_by_candidate_and_job(candidate_id, canonical_job_id)
        if existing is not None:
            return existing

        cycle_id = self._resolve_cycle_id(candidate_id, canonical_job_id, now)
        app = Application(
            id=uuid4(),
            candidate_id=candidate_id,
            canonical_job_id=canonical_job_id,
            state=ApplicationState.FAVORITED,
            apply_url=apply_url,
            cycle_id=cycle_id,
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
                actor_id=str(candidate_id),
                event_data=({"cycle_id": str(cycle_id)} if cycle_id is not None else {}),
                occurred_at=now,
                created_at=now,
            )
        )
        return app

    def get_application(self, *, application_id: UUID, candidate_id: UUID) -> Application:
        """Read an application, enforcing candidate ownership."""
        return self._require_owned(application_id, candidate_id)

    def find_owned_application(
        self, *, application_id: UUID, candidate_id: UUID
    ) -> Application | None:
        """Return the application if owned by ``candidate_id``, else ``None``.

        Additive ownership-read alias consumed by the Section-9 email-payload
        preview (which collects validation errors rather than raising). This
        does not change :meth:`get_application`; it is a non-raising variant
        that returns ``None`` for both missing and not-owned so the preview
        can surface "application not found for candidate" without an exception.
        """
        app = self._applications.find_by_id(application_id)
        if app is None or app.candidate_id != candidate_id:
            return None
        return app

    # ------------------------------------------------------------------
    # 7.3 — favorite → preparing, and preparing → submission gates
    # ------------------------------------------------------------------

    def prepare_application(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        now: datetime,
    ) -> Application:
        """Move an application from FAVORITED (or IGNORED/ON_HOLD) to PREPARING.

        This is a USER action — model output cannot trigger it. Validates the
        legal transition and records a USER-sourced state-changed event.
        """
        app = self._require_owned(application_id, candidate_id)
        validate_transition(app.state, ApplicationState.PREPARING)
        return self._record_state_change(
            app, ApplicationState.PREPARING, candidate_id=candidate_id, now=now
        )

    def apply_user_transition(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        to_state: ApplicationState,
        note: str = "",
        now: datetime,
    ) -> Application:
        """General USER-sourced state transition (e.g. ON_HOLD, WITHDRAWN).

        Submission (PREPARING→SUBMITTED) is NOT handled here — it must go
        through :meth:`confirm_external_submission` (Gate B) or the
        system-managed send path (Section 10), both of which carry the
        required evidence. This keeps a bare transition call from creating a
        submitted record without evidence.
        """
        app = self._require_owned(application_id, candidate_id)
        if to_state is ApplicationState.SUBMITTED:
            raise WorkspaceError(
                "submission requires confirm_external_submission or the "
                "system-managed send path (evidence-bound)"
            )
        validate_transition(app.state, to_state)
        return self._record_state_change(
            app, to_state, candidate_id=candidate_id, note=note, now=now
        )

    # ------------------------------------------------------------------
    # 7.4 — channel eligibility + selection
    # ------------------------------------------------------------------

    def evaluate_channels(
        self, *, application_id: UUID, candidate_id: UUID
    ) -> ChannelEligibilitySnapshot:
        """Return the channel-eligibility snapshot for the application."""
        app = self._require_owned(application_id, candidate_id)
        return self._channel_resolver.resolve(
            candidate_id=app.candidate_id,
            canonical_job_id=app.canonical_job_id,
            apply_url=app.apply_url,
            has_trusted_contact=self._has_trusted_contact(app),
        )

    def select_channel(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        channel: SubmissionChannel,
        now: datetime,
    ) -> Application:
        """Select a submission channel after validating eligibility.

        Blocks selection of an unavailable channel (e.g. EMAIL with no trusted
        contact, EXTERNAL_FORM with no apply URL). Records a USER-sourced
        note event so the selection is auditable; it is NOT a state change.
        """
        app = self._require_owned(application_id, candidate_id)
        snapshot = self._channel_resolver.resolve(
            candidate_id=app.candidate_id,
            canonical_job_id=app.canonical_job_id,
            apply_url=app.apply_url,
            has_trusted_contact=self._has_trusted_contact(app),
        )
        if not snapshot.is_eligible(channel):
            verdict = next(c for c in snapshot.channels if c.channel is channel)
            raise ChannelUnavailableError(channel, verdict.reason)
        updated = replace(
            app,
            submission_channel=channel,
            version=app.version + 1,
            updated_at=now,
        )
        self._applications.save(updated)
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=app.id,
                event_type=ApplicationEventType.NOTE_ADDED,
                source=ApplicationEventSource.USER,
                actor_id=str(candidate_id),
                note=f"Channel selected: {channel.value}",
                event_data={"channel": channel.value},
                occurred_at=now,
                created_at=now,
            )
        )
        return updated

    # ------------------------------------------------------------------
    # 7.5 — package binding
    # ------------------------------------------------------------------

    def get_binding(self, application_id: UUID) -> PackageBinding | None:
        """Read the current package binding (None until Section 8 approves one)."""
        if self._package_binding_store is None:
            return None
        return self._package_binding_store.get_binding(application_id)

    def bind_package(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        package_version_id: UUID,
        payload_hash: str | None = None,
        job_version_id: UUID | None = None,
        resume_version_id: UUID | None = None,
        evidence_refs: tuple[UUID, ...] = (),
        now: datetime,
    ) -> Application:
        """Bind an approved package version to the application.

        Records a PACKAGE_ATTACHED event and stores the package version +
        payload hash on the application. Any later mutation of the bound inputs
        (job/resume/attachment/body) invalidates this binding — enforced by the
        Section 8 package service via payload-hash comparison on the next read.
        """
        app = self._require_owned(application_id, candidate_id)
        updated = replace(
            app,
            package_version_id=package_version_id,
            payload_hash=payload_hash,
            version=app.version + 1,
            updated_at=now,
        )
        self._applications.save(updated)
        event_data: dict[str, object] = {
            "package_version_id": str(package_version_id),
        }
        if job_version_id is not None:
            event_data["job_version_id"] = str(job_version_id)
        if resume_version_id is not None:
            event_data["resume_version_id"] = str(resume_version_id)
        if payload_hash:
            event_data["payload_hash"] = payload_hash
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=app.id,
                event_type=ApplicationEventType.PACKAGE_ATTACHED,
                source=ApplicationEventSource.USER,
                actor_id=str(candidate_id),
                event_data=event_data,
                occurred_at=now,
                created_at=now,
            )
        )
        return updated

    # ------------------------------------------------------------------
    # 7.6 — timeline projection
    # ------------------------------------------------------------------

    def get_timeline(self, *, application_id: UUID, candidate_id: UUID) -> list[TimelineEntry]:
        """Return the chronological timeline projection for the application.

        Gate B projects application events. Later gates merge mail event
        proposals, provider receipts and reminders into the same stream; those
        carry ``status == PROPOSED`` / ``RECONCILIATION_REQUIRED`` so the UI can
        distinguish suggestions from confirmed facts.
        """
        self._require_owned(application_id, candidate_id)
        events = self._applications.get_events(application_id)
        entries = [event_to_timeline_entry(e) for e in events]
        entries.sort(key=lambda e: e.occurred_at)
        return entries

    # ------------------------------------------------------------------
    # 7.7 — external-form / manual submission confirmation
    # ------------------------------------------------------------------

    def confirm_external_submission(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        apply_url: str,
        submitted_at: datetime,
        note: str = "",
        channel: SubmissionChannel = SubmissionChannel.EXTERNAL_FORM,
        now: datetime,
    ) -> Application:
        """Confirm an external-form (or manual) submission with evidence.

        Requires the application to be in PREPARING and, for EXTERNAL_FORM, a
        non-empty official apply URL. Records a SUBMITTED_MANUALLY event
        carrying the apply URL + submitted timestamp as evidence and transitions
        to SUBMITTED. There is NO automatic submitted claim: if the user never
        calls this (abandons the form), the application stays in PREPARING and
        no submitted event is created.
        """
        app = self._require_owned(application_id, candidate_id)
        if app.state is not ApplicationState.PREPARING:
            raise IllegalTransitionError(app.state, ApplicationState.SUBMITTED)
        if channel is SubmissionChannel.EXTERNAL_FORM and not apply_url.strip():
            raise ExternalFormEvidenceError()
        resolved_url = apply_url.strip() or app.apply_url
        updated = replace(
            app,
            state=ApplicationState.SUBMITTED,
            submission_channel=channel,
            apply_url=resolved_url,
            submitted_at=submitted_at or now,
            version=app.version + 1,
            updated_at=now,
        )
        self._applications.save(updated)
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=app.id,
                event_type=ApplicationEventType.SUBMITTED_MANUALLY,
                from_state=ApplicationState.PREPARING,
                to_state=ApplicationState.SUBMITTED,
                source=ApplicationEventSource.USER,
                actor_id=str(candidate_id),
                note=note or "External-form / manual submission confirmed",
                event_data={
                    "apply_url": resolved_url,
                    "submitted_at": (submitted_at or now).isoformat(),
                    "channel": channel.value,
                    "confirmation_method": "user_external_form",
                },
                occurred_at=now,
                created_at=now,
            )
        )
        return updated

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_owned(self, application_id: UUID, candidate_id: UUID) -> Application:
        app = self._applications.find_by_id(application_id)
        if app is None or app.candidate_id != candidate_id:
            raise ApplicationNotOwnedError(application_id)
        return app

    def _record_state_change(
        self,
        app: Application,
        to_state: ApplicationState,
        *,
        candidate_id: UUID,
        note: str = "",
        now: datetime,
    ) -> Application:
        updated = replace(
            app,
            state=to_state,
            version=app.version + 1,
            updated_at=now,
        )
        self._applications.save(updated)
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=app.id,
                event_type=ApplicationEventType.STATE_CHANGED,
                from_state=app.state,
                to_state=to_state,
                source=ApplicationEventSource.USER,
                actor_id=str(candidate_id),
                note=note,
                occurred_at=now,
                created_at=now,
            )
        )
        return updated

    def _resolve_cycle_id(
        self, candidate_id: UUID, canonical_job_id: UUID, now: datetime
    ) -> UUID | None:
        if self._cycle_repo is None:
            return None
        cycle = self._cycle_repo.get_or_create_active(
            candidate_id,
            canonical_job_id,
            cycle_id=uuid4(),
            now=now,
        )
        return cycle.id

    def append_note(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        note: str,
        event_data: dict[str, object] | None = None,
        now: datetime,
    ) -> None:
        """Append a USER-sourced note event to the application timeline.

        Additive (Section 12): used by the mail-intelligence service to record
        mail-derived provenance (proposal id, source message id, category) on
        the unified timeline WITHOUT transitioning state. The note is a
        confirmed USER event — acceptance is itself a user action; the proposal
        never writes state directly. Raises :class:`ApplicationNotOwnedError`
        when the application is missing or not owned.
        """
        app = self._require_owned(application_id, candidate_id)
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=app.id,
                event_type=ApplicationEventType.NOTE_ADDED,
                source=ApplicationEventSource.USER,
                actor_id=str(candidate_id),
                note=note,
                event_data=event_data or {},
                occurred_at=now,
                created_at=now,
            )
        )

    def _has_trusted_contact(self, app: Application) -> bool:
        if self._contact_lookup is None:
            return False
        return bool(self._contact_lookup(app.candidate_id, app.canonical_job_id))


class PackageServiceBindingStore:
    """Adapt a Section-8 ``PackageService``-like reader to PackageBindingStore.

    The workspace reads the LATEST package version; if the latest is not
    approved (e.g. the user edited inputs after a prior approval), the binding
    surfaces that — which is how invalidation-on-mutation becomes visible to
    the submission gate. Implements the ``PackageBindingStore`` protocol.
    """

    def __init__(self, package_service: PackageService) -> None:
        self._packages = package_service

    def get_binding(self, application_id: UUID) -> PackageBinding | None:
        latest = self._packages.get_latest(application_id)
        if latest is None:
            return None
        evidence: list[UUID] = []
        for claim in latest.claims:
            evidence.extend(claim.evidence_ids)
        return PackageBinding(
            application_id=application_id,
            job_version_id=latest.job_version_id,
            resume_version_id=latest.resume_version_id,
            package_version_id=latest.id,
            payload_hash=latest.payload_hash,
            approval_state=latest.approval_state.value,
            evidence_refs=tuple(evidence),
        )
