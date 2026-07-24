"""Integration tests for the approval sweeper.

End-to-end flow: propose with expired approval -> sweeper expires -> verify
EXPIRED status and intent reverted to AWAITING_APPROVAL.

Uses in-memory stores so no Postgres is required.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from careerops.application.side_effect_kernel import (
    ProposalInput,
    SideEffectKernel,
)
from careerops.domain.side_effects import (
    ApprovalDecision,
    IntentStatus,
)
from careerops.infrastructure.database.side_effect_memory import (
    InMemorySideEffectStore,
)
from careerops.integrations.fake_side_effect_provider import FakeSideEffectProvider

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_proposal(**overrides: object) -> ProposalInput:
    defaults = {
        "action_kind": "send_email",
        "resource_type": "email_thread",
        "resource_id": uuid4(),
        "idempotency_key": f"key-{uuid4()}",
        "created_by": "test-user",
        "target": {"to": "hiring@example.com"},
        "payload": {"subject": "Hello", "body": "World"},
        "evidence_refs": ("resume/v3",),
        "trusted_facts": {
            "capability_released": True,
            "target_allowlisted": True,
        },
        "untrusted_claims": {},
    }
    defaults.update(overrides)
    return ProposalInput(**defaults)  # type: ignore[arg-type]


def _build_kernel(
    *,
    approval_ttl_seconds: int = 600,
) -> tuple[SideEffectKernel, InMemorySideEffectStore]:
    store = InMemorySideEffectStore()
    provider = FakeSideEffectProvider()
    kernel = SideEffectKernel(store, provider, approval_ttl_seconds=approval_ttl_seconds)
    return kernel, store


# -- sweeper: expired approval -> EXPIRED + intent AWAITING_APPROVAL ------


def test_sweeper_expires_past_due_approval() -> None:
    """PENDING approval past expires_at is expired; intent reverts."""
    kernel, store = _build_kernel(approval_ttl_seconds=10)

    proposal = _make_proposal()
    result = kernel.propose(proposal, now=NOW)
    assert result.intent.status is IntentStatus.AWAITING_APPROVAL

    # Request approval (expires at NOW + 10s)
    approval = kernel.request_approval(result.intent.id, requested_for="test-user", now=NOW)
    assert approval.decision is ApprovalDecision.PENDING
    assert approval.expires_at == NOW + timedelta(seconds=10)

    # Sweeper at NOW + 5s: not yet expired
    expired = kernel.expire_pending_approvals(now=NOW + timedelta(seconds=5))
    assert len(expired) == 0

    # Sweeper at NOW + 11s: expired
    expired = kernel.expire_pending_approvals(now=NOW + timedelta(seconds=11))
    assert len(expired) == 1
    assert expired[0].decision is ApprovalDecision.EXPIRED
    assert expired[0].decided_at == NOW + timedelta(seconds=11)

    # Intent reverted to AWAITING_APPROVAL
    intent = store.get_intent(result.intent.id)
    assert intent.status is IntentStatus.AWAITING_APPROVAL


def test_sweeper_skips_already_decided_approvals() -> None:
    """APPROVED/REJECTED approvals are not touched by the sweeper."""
    kernel, store = _build_kernel(approval_ttl_seconds=10)

    proposal = _make_proposal()
    result = kernel.propose(proposal, now=NOW)

    approval = kernel.request_approval(result.intent.id, requested_for="test-user", now=NOW)
    # Approve before expiry
    kernel.approve(approval.id, now=NOW + timedelta(seconds=1))

    # Sweeper after expiry
    expired = kernel.expire_pending_approvals(now=NOW + timedelta(seconds=15))
    assert len(expired) == 0

    # Approval still APPROVED
    stored = store.get_approval(approval.id)
    assert stored.decision is ApprovalDecision.APPROVED


def test_sweeper_skips_denied_intents() -> None:
    """Sweeper does not revert DENIED intents."""
    kernel, _store = _build_kernel(approval_ttl_seconds=10)

    proposal = _make_proposal()
    result = kernel.propose(proposal, now=NOW)

    approval = kernel.request_approval(result.intent.id, requested_for="test-user", now=NOW)
    # Reject
    kernel.reject(approval.id, now=NOW + timedelta(seconds=1))

    # Sweeper after expiry — the rejected approval should not be re-expired
    expired = kernel.expire_pending_approvals(now=NOW + timedelta(seconds=15))
    assert len(expired) == 0


def test_sweeper_records_audit_event() -> None:
    """Sweeper writes an ``approval_expired`` audit event."""
    kernel, _store = _build_kernel(approval_ttl_seconds=5)

    proposal = _make_proposal()
    result = kernel.propose(proposal, now=NOW)
    kernel.request_approval(result.intent.id, requested_for="test-user", now=NOW)

    kernel.expire_pending_approvals(now=NOW + timedelta(seconds=6))

    audit_events = kernel._audit.list_for_resource("action_intent", result.intent.id)
    expired_events = [e for e in audit_events if e.event_type == "approval_expired"]
    assert len(expired_events) == 1


def test_sweeper_on_empty_store() -> None:
    """Sweeper returns empty when no approvals exist."""
    kernel, _ = _build_kernel()
    expired = kernel.expire_pending_approvals(now=NOW)
    assert expired == ()


def test_e2e_propose_expired_sweeper_re_propose_approve_execute() -> None:
    """Full cycle: propose -> approval expires -> re-propose -> approve -> execute."""
    kernel, store = _build_kernel(approval_ttl_seconds=5)

    # 1. Propose
    proposal = _make_proposal()
    result = kernel.propose(proposal, now=NOW)
    intent_id = result.intent.id

    # 2. Request approval
    approval = kernel.request_approval(intent_id, requested_for="test-user", now=NOW)

    # 3. Sweeper expires it
    expired = kernel.expire_pending_approvals(now=NOW + timedelta(seconds=6))
    assert len(expired) == 1
    assert store.get_intent(intent_id).status is IntentStatus.AWAITING_APPROVAL

    # 4. Request new approval (re-uses existing via get_or_create or creates new)
    new_approval = kernel.request_approval(
        intent_id, requested_for="test-user", now=NOW + timedelta(seconds=7)
    )
    assert new_approval.decision is ApprovalDecision.PENDING
    assert new_approval.id != approval.id  # new approval record

    # 5. Approve
    kernel.approve(new_approval.id, now=NOW + timedelta(seconds=8))
    assert store.get_intent(intent_id).status is IntentStatus.ELIGIBLE

    # 6. Execute
    outcome = kernel.execute(intent_id, now=NOW + timedelta(seconds=9))
    assert outcome.status is IntentStatus.CONFIRMED
    assert outcome.receipt is not None
