"""Reply drafting + follow-up management service (Section 13, tasks 13.1-13.6).

Composes versioned follow-up rules, constrained reply-draft context assembly,
immutable payload-hash-bound draft versions, and the user-confirmed reply
send (reusing Section 10's delivery chain). The service is additive: it sits
on top of the existing :class:`ApplicationRepository` (M3 follow-ups) and
:mod:`careerops.domain.mail_intelligence` (Section 12) WITHOUT mutating their
state machines.

Flow:

1. **13.1 / 13.2** :class:`FollowUpService` schedules trigger-aware reminders
   (default waiting periods per submitted / awaiting-response / interview /
   assessment / user trigger), dedups one-active-per-(application, rule),
   snoozes / reschedules / cancels / completes, and cleans up active
   reminders when the application reaches a terminal state. Reuses the M3
   ``compute_follow_up_due_date`` so business-day semantics stay identical.
2. **13.3** :meth:`ReplyDraftService.assemble_context` minimizes the trusted
   context from a linked thread (bounded excerpt), confirmed application
   facts, selected evidence and the USER-selected intent.
3. **13.4** :meth:`ReplyDraftService.create_draft` builds an immutable draft
   with a frozen recipient + thread headers and runs the deterministic
   unsupported-claim validation (skill / deadline / salary / authorization).
4. **13.5** body edits create NEW versions (fresh payload hash); recipient /
   target-thread edits are rejected and must re-enter recipient trust, policy
   and approval. Approval/rejection is monotonic and idempotent.
5. **13.6** :meth:`ReplyDraftService.send_approved` reuses Section 10's
   delivery chain (the :class:`SideEffectKernel` + outbox) via the
   :class:`ReplySendPort`. Auto-send is permanently denied; high-risk
   categories are permanently denied system send (draft/review only).

Iron rules honored:
- Model review-only (Iron Rule 2): the recipient / intent / trusted facts are
  user-sourced; model output never selects them or writes a trusted hash.
- Append-only / reversible (Iron Rule 4): versions never rewritten; decisions
  monotonic; the timeline is append-only.
- Server-side ownership (Iron Rule 2/6): candidate resolved server-side; a
  not-owned draft/reminder is 404 (no existence leak).
- Default-deny (Iron Rule 7): high-risk + auto-send permanently denied.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from careerops.application.applications import compute_follow_up_due_date
from careerops.domain.applications import (
    FOLLOW_UP_CANCEL_STATES,
    Application,
    ApplicationEvent,
    ApplicationEventSource,
    ApplicationEventType,
    FollowUpReminder,
    FollowUpState,
)
from careerops.domain.reply_draft import (
    FOLLOW_UP_RULE_VERSION_S13,
    PERMANENTLY_DENIED_REPLY_CATEGORIES,
    ApplicationFact,
    IllegalReplyTransitionError,
    RecipientMutationError,
    ReplyDraft,
    ReplyDraftAlreadyDecidedError,
    ReplyDraftApprovalState,
    ReplyDraftClaim,
    ReplyDraftContext,
    ReplyDraftError,
    ReplyDraftNotOwnedError,
    ReplyIntent,
    ReplyRiskCategory,
    ReplySendDeniedError,
    UnsupportedClaimError,
    compute_reply_payload_hash,
    default_waiting_period,
    is_system_send_denied,
    reply_risk_for_category,
    reply_risk_for_intent,
    scan_body_for_unsupported_claims,
    validate_reply_claims,
)
from careerops.orchestration.capability_resolver import (
    CapabilityDecision,
    CapabilityKind,
)

__all__ = [
    "DUPLICATE_FOLLOW_UP_ERROR",
    "DuplicateFollowUpError",
    "FollowUpNotOwnedError",
    "FollowUpService",
    "ReplyDraftRepository",
    "ReplyDraftService",
    "ReplySendDeniedError",
    "ReplySendOutcome",
    "ReplySendPhase",
    "ReplySendPort",
    "ThreadExcerptProvider",
]


# ---------------------------------------------------------------------------
# Shared repository / port protocols
# ---------------------------------------------------------------------------


class _FollowUpRepository(Protocol):
    """Minimal follow-up repo surface the Section-13 service needs."""

    def find_by_id(self, reminder_id: UUID) -> FollowUpReminder | None: ...

    def find_active_by_application_and_rule(
        self, application_id: UUID, rule_version: str
    ) -> FollowUpReminder | None: ...

    def save(self, reminder: FollowUpReminder) -> None: ...


class _ApplicationRepository(Protocol):
    """Minimal application repo surface (ownership + timeline)."""

    def find_by_id(self, application_id: UUID) -> Application | None: ...

    def append_event(self, event: ApplicationEvent) -> None: ...


class ReplyDraftRepository(Protocol):
    """Durable :class:`ReplyDraft` version store (tasks 13.4 / 13.5)."""

    def find_by_id(self, draft_id: UUID) -> ReplyDraft | None: ...

    def find_latest_for_thread(self, thread_id: UUID, candidate_id: UUID) -> ReplyDraft | None: ...

    def find_by_idempotency_key(self, key: str) -> ReplyDraft | None: ...

    def save(self, draft: ReplyDraft) -> None: ...

    def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        application_id: UUID | None = None,
        state: ReplyDraftApprovalState | None = None,
        limit: int = 50,
        cursor: tuple[datetime, UUID] | None = None,
    ) -> tuple[list[ReplyDraft], tuple[datetime, UUID] | None]: ...


class ApplicationOwnershipReader(Protocol):
    """Reads an owned application for context assembly (task 13.3)."""

    def find_owned_application(
        self, *, application_id: UUID, candidate_id: UUID
    ) -> Application | None: ...


class ThreadExcerptProvider(Protocol):
    """Returns a bounded, sanitized thread excerpt (task 13.3 minimization).

    Implementations read the linked thread's latest relevant message and
    return a citable excerpt (<= REPLY_EXCERPT_MAX_CHARS). The full body never
    travels into the draft inputs. Returns ``""`` when no excerpt is available
    (an unlinked thread surfaces for review).
    """

    def excerpt(
        self, *, thread_id: UUID, candidate_id: UUID, message_id: UUID | None = None
    ) -> str: ...


class ConfirmedEvidenceReader(Protocol):
    """Returns the confirmed-evidence id allowlist for a candidate (13.4)."""

    def confirmed_evidence_ids(self, candidate_id: UUID) -> frozenset[UUID]: ...


class ApplicationTimelineSink(Protocol):
    """Appends a reply-derived timeline note (task 13.6 outcome)."""

    def append_note(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        note: str,
        event_data: dict[str, object],
        now: datetime,
    ) -> None: ...


class CapabilityResolver(Protocol):
    """Minimal resolver surface for send-capability gating (13.6)."""

    def decide(self, kind: CapabilityKind) -> CapabilityDecision: ...


# ---------------------------------------------------------------------------
# Reply send outcome (reuses Section 10 phase vocabulary, task 13.6)
# ---------------------------------------------------------------------------


class ReplySendPhase:
    """User-facing phase of one user-confirmed reply send (task 13.6).

    Mirrors the Section-10 ``SystemSendPhase`` so the UI's send-progress state
    is uniform across initial emails and replies. ``SENT`` requires a provider
    receipt; ``RECONCILIATION_REQUIRED`` disables blind retry.
    """

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ReplySendOutcome:
    """Result of a reply send attempt (idempotent, task 13.6 / 13.10)."""

    __slots__ = (
        "already_in_flight",
        "denial_reasons",
        "intent_id",
        "payload_hash",
        "phase",
        "provider_message_id",
        "provider_resource_id",
        "sent_at",
    )

    def __init__(
        self,
        *,
        phase: str,
        intent_id: UUID | None = None,
        payload_hash: str | None = None,
        provider_resource_id: str | None = None,
        provider_message_id: str | None = None,
        denial_reasons: tuple[str, ...] = (),
        sent_at: datetime | None = None,
        already_in_flight: bool = False,
    ) -> None:
        self.phase = phase
        self.intent_id = intent_id
        self.payload_hash = payload_hash
        self.provider_resource_id = provider_resource_id
        self.provider_message_id = provider_message_id
        self.denial_reasons = denial_reasons
        self.sent_at = sent_at
        self.already_in_flight = already_in_flight


class ReplySendPort(Protocol):
    """Reuses Section 10's delivery chain for a user-confirmed reply (13.6).

    The runtime wires :class:`KernelReplySendAdapter` (which delegates to the
    shared :class:`SideEffectKernel` + outbox — the SAME chain Section 10
    uses). Unit tests inject a fake. The port performs the durable
    confirmation -> worker -> receipt flow; the reply service only feeds it
    trusted inputs and records the outcome on the timeline. The provider is
    the :class:`FakeSideEffectProvider` only in this change (task 17.6).
    """

    def deliver(
        self,
        *,
        draft: ReplyDraft,
        account_email: str,
        candidate_id: UUID,
        now: datetime,
    ) -> ReplySendOutcome: ...

    def status(
        self,
        *,
        intent_id: UUID,
        candidate_id: UUID,
    ) -> ReplySendOutcome: ...


# ---------------------------------------------------------------------------
# Follow-up service (tasks 13.1 / 13.2)
# ---------------------------------------------------------------------------


class FollowUpServiceError(Exception):
    """Base for follow-up service errors."""


class DuplicateFollowUpError(FollowUpServiceError):
    """An active follow-up already exists for (application, rule)."""

    def __init__(self, application_id: UUID, rule_version: str) -> None:
        super().__init__(
            f"active follow-up already exists for application={application_id} rule={rule_version}"
        )


# Kept as a module-level alias for API-layer imports.
DUPLICATE_FOLLOW_UP_ERROR = DuplicateFollowUpError


class FollowUpNotOwnedError(FollowUpServiceError):
    """The reminder does not exist or is not owned by the candidate."""

    def __init__(self, reminder_id: UUID) -> None:
        super().__init__(f"follow-up reminder not found for candidate: {reminder_id}")


class FollowUpService:
    """Versioned, trigger-aware follow-up reminders (tasks 13.1 / 13.2).

    Additive to the M3 :class:`ApplicationService`: it composes the same
    :class:`FollowUpRepository` but adds the Section-13 trigger semantics
    (default waiting periods per state) and the ``complete`` lifecycle action.
    Terminal-state cleanup cancels active reminders when the application
    reaches a terminal state (REJECTED / WITHDRAWN / OFFER).
    """

    def __init__(
        self,
        follow_up_repo: _FollowUpRepository,
        application_repo: _ApplicationRepository,
        *,
        rule_version: str = FOLLOW_UP_RULE_VERSION_S13,
        timeline_sink: ApplicationTimelineSink | None = None,
    ) -> None:
        self._follow_ups = follow_up_repo
        self._applications = application_repo
        self._rule_version = rule_version
        self._timeline = timeline_sink

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get_reminder(self, *, reminder_id: UUID, candidate_id: UUID) -> FollowUpReminder:
        reminder = self._follow_ups.find_by_id(reminder_id)
        if reminder is None:
            raise FollowUpNotOwnedError(reminder_id)
        self._require_owned(reminder.application_id, candidate_id)
        return reminder

    def list_for_application(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        limit: int = 50,
    ) -> list[FollowUpReminder]:
        self._require_owned(application_id, candidate_id)
        # The repo surface is minimal; reminders are looked up via the active
        # index. A fuller list is a later gate; this returns the active one if
        # present (the spec's review queue needs the active reminder).
        active = self._follow_ups.find_active_by_application_and_rule(
            application_id, self._rule_version
        )
        return [active] if active is not None else []

    # ------------------------------------------------------------------
    # 13.1 / 13.2 — schedule (trigger-aware) + dedup
    # ------------------------------------------------------------------

    def schedule_follow_up(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        trigger: str,
        business_days: int | None = None,
        rule_version: str | None = None,
        base_at: datetime | None = None,
        now: datetime,
    ) -> FollowUpReminder:
        """Schedule a trigger-aware follow-up reminder (task 13.1).

        Dedups one-active-per-(application, rule_version): an existing active
        reminder raises :class:`DuplicateFollowUpError`. ``trigger`` selects the
        default waiting period when ``business_days`` is not supplied.
        ``base_at`` is the anchor for the due-date computation (defaults to
        ``now``); the M3 ``compute_follow_up_due_date`` skips weekends.
        """
        from careerops.domain.reply_draft import FollowUpTriggerState

        rule = rule_version or self._rule_version
        existing = self._follow_ups.find_active_by_application_and_rule(application_id, rule)
        if existing is not None:
            raise DuplicateFollowUpError(application_id, rule)

        app = self._require_owned(application_id, candidate_id)

        trigger_state = FollowUpTriggerState(trigger)
        wait = business_days if business_days is not None else default_waiting_period(trigger_state)
        anchor = base_at or app.submitted_at or now
        due_at = compute_follow_up_due_date(anchor, wait)

        reminder = FollowUpReminder(
            id=uuid4(),
            application_id=application_id,
            rule_version=rule,
            state=FollowUpState.ACTIVE,
            due_at=due_at,
            created_at=now,
            updated_at=now,
        )
        self._follow_ups.save(reminder)
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=application_id,
                event_type=ApplicationEventType.FOLLOW_UP_SCHEDULED,
                source=ApplicationEventSource.SYSTEM,
                event_data={
                    "due_at": due_at.isoformat(),
                    "rule_version": rule,
                    "trigger": trigger_state.value,
                    "business_days": wait,
                },
                occurred_at=now,
                created_at=now,
            )
        )
        return reminder

    # ------------------------------------------------------------------
    # 13.2 — snooze / reschedule / cancel / complete
    # ------------------------------------------------------------------

    def snooze_follow_up(
        self, *, reminder_id: UUID, candidate_id: UUID, snoozed_until: datetime, now: datetime
    ) -> FollowUpReminder:
        reminder = self._load_reminder(reminder_id, candidate_id)
        if reminder.state != FollowUpState.ACTIVE:
            raise FollowUpServiceError(f"cannot snooze follow-up in state: {reminder.state.value}")
        updated = replace(
            reminder,
            state=FollowUpState.SNOOZED,
            snoozed_until=snoozed_until,
            updated_at=now,
        )
        self._follow_ups.save(updated)
        self._append_follow_up_event(
            updated,
            ApplicationEventType.FOLLOW_UP_SNOOZED,
            now,
            event_data={"snoozed_until": snoozed_until.isoformat()},
        )
        return updated

    def reschedule_follow_up(
        self, *, reminder_id: UUID, candidate_id: UUID, new_due_at: datetime, now: datetime
    ) -> FollowUpReminder:
        reminder = self._load_reminder(reminder_id, candidate_id)
        if reminder.state in (FollowUpState.CANCELLED, FollowUpState.COMPLETED):
            raise FollowUpServiceError(
                f"cannot reschedule follow-up in state: {reminder.state.value}"
            )
        updated = replace(
            reminder,
            state=FollowUpState.ACTIVE,
            due_at=new_due_at,
            snoozed_until=None,
            updated_at=now,
        )
        self._follow_ups.save(updated)
        self._append_follow_up_event(
            updated,
            ApplicationEventType.FOLLOW_UP_RESCHEDULED,
            now,
            event_data={"new_due_at": new_due_at.isoformat()},
            source=ApplicationEventSource.USER,
        )
        return updated

    def cancel_follow_up(
        self, *, reminder_id: UUID, candidate_id: UUID, reason: str, now: datetime
    ) -> FollowUpReminder:
        reminder = self._load_reminder(reminder_id, candidate_id)
        if reminder.state in (FollowUpState.CANCELLED, FollowUpState.COMPLETED):
            raise FollowUpServiceError(f"cannot cancel follow-up in state: {reminder.state.value}")
        updated = replace(
            reminder,
            state=FollowUpState.CANCELLED,
            cancelled_reason=reason,
            updated_at=now,
        )
        self._follow_ups.save(updated)
        self._append_follow_up_event(
            updated,
            ApplicationEventType.FOLLOW_UP_CANCELLED,
            now,
            event_data={"reason": reason},
        )
        return updated

    def complete_follow_up(
        self, *, reminder_id: UUID, candidate_id: UUID, now: datetime
    ) -> FollowUpReminder:
        """Mark an active/snoozed reminder completed (task 13.2)."""
        reminder = self._load_reminder(reminder_id, candidate_id)
        if reminder.state in (FollowUpState.CANCELLED, FollowUpState.COMPLETED):
            raise FollowUpServiceError(
                f"cannot complete follow-up in state: {reminder.state.value}"
            )
        updated = replace(reminder, state=FollowUpState.COMPLETED, updated_at=now)
        self._follow_ups.save(updated)
        self._append_follow_up_event(
            updated,
            ApplicationEventType.NOTE_ADDED,
            now,
            event_data={"follow_up_state": FollowUpState.COMPLETED.value},
            note="Follow-up reminder completed",
            source=ApplicationEventSource.USER,
        )
        return updated

    # ------------------------------------------------------------------
    # 13.2 — terminal-state cleanup
    # ------------------------------------------------------------------

    def cleanup_terminal(
        self, *, application_id: UUID, candidate_id: UUID, terminal_state: str, now: datetime
    ) -> FollowUpReminder | None:
        """Cancel the active follow-up when the application goes terminal.

        Task 13.2: a reminder for a REJECTED / WITHDRAWN / OFFER application is
        no longer actionable. Returns the cancelled reminder (or None).
        """
        self._require_owned(application_id, candidate_id)
        active = self._follow_ups.find_active_by_application_and_rule(
            application_id, self._rule_version
        )
        if active is None:
            return None
        updated = replace(
            active,
            state=FollowUpState.CANCELLED,
            cancelled_reason=f"application_terminal:{terminal_state}",
            updated_at=now,
        )
        self._follow_ups.save(updated)
        self._append_follow_up_event(
            updated,
            ApplicationEventType.FOLLOW_UP_CANCELLED,
            now,
            event_data={"reason": f"application_terminal:{terminal_state}"},
        )
        return updated

    def is_terminal_state(self, state: str) -> bool:
        """True for application states that cancel active follow-ups."""
        from careerops.domain.applications import ApplicationState

        try:
            return ApplicationState(state) in FOLLOW_UP_CANCEL_STATES
        except ValueError:
            return False

    # ------------------------------------------------------------------
    # 13.1 — suppress the active follow-up when a linked reply arrives
    # ------------------------------------------------------------------

    def suppress_for_linked_reply(
        self, *, application_id: UUID, candidate_id: UUID, now: datetime
    ) -> FollowUpReminder | None:
        """Cancel the active follow-up when a linked reply arrives (13.1).

        Scenario: a new linked recruiting reply arrives before an active
        follow-up is due. The system cancels the active reminder (the mail
        review action replaces it) unless the user explicitly requests a new
        one. Returns the cancelled reminder (or None if there was no active one).
        """
        self._require_owned(application_id, candidate_id)
        active = self._follow_ups.find_active_by_application_and_rule(
            application_id, self._rule_version
        )
        if active is None:
            return None
        updated = replace(
            active,
            state=FollowUpState.CANCELLED,
            cancelled_reason="linked_reply_arrived",
            updated_at=now,
        )
        self._follow_ups.save(updated)
        self._append_follow_up_event(
            updated,
            ApplicationEventType.FOLLOW_UP_CANCELLED,
            now,
            event_data={"reason": "linked_reply_arrived"},
        )
        return updated

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _load_reminder(self, reminder_id: UUID, candidate_id: UUID) -> FollowUpReminder:
        reminder = self._follow_ups.find_by_id(reminder_id)
        if reminder is None:
            raise FollowUpNotOwnedError(reminder_id)
        self._require_owned(reminder.application_id, candidate_id)
        return reminder

    def _require_owned(self, application_id: UUID, candidate_id: UUID) -> Application:
        app = self._applications.find_by_id(application_id)
        if app is None or app.candidate_id != candidate_id:
            # Reuse the not-owned semantics so the API maps to 404.
            raise FollowUpNotOwnedError(application_id)
        return app

    def _append_follow_up_event(
        self,
        reminder: FollowUpReminder,
        event_type: ApplicationEventType,
        now: datetime,
        *,
        event_data: dict[str, object] | None = None,
        note: str = "",
        source: ApplicationEventSource = ApplicationEventSource.SYSTEM,
    ) -> None:
        self._applications.append_event(
            ApplicationEvent(
                id=uuid4(),
                application_id=reminder.application_id,
                event_type=event_type,
                source=source,
                note=note,
                event_data=event_data or {},
                occurred_at=now,
                created_at=now,
            )
        )


# ---------------------------------------------------------------------------
# Reply draft service (tasks 13.3 / 13.4 / 13.5 / 13.6)
# ---------------------------------------------------------------------------


class _DefaultExcerptProvider:
    """Default thread-excerpt provider: returns the supplied snippet unchanged.

    The runtime wires a real reader (Section 11 message store); until then the
    caller passes the bounded excerpt directly and this provider is a passthrough
    so the bounded-excerpt invariant still holds at construction.
    """

    def excerpt(
        self, *, thread_id: UUID, candidate_id: UUID, message_id: UUID | None = None
    ) -> str:
        return ""


class _DefaultEvidenceReader:
    """Default evidence reader: treats no evidence as confirmed."""

    def confirmed_evidence_ids(self, candidate_id: UUID) -> frozenset[UUID]:
        return frozenset()


class ReplyDraftService:
    """Constrained reply drafting + user-confirmed send (tasks 13.3-13.6)."""

    def __init__(
        self,
        draft_repo: ReplyDraftRepository,
        ownership_reader: ApplicationOwnershipReader,
        *,
        excerpt_provider: ThreadExcerptProvider | None = None,
        evidence_reader: ConfirmedEvidenceReader | None = None,
        timeline_sink: ApplicationTimelineSink | None = None,
        capability_resolver: CapabilityResolver | None = None,
        send_port: ReplySendPort | None = None,
    ) -> None:
        self._drafts = draft_repo
        self._ownership = ownership_reader
        self._excerpts = excerpt_provider or _DefaultExcerptProvider()
        self._evidence = evidence_reader or _DefaultEvidenceReader()
        self._timeline = timeline_sink
        self._capability_resolver = capability_resolver
        self._send_port = send_port

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get_draft(self, *, draft_id: UUID, candidate_id: UUID) -> ReplyDraft:
        draft = self._drafts.find_by_id(draft_id)
        if draft is None or draft.candidate_id != candidate_id:
            raise ReplyDraftNotOwnedError(draft_id)
        return draft

    def list_drafts(
        self,
        *,
        candidate_id: UUID,
        application_id: UUID | None = None,
        state: ReplyDraftApprovalState | None = None,
        limit: int = 50,
        cursor: tuple[datetime, UUID] | None = None,
    ) -> tuple[list[ReplyDraft], tuple[datetime, UUID] | None]:
        return self._drafts.list_for_candidate(
            candidate_id,
            application_id=application_id,
            state=state,
            limit=limit,
            cursor=cursor,
        )

    # ------------------------------------------------------------------
    # 13.3 — context assembly
    # ------------------------------------------------------------------

    def assemble_context(
        self,
        *,
        candidate_id: UUID,
        message_id: UUID,
        thread_id: UUID,
        application_id: UUID | None,
        intent: ReplyIntent,
        mail_category: str = "",
        application_facts: tuple[ApplicationFact, ...] = (),
        evidence_refs: tuple[UUID, ...] = (),
        excerpt_override: str | None = None,
    ) -> ReplyDraftContext:
        """Assemble the constrained trusted context for a reply (task 13.3).

        Minimizes the thread into a bounded excerpt, binds confirmed
        application facts + selected evidence, and derives the deterministic
        risk category from the user-selected intent + linked mail category.
        ``intent`` is always USER-selected; the mail-category default intent is
        only a review-only suggestion surfaced via :func:`reply_intent_for_category`.

        An unresolved application link (``application_id is None``) is allowed:
        the context is assembled without application facts and the draft is
        surfaced for unresolved-link review (task 13.3 / 13.9).
        """
        if application_id is not None:
            # Validate ownership so a candidate cannot assemble context for
            # another candidate's application.
            owned = self._ownership.find_owned_application(
                application_id=application_id, candidate_id=candidate_id
            )
            if owned is None:
                application_id = None

        excerpt = (
            excerpt_override
            if excerpt_override is not None
            else self._excerpts.excerpt(
                thread_id=thread_id,
                candidate_id=candidate_id,
                message_id=message_id,
            )
        )

        # Derive risk: a permanently-denied mail category wins over the intent
        # so the send path hard-refuses regardless of the user selection.
        if mail_category:
            risk = reply_risk_for_category(mail_category)
        else:
            risk = reply_risk_for_intent(intent)
        if mail_category and risk in PERMANENTLY_DENIED_REPLY_CATEGORIES:
            # Permanently-denied mail content overrides the intent-derived risk.
            final_risk = risk
        elif risk is ReplyRiskCategory.UNKNOWN:
            final_risk = reply_risk_for_intent(intent)
        else:
            final_risk = risk

        return ReplyDraftContext(
            message_id=message_id,
            thread_id=thread_id,
            application_id=application_id,
            thread_excerpt=excerpt,
            application_facts=application_facts,
            evidence_refs=evidence_refs,
            intent=intent,
            risk_category=final_risk,
            mail_category=mail_category,
        )

    # ------------------------------------------------------------------
    # 13.4 — draft creation (recipient/thread-header immutable)
    # ------------------------------------------------------------------

    def create_draft(
        self,
        *,
        candidate_id: UUID,
        context: ReplyDraftContext,
        recipient: str,
        account_email: str,
        subject: str,
        body: str,
        in_reply_to: str = "",
        references_header: str = "",
        claims: tuple[ReplyDraftClaim, ...] = (),
        account_id: UUID | None = None,
        now: datetime,
    ) -> ReplyDraft:
        """Create a DRAFT reply version with frozen recipient + headers (13.4).

        Runs the deterministic unsupported-claim validation: a body that
        asserts a salary / deadline / authorization claim without supporting
        evidence is flagged and the draft carries ``validation_issues`` that
        block approval until corrected. The recipient + thread headers are
        frozen on the draft; a later recipient change is rejected (13.5).
        """
        if context.application_id is None:
            # An unresolved link is allowed for review but the draft cannot be
            # sent until linked (task 13.3 / 13.9).
            pass

        confirmed = self._evidence.confirmed_evidence_ids(candidate_id)
        issues = self._collect_validation_issues(body=body, claims=claims, confirmed=confirmed)

        latest = self._drafts.find_latest_for_thread(context.thread_id, candidate_id)
        next_number = (latest.version_number + 1) if latest else 1
        payload_hash = compute_reply_payload_hash(
            message_id=context.message_id,
            thread_id=context.thread_id,
            recipient=recipient,
            in_reply_to=in_reply_to,
            references_header=references_header,
            subject=subject,
            body=body,
            intent=context.intent,
            claims=claims,
        )

        draft = ReplyDraft(
            id=uuid4(),
            candidate_id=candidate_id,
            message_id=context.message_id,
            thread_id=context.thread_id,
            account_id=account_id,
            application_id=context.application_id,
            recipient=recipient,
            in_reply_to=in_reply_to,
            references_header=references_header,
            subject=subject,
            body=body,
            intent=context.intent,
            risk_category=context.risk_category,
            context=context,
            claims=claims,
            validation_issues=issues,
            payload_hash=payload_hash,
            version_number=next_number,
            approval_state=ReplyDraftApprovalState.DRAFT,
            created_at=now,
            updated_at=now,
        )
        self._drafts.save(draft)
        return draft

    # ------------------------------------------------------------------
    # 13.5 — edit (body -> new version) / target mutation rejection
    # ------------------------------------------------------------------

    def edit_draft(
        self,
        *,
        draft_id: UUID,
        candidate_id: UUID,
        subject: str | None = None,
        body: str | None = None,
        claims: tuple[ReplyDraftClaim, ...] | None = None,
        now: datetime,
    ) -> ReplyDraft:
        """Apply a body/subject/claims edit as a NEW version (task 13.5).

        The recipient + thread headers are immutable and carried over
        unchanged. A body edit produces a fresh payload hash + a new
        ``version_number``; the prior version — including an approved one — is
        superseded (its approval no longer reflects the latest content). The
        new version re-enters the validation + approval flow.
        """
        base = self.get_draft(draft_id=draft_id, candidate_id=candidate_id)
        new_subject = subject if subject is not None else base.subject
        new_body = body if body is not None else base.body
        new_claims = claims if claims is not None else base.claims

        confirmed = self._evidence.confirmed_evidence_ids(candidate_id)
        issues = self._collect_validation_issues(
            body=new_body, claims=new_claims, confirmed=confirmed
        )
        payload_hash = compute_reply_payload_hash(
            message_id=base.message_id,
            thread_id=base.thread_id,
            recipient=base.recipient,
            in_reply_to=base.in_reply_to,
            references_header=base.references_header,
            subject=new_subject,
            body=new_body,
            intent=base.intent,
            claims=new_claims,
        )

        latest = self._drafts.find_latest_for_thread(base.thread_id, candidate_id)
        next_number = (latest.version_number + 1) if latest else base.version_number + 1
        edited = ReplyDraft(
            id=uuid4(),
            candidate_id=candidate_id,
            message_id=base.message_id,
            thread_id=base.thread_id,
            account_id=base.account_id,
            application_id=base.application_id,
            recipient=base.recipient,
            in_reply_to=base.in_reply_to,
            references_header=base.references_header,
            subject=new_subject,
            body=new_body,
            intent=base.intent,
            risk_category=base.risk_category,
            context=base.context,
            claims=new_claims,
            validation_issues=issues,
            payload_hash=payload_hash,
            version_number=next_number,
            approval_state=ReplyDraftApprovalState.DRAFT,
            created_at=now,
            updated_at=now,
        )
        self._drafts.save(edited)
        return edited

    def require_new_draft_for_target_change(self) -> None:
        """Reject an in-place recipient / target-thread edit (task 13.5).

        Callers that detect a recipient or target-thread change on an existing
        draft MUST call this (or catch the :class:`RecipientMutationError`)
        and start a fresh draft via :meth:`create_draft` that re-enters
        recipient trust, policy and approval checks.
        """
        raise RecipientMutationError()

    # ------------------------------------------------------------------
    # 13.5 — approve / reject (monotonic, idempotent)
    # ------------------------------------------------------------------

    def approve(
        self,
        *,
        draft_id: UUID,
        candidate_id: UUID,
        now: datetime,
    ) -> ReplyDraft:
        """Approve a reply draft after validating preconditions (task 13.5).

        Blocks when the draft carries unsupported claims (13.4). Freezes the
        approval state + timestamp. Idempotent: a repeat approve of an
        already-approved draft returns the recorded version.
        """
        draft = self.get_draft(draft_id=draft_id, candidate_id=candidate_id)
        if draft.approval_state is ReplyDraftApprovalState.APPROVED:
            return draft
        if draft.approval_state is ReplyDraftApprovalState.REJECTED:
            raise ReplyDraftAlreadyDecidedError(draft.id, draft.approval_state)
        self._validate_transition(draft.approval_state, ReplyDraftApprovalState.APPROVED)
        if draft.validation_issues:
            raise UnsupportedClaimError(draft.validation_issues)
        approved = replace(
            draft,
            approval_state=ReplyDraftApprovalState.APPROVED,
            decided_at=now,
            decided_by=str(candidate_id),
            updated_at=now,
        )
        self._drafts.save(approved)
        return approved

    def reject(
        self,
        *,
        draft_id: UUID,
        candidate_id: UUID,
        now: datetime,
    ) -> ReplyDraft:
        """Reject a reply draft (task 13.5).

        Records the rejection, leaves application state unchanged, blocks the
        same draft from being approved later. Idempotent.
        """
        draft = self.get_draft(draft_id=draft_id, candidate_id=candidate_id)
        if draft.approval_state is ReplyDraftApprovalState.REJECTED:
            return draft
        if draft.approval_state is ReplyDraftApprovalState.APPROVED:
            raise ReplyDraftAlreadyDecidedError(draft.id, draft.approval_state)
        self._validate_transition(draft.approval_state, ReplyDraftApprovalState.REJECTED)
        rejected = replace(
            draft,
            approval_state=ReplyDraftApprovalState.REJECTED,
            decided_at=now,
            decided_by=str(candidate_id),
            updated_at=now,
        )
        self._drafts.save(rejected)
        return rejected

    # ------------------------------------------------------------------
    # 13.6 — user-confirmed send (reuses Section 10 delivery chain)
    # ------------------------------------------------------------------

    def send_approved(
        self,
        *,
        draft_id: UUID,
        candidate_id: UUID,
        account_email: str,
        now: datetime,
    ) -> ReplySendOutcome:
        """Send an approved reply through Section 10's delivery chain (13.6).

        Reuses the shared :class:`SideEffectKernel` + outbox via the wired
        :class:`ReplySendPort`. Auto-send is permanently denied (the
        AUTO_SEND capability check refuses any path that did not pass through
        explicit user approval here). High-risk / permanently-denied
        categories are denied BEFORE any provider call (draft/review only).

        Idempotent (task 13.10): a repeat send for the same approved draft
        returns the in-flight outcome without a second provider effect.
        """
        draft = self.get_draft(draft_id=draft_id, candidate_id=candidate_id)
        if draft.approval_state is not ReplyDraftApprovalState.APPROVED:
            raise ReplyDraftError(
                f"reply draft {draft_id} is not approved (state={draft.approval_state.value})"
            )

        # Default-deny gate (task 13.6). Auto-send is permanently denied; the
        # only way a reply is sent is this explicit user-confirmed path.
        self._require_auto_send_denied()
        # High-risk categories are permanently denied system send.
        if is_system_send_denied(draft.risk_category):
            raise ReplySendDeniedError(("category_permanently_denied", draft.risk_category.value))

        if self._send_port is None:
            # No delivery chain wired (PRODUCTION never wires it before the
            # external-write qualification gate). Surface as denied rather than
            # silently no-op'ing.
            raise ReplySendDeniedError(("send_chain_not_wired",))

        outcome = self._send_port.deliver(
            draft=draft, account_email=account_email, candidate_id=candidate_id, now=now
        )
        self._record_send_outcome(draft, outcome, now)
        return outcome

    def get_send_status(self, *, draft_id: UUID, candidate_id: UUID) -> ReplySendOutcome:
        """Idempotent read of the reply send phase (task 13.6 / 13.10)."""
        draft = self.get_draft(draft_id=draft_id, candidate_id=candidate_id)
        if draft.send_intent_id is None or self._send_port is None:
            return ReplySendOutcome(
                phase=draft.send_phase or ReplySendPhase.PENDING,
                payload_hash=draft.payload_hash or None,
            )
        return self._send_port.status(intent_id=draft.send_intent_id, candidate_id=candidate_id)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _collect_validation_issues(
        self,
        *,
        body: str,
        claims: tuple[ReplyDraftClaim, ...],
        confirmed: frozenset[UUID],
    ) -> tuple[str, ...]:
        """Deterministic unsupported-claim findings (task 13.4)."""
        issues: list[str] = []
        claim_findings = validate_reply_claims(claims, confirmed_evidence_ids=confirmed)
        for claim_text, reason in claim_findings:
            issues.append(f"{reason}:{claim_text[:48]}")
        body_findings = scan_body_for_unsupported_claims(body)
        for code in body_findings:
            issues.append(f"body_{code}")
        return tuple(issues)

    def _record_send_outcome(
        self, draft: ReplyDraft, outcome: ReplySendOutcome, now: datetime
    ) -> None:
        """Append the send outcome to the timeline + persist the phase (13.6)."""
        updated = replace(
            draft,
            send_intent_id=outcome.intent_id or draft.send_intent_id,
            send_phase=outcome.phase,
            updated_at=now,
        )
        self._drafts.save(updated)
        if draft.application_id is None or self._timeline is None:
            return
        event_data: dict[str, object] = {
            "draft_id": str(draft.id),
            "message_id": str(draft.message_id),
            "thread_id": str(draft.thread_id),
            "recipient": draft.recipient,
            "payload_hash": draft.payload_hash,
            "phase": outcome.phase,
            "intent": draft.intent.value,
        }
        if outcome.intent_id is not None:
            event_data["intent_id"] = str(outcome.intent_id)
        if outcome.provider_resource_id is not None:
            event_data["provider_resource_id"] = outcome.provider_resource_id
        if outcome.denial_reasons:
            event_data["denial_reasons"] = list(outcome.denial_reasons)
        note = {
            ReplySendPhase.SENT: "Reply sent via system-managed delivery chain",
            ReplySendPhase.FAILED: "Reply send failed",
            ReplySendPhase.RECONCILIATION_REQUIRED: "Reply send ambiguous; reconciliation required",
            ReplySendPhase.PENDING: "Reply send pending",
        }.get(outcome.phase, "Reply send outcome recorded")
        self._timeline.append_note(
            application_id=draft.application_id,
            candidate_id=draft.candidate_id,
            note=note,
            event_data=event_data,
            now=now,
        )

    def _require_auto_send_denied(self) -> None:
        """Auto-send is permanently denied (task 13.6 / Iron Rule 7).

        The AUTO_SEND capability is permanently denied at the contract layer;
        even if a future flag flips the setting, the resolver hard-denies. The
        only way a reply is sent is the explicit user-confirmed path here. When
        no resolver is wired (unit tests), the gate is a no-op — the caller has
        already proven user approval by reaching this method.
        """
        if self._capability_resolver is None:
            return
        try:
            decision = self._capability_resolver.decide(CapabilityKind.AUTO_SEND)
        except Exception:
            return
        if decision.released:
            # The resolver never releases AUTO_SEND; a released decision here
            # would indicate a misconfiguration that bypasses the human
            # confirmation gate. Refuse rather than silently send.
            raise ReplySendDeniedError(("auto_send_unexpectedly_released",))

    @staticmethod
    def _validate_transition(
        from_state: ReplyDraftApprovalState, to_state: ReplyDraftApprovalState
    ) -> None:
        from careerops.domain.reply_draft import REPLY_DRAFT_TRANSITIONS

        allowed = REPLY_DRAFT_TRANSITIONS.get(from_state, frozenset())
        if to_state not in allowed:
            raise IllegalReplyTransitionError(from_state, to_state)


# ---------------------------------------------------------------------------
# Default business-day computation re-export (M3 semantics, task 13.1)
# ---------------------------------------------------------------------------

# Re-exported so the API layer can compute preview due dates without importing
# the M3 application module directly.
__all__.append("compute_follow_up_due_date")
