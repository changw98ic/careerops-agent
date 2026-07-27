"""Domain contracts for reply drafting + follow-up management (Section 13).

Additive types that sit on top of :mod:`careerops.domain.applications` (M3
follow-ups) and :mod:`careerops.domain.mail_intelligence` (Section 12 mail
categories) WITHOUT mutating their state machines. The flow is:

- **13.1** versioned follow-up rules with default waiting periods per trigger
  state (``submitted`` / ``awaiting_response`` / ``interview`` /
  ``assessment`` / ``user``). Each rule version is an opaque string; a
  reminder links to its rule version so a rule change never silently rewrites
  an existing reminder (Iron Rule 4).
- **13.3** :class:`ReplyDraftContext` is the constrained trusted context
  assembled from a linked thread (bounded excerpt), confirmed application
  facts, selected evidence and the user-selected intent. Model output is DATA:
  it never selects the recipient, intent or trusted facts.
- **13.4 / 13.5** :class:`ReplyDraft` is an immutable, payload-hash-bound
  version. The recipient + thread headers (``in_reply_to`` /
  ``references_header``) are immutable per draft; a body edit creates a NEW
  version with a fresh payload hash, and a recipient / target-thread edit is
  rejected — it must create a brand-new draft that re-enters recipient trust,
  policy and approval checks.
- **13.6** high-risk categories are permanently denied system-managed sending
  (draft/review only); auto-send is permanently denied for every reply.

Iron rules honored:
- Additive (Iron Rule 8): new types only; no existing domain record mutated.
- Model review-only (Iron Rule 2): no type here lets model output select a
  recipient, fabricate a verdict, or compute a trusted hash.
- Append-only / reversible (Iron Rule 4): versions are immutable; decisions
  are monotonic (draft -> terminal).
- Default-deny (Iron Rule 7): high-risk categories + auto-send are
  permanently denied at the contract layer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

__all__ = [
    "DEFAULT_FOLLOW_UP_WAITING_PERIODS",
    "FOLLOW_UP_RULE_VERSION_S13",
    "HIGH_RISK_REPLY_CATEGORIES",
    "PERMANENTLY_DENIED_REPLY_CATEGORIES",
    "REPLY_DRAFT_RULES_VERSION",
    "REPLY_DRAFT_TRANSITIONS",
    "REPLY_EXCERPT_MAX_CHARS",
    "ApplicationFact",
    "FollowUpTriggerState",
    "IllegalReplyTransitionError",
    "MessageNotLinkedError",
    "RecipientMutationError",
    "ReplyDraft",
    "ReplyDraftAlreadyDecidedError",
    "ReplyDraftApprovalState",
    "ReplyDraftClaim",
    "ReplyDraftContext",
    "ReplyDraftError",
    "ReplyDraftNotOwnedError",
    "ReplyIntent",
    "ReplyRiskCategory",
    "ReplySendDeniedError",
    "UnsupportedClaimError",
    "compute_reply_payload_hash",
    "default_waiting_period",
    "is_high_risk_reply",
    "is_system_send_denied",
    "reply_intent_for_category",
    "reply_risk_for_category",
    "reply_risk_for_intent",
    "validate_reply_claims",
]


# ---------------------------------------------------------------------------
# 13.1 — versioned follow-up rules + default waiting periods
# ---------------------------------------------------------------------------


# Section-13 rule version. Distinct from the M3 ``m3-followup-v1`` so a
# reminder scheduled under the Section-13 ruleset is never confused with one
# scheduled under the legacy ruleset (the (application, rule_version) unique
# index keeps them separate). Bumped whenever the waiting-period table or the
# trigger semantics change.
FOLLOW_UP_RULE_VERSION_S13: str = "section13-followup-v2"

# Version metadata stamped on every reply-draft validation result (task 13.3).
REPLY_DRAFT_RULES_VERSION: str = "section13-reply-draft-v1"


class FollowUpTriggerState(StrEnum):
    """Why a follow-up reminder exists (task 13.1).

    Drives the default business-day waiting period. ``USER`` is the explicit
    user-scheduled case (the user picked the wait time); the others are
    derived from confirmed application events / mail outcomes.
    """

    SUBMITTED = "submitted"
    AWAITING_RESPONSE = "awaiting_response"
    INTERVIEW = "interview"
    ASSESSMENT = "assessment"
    USER = "user"


# Default business-day waiting periods per trigger state (task 13.1). Mon-Fri
# only; weekends skipped by ``compute_follow_up_due_date`` in the M3 service.
# Deliberately conservative: a submitted application waits longer than an
# interview slot that has a concrete time.
DEFAULT_FOLLOW_UP_WAITING_PERIODS: dict[FollowUpTriggerState, int] = {
    FollowUpTriggerState.SUBMITTED: 5,
    FollowUpTriggerState.AWAITING_RESPONSE: 7,
    FollowUpTriggerState.INTERVIEW: 3,
    FollowUpTriggerState.ASSESSMENT: 4,
    FollowUpTriggerState.USER: 10,
}


def default_waiting_period(trigger: FollowUpTriggerState) -> int:
    """Return the default business-day wait for a trigger state."""
    return DEFAULT_FOLLOW_UP_WAITING_PERIODS.get(trigger, 5)


# ---------------------------------------------------------------------------
# 13.3 — reply intent + risk taxonomy
# ---------------------------------------------------------------------------


class ReplyIntent(StrEnum):
    """User-selected intent for a reply draft (task 13.3).

    Selected by the USER (never the model). Drives the default risk category
    and the claim-validation policy. ``CUSTOM`` is the explicit free-form case.
    """

    ACKNOWLEDGE = "acknowledge"
    SCHEDULE = "schedule"
    PROVIDE_INFO = "provide_info"
    REQUEST_INFO = "request_info"
    DECLINE = "decline"
    CUSTOM = "custom"


class ReplyRiskCategory(StrEnum):
    """Risk classification of a reply draft (tasks 13.4 / 13.6).

    ``LOW_RISK`` / ``SCHEDULING`` / ``INFO_REQUEST`` may be system-sent after
    explicit user approval. ``HIGH_RISK`` and ``PERMANENTLY_DENIED`` can never
    be sent through the system-managed chain — they are draft/review only and
    the user handles them manually (Iron Rule 7). The mapping from a Section-12
    mail category is deterministic (task 13.6 high-risk denial).
    """

    LOW_RISK = "low_risk"
    SCHEDULING = "scheduling"
    INFO_REQUEST = "info_request"
    HIGH_RISK = "high_risk"
    PERMANENTLY_DENIED = "permanently_denied"
    UNKNOWN = "unknown"


# Categories whose reply can NEVER be system-sent (task 13.6). These carry
# legal / financial / authorization / identity consequences: salary, offer,
# visa, relocation, tax, background check, identity/bank, withdrawal, unknown,
# deadline commitments, resume/link delivery, work authorization. A draft is
# still created (so the user can review) but the send path hard-refuses.
PERMANENTLY_DENIED_REPLY_CATEGORIES: frozenset[ReplyRiskCategory] = frozenset(
    {
        ReplyRiskCategory.PERMANENTLY_DENIED,
        ReplyRiskCategory.HIGH_RISK,
    }
)

# Alias kept for readability at call sites that reason about "high risk".
HIGH_RISK_REPLY_CATEGORIES: frozenset[ReplyRiskCategory] = frozenset(
    {ReplyRiskCategory.HIGH_RISK, ReplyRiskCategory.PERMANENTLY_DENIED}
)


# Section-12 mail categories that force a permanently-denied reply risk. The
# reply-draft context derives the risk from the linked message's category when
# the user does not supply one explicitly. Kept in sync with
# ``careerops.domain.mail_intelligence.HIGH_RISK_CATEGORIES`` plus the
# additional permanent-deny categories named in the reply spec (relocation,
# tax, background check, deadline commitment, resume/link, work authorization).
_PERMANENTLY_DENIED_MAIL_CATEGORIES: frozenset[str] = frozenset(
    {
        "offer",
        "salary",
        "visa",
        "identity",
        "withdrawal",
        "relocation",
        "tax",
        "background_check",
        "deadline_commitment",
        "resume_link",
        "work_authorization",
        "unknown",
    }
)


def reply_risk_for_intent(intent: ReplyIntent) -> ReplyRiskCategory:
    """Default risk category for a user-selected intent (task 13.3)."""
    if intent is ReplyIntent.SCHEDULE:
        return ReplyRiskCategory.SCHEDULING
    if intent in (ReplyIntent.PROVIDE_INFO, ReplyIntent.REQUEST_INFO):
        return ReplyRiskCategory.INFO_REQUEST
    if intent is ReplyIntent.DECLINE:
        return ReplyRiskCategory.HIGH_RISK
    if intent is ReplyIntent.ACKNOWLEDGE:
        return ReplyRiskCategory.LOW_RISK
    return ReplyRiskCategory.UNKNOWN


def reply_risk_for_category(mail_category: str) -> ReplyRiskCategory:
    """Derive the reply risk from a linked message's mail category (13.6).

    A permanently-denied mail category forces a permanently-denied reply risk
    so the send path hard-refuses regardless of the user-selected intent.
    """
    if mail_category in _PERMANENTLY_DENIED_MAIL_CATEGORIES:
        return ReplyRiskCategory.PERMANENTLY_DENIED
    if mail_category == "interview":
        return ReplyRiskCategory.SCHEDULING
    if mail_category in ("request_more_info", "acknowledgement", "screening"):
        return ReplyRiskCategory.INFO_REQUEST
    if mail_category == "assessment":
        return ReplyRiskCategory.INFO_REQUEST
    return ReplyRiskCategory.UNKNOWN


def reply_intent_for_category(mail_category: str) -> ReplyIntent:
    """Suggested default intent for a mail category (review-only suggestion).

    The USER always selects the final intent; this is a convenience default
    surfaced by the context assembler. It never writes trusted state.
    """
    if mail_category == "interview":
        return ReplyIntent.SCHEDULE
    if mail_category == "request_more_info":
        return ReplyIntent.PROVIDE_INFO
    if mail_category == "rejection":
        return ReplyIntent.DECLINE
    return ReplyIntent.ACKNOWLEDGE


def is_high_risk_reply(category: ReplyRiskCategory) -> bool:
    """True for high-risk / permanently-denied reply categories."""
    return category in HIGH_RISK_REPLY_CATEGORIES


def is_system_send_denied(category: ReplyRiskCategory) -> bool:
    """True when a reply of this risk category may NEVER be system-sent.

    Task 13.6: high-risk + permanently-denied categories are never sent
    through the system-managed chain (draft/review only). Auto-send is denied
    for EVERY category via the AUTO_SEND capability; this check is the
    additional user-confirmed-send denial for high-risk content.
    """
    return category in PERMANENTLY_DENIED_REPLY_CATEGORIES


# ---------------------------------------------------------------------------
# 13.3 — constrained trusted context
# ---------------------------------------------------------------------------


# Maximum character length of the thread excerpt carried into the reply
# context (task 13.3 context minimization). The full body never travels into
# the draft; only a bounded, citable excerpt + the application facts do.
REPLY_EXCERPT_MAX_CHARS: int = 800


@dataclass(frozen=True, slots=True)
class ApplicationFact:
    """One confirmed application fact cited by a reply draft (task 13.3).

    ``source`` names the trusted provenance (e.g. ``application_state``,
    ``approved_package``, ``confirmed_evidence``). Facts are DATA: model output
    never produces one.
    """

    label: str
    value: str
    source: str = "application"


@dataclass(frozen=True, slots=True)
class ReplyDraftClaim:
    """A claim in a reply draft body, optionally bound to evidence (13.4).

    ``supported`` is the deterministic verdict from
    :func:`validate_reply_claims`: a claim that makes a positive assertion
    (skill, deadline, salary, authorization) without supporting evidence is
    ``supported=False`` and blocks approval until the user corrects it.
    """

    claim_text: str
    evidence_ids: tuple[UUID, ...] = ()
    supported: bool = True

    def __post_init__(self) -> None:
        if not self.claim_text.strip():
            raise ValueError("claim_text is required")


@dataclass(frozen=True, slots=True)
class ReplyDraftContext:
    """The constrained trusted context assembled for one reply (task 13.3).

    Every field is server-sourced / user-selected. ``thread_excerpt`` is the
    bounded excerpt (<= ``REPLY_EXCERPT_MAX_CHARS``) of the linked message so
    the model / user never sees the full thread body in the draft inputs.
    ``intent`` is the USER-selected intent; ``risk_category`` is derived
    deterministically from the intent + linked mail category.
    """

    message_id: UUID
    thread_id: UUID
    application_id: UUID | None
    thread_excerpt: str
    application_facts: tuple[ApplicationFact, ...] = ()
    evidence_refs: tuple[UUID, ...] = ()
    intent: ReplyIntent = ReplyIntent.ACKNOWLEDGE
    risk_category: ReplyRiskCategory = ReplyRiskCategory.LOW_RISK
    mail_category: str = ""

    def __post_init__(self) -> None:
        if len(self.thread_excerpt) > REPLY_EXCERPT_MAX_CHARS:
            # Clamp at construction so the bounded-excerpt invariant holds
            # regardless of the provider. ``object.__setattr__`` because the
            # dataclass is frozen.
            object.__setattr__(
                self, "thread_excerpt", self.thread_excerpt[:REPLY_EXCERPT_MAX_CHARS]
            )


# ---------------------------------------------------------------------------
# 13.4 / 13.5 — reply draft version (immutable, payload-hash-bound)
# ---------------------------------------------------------------------------


class ReplyDraftApprovalState(StrEnum):
    """Lifecycle of a reply draft version (tasks 13.4 / 13.5)."""

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


# Legal reply-draft lifecycle transitions (task 13.5). Terminal states are
# sink-only: a repeat decision is idempotent (returns the recorded result).
REPLY_DRAFT_TRANSITIONS: dict[ReplyDraftApprovalState, frozenset[ReplyDraftApprovalState]] = {
    ReplyDraftApprovalState.DRAFT: frozenset(
        {
            ReplyDraftApprovalState.PENDING_REVIEW,
            ReplyDraftApprovalState.APPROVED,
            ReplyDraftApprovalState.REJECTED,
            ReplyDraftApprovalState.EXPIRED,
        }
    ),
    ReplyDraftApprovalState.PENDING_REVIEW: frozenset(
        {ReplyDraftApprovalState.APPROVED, ReplyDraftApprovalState.REJECTED}
    ),
    ReplyDraftApprovalState.APPROVED: frozenset(),
    ReplyDraftApprovalState.REJECTED: frozenset(),
    ReplyDraftApprovalState.EXPIRED: frozenset(),
    ReplyDraftApprovalState.SUPERSEDED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class ReplyDraft:
    """An immutable reply-draft version (tasks 13.4 / 13.5).

    The recipient + thread headers (``in_reply_to`` / ``references_header``)
    are immutable per draft: they bind the trusted reply target. A body edit
    creates a NEW version (``version_number`` increments) sharing the same
    anchor (message/thread/recipient); a recipient or target-thread edit is
    rejected and must start a fresh draft (task 13.5 re-enter policy).

    ``payload_hash`` binds the exact bytes the user reviewed; approval freezes
    it. ``validation_issues`` carries the deterministic unsupported-claim
    findings that block approval until corrected (task 13.4).
    """

    id: UUID
    candidate_id: UUID
    message_id: UUID
    thread_id: UUID
    account_id: UUID | None
    application_id: UUID | None
    # Immutable trusted reply target (task 13.4 recipient/thread-header
    # immutability). A draft that needs a different recipient is a NEW draft.
    recipient: str
    in_reply_to: str
    references_header: str
    # Editable content (body edits -> new version, new payload hash).
    subject: str
    body: str
    intent: ReplyIntent
    risk_category: ReplyRiskCategory
    context: ReplyDraftContext
    claims: tuple[ReplyDraftClaim, ...] = ()
    validation_issues: tuple[str, ...] = ()
    payload_hash: str = ""
    version_number: int = 1
    approval_state: ReplyDraftApprovalState = ReplyDraftApprovalState.DRAFT
    decided_at: datetime | None = None
    decided_by: str = ""
    send_intent_id: UUID | None = None
    send_phase: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_approved(self) -> bool:
        return self.approval_state is ReplyDraftApprovalState.APPROVED

    @property
    def is_terminal(self) -> bool:
        return self.approval_state in (
            ReplyDraftApprovalState.APPROVED,
            ReplyDraftApprovalState.REJECTED,
            ReplyDraftApprovalState.EXPIRED,
        )


# ---------------------------------------------------------------------------
# Payload hash + claim validation (tasks 13.4 / 13.5)
# ---------------------------------------------------------------------------


def compute_reply_payload_hash(
    *,
    message_id: UUID,
    thread_id: UUID,
    recipient: str,
    in_reply_to: str,
    references_header: str,
    subject: str,
    body: str,
    intent: ReplyIntent,
    claims: tuple[ReplyDraftClaim, ...],
) -> str:
    """Return the canonical sha256 (64 hex) over the exact reply inputs.

    The hash binds the trusted reply target (message/thread/recipient/headers)
    + the editable content (subject/body/intent/claims). It is frozen at
    approval; any byte change yields a new hash, so a body edit (new version)
    is always detectable. Recipient / target edits are rejected before hashing
    (they start a new draft) — this hash is what makes body-only edits
    reviewable as a precise diff (task 13.5).
    """
    canonical = {
        "message_id": str(message_id),
        "thread_id": str(thread_id),
        "recipient": recipient.strip().lower(),
        "in_reply_to": in_reply_to,
        "references_header": references_header,
        "subject": subject,
        "body": body,
        "intent": intent.value,
        "claims": [
            {
                "claim_text": c.claim_text,
                "evidence_ids": sorted(str(e) for e in c.evidence_ids),
            }
            for c in claims
        ],
    }
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# Claim-text patterns that, when asserted without supporting evidence, mark a
# draft invalid (task 13.4 unsupported-claim validation). Conservative and
# case-insensitive; matched as substrings so phrasing variants still trip.
_UNSUPPORTED_CLAIM_PATTERNS: tuple[tuple[str, str], ...] = (
    # (pattern, label)
    ("salary", "salary_statement"),
    ("compensation", "salary_statement"),
    ("$", "salary_statement"),
    ("visa", "authorization_claim"),
    ("work authorization", "authorization_claim"),
    ("work authorisation", "authorization_claim"),
    ("sponsorship", "authorization_claim"),
    ("relocate", "authorization_claim"),
    ("background check", "authorization_claim"),
    ("deadline", "deadline_commitment"),
    ("by tomorrow", "deadline_commitment"),
    ("i will", "deadline_commitment"),
    ("i promise", "deadline_commitment"),
)


def validate_reply_claims(
    claims: tuple[ReplyDraftClaim, ...],
    *,
    confirmed_evidence_ids: frozenset[UUID] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return the unsupported-claim findings for a draft body (task 13.4).

    Each finding is ``(claim_text, reason_code)``. A claim is unsupported when:

    - it makes a positive assertion (skill / deadline / salary / authorization)
      with no evidence binding; OR
    - it references evidence that is not in the confirmed-evidence allowlist.

    The findings feed ``ReplyDraft.validation_issues`` and block approval
    until the user corrects the draft. This is deterministic DATA; model
    output never overrides it.
    """
    confirmed = confirmed_evidence_ids or frozenset()
    findings: list[tuple[str, str]] = []
    for claim in claims:
        if not claim.evidence_ids:
            findings.append((claim.claim_text, "claim_lacks_evidence"))
            continue
        # Evidence must be confirmed (trusted). An unconfirmed reference is
        # treated as unsupported (task 13.4 unverified skill / authorization).
        unconfirmed = [eid for eid in claim.evidence_ids if eid not in confirmed]
        if unconfirmed:
            findings.append((claim.claim_text, "claim_references_unconfirmed_evidence"))
    return tuple(findings)


def scan_body_for_unsupported_claims(body: str) -> tuple[str, ...]:
    """Scan free-form body text for unsupported-commitment patterns (13.4).

    Returns the matched reason codes. Used when the draft body carries
    assertions the user typed directly (not via structured claims) — a salary
    figure, a deadline commitment, an authorization claim — so the draft is
    flagged invalid even without a structured claim object.
    """
    lowered = body.lower()
    matched: list[str] = []
    seen: set[str] = set()
    for pattern, label in _UNSUPPORTED_CLAIM_PATTERNS:
        if pattern in lowered and label not in seen:
            seen.add(label)
            matched.append(label)
    return tuple(matched)


# ---------------------------------------------------------------------------
# Errors (translated at the API layer)
# ---------------------------------------------------------------------------


class ReplyDraftError(Exception):
    """Base for reply-draft service errors."""


class ReplyDraftNotOwnedError(ReplyDraftError):
    """The draft does not exist or is not owned by the candidate.

    Raised for BOTH missing and not-owned so the response never leaks that a
    draft exists for another candidate (Iron Rule 2 / 6).
    """

    def __init__(self, draft_id: UUID) -> None:
        super().__init__(f"reply draft not found for candidate: {draft_id}")


class IllegalReplyTransitionError(ReplyDraftError):
    """The reply-draft lifecycle transition itself is illegal."""

    def __init__(
        self, from_state: ReplyDraftApprovalState, to_state: ReplyDraftApprovalState
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"illegal reply-draft transition: {from_state.value} -> {to_state.value}")


class ReplyDraftAlreadyDecidedError(ReplyDraftError):
    """A terminal draft received a new decision request (task 13.5)."""

    def __init__(self, draft_id: UUID, state: ReplyDraftApprovalState) -> None:
        self.state = state
        super().__init__(f"reply draft {draft_id} already decided: {state.value}")


class RecipientMutationError(ReplyDraftError):
    """An in-place recipient / target-thread edit was attempted (task 13.5).

    The trusted recipient and thread headers are immutable per draft. A
    recipient or target change MUST start a new draft that re-enters recipient
    trust, policy and approval checks.
    """

    _DEFAULT_REASON = "recipient or target-thread edits require a new draft"

    def __init__(self, reason: str = _DEFAULT_REASON) -> None:
        super().__init__(reason)


class UnsupportedClaimError(ReplyDraftError):
    """The draft contains an unsupported claim and cannot be approved (13.4)."""

    def __init__(self, issues: tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("reply draft has unsupported claims: " + "; ".join(issues))


class MessageNotLinkedError(ReplyDraftError):
    """The draft has no resolved application/thread link (task 13.3)."""

    def __init__(self, draft_id: UUID) -> None:
        super().__init__(f"reply draft {draft_id} has no resolved thread/application link")


class ReplySendDeniedError(ReplyDraftError):
    """The reply send was denied BEFORE any provider call (task 13.6).

    Carries the fail-closed reason codes safe to surface in UI messaging.
    High-risk categories + auto-send are permanently denied; a denied
    SYSTEM_MANAGED_SEND capability or inactive account also refuses here.
    """

    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        self.reason_codes = reason_codes
        super().__init__("reply send denied: " + ",".join(reason_codes))
