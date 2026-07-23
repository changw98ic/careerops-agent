"""M6 Gmail Send service: four-layer gate, send execution, and reconciliation.

Integrates with the M5A side-effect kernel authorization chain:
``ActionIntent -> Immutable Payload -> Policy -> Approval -> Outbox -> Worker ->
Provider Reconciliation -> Audit``.

The four-layer gate for auto-send:
1. Global kill switch (system-level, production default OFF).
2. Valid Release Qualification.
3. Account-level explicit opt-in.
4. Policy allowlist (category-specific, default all OFF).

Safety invariants:
- High-risk categories NEVER auto-send; permanent rules cannot override.
- No-source contact / low-trust / no-Application = no auto-send.
- Prompt Injection cannot change recipient, payload, Policy, or tools.
- Ambiguous results -> reconciliation_required + no auto-retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from careerops.application.audit import AuditActorType, AuditEventDraft
from careerops.application.side_effect_kernel import (
    InMemoryAuditWriter,
    ProposalInput,
    ProposalResult,
    SideEffectKernel,
)
from careerops.domain.send import (
    AUTO_SEND_CANDIDATES,
    PERMANENT_DENY_CATEGORIES,
    FourLayerGateState,
    ReconciliationRecord,
    ReconciliationStatus,
    SendAttemptRecord,
    SendAttemptStatus,
    SendCategory,
    SendEligibilityContext,
    SendReceiptRecord,
    compute_body_hash,
    compute_send_idempotency_key,
)
from careerops.domain.side_effects import IntentStatus


class SendPolicyError(RuntimeError):
    """Raised when a send violates M6 policy invariants."""


class AutoSendDeniedError(SendPolicyError):
    """Raised when auto-send is denied by the four-layer gate or category rules."""

    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        self.reason_codes = reason_codes
        super().__init__(f"auto-send denied: {','.join(reason_codes)}")


class PermanentDenyError(SendPolicyError):
    """Raised when a category is permanently forbidden from auto-send."""

    def __init__(self, category: SendCategory) -> None:
        self.category = category
        super().__init__(f"category {category.value} is permanently forbidden from auto-send")


@dataclass(frozen=True, slots=True)
class SendRequest:
    """A request to send an email through the M5A authorization chain.

    All fields are from trusted business state. The recipient is derived from
    thread metadata (not from model output or untrusted content).
    """

    account_email: str
    recipient: str
    subject: str
    body: str
    thread_id: str
    in_reply_to: str
    references: tuple[str, ...]
    category: SendCategory
    inbound_message_id: str
    application_id: UUID | None = None
    evidence_refs: tuple[str, ...] = ()
    trusted_facts: dict[str, object] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class SendOutcome:
    """Result of a send operation through the M5A chain."""

    intent_id: UUID
    status: IntentStatus
    send_attempt: SendAttemptRecord | None = None
    receipt: SendReceiptRecord | None = None
    reconciliation: ReconciliationRecord | None = None
    auto_send: bool = False
    denial_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AutoSendConfig:
    """Configuration for auto-send categories. Default: all OFF."""

    enabled_categories: frozenset[SendCategory] = frozenset()


class GmailSendService:
    """M6 send service integrating with the M5A side-effect kernel.

    The service enforces the four-layer gate and category-based rules before
    delegating to the kernel for the full authorization chain.
    """

    def __init__(
        self,
        kernel: SideEffectKernel,
        *,
        audit_writer: InMemoryAuditWriter | None = None,
        global_kill_switch_off: bool = False,
        release_qualification_valid: bool = False,
        auto_send_config: AutoSendConfig | None = None,
        reconciliation_window_minutes: int = 15,
    ) -> None:
        self._kernel = kernel
        self._audit = audit_writer or InMemoryAuditWriter()
        self._global_kill_switch_off = global_kill_switch_off
        self._release_qualification_valid = release_qualification_valid
        self._auto_send_config = auto_send_config or AutoSendConfig()
        self._reconciliation_window_minutes = reconciliation_window_minutes
        self._send_attempts: dict[UUID, SendAttemptRecord] = {}
        self._reconciliation_records: dict[UUID, ReconciliationRecord] = {}

    @property
    def global_kill_switch_off(self) -> bool:
        return self._global_kill_switch_off

    def evaluate_four_layer_gate(
        self,
        *,
        account_opt_in: bool,
        category: SendCategory,
    ) -> FourLayerGateState:
        """Evaluate the four-layer gate for a specific category and account."""
        policy_allow = category in self._auto_send_config.enabled_categories
        return FourLayerGateState(
            global_kill_switch_off=self._global_kill_switch_off,
            release_qualification_valid=self._release_qualification_valid,
            account_opt_in=account_opt_in,
            policy_allow=policy_allow,
        )

    def evaluate_send_eligibility(
        self, context: SendEligibilityContext
    ) -> tuple[bool, tuple[str, ...]]:
        """Evaluate whether a send is eligible based on trusted business context.

        Returns (eligible, denial_reasons).
        """
        reasons: list[str] = []

        if context.category in PERMANENT_DENY_CATEGORIES:
            reasons.append(f"PERMANENT_DENY_{context.category.value.upper()}")

        if not context.has_source_contact:
            reasons.append("NO_SOURCE_CONTACT")

        if context.sender_trust_level in ("low", "unknown"):
            reasons.append("LOW_TRUST_SENDER")

        if not context.has_application_linkage:
            reasons.append("NO_APPLICATION_LINKAGE")

        if not context.contact_domain_matches:
            reasons.append("CONTACT_DOMAIN_MISMATCH")

        return (len(reasons) == 0, tuple(reasons))

    def can_auto_send(
        self,
        *,
        context: SendEligibilityContext,
        account_opt_in: bool,
    ) -> tuple[bool, tuple[str, ...]]:
        """Determine if a message can be auto-sent.

        Auto-send requires:
        1. Category is an auto-send candidate (not approval-required or permanent-deny).
        2. Four-layer gate passes.
        3. Send eligibility context passes (source contact, trust, application).
        """
        reasons: list[str] = []

        # Category must be an auto-send candidate
        if context.category in PERMANENT_DENY_CATEGORIES:
            reasons.append(f"PERMANENT_DENY_{context.category.value.upper()}")
            return (False, tuple(reasons))

        if context.category not in AUTO_SEND_CANDIDATES:
            reasons.append("CATEGORY_REQUIRES_APPROVAL")
            return (False, tuple(reasons))

        # Four-layer gate
        gate = self.evaluate_four_layer_gate(
            account_opt_in=account_opt_in,
            category=context.category,
        )
        if not gate.all_satisfied:
            reasons.extend(gate.denial_reasons)

        # Send eligibility context
        eligible, eligibility_reasons = self.evaluate_send_eligibility(context)
        if not eligible:
            reasons.extend(eligibility_reasons)

        return (len(reasons) == 0, tuple(reasons))

    def submit_send(
        self,
        request: SendRequest,
        *,
        account_opt_in: bool = False,
        now: datetime,
    ) -> SendOutcome:
        """Submit a send request through the M5A authorization chain.

        If auto-send is allowed (four-layer gate + eligibility), the intent is
        proposed with policy ALLOW. Otherwise, it requires human approval.
        """
        body_hash = compute_body_hash(request.body)
        idempotency_key = compute_send_idempotency_key(
            account_email=request.account_email,
            inbound_message_id=request.inbound_message_id,
            intent_kind="send_email",
            normalized_body_hash=body_hash,
        )

        context = SendEligibilityContext(
            category=request.category,
            has_source_contact=bool(request.trusted_facts.get("has_source_contact", False)),
            sender_trust_level=str(request.trusted_facts.get("sender_trust_level", "unknown")),
            has_application_linkage=request.application_id is not None,
            contact_domain_matches=bool(request.trusted_facts.get("contact_domain_matches", False)),
            evidence_refs=request.evidence_refs,
        )

        auto_send_allowed, denial_reasons = self.can_auto_send(
            context=context,
            account_opt_in=account_opt_in,
        )

        # Build trusted facts for the M5A policy engine
        trusted_facts = dict(request.trusted_facts)
        trusted_facts["capability_released"] = True
        trusted_facts["target_allowlisted"] = True
        if auto_send_allowed:
            trusted_facts["auto_send_approved"] = True

        proposal = ProposalInput(
            action_kind="send_email",
            resource_type="application" if request.application_id else "email_thread",
            resource_id=request.application_id or uuid4(),
            idempotency_key=idempotency_key,
            created_by="m6-send-service",
            target={
                "to": request.recipient,
                "account": request.account_email,
                "thread_id": request.thread_id,
            },
            payload={
                "subject": request.subject,
                "body": request.body,
                "body_hash": body_hash,
                "in_reply_to": request.in_reply_to,
                "references": list(request.references),
                "category": request.category.value,
            },
            evidence_refs=request.evidence_refs,
            trusted_facts=trusted_facts,
            untrusted_claims={},  # M6 never passes untrusted claims to policy
        )

        result: ProposalResult = self._kernel.propose(proposal, now=now)

        # Record the send attempt
        send_attempt = SendAttemptRecord(
            id=uuid4(),
            action_intent_id=result.intent.id,
            account_email=request.account_email,
            recipient=request.recipient,
            subject=request.subject,
            body_hash=body_hash,
            thread_id=request.thread_id,
            in_reply_to=request.in_reply_to,
            references=request.references,
            category=request.category,
            status=SendAttemptStatus.PENDING,
            idempotency_key=idempotency_key,
            reconciliation_key=f"{idempotency_key}:{result.payload_version.payload_hash}",
            created_at=now,
        )
        self._send_attempts[send_attempt.id] = send_attempt

        self._audit.append(
            AuditEventDraft(
                event_type="m6_send_submitted",
                actor_type=AuditActorType.AGENT,
                actor_id="m6-send-service",
                resource_type="action_intent",
                resource_id=result.intent.id,
                trace_id=idempotency_key,
                event_data={
                    "category": request.category.value,
                    "auto_send_allowed": auto_send_allowed,
                    "denial_reasons": list(denial_reasons),
                    "recipient": request.recipient,
                    "body_hash": body_hash,
                },
            )
        )

        return SendOutcome(
            intent_id=result.intent.id,
            status=result.intent.status,
            send_attempt=send_attempt,
            auto_send=auto_send_allowed,
            denial_reasons=denial_reasons,
        )

    def execute_send(self, intent_id: UUID, *, now: datetime) -> SendOutcome:
        """Execute a send through the M5A kernel (requires approval or auto-send)."""
        outcome = self._kernel.execute(intent_id, now=now)

        # Update send attempt status
        for attempt in self._send_attempts.values():
            if attempt.action_intent_id == intent_id:
                if outcome.status is IntentStatus.CONFIRMED:
                    updated = SendAttemptRecord(
                        id=attempt.id,
                        action_intent_id=attempt.action_intent_id,
                        account_email=attempt.account_email,
                        recipient=attempt.recipient,
                        subject=attempt.subject,
                        body_hash=attempt.body_hash,
                        thread_id=attempt.thread_id,
                        in_reply_to=attempt.in_reply_to,
                        references=attempt.references,
                        category=attempt.category,
                        status=SendAttemptStatus.SUCCEEDED,
                        idempotency_key=attempt.idempotency_key,
                        reconciliation_key=attempt.reconciliation_key,
                        provider_message_id=(
                            outcome.receipt.provider_resource_id if outcome.receipt else None
                        ),
                        finished_at=now,
                        created_at=attempt.created_at,
                    )
                    self._send_attempts[attempt.id] = updated
                elif outcome.status is IntentStatus.RECONCILIATION_REQUIRED:
                    updated = SendAttemptRecord(
                        id=attempt.id,
                        action_intent_id=attempt.action_intent_id,
                        account_email=attempt.account_email,
                        recipient=attempt.recipient,
                        subject=attempt.subject,
                        body_hash=attempt.body_hash,
                        thread_id=attempt.thread_id,
                        in_reply_to=attempt.in_reply_to,
                        references=attempt.references,
                        category=attempt.category,
                        status=SendAttemptStatus.RECONCILIATION_REQUIRED,
                        idempotency_key=attempt.idempotency_key,
                        reconciliation_key=attempt.reconciliation_key,
                        error_code=(outcome.reason_codes[0] if outcome.reason_codes else None),
                        finished_at=now,
                        created_at=attempt.created_at,
                    )
                    self._send_attempts[attempt.id] = updated
                    # Create reconciliation record
                    recon = ReconciliationRecord(
                        id=uuid4(),
                        send_attempt_id=attempt.id,
                        action_intent_id=intent_id,
                        reconciliation_key=attempt.reconciliation_key,
                        status=ReconciliationStatus.PENDING,
                        window_minutes=self._reconciliation_window_minutes,
                        auto_retry_disabled=True,
                        created_at=now,
                    )
                    self._reconciliation_records[recon.id] = recon
                break

        return SendOutcome(
            intent_id=intent_id,
            status=outcome.status,
            send_attempt=self._find_attempt_for_intent(intent_id),
            reconciliation=self._find_reconciliation_for_intent(intent_id),
            denial_reasons=outcome.reason_codes,
        )

    def get_reconciliation_records(self) -> tuple[ReconciliationRecord, ...]:
        return tuple(self._reconciliation_records.values())

    def get_send_attempts(self) -> tuple[SendAttemptRecord, ...]:
        return tuple(self._send_attempts.values())

    def _find_attempt_for_intent(self, intent_id: UUID) -> SendAttemptRecord | None:
        for attempt in self._send_attempts.values():
            if attempt.action_intent_id == intent_id:
                return attempt
        return None

    def _find_reconciliation_for_intent(self, intent_id: UUID) -> ReconciliationRecord | None:
        for recon in self._reconciliation_records.values():
            if recon.action_intent_id == intent_id:
                return recon
        return None
