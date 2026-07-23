"""M5A side-effect kernel: the full external-write authorization chain.

Chain: ``ActionIntent -> Immutable Payload Version -> Policy Decision ->
Approval -> Outbox -> Side-effect Worker -> Provider Reconciliation -> Audit``.

Hard invariants enforced here:
- Policy input is trusted business state + evidence refs only; the model's
  self-assertion of safety is recorded as ignored and never consulted.
- Approval binds target + payload hash + policy decision + expiration. Any
  payload edit creates a new version and invalidates the prior approval.
- Unknown provider results reconcile FIRST; never blind retry. API timeout
  does not mean the provider did not execute.
- No Policy Decision + Audit => no provider effect.
- Provider calls are idempotent on a stable reconciliation key, so 100
  concurrent submissions with one idempotency key yield exactly one effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import RLock
from typing import Protocol
from uuid import UUID, uuid4

from careerops.application.audit import AuditActorType, AuditEventDraft
from careerops.domain.side_effects import (
    ActionIntent,
    ActionPayloadVersion,
    ApprovalDecision,
    ApprovalRequest,
    AttemptState,
    IntentStatus,
    PolicyDecisionRecord,
    PolicyDecisionValue,
    ProviderCallResult,
    ProviderFailureClass,
    ProviderReceipt,
    ProviderResultKind,
    ReceiptFinalState,
    ReconciliationResult,
    SideEffectAttempt,
    canonical_payload_hash,
    request_fingerprint,
)
from careerops.integrations.fake_side_effect_provider import (
    FakeProviderCrash,
    SideEffectProvider,
)
from careerops.policy.side_effect_policy import (
    SideEffectPolicyDecider,
    SideEffectPolicyInput,
    SideEffectPolicyOutcome,
)


class SideEffectError(RuntimeError):
    """Base error for side-effect kernel operations."""


class IntentNotFoundError(SideEffectError):
    pass


class ApprovalInvalidError(SideEffectError):
    pass


class PolicyDeniedError(SideEffectError):
    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        self.reason_codes = reason_codes
        super().__init__(f"policy denied: {','.join(reason_codes)}")


class AuditWriter(Protocol):
    def append(self, draft: AuditEventDraft) -> int: ...

    def list_for_resource(
        self, resource_type: str, resource_id: UUID
    ) -> tuple[AuditEventDraft, ...]: ...


class InMemoryAuditWriter:
    """Append-only in-memory audit log used for tests and replay."""

    def __init__(self) -> None:
        self._events: list[AuditEventDraft] = []
        self._sequence = 0

    def append(self, draft: AuditEventDraft) -> int:
        self._sequence += 1
        self._events.append(draft)
        return self._sequence

    def list_for_resource(
        self, resource_type: str, resource_id: UUID
    ) -> tuple[AuditEventDraft, ...]:
        return tuple(
            event
            for event in self._events
            if event.resource_type == resource_type and event.resource_id == resource_id
        )

    def all_events(self) -> tuple[AuditEventDraft, ...]:
        return tuple(self._events)


@dataclass(frozen=True, slots=True)
class ProposalInput:
    """Trusted business input for proposing a side effect.

    ``untrusted_claims`` is captured only to record that the policy ignored it.
    """

    action_kind: str
    resource_type: str
    resource_id: UUID
    idempotency_key: str
    created_by: str
    target: dict[str, object]
    payload: dict[str, object]
    attachment_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    trusted_facts: dict[str, object] = field(default_factory=lambda: {})
    untrusted_claims: dict[str, object] = field(default_factory=lambda: {})
    authenticated: bool = True


@dataclass(frozen=True, slots=True)
class ProposalResult:
    intent: ActionIntent
    payload_version: ActionPayloadVersion
    policy_decision: PolicyDecisionRecord


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """Result of one worker execution pass over an intent."""

    intent_id: UUID
    status: IntentStatus
    attempt: SideEffectAttempt | None
    receipt: ProviderReceipt | None
    reconciled: bool = False
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalPageReplay:
    """Everything required to replay an executed action from the Approval page."""

    intent: ActionIntent
    payload_versions: tuple[ActionPayloadVersion, ...]
    policy_decisions: tuple[PolicyDecisionRecord, ...]
    approvals: tuple[ApprovalRequest, ...]
    attempts: tuple[SideEffectAttempt, ...]
    receipts: tuple[ProviderReceipt, ...]
    audit_events: tuple[AuditEventDraft, ...]


class SideEffectStore(Protocol):
    """Repository contract for the M5A authorization chain."""

    def insert_intent(self, intent: ActionIntent) -> ActionIntent: ...

    def find_intent_by_idempotency_key(self, idempotency_key: str) -> ActionIntent | None: ...

    def get_intent(self, intent_id: UUID) -> ActionIntent: ...

    def update_intent_status(
        self,
        intent_id: UUID,
        status: IntentStatus,
        *,
        now: datetime,
        current_payload_version_id: UUID | None = None,
    ) -> ActionIntent: ...

    def insert_payload_version(self, version: ActionPayloadVersion) -> ActionPayloadVersion: ...

    def get_payload_version(self, version_id: UUID) -> ActionPayloadVersion: ...

    def list_payload_versions(self, intent_id: UUID) -> tuple[ActionPayloadVersion, ...]: ...

    def insert_policy_decision(self, decision: PolicyDecisionRecord) -> PolicyDecisionRecord: ...

    def get_policy_decision(self, decision_id: UUID) -> PolicyDecisionRecord: ...

    def list_policy_decisions(self, intent_id: UUID) -> tuple[PolicyDecisionRecord, ...]: ...

    def insert_approval(self, approval: ApprovalRequest) -> ApprovalRequest: ...

    def get_approval(self, approval_id: UUID) -> ApprovalRequest: ...

    def update_approval_decision(
        self,
        approval_id: UUID,
        decision: ApprovalDecision,
        *,
        decided_at: datetime,
        decision_rule_reference: str | None = None,
    ) -> ApprovalRequest: ...

    def list_approvals(self, intent_id: UUID) -> tuple[ApprovalRequest, ...]: ...

    def insert_attempt(self, attempt: SideEffectAttempt) -> SideEffectAttempt: ...

    def get_attempt(self, attempt_id: UUID) -> SideEffectAttempt: ...

    def update_attempt(
        self,
        attempt_id: UUID,
        *,
        state: AttemptState,
        finished_at: datetime | None = None,
        error_code: str | None = None,
        response_metadata: dict[str, object] | None = None,
    ) -> SideEffectAttempt: ...

    def list_attempts(self, intent_id: UUID) -> tuple[SideEffectAttempt, ...]: ...

    def next_attempt_ordinal(self, intent_id: UUID) -> int: ...

    def insert_receipt(self, receipt: ProviderReceipt) -> ProviderReceipt: ...

    def find_receipt_by_reconciliation_key(
        self, provider: str, reconciliation_key: str
    ) -> ProviderReceipt | None: ...

    def list_receipts(self, intent_id: UUID) -> tuple[ProviderReceipt, ...]: ...


class SideEffectKernel:
    """Coordinates proposal, policy, approval, execution, and reconciliation."""

    def __init__(
        self,
        store: SideEffectStore,
        provider: SideEffectProvider,
        *,
        policy_decider: SideEffectPolicyDecider | None = None,
        audit_writer: InMemoryAuditWriter | None = None,
        max_attempts: int = 5,
        approval_ttl_seconds: int = 600,
    ) -> None:
        self._store = store
        self._provider = provider
        self._policy = policy_decider or SideEffectPolicyDecider()
        self._audit = audit_writer or InMemoryAuditWriter()
        self._max_attempts = max_attempts
        self._approval_ttl_seconds = approval_ttl_seconds
        self._lock = RLock()

    # -- proposal --------------------------------------------------------

    def propose(self, proposal: ProposalInput, *, now: datetime) -> ProposalResult:
        """Create (or reuse) an intent, freeze an immutable payload, run policy."""
        with self._lock:
            return self._propose_locked(proposal, now=now)

    def _propose_locked(self, proposal: ProposalInput, *, now: datetime) -> ProposalResult:
        store = self._store
        existing = store.find_intent_by_idempotency_key(proposal.idempotency_key)
        if existing is not None:
            intent = existing
        else:
            intent = ActionIntent(
                id=uuid4(),
                action_kind=proposal.action_kind,
                resource_type=proposal.resource_type,
                resource_id=proposal.resource_id,
                idempotency_key=proposal.idempotency_key,
                status=IntentStatus.PROPOSED,
                current_payload_version_id=None,
                created_by=proposal.created_by,
                created_at=now,
                updated_at=now,
            )
            store.insert_intent(intent)

        payload_hash = canonical_payload_hash(
            target=proposal.target,
            payload=proposal.payload,
            attachment_refs=proposal.attachment_refs,
        )
        existing_versions = store.list_payload_versions(intent.id)
        payload_version: ActionPayloadVersion | None = None
        for version in existing_versions:
            if version.payload_hash == payload_hash:
                payload_version = version
                break
        if payload_version is None:
            next_version_number = len(existing_versions) + 1
            payload_version = ActionPayloadVersion(
                id=uuid4(),
                action_intent_id=intent.id,
                version=next_version_number,
                target=dict(proposal.target),
                payload=dict(proposal.payload),
                attachment_refs=tuple(proposal.attachment_refs),
                payload_hash=payload_hash,
                created_at=now,
            )
            store.insert_payload_version(payload_version)

        existing_decision = self._policy_decision_for_payload(intent.id, payload_version.id)
        if existing_decision is not None:
            return ProposalResult(
                intent=intent,
                payload_version=payload_version,
                policy_decision=existing_decision,
            )

        policy_input = SideEffectPolicyInput(
            action_kind=proposal.action_kind,
            authenticated=proposal.authenticated,
            trusted_facts=dict(proposal.trusted_facts),
            evidence_refs=tuple(proposal.evidence_refs),
            untrusted_claims=dict(proposal.untrusted_claims),
        )
        decision = self._policy.decide(policy_input)
        policy_record = PolicyDecisionRecord(
            id=uuid4(),
            action_intent_id=intent.id,
            payload_version_id=payload_version.id,
            ruleset_version=decision.ruleset_version,
            decision=PolicyDecisionValue(decision.outcome.value),
            reason_codes=tuple(decision.reason_codes),
            payload_hash=payload_hash,
            trusted_facts=dict(proposal.trusted_facts),
            evidence_refs=tuple(proposal.evidence_refs),
            untrusted_claims=dict(proposal.untrusted_claims),
            created_at=now,
        )
        store.insert_policy_decision(policy_record)

        if decision.outcome is SideEffectPolicyOutcome.DENY:
            new_status = IntentStatus.DENIED
        elif decision.outcome is SideEffectPolicyOutcome.ALLOW:
            new_status = IntentStatus.ELIGIBLE
        else:
            new_status = IntentStatus.AWAITING_APPROVAL
        intent = store.update_intent_status(
            intent.id,
            new_status,
            now=now,
            current_payload_version_id=payload_version.id,
        )

        self._audit.append(
            AuditEventDraft(
                event_type="side_effect_proposed",
                actor_type=AuditActorType.AGENT,
                actor_id=proposal.created_by,
                resource_type="action_intent",
                resource_id=intent.id,
                trace_id=proposal.idempotency_key,
                event_data={
                    "action_kind": proposal.action_kind,
                    "payload_hash": payload_hash,
                    "policy_decision": decision.outcome.value,
                    "policy_reason_codes": list(decision.reason_codes),
                    "untrusted_claims_ignored": True,
                },
            )
        )

        return ProposalResult(
            intent=intent,
            payload_version=payload_version,
            policy_decision=policy_record,
        )

    # -- approval --------------------------------------------------------

    def request_approval(
        self,
        intent_id: UUID,
        *,
        requested_for: str,
        now: datetime,
        decision_rule_reference: str | None = None,
    ) -> ApprovalRequest:
        store = self._store
        intent = self._require_intent(intent_id)
        if intent.current_payload_version_id is None:
            raise ApprovalInvalidError("intent has no payload version to approve")
        policy_decision = self._latest_policy_decision(intent_id)
        if policy_decision is None:
            raise ApprovalInvalidError("intent has no policy decision to approve")
        if policy_decision.decision is PolicyDecisionValue.DENY:
            raise PolicyDeniedError(policy_decision.reason_codes)

        approval = ApprovalRequest(
            id=uuid4(),
            action_intent_id=intent_id,
            payload_version_id=intent.current_payload_version_id,
            policy_decision_id=policy_decision.id,
            requested_for=requested_for,
            decision=ApprovalDecision.PENDING,
            decision_rule_reference=decision_rule_reference,
            expires_at=now + timedelta(seconds=self._approval_ttl_seconds),
            decided_at=None,
            created_at=now,
        )
        store.insert_approval(approval)
        return approval

    def approve(
        self,
        approval_id: UUID,
        *,
        now: datetime,
        decision_rule_reference: str | None = None,
    ) -> ApprovalRequest:
        store = self._store
        approval = store.get_approval(approval_id)
        if approval.decision is not ApprovalDecision.PENDING:
            raise ApprovalInvalidError(f"approval already decided: {approval.decision.value}")
        if now >= approval.expires_at:
            approval = store.update_approval_decision(
                approval_id,
                ApprovalDecision.EXPIRED,
                decided_at=now,
            )
            raise ApprovalInvalidError("approval expired before decision")

        # Re-validate the binding: payload hash + policy decision must still match
        # the intent's current payload version.
        intent = self._require_intent(approval.action_intent_id)
        if intent.current_payload_version_id != approval.payload_version_id:
            raise ApprovalInvalidError(
                "payload changed since approval was requested; approval invalidated"
            )
        policy_decision = store.get_policy_decision(approval.policy_decision_id)
        payload_version = store.get_payload_version(approval.payload_version_id)
        if policy_decision.payload_hash != payload_version.payload_hash:
            raise ApprovalInvalidError("policy decision payload hash mismatch")

        approval = store.update_approval_decision(
            approval_id,
            ApprovalDecision.APPROVED,
            decided_at=now,
            decision_rule_reference=decision_rule_reference,
        )
        store.update_intent_status(
            intent.id,
            IntentStatus.ELIGIBLE,
            now=now,
            current_payload_version_id=payload_version.id,
        )
        self._audit.append(
            AuditEventDraft(
                event_type="side_effect_approved",
                actor_type=AuditActorType.USER,
                actor_id=approval.requested_for,
                resource_type="action_intent",
                resource_id=intent.id,
                trace_id=intent.idempotency_key,
                event_data={
                    "approval_id": str(approval.id),
                    "payload_hash": payload_version.payload_hash,
                    "policy_decision_id": str(policy_decision.id),
                    "ruleset_version": policy_decision.ruleset_version,
                },
            )
        )
        return approval

    def reject(
        self,
        approval_id: UUID,
        *,
        now: datetime,
        decision_rule_reference: str | None = None,
    ) -> ApprovalRequest:
        store = self._store
        approval = store.get_approval(approval_id)
        if approval.decision is not ApprovalDecision.PENDING:
            raise ApprovalInvalidError(f"approval already decided: {approval.decision.value}")
        approval = store.update_approval_decision(
            approval_id,
            ApprovalDecision.REJECTED,
            decided_at=now,
            decision_rule_reference=decision_rule_reference,
        )
        store.update_intent_status(
            approval.action_intent_id,
            IntentStatus.DENIED,
            now=now,
        )
        return approval

    # -- execution -------------------------------------------------------

    def execute(self, intent_id: UUID, *, now: datetime) -> ExecutionOutcome:
        """Run one execution pass for an eligible intent.

        Reconciliation always precedes any retry. The provider is called at most
        once per reconciliation key per pass; duplicate calls are idempotent.
        """
        with self._lock:
            return self._execute_locked(intent_id, now=now)

    def _execute_locked(self, intent_id: UUID, *, now: datetime) -> ExecutionOutcome:
        store = self._store
        intent = self._require_intent(intent_id)

        # Gate 1: a non-denied policy decision must exist.
        policy_decision = self._latest_policy_decision(intent_id)
        if policy_decision is None or policy_decision.decision is PolicyDecisionValue.DENY:
            reason_codes = (
                policy_decision.reason_codes if policy_decision else ("NO_POLICY_DECISION",)
            )
            store.update_intent_status(intent_id, IntentStatus.DENIED, now=now)
            raise PolicyDeniedError(reason_codes)

        # Gate 2: external writes require an approved approval bound to the
        # current payload version, unless policy directly allowed.
        if intent.current_payload_version_id is None:
            raise SideEffectError("intent has no payload version to execute")
        payload_version = store.get_payload_version(intent.current_payload_version_id)
        if policy_decision.decision is PolicyDecisionValue.REQUIRE_APPROVAL:
            approval = self._approved_approval_for_payload(intent_id, payload_version.id)
            if approval is None:
                store.update_intent_status(intent_id, IntentStatus.AWAITING_APPROVAL, now=now)
                raise ApprovalInvalidError("no approved approval bound to current payload version")
            if now >= approval.expires_at:
                store.update_approval_decision(
                    approval.id, ApprovalDecision.EXPIRED, decided_at=now
                )
                store.update_intent_status(intent_id, IntentStatus.AWAITING_APPROVAL, now=now)
                raise ApprovalInvalidError("approval expired before execution")

        # Gate 3: audit must record both proposal and (when required) approval.
        if not self._audit_chain_complete(intent_id, policy_decision):
            raise SideEffectError("audit chain incomplete; refusing provider effect")

        reconciliation_key = f"{intent.idempotency_key}:{payload_version.payload_hash}"

        # Reconcile FIRST: a receipt may already exist (duplicate delivery) or a
        # prior attempt may have applied the effect without committing a receipt.
        existing_receipt = store.find_receipt_by_reconciliation_key(
            self._provider.provider_name, reconciliation_key
        )
        if existing_receipt is not None:
            store.update_intent_status(intent_id, IntentStatus.CONFIRMED, now=now)
            attempts = store.list_attempts(intent_id)
            return ExecutionOutcome(
                intent_id=intent_id,
                status=IntentStatus.CONFIRMED,
                attempt=attempts[-1] if attempts else None,
                receipt=existing_receipt,
                reconciled=True,
                reason_codes=("RECEIPT_ALREADY_RECORDED",),
            )

        prior_attempts = store.list_attempts(intent_id)
        if prior_attempts:
            reconciled = self._reconcile_prior_attempt(
                intent_id=intent_id,
                reconciliation_key=reconciliation_key,
                prior_attempts=prior_attempts,
                now=now,
            )
            if reconciled:
                receipt = store.find_receipt_by_reconciliation_key(
                    self._provider.provider_name, reconciliation_key
                )
                store.update_intent_status(intent_id, IntentStatus.CONFIRMED, now=now)
                return ExecutionOutcome(
                    intent_id=intent_id,
                    status=IntentStatus.CONFIRMED,
                    attempt=prior_attempts[-1],
                    receipt=receipt,
                    reconciled=True,
                    reason_codes=("RECONCILED_PRIOR_ATTEMPT",),
                )

        if len(prior_attempts) >= self._max_attempts:
            store.update_intent_status(intent_id, IntentStatus.RECONCILIATION_REQUIRED, now=now)
            return ExecutionOutcome(
                intent_id=intent_id,
                status=IntentStatus.RECONCILIATION_REQUIRED,
                attempt=prior_attempts[-1] if prior_attempts else None,
                receipt=None,
                reason_codes=("MAX_ATTEMPTS_EXHAUSTED",),
            )

        # Record the attempt BEFORE crossing the provider boundary so a crash
        # after the call still leaves a reconcilable record.
        ordinal = store.next_attempt_ordinal(intent_id)
        fingerprint = request_fingerprint(
            idempotency_key=intent.idempotency_key,
            payload_hash=payload_version.payload_hash,
            attempt_ordinal=ordinal,
        )
        attempt = SideEffectAttempt(
            id=uuid4(),
            action_intent_id=intent_id,
            outbox_event_id=intent_id,  # in-memory kernel uses intent id as outbox ref
            ordinal=ordinal,
            state=AttemptState.STARTED,
            request_fingerprint=fingerprint,
            started_at=now,
        )
        store.insert_attempt(attempt)
        store.update_intent_status(intent_id, IntentStatus.PROCESSING, now=now)

        try:
            result: ProviderCallResult = self._provider.execute(
                reconciliation_key=reconciliation_key,
                request_fingerprint=fingerprint,
                target=payload_version.target,
                payload=payload_version.payload,
                now=now,
            )
        except FakeProviderCrash:
            # Crash after call before commit: leave attempt UNKNOWN so the next
            # pass reconciles instead of blindly retrying.
            store.update_attempt(
                attempt.id,
                state=AttemptState.UNKNOWN,
                finished_at=now,
                error_code="WORKER_CRASH_AFTER_CALL",
            )
            store.update_intent_status(intent_id, IntentStatus.RECONCILIATION_REQUIRED, now=now)
            return ExecutionOutcome(
                intent_id=intent_id,
                status=IntentStatus.RECONCILIATION_REQUIRED,
                attempt=store.get_attempt(attempt.id),
                receipt=None,
                reason_codes=("CRASH_AFTER_CALL_RECONCILE_REQUIRED",),
            )

        return self._settle_result(
            intent_id=intent_id,
            attempt=attempt,
            result=result,
            reconciliation_key=reconciliation_key,
            now=now,
        )

    def _settle_result(
        self,
        *,
        intent_id: UUID,
        attempt: SideEffectAttempt,
        result: ProviderCallResult,
        reconciliation_key: str,
        now: datetime,
    ) -> ExecutionOutcome:
        store = self._store
        if result.kind is ProviderResultKind.SUCCESS:
            store.update_attempt(
                attempt.id,
                state=AttemptState.SUCCEEDED,
                finished_at=now,
                response_metadata=dict(result.metadata),
            )
            receipt = self._record_receipt(
                attempt_id=attempt.id,
                reconciliation_key=reconciliation_key,
                provider_resource_id=result.provider_resource_id or "",
                final_state=ReceiptFinalState.SUCCEEDED,
                now=now,
                metadata=dict(result.metadata),
            )
            store.update_intent_status(intent_id, IntentStatus.CONFIRMED, now=now)
            self._audit.append(
                AuditEventDraft(
                    event_type="side_effect_executed",
                    actor_type=AuditActorType.WORKER,
                    actor_id=self._provider.provider_name,
                    resource_type="action_intent",
                    resource_id=intent_id,
                    trace_id=reconciliation_key,
                    event_data={
                        "attempt_id": str(attempt.id),
                        "receipt_id": str(receipt.id),
                        "provider_resource_id": receipt.provider_resource_id,
                    },
                )
            )
            return ExecutionOutcome(
                intent_id=intent_id,
                status=IntentStatus.CONFIRMED,
                attempt=store.get_attempt(attempt.id),
                receipt=receipt,
            )

        if result.kind is ProviderResultKind.UNKNOWN:
            # Timeout/ambiguous: reconcile immediately before any retry decision.
            reconciliation = self._provider.reconcile(
                reconciliation_key=reconciliation_key, now=now
            )
            if reconciliation.found and reconciliation.final_state is not None:
                store.update_attempt(
                    attempt.id,
                    state=AttemptState.SUCCEEDED,
                    finished_at=now,
                    response_metadata=dict(reconciliation.metadata),
                )
                receipt = self._record_receipt(
                    attempt_id=attempt.id,
                    reconciliation_key=reconciliation_key,
                    provider_resource_id=reconciliation.provider_resource_id or "",
                    final_state=reconciliation.final_state,
                    now=now,
                    metadata=dict(reconciliation.metadata),
                )
                store.update_intent_status(intent_id, IntentStatus.CONFIRMED, now=now)
                return ExecutionOutcome(
                    intent_id=intent_id,
                    status=IntentStatus.CONFIRMED,
                    attempt=store.get_attempt(attempt.id),
                    receipt=receipt,
                    reconciled=True,
                    reason_codes=("RECONCILED_AFTER_UNKNOWN",),
                )
            store.update_attempt(
                attempt.id,
                state=AttemptState.UNKNOWN,
                finished_at=now,
                error_code=result.error_code,
            )
            store.update_intent_status(intent_id, IntentStatus.RECONCILIATION_REQUIRED, now=now)
            return ExecutionOutcome(
                intent_id=intent_id,
                status=IntentStatus.RECONCILIATION_REQUIRED,
                attempt=store.get_attempt(attempt.id),
                receipt=None,
                reason_codes=("UNKNOWN_RESULT_RECONCILE_REQUIRED",),
            )

        # Definitive failure.
        terminal = result.failure_class is ProviderFailureClass.VALIDATION
        store.update_attempt(
            attempt.id,
            state=AttemptState.FAILED,
            finished_at=now,
            error_code=result.error_code,
            response_metadata=dict(result.metadata),
        )
        new_status = IntentStatus.FAILED if terminal else IntentStatus.RECONCILIATION_REQUIRED
        store.update_intent_status(intent_id, new_status, now=now)
        return ExecutionOutcome(
            intent_id=intent_id,
            status=new_status,
            attempt=store.get_attempt(attempt.id),
            receipt=None,
            reason_codes=(result.error_code or "PROVIDER_FAILURE",),
        )

    def _reconcile_prior_attempt(
        self,
        *,
        intent_id: UUID,
        reconciliation_key: str,
        prior_attempts: tuple[SideEffectAttempt, ...],
        now: datetime,
    ) -> bool:
        """Ask the provider whether a prior attempt actually applied the effect."""
        reconciliation: ReconciliationResult = self._provider.reconcile(
            reconciliation_key=reconciliation_key, now=now
        )
        if not reconciliation.found or reconciliation.final_state is None:
            return False
        last_attempt = prior_attempts[-1]
        self._store.update_attempt(
            last_attempt.id,
            state=AttemptState.SUCCEEDED,
            finished_at=now,
            response_metadata=dict(reconciliation.metadata),
        )
        self._record_receipt(
            attempt_id=last_attempt.id,
            reconciliation_key=reconciliation_key,
            provider_resource_id=reconciliation.provider_resource_id or "",
            final_state=reconciliation.final_state,
            now=now,
            metadata=dict(reconciliation.metadata),
        )
        return True

    def _record_receipt(
        self,
        *,
        attempt_id: UUID,
        reconciliation_key: str,
        provider_resource_id: str,
        final_state: ReceiptFinalState,
        now: datetime,
        metadata: dict[str, object],
    ) -> ProviderReceipt:
        receipt = ProviderReceipt(
            id=uuid4(),
            side_effect_attempt_id=attempt_id,
            provider=self._provider.provider_name,
            provider_resource_id=provider_resource_id,
            reconciliation_key=reconciliation_key,
            final_state=final_state,
            provider_timestamp=now,
            received_at=now,
            receipt_metadata=metadata,
        )
        return self._store.insert_receipt(receipt)

    # -- revoke ----------------------------------------------------------

    def revoke(self, intent_id: UUID, *, now: datetime) -> ReconciliationResult:
        store = self._store
        intent = self._require_intent(intent_id)
        if intent.current_payload_version_id is None:
            raise SideEffectError("intent has no payload version to revoke")
        payload_version = store.get_payload_version(intent.current_payload_version_id)
        reconciliation_key = f"{intent.idempotency_key}:{payload_version.payload_hash}"
        receipt = store.find_receipt_by_reconciliation_key(
            self._provider.provider_name, reconciliation_key
        )
        if receipt is None:
            raise SideEffectError("no receipt to revoke")
        result = self._provider.revoke(
            reconciliation_key=reconciliation_key,
            provider_resource_id=receipt.provider_resource_id,
            now=now,
        )
        if result.found and result.final_state is ReceiptFinalState.REVOKED:
            self._record_receipt(
                attempt_id=receipt.side_effect_attempt_id,
                reconciliation_key=f"{reconciliation_key}:revoke",
                provider_resource_id=receipt.provider_resource_id,
                final_state=ReceiptFinalState.REVOKED,
                now=now,
                metadata={"revoked_receipt_id": str(receipt.id)},
            )
            self._audit.append(
                AuditEventDraft(
                    event_type="side_effect_revoked",
                    actor_type=AuditActorType.USER,
                    resource_type="action_intent",
                    resource_id=intent_id,
                    trace_id=intent.idempotency_key,
                    event_data={"provider_resource_id": receipt.provider_resource_id},
                )
            )
        return result

    # -- replay ----------------------------------------------------------

    def replay(self, intent_id: UUID) -> ApprovalPageReplay:
        """Assemble the full authorization chain for the Approval page."""
        store = self._store
        intent = self._require_intent(intent_id)
        return ApprovalPageReplay(
            intent=intent,
            payload_versions=store.list_payload_versions(intent_id),
            policy_decisions=store.list_policy_decisions(intent_id),
            approvals=store.list_approvals(intent_id),
            attempts=store.list_attempts(intent_id),
            receipts=store.list_receipts(intent_id),
            audit_events=self._audit.list_for_resource("action_intent", intent_id),
        )

    # -- helpers ---------------------------------------------------------

    def _require_intent(self, intent_id: UUID) -> ActionIntent:
        store = self._store
        try:
            return store.get_intent(intent_id)
        except KeyError as exc:
            raise IntentNotFoundError(str(intent_id)) from exc

    def _latest_policy_decision(self, intent_id: UUID) -> PolicyDecisionRecord | None:
        decisions = self._store.list_policy_decisions(intent_id)
        if not decisions:
            return None
        # Decisions are recorded in insertion order; the last is the most recent.
        return decisions[-1]

    def _policy_decision_for_payload(
        self, intent_id: UUID, payload_version_id: UUID
    ) -> PolicyDecisionRecord | None:
        decisions = self._store.list_policy_decisions(intent_id)
        for decision in decisions:
            if decision.payload_version_id == payload_version_id:
                return decision
        return None

    def _approved_approval_for_payload(
        self, intent_id: UUID, payload_version_id: UUID
    ) -> ApprovalRequest | None:
        approvals = self._store.list_approvals(intent_id)
        for approval in approvals:
            if (
                approval.payload_version_id == payload_version_id
                and approval.decision is ApprovalDecision.APPROVED
            ):
                return approval
        return None

    def _audit_chain_complete(self, intent_id: UUID, policy_decision: PolicyDecisionRecord) -> bool:
        events = self._audit.list_for_resource("action_intent", intent_id)
        event_types = {event.event_type for event in events}
        if "side_effect_proposed" not in event_types:
            return False
        if policy_decision.decision is PolicyDecisionValue.REQUIRE_APPROVAL:
            return "side_effect_approved" in event_types
        return True
