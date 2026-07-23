"""Domain models for M5A: the general external side-effect kernel.

The authorization chain is:
``ActionIntent -> Immutable Payload Version -> Policy Decision -> Approval ->
Outbox -> Side-effect Worker -> Provider Reconciliation -> Audit Event``.

Safety invariants encoded here:
- Payload versions are immutable; any edit creates a new version with a new hash.
- Approval binds target + payload hash + policy decision (ruleset version) + expiration.
- Policy input is trusted business state and evidence refs only; model self-assertion
  is never an input to the policy decision.
- Unknown provider results require reconciliation before any retry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class IntentStatus(StrEnum):
    PROPOSED = "proposed"
    DENIED = "denied"
    AWAITING_APPROVAL = "awaiting_approval"
    ELIGIBLE = "eligible"
    PROCESSING = "processing"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class PolicyDecisionValue(StrEnum):
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    ALLOW = "allow"


class ApprovalDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class AttemptState(StrEnum):
    STARTED = "started"
    PROVIDER_CALL_MADE = "provider_call_made"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ReceiptFinalState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REVOKED = "revoked"


class ProviderResultKind(StrEnum):
    """Classification of a provider call outcome.

    ``UNKNOWN`` means the call did not return a definitive answer (e.g. timeout).
    API timeout does NOT imply the provider did not execute the action.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class ProviderFailureClass(StrEnum):
    """Provider failure classification used to decide retry eligibility."""

    VALIDATION = "validation"  # 4xx-class: never retry
    TRANSIENT = "transient"  # 5xx/network: bounded retry
    UNKNOWN = "unknown"  # timeout/ambiguous: reconcile first, never blind retry


def canonical_payload_hash(
    *,
    target: dict[str, object],
    payload: dict[str, object],
    attachment_refs: tuple[str, ...] = (),
) -> str:
    """Compute a stable SHA-256 over the immutable payload bytes.

    Any byte change in target, payload, or attachment refs produces a new hash,
    which invalidates every prior approval and policy decision bound to it.
    """
    canonical = json.dumps(
        {
            "target": target,
            "payload": payload,
            "attachment_refs": list(attachment_refs),
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def request_fingerprint(*, idempotency_key: str, payload_hash: str, attempt_ordinal: int) -> str:
    """Stable per-attempt fingerprint used by the provider for idempotent calls."""
    canonical = f"{idempotency_key}:{payload_hash}:{attempt_ordinal}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ActionPayloadVersion:
    """Immutable target + body + attachment refs with a content hash."""

    id: UUID
    action_intent_id: UUID
    version: int
    target: dict[str, object]
    payload: dict[str, object]
    attachment_refs: tuple[str, ...]
    payload_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("payload version must be positive")
        if len(self.payload_hash) != 64:
            raise ValueError("payload_hash must be a 64-char sha256 hex digest")
        expected = canonical_payload_hash(
            target=self.target,
            payload=self.payload,
            attachment_refs=self.attachment_refs,
        )
        if self.payload_hash != expected:
            raise ValueError("payload_hash does not match canonical payload bytes")


@dataclass(frozen=True, slots=True)
class ActionIntent:
    """A requested external action keyed by an idempotency key."""

    id: UUID
    action_kind: str
    resource_type: str
    resource_id: UUID
    idempotency_key: str
    status: IntentStatus
    current_payload_version_id: UUID | None
    created_by: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PolicyDecisionRecord:
    """A policy decision bound to a specific immutable payload version.

    ``trusted_facts`` and ``evidence_refs`` are recorded for replay; they are the
    ONLY inputs the policy engine may consult. ``untrusted_claims`` is recorded
    solely to evidence that it was ignored.
    """

    id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    ruleset_version: str
    decision: PolicyDecisionValue
    reason_codes: tuple[str, ...]
    payload_hash: str
    trusted_facts: dict[str, object] = field(default_factory=lambda: {})
    evidence_refs: tuple[str, ...] = ()
    untrusted_claims: dict[str, object] = field(default_factory=lambda: {})
    expires_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Approval bound to target + payload hash + policy decision + expiration."""

    id: UUID
    action_intent_id: UUID
    payload_version_id: UUID
    policy_decision_id: UUID
    requested_for: str
    decision: ApprovalDecision
    decision_rule_reference: str | None
    expires_at: datetime
    decided_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SideEffectAttempt:
    """One provider call attempt with lease, fingerprint, and outcome."""

    id: UUID
    action_intent_id: UUID
    outbox_event_id: UUID
    ordinal: int
    state: AttemptState
    request_fingerprint: str
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None
    response_metadata: dict[str, object] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class ProviderReceipt:
    """Provider-issued receipt with a stable reconciliation key."""

    id: UUID
    side_effect_attempt_id: UUID
    provider: str
    provider_resource_id: str
    reconciliation_key: str
    final_state: ReceiptFinalState
    provider_timestamp: datetime | None
    received_at: datetime
    receipt_metadata: dict[str, object] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class ProviderCallResult:
    """Outcome of a single provider call.

    ``kind`` distinguishes definitive success/failure from UNKNOWN (timeout).
    ``failure_class`` drives retry policy: VALIDATION never retries, UNKNOWN
    requires reconciliation before any retry.
    """

    kind: ProviderResultKind
    provider_resource_id: str | None = None
    reconciliation_key: str | None = None
    error_code: str | None = None
    failure_class: ProviderFailureClass = ProviderFailureClass.TRANSIENT
    metadata: dict[str, object] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Result of asking the provider whether a reconciliation key took effect."""

    found: bool
    final_state: ReceiptFinalState | None = None
    provider_resource_id: str | None = None
    metadata: dict[str, object] = field(default_factory=lambda: {})
