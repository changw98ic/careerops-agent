"""Domain models for M6: Gmail Send with controlled auto-send, default OFF.

The four-layer gate for auto-send:
1. Global kill switch (system-level, cannot be opened by config/UI/model).
2. Valid Release Qualification (commit SHA, migration head, dataset versions).
3. Account-level explicit opt-in (user must enable per account).
4. Policy allowlist (only specific low-risk categories, default all OFF).

Safety invariants:
- High-risk categories (salary, offer, visa, relocation, tax, background check,
  identity/bank, withdrawal, unknown) can NEVER auto-send; permanent rules cannot
  override this.
- No-source contact, low-trust sender, no Application linkage = no auto-send.
- Prompt Injection cannot change recipient, payload, Policy, or invoke tools.
- Ambiguous provider results enter reconciliation_required with no auto-retry.
- Production global kill switch remains OFF.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class SendCategory(StrEnum):
    """Classification of outbound email for auto-send eligibility."""

    # Auto-send candidates (default ALL OFF; require four-layer gate to enable)
    DELIVERY_CONFIRMATION = "delivery_confirmation"
    RECRUITER_CONTACT_ACK = "recruiter_contact_ack"
    ASSESSMENT_RECEIPT_ACK = "assessment_receipt_ack"
    CONFIRMED_TIME_ACK = "confirmed_time_ack"
    THANKS_NO_QUESTIONS = "thanks_no_questions"

    # Requires human approval (never auto-send)
    SCHEDULING_OPTIONS = "scheduling_options"
    FOLLOW_UP = "follow_up"
    RESUME_OR_LINK = "resume_or_link"
    WORK_AUTHORIZATION = "work_authorization"
    DEADLINE_COMMITMENT = "deadline_commitment"

    # Permanently forbidden from auto-send (no rule can override)
    SALARY = "salary"
    OFFER = "offer"
    VISA = "visa"
    RELOCATION = "relocation"
    TAX = "tax"
    BACKGROUND_CHECK = "background_check"
    IDENTITY_OR_BANK = "identity_or_bank"
    WITHDRAWAL = "withdrawal"
    UNKNOWN = "unknown"


# Categories eligible for auto-send IF the four-layer gate passes.
AUTO_SEND_CANDIDATES: frozenset[SendCategory] = frozenset(
    {
        SendCategory.DELIVERY_CONFIRMATION,
        SendCategory.RECRUITER_CONTACT_ACK,
        SendCategory.ASSESSMENT_RECEIPT_ACK,
        SendCategory.CONFIRMED_TIME_ACK,
        SendCategory.THANKS_NO_QUESTIONS,
    }
)

# Categories that require human approval (never auto-send).
APPROVAL_REQUIRED_CATEGORIES: frozenset[SendCategory] = frozenset(
    {
        SendCategory.SCHEDULING_OPTIONS,
        SendCategory.FOLLOW_UP,
        SendCategory.RESUME_OR_LINK,
        SendCategory.WORK_AUTHORIZATION,
        SendCategory.DEADLINE_COMMITMENT,
    }
)

# Categories permanently forbidden from auto-send; no rule can override.
PERMANENT_DENY_CATEGORIES: frozenset[SendCategory] = frozenset(
    {
        SendCategory.SALARY,
        SendCategory.OFFER,
        SendCategory.VISA,
        SendCategory.RELOCATION,
        SendCategory.TAX,
        SendCategory.BACKGROUND_CHECK,
        SendCategory.IDENTITY_OR_BANK,
        SendCategory.WITHDRAWAL,
        SendCategory.UNKNOWN,
    }
)


class SendAttemptStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ReconciliationStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED_SENT = "confirmed_sent"
    CONFIRMED_NOT_SENT = "confirmed_not_sent"
    AMBIGUOUS = "ambiguous"
    ESCALATED_MANUAL = "escalated_manual"


class FourLayerGateResult(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class FourLayerGateState:
    """State of the four-layer auto-send gate.

    All four layers must be satisfied for auto-send to proceed:
    1. global_kill_switch_off: system-level, cannot be opened by config/UI/model.
    2. release_qualification_valid: commit SHA + migration head + datasets.
    3. account_opt_in: user explicitly enabled auto-send for this account.
    4. policy_allow: category is in the active allowlist.
    """

    global_kill_switch_off: bool
    release_qualification_valid: bool
    account_opt_in: bool
    policy_allow: bool

    @property
    def all_satisfied(self) -> bool:
        return (
            self.global_kill_switch_off
            and self.release_qualification_valid
            and self.account_opt_in
            and self.policy_allow
        )

    @property
    def denial_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.global_kill_switch_off:
            reasons.append("GLOBAL_KILL_SWITCH_ACTIVE")
        if not self.release_qualification_valid:
            reasons.append("RELEASE_QUALIFICATION_INVALID")
        if not self.account_opt_in:
            reasons.append("ACCOUNT_OPT_IN_MISSING")
        if not self.policy_allow:
            reasons.append("POLICY_NOT_ALLOWED")
        return tuple(reasons)


@dataclass(frozen=True, slots=True)
class SendEligibilityContext:
    """Trusted business context required to evaluate send eligibility.

    All fields come from trusted business state, never from model output
    or untrusted email content.
    """

    category: SendCategory
    has_source_contact: bool
    sender_trust_level: str  # "high", "medium", "low", "unknown"
    has_application_linkage: bool
    contact_domain_matches: bool
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SendAttemptRecord:
    """M6 send attempt record bound to the M5A authorization chain."""

    id: UUID
    action_intent_id: UUID
    account_email: str
    recipient: str
    subject: str
    body_hash: str
    thread_id: str
    in_reply_to: str
    references: tuple[str, ...]
    category: SendCategory
    status: SendAttemptStatus
    idempotency_key: str
    reconciliation_key: str
    provider_message_id: str | None = None
    error_code: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SendReceiptRecord:
    """Provider receipt for a sent email."""

    id: UUID
    send_attempt_id: UUID
    provider_message_id: str
    thread_id: str
    reconciliation_key: str
    final_state: str
    provider_timestamp: datetime | None = None
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    """Reconciliation record for ambiguous send outcomes."""

    id: UUID
    send_attempt_id: UUID
    action_intent_id: UUID
    reconciliation_key: str
    status: ReconciliationStatus
    window_minutes: int = 15
    attempts_within_window: int = 0
    auto_retry_disabled: bool = True
    escalated_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None
    created_at: datetime | None = None


def compute_send_idempotency_key(
    *,
    account_email: str,
    inbound_message_id: str,
    intent_kind: str,
    normalized_body_hash: str,
) -> str:
    """Stable idempotency key binding account + inbound message + intent + body.

    This ensures the same reply to the same message with the same content
    is never sent twice, even across crashes and retries.
    """
    canonical = f"{account_email}:{inbound_message_id}:{intent_kind}:{normalized_body_hash}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_body_hash(body: str) -> str:
    """SHA-256 hash of the normalized email body."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
