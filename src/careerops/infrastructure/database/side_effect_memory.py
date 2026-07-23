"""In-memory repositories for the M5A side-effect kernel.

These repositories are used by unit tests and by the fake-provider chaos
scenarios. They are intentionally explicit about every invariant the kernel
relies on (idempotency, immutability, approval binding, reconciliation).
"""

from __future__ import annotations

import threading
from datetime import datetime
from uuid import UUID

from careerops.domain.side_effects import (
    ActionIntent,
    ActionPayloadVersion,
    ApprovalDecision,
    ApprovalRequest,
    AttemptState,
    IntentStatus,
    PolicyDecisionRecord,
    ProviderReceipt,
    SideEffectAttempt,
)


class DuplicateIdempotencyKeyError(RuntimeError):
    pass


class InMemorySideEffectStore:
    """Thread-safe in-memory store covering the full M5A authorization chain."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._intents: dict[UUID, ActionIntent] = {}
        self._intents_by_idempotency_key: dict[str, UUID] = {}
        self._payload_versions: dict[UUID, ActionPayloadVersion] = {}
        self._payload_versions_by_intent: dict[UUID, list[UUID]] = {}
        self._policy_decisions: dict[UUID, PolicyDecisionRecord] = {}
        self._approvals: dict[UUID, ApprovalRequest] = {}
        self._attempts: dict[UUID, SideEffectAttempt] = {}
        self._attempts_by_intent: dict[UUID, list[UUID]] = {}
        self._receipts: dict[UUID, ProviderReceipt] = {}
        self._receipts_by_reconciliation_key: dict[tuple[str, str], UUID] = {}

    # -- intents ---------------------------------------------------------

    def insert_intent(self, intent: ActionIntent) -> ActionIntent:
        with self._lock:
            existing = self._intents_by_idempotency_key.get(intent.idempotency_key)
            if existing is not None and existing != intent.id:
                raise DuplicateIdempotencyKeyError(intent.idempotency_key)
            self._intents[intent.id] = intent
            self._intents_by_idempotency_key[intent.idempotency_key] = intent.id
            self._payload_versions_by_intent.setdefault(intent.id, [])
            self._attempts_by_intent.setdefault(intent.id, [])
            return intent

    def find_intent_by_idempotency_key(self, idempotency_key: str) -> ActionIntent | None:
        with self._lock:
            intent_id = self._intents_by_idempotency_key.get(idempotency_key)
            return self._intents.get(intent_id) if intent_id else None

    def get_intent(self, intent_id: UUID) -> ActionIntent:
        with self._lock:
            return self._intents[intent_id]

    def update_intent_status(
        self,
        intent_id: UUID,
        status: IntentStatus,
        *,
        now: datetime,
        current_payload_version_id: UUID | None = None,
    ) -> ActionIntent:
        with self._lock:
            intent = self._intents[intent_id]
            updated = ActionIntent(
                id=intent.id,
                action_kind=intent.action_kind,
                resource_type=intent.resource_type,
                resource_id=intent.resource_id,
                idempotency_key=intent.idempotency_key,
                status=status,
                current_payload_version_id=(
                    current_payload_version_id
                    if current_payload_version_id is not None
                    else intent.current_payload_version_id
                ),
                created_by=intent.created_by,
                created_at=intent.created_at,
                updated_at=now,
            )
            self._intents[intent_id] = updated
            return updated

    # -- payload versions ------------------------------------------------

    def insert_payload_version(self, version: ActionPayloadVersion) -> ActionPayloadVersion:
        with self._lock:
            existing = self._payload_versions_by_intent.get(version.action_intent_id, [])
            for version_id in existing:
                stored = self._payload_versions[version_id]
                if stored.payload_hash == version.payload_hash:
                    return stored
                if stored.version == version.version:
                    raise ValueError("payload version number already used for this intent")
            self._payload_versions[version.id] = version
            self._payload_versions_by_intent.setdefault(version.action_intent_id, []).append(
                version.id
            )
            return version

    def get_payload_version(self, version_id: UUID) -> ActionPayloadVersion:
        with self._lock:
            return self._payload_versions[version_id]

    def list_payload_versions(self, intent_id: UUID) -> tuple[ActionPayloadVersion, ...]:
        with self._lock:
            ids = self._payload_versions_by_intent.get(intent_id, [])
            return tuple(
                sorted(
                    (self._payload_versions[version_id] for version_id in ids),
                    key=lambda version: version.version,
                )
            )

    # -- policy decisions ------------------------------------------------

    def insert_policy_decision(self, decision: PolicyDecisionRecord) -> PolicyDecisionRecord:
        with self._lock:
            self._policy_decisions[decision.id] = decision
            return decision

    def get_policy_decision(self, decision_id: UUID) -> PolicyDecisionRecord:
        with self._lock:
            return self._policy_decisions[decision_id]

    def list_policy_decisions(self, intent_id: UUID) -> tuple[PolicyDecisionRecord, ...]:
        with self._lock:
            return tuple(
                decision
                for decision in self._policy_decisions.values()
                if decision.action_intent_id == intent_id
            )

    # -- approvals -------------------------------------------------------

    def insert_approval(self, approval: ApprovalRequest) -> ApprovalRequest:
        with self._lock:
            self._approvals[approval.id] = approval
            return approval

    def get_approval(self, approval_id: UUID) -> ApprovalRequest:
        with self._lock:
            return self._approvals[approval_id]

    def update_approval_decision(
        self,
        approval_id: UUID,
        decision: ApprovalDecision,
        *,
        decided_at: datetime,
        decision_rule_reference: str | None = None,
    ) -> ApprovalRequest:
        with self._lock:
            approval = self._approvals[approval_id]
            updated = ApprovalRequest(
                id=approval.id,
                action_intent_id=approval.action_intent_id,
                payload_version_id=approval.payload_version_id,
                policy_decision_id=approval.policy_decision_id,
                requested_for=approval.requested_for,
                decision=decision,
                decision_rule_reference=(
                    decision_rule_reference
                    if decision_rule_reference is not None
                    else approval.decision_rule_reference
                ),
                expires_at=approval.expires_at,
                decided_at=decided_at,
                created_at=approval.created_at,
            )
            self._approvals[approval_id] = updated
            return updated

    def list_approvals(self, intent_id: UUID) -> tuple[ApprovalRequest, ...]:
        with self._lock:
            return tuple(
                approval
                for approval in self._approvals.values()
                if approval.action_intent_id == intent_id
            )

    # -- attempts --------------------------------------------------------

    def insert_attempt(self, attempt: SideEffectAttempt) -> SideEffectAttempt:
        with self._lock:
            self._attempts[attempt.id] = attempt
            self._attempts_by_intent.setdefault(attempt.action_intent_id, []).append(attempt.id)
            return attempt

    def get_attempt(self, attempt_id: UUID) -> SideEffectAttempt:
        with self._lock:
            return self._attempts[attempt_id]

    def update_attempt(
        self,
        attempt_id: UUID,
        *,
        state: AttemptState,
        finished_at: datetime | None = None,
        error_code: str | None = None,
        response_metadata: dict[str, object] | None = None,
    ) -> SideEffectAttempt:
        with self._lock:
            attempt = self._attempts[attempt_id]
            updated = SideEffectAttempt(
                id=attempt.id,
                action_intent_id=attempt.action_intent_id,
                outbox_event_id=attempt.outbox_event_id,
                ordinal=attempt.ordinal,
                state=state,
                request_fingerprint=attempt.request_fingerprint,
                started_at=attempt.started_at,
                finished_at=finished_at if finished_at is not None else attempt.finished_at,
                error_code=error_code if error_code is not None else attempt.error_code,
                response_metadata=(
                    dict(response_metadata)
                    if response_metadata is not None
                    else dict(attempt.response_metadata)
                ),
            )
            self._attempts[attempt_id] = updated
            return updated

    def list_attempts(self, intent_id: UUID) -> tuple[SideEffectAttempt, ...]:
        with self._lock:
            ids = self._attempts_by_intent.get(intent_id, [])
            return tuple(
                sorted(
                    (self._attempts[attempt_id] for attempt_id in ids),
                    key=lambda attempt: attempt.ordinal,
                )
            )

    def next_attempt_ordinal(self, intent_id: UUID) -> int:
        with self._lock:
            return len(self._attempts_by_intent.get(intent_id, [])) + 1

    # -- receipts --------------------------------------------------------

    def insert_receipt(self, receipt: ProviderReceipt) -> ProviderReceipt:
        with self._lock:
            key = (receipt.provider, receipt.reconciliation_key)
            existing_id = self._receipts_by_reconciliation_key.get(key)
            if existing_id is not None:
                return self._receipts[existing_id]
            self._receipts[receipt.id] = receipt
            self._receipts_by_reconciliation_key[key] = receipt.id
            return receipt

    def find_receipt_by_reconciliation_key(
        self, provider: str, reconciliation_key: str
    ) -> ProviderReceipt | None:
        with self._lock:
            receipt_id = self._receipts_by_reconciliation_key.get((provider, reconciliation_key))
            return self._receipts.get(receipt_id) if receipt_id else None

    def list_receipts(self, intent_id: UUID) -> tuple[ProviderReceipt, ...]:
        with self._lock:
            attempt_ids = set(self._attempts_by_intent.get(intent_id, []))
            return tuple(
                receipt
                for receipt in self._receipts.values()
                if receipt.side_effect_attempt_id in attempt_ids
            )
