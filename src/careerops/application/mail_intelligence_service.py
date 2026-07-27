"""Mail intelligence service (Section 12, tasks 12.5-12.7).

Composes the deterministic + optional-model extractors with the
:class:`EmailEventProposal` lifecycle and the application workspace. The
service is the ONLY place a proposal decision is translated into an
application effect, and it does so by delegating to the existing
USER-sourced transition service — the proposal itself never writes
:class:`ApplicationState` (Iron Rule 2).

Flow:

1. ``extract_and_propose`` (12.5): deterministic extraction ALWAYS; optional
   model enrichment when wired AND the ``MODEL_TAILORING`` capability is
   released (default-disabled). Idempotent by
   ``(message_id, extraction_hash)``: a re-run returns the existing proposal.
   High-risk / low-confidence / unknown / injection-flagged extractions stay
   ``review_required`` and never auto-apply (12.4).
2. ``list_proposals`` / ``get_proposal``: server-side candidate ownership.
3. ``accept_proposal`` (12.6 + 12.7): validates ownership + the legal proposal
   transition (pending → accepted); validates the proposed application
   transition is legal from the current state; delegates to
   :class:`ApplicationWorkspaceService.apply_user_transition` (USER-sourced
   event); appends a mail-derived timeline note; schedules an interview /
   follow-up task where policy permits. Idempotent: a repeat accept returns the
   recorded result without re-applying.
4. ``reject_proposal`` (12.6): records the rejection, leaves application state
   unchanged, blocks the same proposal from being accepted later.

Iron rules honored:
- Model review-only (Iron Rule 2): model output only refines the extraction;
  the proposal never writes state directly.
- Append-only / reversible (Iron Rule 4): decisions are monotonic
  (pending → terminal); the application timeline is append-only.
- Server-side ownership (Iron Rule 2/6): candidate resolved server-side; a
  not-owned proposal is 404 (no existence leak).
- Default-deny (Iron Rule 7): unresolved-link + high-risk + low-confidence
  proposals never auto-apply.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from careerops.application.mail_extraction import (
    DeterministicMailExtractor,
    MailMessageInput,
    MailModelExtractor,
    ModelExtractionInvalidError,
    ModelExtractionUnavailableError,
    compute_extraction_hash,
)
from careerops.domain.applications import (
    Application,
    ApplicationState,
    IllegalTransitionError,
    validate_transition,
)
from careerops.domain.mail_intelligence import (
    EmailEventProposal,
    EmailEventProposalState,
    MailExtraction,
    MessageNotLinkedError,
    ProposalAlreadyDecidedError,
    ProposalNotOwnedError,
    ProposedStateIllegalError,
    category_to_proposed_state,
    validate_proposal_transition,
)
from careerops.observability.career_loop_trace import CareerLoopTrace
from careerops.orchestration.capability_resolver import (
    CapabilityDecision,
    CapabilityKind,
)

__all__ = [
    "STALE_MESSAGE_DAYS",
    "ApplicationOwnershipReader",
    "ApplicationTimelineSink",
    "FollowUpScheduler",
    "MailIntelligenceService",
    "MailMessageRepository",
    "MailProposalRepository",
    "ProposalDecisionResult",
]


# A message older than this many days is flagged stale (task 12.9). The proposal
# is still created for review; staleness blocks auto-apply (not enabled here)
# and surfaces a stale marker in the UI.
STALE_MESSAGE_DAYS: int = 30


# ---------------------------------------------------------------------------
# Protocols (repos / sinks)
# ---------------------------------------------------------------------------


class MailMessageRepository(Protocol):
    """Read a minimized, sanitized message for extraction (task 12.3 input)."""

    def find_message(self, message_id: UUID, candidate_id: UUID) -> MailMessageInput | None: ...


class MailProposalRepository(Protocol):
    """Durable :class:`EmailEventProposal` store (task 12.5)."""

    def find_by_id(self, proposal_id: UUID) -> EmailEventProposal | None: ...

    def find_by_idempotency_key(self, key: str) -> EmailEventProposal | None: ...

    def find_active_for_message(self, message_id: UUID) -> EmailEventProposal | None: ...

    def save(self, proposal: EmailEventProposal) -> None: ...

    def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        state: EmailEventProposalState | None = None,
        limit: int = 50,
        cursor: tuple[datetime, UUID] | None = None,
    ) -> tuple[list[EmailEventProposal], tuple[datetime, UUID] | None]: ...


class ApplicationOwnershipReader(Protocol):
    """Reads an owned application for transition validation (task 12.6)."""

    def find_owned_application(
        self, *, application_id: UUID, candidate_id: UUID
    ) -> Application | None: ...


class ApplicationTimelineSink(Protocol):
    """Appends a mail-derived timeline note (task 12.7).

    The note records that a mail-derived event was accepted/rejected WITHOUT
    re-implementing the event log: the state-change event itself is appended by
    the workspace transition service (USER-sourced); this sink adds the
    mail-specific provenance (message id, proposal id) so the unified timeline
    can cite the source message.
    """

    def append_note(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        note: str,
        event_data: dict[str, object],
        now: datetime,
    ) -> None: ...


class FollowUpScheduler(Protocol):
    """Creates an interview / deadline / follow-up task (task 12.7).

    Called ONLY when policy permits (interview/assessment category with a
    concrete time). Implementations append the appropriate scheduling event;
    this Protocol keeps the service decoupled from the Section 13 follow-up
    rules.
    """

    def schedule_for_proposal(
        self,
        *,
        application_id: UUID,
        candidate_id: UUID,
        extraction: MailExtraction,
        proposal_id: UUID,
        now: datetime,
    ) -> UUID | None: ...


class CapabilityResolver(Protocol):
    """Minimal resolver surface for model-enrichment gating."""

    def decide(self, capability: CapabilityKind) -> CapabilityDecision: ...


# ---------------------------------------------------------------------------
# Decision result DTO
# ---------------------------------------------------------------------------


class ProposalDecisionResult:
    """Result of an accept/reject decision (task 12.6 idempotent response).

    ``already_decided`` is True when the proposal was already in the requested
    terminal state; the caller surfaces the recorded result rather than
    re-applying a transition. ``application`` is the post-accept application
    (None for unresolved-link / rejected proposals).
    """

    __slots__ = ("already_decided", "application", "proposal")

    def __init__(
        self,
        *,
        proposal: EmailEventProposal,
        application: Application | None,
        already_decided: bool = False,
    ) -> None:
        self.proposal = proposal
        self.application = application
        self.already_decided = already_decided


# ---------------------------------------------------------------------------
# MailIntelligenceService
# ---------------------------------------------------------------------------


class MailIntelligenceService:
    """Extract → propose → review (accept/reject) for recruiting mail."""

    def __init__(
        self,
        *,
        message_repo: MailMessageRepository,
        proposal_repo: MailProposalRepository,
        ownership_reader: ApplicationOwnershipReader,
        timeline_sink: ApplicationTimelineSink | None = None,
        follow_up_scheduler: FollowUpScheduler | None = None,
        capability_resolver: CapabilityResolver | None = None,
        deterministic_extractor: DeterministicMailExtractor | None = None,
        model_extractor: MailModelExtractor | None = None,
        stale_days: int = STALE_MESSAGE_DAYS,
        trace: CareerLoopTrace | None = None,
    ) -> None:
        self._messages = message_repo
        self._proposals = proposal_repo
        self._ownership = ownership_reader
        self._timeline = timeline_sink
        self._follow_ups = follow_up_scheduler
        self._capability_resolver = capability_resolver
        self._deterministic = deterministic_extractor or DeterministicMailExtractor()
        self._model = model_extractor
        self._stale_days = stale_days
        self._trace = trace

    # ------------------------------------------------------------------
    # 12.5 — extract + propose (idempotent)
    # ------------------------------------------------------------------

    def extract_and_propose(
        self,
        *,
        message_id: UUID,
        candidate_id: UUID,
        application_id: UUID | None = None,
        now: datetime,
    ) -> EmailEventProposal:
        """Extract a message and create/reuse a proposal (task 12.5).

        ``application_id`` is the resolved application link (from the
        thread-association step). ``None`` is allowed: the proposal is created
        without a link and surfaced for unresolved-link review (task 12.10).

        Idempotency: a re-run for the same message returns the existing
        proposal (matched by message id + extraction content hash). The
        application link, once set on the existing pending proposal, is kept.

        Iron Rule 2: this method writes NO application state — it only records a
        reviewable proposal. High-risk / low-confidence / unknown /
        injection-flagged proposals stay ``review_required``.
        """
        message = self._messages.find_message(message_id, candidate_id)
        if message is None:
            raise ProposalNotOwnedError(message_id)

        extraction = self._run_extraction(message)

        idempotency_key = self._idempotency_key(message.message_id, extraction)
        existing = self._proposals.find_by_idempotency_key(idempotency_key)
        if existing is not None:
            # Keep the most-recently resolved application link (do not lose a
            # link that was set after the original extraction).
            if application_id is not None and existing.application_id is None:
                updated = self._with_link(existing, application_id, now)
                self._proposals.save(updated)
                return updated
            return existing

        proposal = EmailEventProposal(
            id=uuid4(),
            message_id=message.message_id,
            thread_id=message.thread_id,
            account_id=message.account_id,
            candidate_id=candidate_id,
            application_id=application_id,
            extraction=extraction,
            proposed_state=category_to_proposed_state(extraction.category),
            idempotency_key=idempotency_key,
            state=EmailEventProposalState.PENDING,
            decided_at=None,
            decided_by="",
            created_at=now,
            updated_at=now,
        )
        self._proposals.save(proposal)
        if self._trace is not None and application_id is None:
            self._trace.record_mail_linkage(outcome="unresolved")
        return proposal

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get_proposal(self, *, proposal_id: UUID, candidate_id: UUID) -> EmailEventProposal:
        proposal = self._proposals.find_by_id(proposal_id)
        if proposal is None or proposal.candidate_id != candidate_id:
            raise ProposalNotOwnedError(proposal_id)
        return proposal

    def list_proposals(
        self,
        *,
        candidate_id: UUID,
        state: EmailEventProposalState | None = None,
        limit: int = 50,
        cursor: tuple[datetime, UUID] | None = None,
    ) -> tuple[list[EmailEventProposal], tuple[datetime, UUID] | None]:
        return self._proposals.list_for_candidate(
            candidate_id, state=state, limit=limit, cursor=cursor
        )

    # ------------------------------------------------------------------
    # 12.6 + 12.7 — accept
    # ------------------------------------------------------------------

    def accept_proposal(
        self,
        *,
        proposal_id: UUID,
        candidate_id: UUID,
        now: datetime,
    ) -> ProposalDecisionResult:
        """Accept a proposal (task 12.6 + 12.7).

        Validates ownership + the legal proposal lifecycle transition; for a
        linked proposal with a proposed state, validates the application
        transition is legal from the current state and delegates to the
        workspace USER-transition path (the proposal itself never writes state).
        Appends a mail-derived timeline note and schedules any follow-up task.

        Idempotent: a repeat accept of an already-accepted proposal returns the
        recorded result without re-applying the transition. A rejected proposal
        may not be accepted (terminal).
        """
        proposal = self.get_proposal(proposal_id=proposal_id, candidate_id=candidate_id)

        if proposal.state is EmailEventProposalState.ACCEPTED:
            application = self._read_application(proposal, candidate_id)
            return ProposalDecisionResult(
                proposal=proposal, application=application, already_decided=True
            )
        if proposal.state is EmailEventProposalState.REJECTED:
            raise ProposalAlreadyDecidedError(proposal.id, proposal.state)
        validate_proposal_transition(proposal.state, EmailEventProposalState.ACCEPTED)

        application = self._apply_acceptance(proposal, candidate_id, now)
        accepted = self._mark_decided(proposal, EmailEventProposalState.ACCEPTED, candidate_id, now)
        self._proposals.save(accepted)
        if self._trace is not None:
            self._trace.record_mail_linkage(outcome="linked")
            self._trace.record_review_latency(
                review_kind="mail_proposal",
                created_at=proposal.created_at,
                decided_at=now,
            )
        return ProposalDecisionResult(
            proposal=accepted, application=application, already_decided=False
        )

    # ------------------------------------------------------------------
    # 12.6 — reject
    # ------------------------------------------------------------------

    def reject_proposal(
        self,
        *,
        proposal_id: UUID,
        candidate_id: UUID,
        now: datetime,
    ) -> ProposalDecisionResult:
        """Reject a proposal (task 12.6).

        Records the rejection, leaves application state unchanged, and blocks
        the same proposal from being accepted later (terminal). Idempotent: a
        repeat reject returns the recorded result.
        """
        proposal = self.get_proposal(proposal_id=proposal_id, candidate_id=candidate_id)

        if proposal.state is EmailEventProposalState.REJECTED:
            return ProposalDecisionResult(proposal=proposal, application=None, already_decided=True)
        if proposal.state is EmailEventProposalState.ACCEPTED:
            raise ProposalAlreadyDecidedError(proposal.id, proposal.state)
        validate_proposal_transition(proposal.state, EmailEventProposalState.REJECTED)

        rejected = self._mark_decided(proposal, EmailEventProposalState.REJECTED, candidate_id, now)
        self._proposals.save(rejected)
        if self._trace is not None:
            self._trace.record_review_latency(
                review_kind="mail_proposal",
                created_at=proposal.created_at,
                decided_at=now,
            )
        return ProposalDecisionResult(proposal=rejected, application=None, already_decided=False)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _run_extraction(self, message: MailMessageInput) -> MailExtraction:
        deterministic = self._deterministic.extract(message)
        if not self._is_model_enabled():
            return deterministic
        if self._model is None:
            return deterministic
        try:
            model_out = self._model.extract(message)
        except (ModelExtractionUnavailableError, Exception):
            # Model unavailable / crashed → fall back to deterministic
            # (task 12.9 model-unavailable path).
            return deterministic
        try:
            from careerops.application.mail_extraction import merge_model_output

            return merge_model_output(deterministic, model_out)
        except ModelExtractionInvalidError:
            # Invalid schema → fall back to deterministic (task 12.9).
            return deterministic

    def _is_model_enabled(self) -> bool:
        if self._capability_resolver is None or self._model is None:
            return False
        try:
            decision = self._capability_resolver.decide(CapabilityKind.MODEL_TAILORING)
        except Exception:
            return False
        return bool(decision.released)

    def _idempotency_key(self, message_id: UUID, extraction: MailExtraction) -> str:
        return f"mail:{message_id}:{compute_extraction_hash(extraction)}"

    def _with_link(
        self, proposal: EmailEventProposal, application_id: UUID, now: datetime
    ) -> EmailEventProposal:
        from dataclasses import replace

        return replace(proposal, application_id=application_id, updated_at=now)

    def _mark_decided(
        self,
        proposal: EmailEventProposal,
        to_state: EmailEventProposalState,
        candidate_id: UUID,
        now: datetime,
    ) -> EmailEventProposal:
        from dataclasses import replace

        return replace(
            proposal,
            state=to_state,
            decided_at=now,
            decided_by=str(candidate_id),
            updated_at=now,
        )

    def _read_application(
        self, proposal: EmailEventProposal, candidate_id: UUID
    ) -> Application | None:
        if proposal.application_id is None:
            return None
        return self._ownership.find_owned_application(
            application_id=proposal.application_id, candidate_id=candidate_id
        )

    def _apply_acceptance(
        self,
        proposal: EmailEventProposal,
        candidate_id: UUID,
        now: datetime,
    ) -> Application | None:
        """Apply the USER-sourced transition + timeline note (task 12.7).

        Delegates the actual state change to the workspace service so the
        proposal never writes :class:`ApplicationState` directly. An unlinked
        proposal cannot transition (task 12.10).
        """
        if proposal.application_id is None:
            raise MessageNotLinkedError(proposal.id)

        application = self._ownership.find_owned_application(
            application_id=proposal.application_id, candidate_id=candidate_id
        )
        if application is None:
            raise ProposalNotOwnedError(proposal.id)

        proposed_state = proposal.proposed_state
        note = self._acceptance_note(proposal)
        event_data: dict[str, object] = {
            "proposal_id": str(proposal.id),
            "message_id": str(proposal.message_id),
            "category": proposal.extraction.category.value,
            "mail_source": proposal.extraction.source.value,
        }

        # If the category proposes a state, validate + delegate the USER
        # transition (Iron Rule 2). IllegalTransitionError is caught at the API
        # layer and mapped to 422 (task 12.6 legal-transition validation).
        if proposed_state is not None:
            to_state = ApplicationState(proposed_state)
            try:
                validate_transition(application.state, to_state)
            except IllegalTransitionError as err:
                raise ProposedStateIllegalError(application.state.value, proposed_state) from err
            # Delegate to the workspace's USER-transition path. The workspace
            # service is the authoritative writer of ApplicationState; this
            # indirection is what keeps the proposal from writing state itself.
            # The mail provenance (proposal_id / message_id / category) is
            # recorded in a SEPARATE timeline note below, not on the
            # state-changed event.
            self._delegate_transition(
                application=application,
                candidate_id=candidate_id,
                to_state=to_state,
                note=note,
                now=now,
            )
            refreshed = self._refresh_application(application.id, candidate_id)
            if refreshed is None:
                raise ProposalNotOwnedError(proposal.id)
            application = refreshed
        # Always append the mail-derived provenance note (state change or not)
        # so the unified timeline cites the source message + proposal.
        if self._timeline is not None:
            self._timeline.append_note(
                application_id=application.id,
                candidate_id=candidate_id,
                note=note,
                event_data=event_data,
                now=now,
            )

        # Schedule interview / follow-up where policy permits (12.7).
        if self._follow_ups is not None and self._policy_allows_follow_up(proposal):
            self._follow_ups.schedule_for_proposal(
                application_id=application.id,
                candidate_id=candidate_id,
                extraction=proposal.extraction,
                proposal_id=proposal.id,
                now=now,
            )
        return application

    def _delegate_transition(
        self,
        *,
        application: Application,
        candidate_id: UUID,
        to_state: ApplicationState,
        note: str,
        now: datetime,
    ) -> None:
        """Write the USER-sourced transition via the workspace sink.

        ``ApplicationWorkspaceService.apply_user_transition`` is wired by the
        runtime via :meth:`set_transition_sink`; it records a USER-sourced
        state-changed event and is the authoritative writer of
        :class:`ApplicationState`. This indirection is what keeps the proposal
        from writing state itself (Iron Rule 2). The callable signature matches
        ``apply_user_transition(application_id, candidate_id, to_state, note, now)``.
        """
        sink = getattr(self, "_transition_sink", None)
        if sink is not None:
            sink(
                application_id=application.id,
                candidate_id=candidate_id,
                to_state=to_state,
                note=note,
                now=now,
            )
            return
        # Fallback for tests / standalone service construction with no
        # transition sink wired: record a note only (no state change). The full
        # runtime always wires the transition sink.
        if self._timeline is not None:
            self._timeline.append_note(
                application_id=application.id,
                candidate_id=candidate_id,
                note=note,
                event_data={"to_state": to_state.value},
                now=now,
            )

    def set_transition_sink(self, sink: Callable[..., object] | None) -> None:
        """Wire the USER-sourced transition writer (runtime DI helper).

        The runtime passes ``ApplicationWorkspaceService.apply_user_transition``
        (bound) so this service can delegate the state change without owning the
        ApplicationRepository directly. The callable must record a USER-sourced
        ApplicationEvent.
        """
        self._transition_sink = sink  # type: ignore[attr-defined]

    def _refresh_application(self, application_id: UUID, candidate_id: UUID) -> Application | None:
        return self._ownership.find_owned_application(
            application_id=application_id, candidate_id=candidate_id
        )

    def _policy_allows_follow_up(self, proposal: EmailEventProposal) -> bool:
        from careerops.domain.mail_intelligence import MailCategory

        category = proposal.extraction.category
        # Interview / assessment categories with a concrete time get a follow-up.
        if category in (MailCategory.INTERVIEW, MailCategory.ASSESSMENT):
            return proposal.extraction.interview_at is not None
        # Request-more-info / acknowledgement with a deadline get a follow-up.
        if category in (MailCategory.REQUEST_MORE_INFO, MailCategory.ACKNOWLEDGEMENT):
            return proposal.extraction.deadline is not None
        return False

    def _acceptance_note(self, proposal: EmailEventProposal) -> str:
        category = proposal.extraction.category.value.replace("_", " ")
        who = proposal.extraction.sender_name or proposal.extraction.sender_email
        who = f" from {who}" if who else ""
        return f"Mail-derived event accepted ({category}{who})"

    def is_stale(self, proposal: EmailEventProposal, now: datetime) -> bool:
        """Return True if the source message is older than the stale window."""
        created = proposal.created_at or now
        age_days = (now - created).days
        return age_days > self._stale_days
