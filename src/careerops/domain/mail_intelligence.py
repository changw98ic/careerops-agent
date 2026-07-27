"""Domain contracts for recruiting mail intelligence (Section 12).

Additive types that sit on top of :mod:`careerops.domain.applications`
WITHOUT mutating its state machine or writing application state. The flow is:

- **12.1** a controlled ``MailCategory`` taxonomy covering every recruiting-mail
  outcome the system reasons about (``unknown`` is the explicit fallback so a
  message is never forced into a wrong bucket).
- **12.2 / 12.3** a :class:`MailExtraction` record produced FIRST by
  deterministic rules (sender, dates, timezones, deadlines, obvious outcomes)
  and OPTIONALLY enriched by a model. The model output is DATA: it never
  selects a recipient, transitions state, or writes a trusted claim.
- **12.4** high-risk categories (offer, salary, visa, identity, withdrawal)
  force ``review_required = True`` and block any autonomous action.
- **12.5** a durable :class:`EmailEventProposal` linking a message/thread to an
  application (or left unresolved when the link is ambiguous). The proposal has
  its own lifecycle (``pending`` → ``accepted`` / ``rejected``) and is
  idempotent by a stable key derived from (message, extraction hash).

Iron rules honored:
- Additive (Iron Rule 8): new types only; no existing domain record mutated.
- Model review-only (Iron Rule 2): no type here lets a proposal write
  :class:`ApplicationState`. Acceptance delegates to the existing USER-sourced
  transition service; the proposal itself only records a reviewable suggestion.
- Append-only / reversible (Iron Rule 4): a decision never rewrites a prior
  decision — the lifecycle table is monotonic (pending → terminal) and the
  application timeline is append-only.
- Default-deny (Iron Rule 7): high-risk + low-confidence + unresolved-link
  proposals stay ``review_required`` and never auto-apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

__all__ = [
    "HIGH_RISK_CATEGORIES",
    "LOW_CONFIDENCE_THRESHOLD",
    "MAIL_MODEL_VERSION",
    "MAIL_RULES_VERSION",
    "PROPOSAL_TRANSITIONS",
    "EmailEventProposal",
    "EmailEventProposalState",
    "EmailEvidenceSpan",
    "IllegalProposalTransitionError",
    "MailCategory",
    "MailExtraction",
    "MailExtractionSource",
    "MailIntelligenceError",
    "MailOutcome",
    "MessageNotLinkedError",
    "ProposalAlreadyDecidedError",
    "ProposalNotOwnedError",
    "ProposedStateIllegalError",
    "StaleMessageError",
    "category_to_proposed_state",
    "is_high_risk_category",
    "outcome_for_category",
    "validate_proposal_transition",
]


# ---------------------------------------------------------------------------
# 12.1 — controlled mail taxonomy
# ---------------------------------------------------------------------------


class MailCategory(StrEnum):
    """Controlled recruiting-mail taxonomy (task 12.1).

    ``UNKNOWN`` is the explicit fallback: a message that does not match a
    controlled bucket is never forced into one. The category drives the
    proposed application state via :func:`category_to_proposed_state`.
    """

    ACKNOWLEDGEMENT = "acknowledgement"
    SCREENING = "screening"
    INTERVIEW = "interview"
    ASSESSMENT = "assessment"
    REQUEST_MORE_INFO = "request_more_info"
    REJECTION = "rejection"
    OFFER = "offer"
    SALARY = "salary"
    VISA = "visa"
    IDENTITY = "identity"
    WITHDRAWAL = "withdrawal"
    UNKNOWN = "unknown"


class MailOutcome(StrEnum):
    """Obvious outcome of a recruiting message (deterministic, task 12.2).

    ``POSITIVE`` advances the candidate (interview / offer), ``NEGATIVE`` ends
    the loop (rejection / withdrawal), ``NEUTRAL`` keeps it open
    (acknowledgement / screening / assessment / request-more-info / salary /
    visa / identity), ``UNKNOWN`` is the fail-safe.
    """

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class MailExtractionSource(StrEnum):
    """Which stage produced the extraction (task 12.2 vs 12.3)."""

    RULES = "rules"
    MODEL = "model"


class EmailEventProposalState(StrEnum):
    """Lifecycle of an :class:`EmailEventProposal` (task 12.5 / 12.6).

    ``PENDING`` proposals await a user review decision. ``ACCEPTED`` /
    ``REJECTED`` are terminal: a repeat decision returns the recorded result
    without re-applying a transition. ``SUPERSEDED`` is reserved for a future
    re-extraction that produces a newer proposal for the same message (the
    prior proposal is retired, never deleted).
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


# Task 12.4: categories that ALWAYS force mandatory user review and block any
# autonomous provider/action side effect. Offer / salary / visa / identity /
# withdrawal carry legal, financial, or authorization consequences.
HIGH_RISK_CATEGORIES: frozenset[MailCategory] = frozenset(
    {
        MailCategory.OFFER,
        MailCategory.SALARY,
        MailCategory.VISA,
        MailCategory.IDENTITY,
        MailCategory.WITHDRAWAL,
    }
)

# A deterministic extraction below this confidence (or any high-risk category)
# forces ``review_required = True`` (task 12.4 + 12.9 low-confidence path).
LOW_CONFIDENCE_THRESHOLD: float = 0.6

# Version metadata stamped on every extraction (task 12.3 version metadata).
MAIL_RULES_VERSION: str = "section12-mail-rules-v1"
MAIL_MODEL_VERSION: str = "section12-mail-model-v1"


def is_high_risk_category(category: MailCategory) -> bool:
    """Return True for the categories that force mandatory user review."""
    return category in HIGH_RISK_CATEGORIES


def outcome_for_category(category: MailCategory) -> MailOutcome:
    """Deterministic obvious-outcome mapping (task 12.2)."""
    if category is MailCategory.INTERVIEW:
        return MailOutcome.POSITIVE
    if category in (MailCategory.OFFER, MailCategory.SALARY):
        # Offer / salary are advancement signals but high-risk, so the
        # *outcome* is positive while the *review requirement* is mandatory.
        return MailOutcome.POSITIVE
    if category in (MailCategory.REJECTION, MailCategory.WITHDRAWAL):
        return MailOutcome.NEGATIVE
    if category is MailCategory.UNKNOWN:
        return MailOutcome.UNKNOWN
    return MailOutcome.NEUTRAL


def category_to_proposed_state(category: MailCategory) -> str | None:
    """Map a category to the application state it would propose (task 12.7).

    Returns ``None`` for categories that do NOT propose a state change
    (acknowledgement, screening, assessment, request-more-info, salary, visa,
    identity, unknown) — those produce a reviewable timeline note only, never a
    state transition. ``interview``/``offer``/``rejection``/``withdrawal``
    propose the corresponding legal target state; the transition is still
    validated against the application's current state at accept time and is
      applied by the existing USER-sourced transition service (Iron Rule 2).
    """
    mapping: dict[MailCategory, str] = {
        MailCategory.INTERVIEW: "interviewing",
        MailCategory.OFFER: "offer",
        MailCategory.REJECTION: "rejected",
        MailCategory.WITHDRAWAL: "withdrawn",
    }
    return mapping.get(category)


# ---------------------------------------------------------------------------
# Evidence spans + extraction record (tasks 12.2 / 12.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmailEvidenceSpan:
    """A bounded, citable excerpt from the message body (task 12.3).

    ``start`` / ``end`` are character offsets into the cleaned message text so
    the UI can highlight exactly what supported the extraction. The offsets are
    clamped at construction; an empty span (``start == end``) is allowed for
    metadata-only fields (sender / provider header) that have no body text.
    """

    label: str
    text: str
    start: int = 0
    end: int = 0

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < 0:
            raise ValueError("evidence span offsets must be non-negative")
        if self.end < self.start:
            raise ValueError("evidence span end must be >= start")


@dataclass(frozen=True, slots=True)
class MailExtraction:
    """Structured extraction for one recruiting message (tasks 12.2 / 12.3).

    Fields:

    - ``category``: the controlled-taxonomy bucket.
    - ``outcome``: the obvious deterministic outcome.
    - ``sender_email`` / ``sender_name`` / ``sender_domain``: trusted provider
      metadata + parsed identity (NEVER trusted enough to act without review).
    - ``interview_at`` / ``timezone`` / ``deadline``: extracted date/time
      expressions with their named timezone (``America/New_York`` etc.).
    - ``requested_materials``: free-text list of items the recruiter asked for.
    - ``compensation``: any compensation figure mentioned (review-only).
    - ``summary``: one-line plain-text summary (model or rules produced).
    - ``confidence``: ``[0.0, 1.0]``. Below :data:`LOW_CONFIDENCE_THRESHOLD` the
      proposal is ``review_required``.
    - ``evidence_spans``: bounded citable excerpts (never the full body).
    - ``source``: RULES (deterministic, always) or MODEL (optional enrichment).
    - ``rules_version`` / ``model_version``: version metadata for replay.
    - ``high_risk`` / ``review_required``: derived flags (12.4).
    - ``prompt_injection_detected``: True if the deterministic scanner flagged
      injection-shaped content; the proposal stays review-only regardless.
    """

    category: MailCategory
    outcome: MailOutcome
    sender_email: str = ""
    sender_name: str = ""
    sender_domain: str = ""
    interview_at: datetime | None = None
    timezone: str = ""
    deadline: datetime | None = None
    requested_materials: tuple[str, ...] = ()
    compensation: str = ""
    summary: str = ""
    confidence: float = 0.0
    evidence_spans: tuple[EmailEvidenceSpan, ...] = ()
    source: MailExtractionSource = MailExtractionSource.RULES
    rules_version: str = MAIL_RULES_VERSION
    model_version: str = ""
    high_risk: bool = False
    review_required: bool = True
    prompt_injection_detected: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0.0, 1.0]")
        # Derive high_risk from the category when the caller did not set it.
        # ``object.__setattr__`` because the dataclass is frozen.
        if not self.high_risk and is_high_risk_category(self.category):
            object.__setattr__(self, "high_risk", True)
        # Anything high-risk, low-confidence, unknown, or injection-flagged is
        # mandatory-review (task 12.4 / 12.9).
        forced_review = (
            self.high_risk
            or self.confidence < LOW_CONFIDENCE_THRESHOLD
            or self.category is MailCategory.UNKNOWN
            or self.prompt_injection_detected
        )
        if forced_review and not self.review_required:
            object.__setattr__(self, "review_required", True)


# ---------------------------------------------------------------------------
# EmailEventProposal (task 12.5)
# ---------------------------------------------------------------------------


# Legal proposal lifecycle transitions (task 12.6). Terminal states are
# sink-only: a repeat decision is idempotent (returns the recorded result) and
# never re-applies a transition.
PROPOSAL_TRANSITIONS: dict[EmailEventProposalState, frozenset[EmailEventProposalState]] = {
    EmailEventProposalState.PENDING: frozenset(
        {EmailEventProposalState.ACCEPTED, EmailEventProposalState.REJECTED}
    ),
    EmailEventProposalState.ACCEPTED: frozenset(),
    EmailEventProposalState.REJECTED: frozenset(),
    EmailEventProposalState.SUPERSEDED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class EmailEventProposal:
    """Durable, reviewable mail-derived event proposal (task 12.5).

    Links one message (and its thread) to at most one application. When the
    thread-association step could not resolve a single application
    (``application_id is None``) the proposal is created without a link and
    surfaced for unresolved-link review (task 12.10). Acceptance is the ONLY
    path that may touch application state, and it does so by delegating to the
    existing USER-sourced transition service — the proposal itself never writes
    :class:`ApplicationState` (Iron Rule 2).

    ``idempotency_key`` makes extraction idempotent: re-running the deterministic
    + model extraction for the same message produces the same key (message id +
    extraction content hash) and returns the existing proposal unchanged.
    """

    id: UUID
    message_id: UUID
    thread_id: UUID | None
    account_id: UUID | None
    candidate_id: UUID
    application_id: UUID | None
    extraction: MailExtraction
    proposed_state: str | None
    idempotency_key: str
    state: EmailEventProposalState = EmailEventProposalState.PENDING
    decided_at: datetime | None = None
    decided_by: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Errors (translated at the API layer)
# ---------------------------------------------------------------------------


class MailIntelligenceError(Exception):
    """Base for mail-intelligence service errors."""


class ProposalNotOwnedError(MailIntelligenceError):
    """The proposal does not exist or is not owned by the candidate.

    Raised for BOTH missing and not-owned so the response never leaks that a
    proposal exists for another candidate (Iron Rule 2 / 6).
    """

    def __init__(self, proposal_id: UUID) -> None:
        super().__init__(f"proposal not found for candidate: {proposal_id}")


class ProposalAlreadyDecidedError(MailIntelligenceError):
    """A terminal proposal received a new decision request (task 12.6).

    Carries the recorded state so the API layer can map a repeat decision to a
    409/idempotent-response instead of re-applying a transition.
    """

    def __init__(self, proposal_id: UUID, state: EmailEventProposalState) -> None:
        self.state = state
        super().__init__(f"proposal {proposal_id} already decided: {state.value}")


class ProposedStateIllegalError(MailIntelligenceError):
    """The proposed application transition is illegal from the current state."""

    def __init__(self, current_state: str, proposed_state: str) -> None:
        self.current_state = current_state
        self.proposed_state = proposed_state
        super().__init__(f"proposed mail transition {current_state} -> {proposed_state} is illegal")


class IllegalProposalTransitionError(MailIntelligenceError):
    """The proposal lifecycle transition itself is illegal (not the app state)."""

    def __init__(
        self, from_state: EmailEventProposalState, to_state: EmailEventProposalState
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"illegal proposal transition: {from_state.value} -> {to_state.value}")


class MessageNotLinkedError(MailIntelligenceError):
    """The proposal has no resolved application link (task 12.10).

    Acceptance requires the user (or the unresolved-link review step) to
    confirm the application first; an unlinked proposal cannot transition state.
    """

    def __init__(self, proposal_id: UUID) -> None:
        super().__init__(f"proposal {proposal_id} has no resolved application link")


class StaleMessageError(MailIntelligenceError):
    """The source message is older than the stale-message policy window.

    The proposal is still created (so the user can review it) but is flagged
    stale; this error is raised only when a caller tries to treat a stale
    proposal as fresh (e.g. auto-accept, which is not enabled in this change).
    """


def validate_proposal_transition(
    from_state: EmailEventProposalState, to_state: EmailEventProposalState
) -> None:
    """Validate a proposal lifecycle transition (task 12.6).

    A terminal → anything transition raises so the API can map it to an
    idempotent repeat-decision response (return the recorded result) rather
    than silently re-applying.
    """
    allowed = PROPOSAL_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise IllegalProposalTransitionError(from_state, to_state)
