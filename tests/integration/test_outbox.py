"""Integration tests for the outbox driver.

End-to-end flow: propose -> approve -> execute -> outbox drain -> verify
provider received the event -> verify receipt.

Uses the in-memory stores and ``FakeSideEffectProvider`` so no Postgres is
required.  The ``OutboxPublisher`` delivers to a ``RecordingSink`` that
captures events for assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from careerops.application.outbox import (
    ClaimedOutboxEvent,
    OutboxEventType,
    OutboxPublisher,
)
from careerops.application.side_effect_kernel import (
    ProposalInput,
    SideEffectKernel,
)
from careerops.domain.side_effects import (
    ApprovalDecision,
    IntentStatus,
    PolicyDecisionValue,
)
from careerops.infrastructure.database.side_effect_memory import (
    InMemoryOutboxStore,
    InMemorySideEffectStore,
)
from careerops.integrations.fake_side_effect_provider import FakeSideEffectProvider

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class RecordingSink:
    """Captures delivered events for assertions."""

    def __init__(self) -> None:
        self.delivered: list[ClaimedOutboxEvent] = []

    def deliver(self, event: ClaimedOutboxEvent) -> None:
        self.delivered.append(event)


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


def _build_kernel() -> tuple[
    SideEffectKernel,
    InMemorySideEffectStore,
    InMemoryOutboxStore,
    FakeSideEffectProvider,
]:
    store = InMemorySideEffectStore()
    provider = FakeSideEffectProvider()
    outbox = InMemoryOutboxStore()
    kernel = SideEffectKernel(store, provider, outbox_store=outbox)
    return kernel, store, outbox, provider


# -- e2e: propose -> approve -> execute -> outbox drain -> verify ----------


def test_e2e_propose_approve_execute_outbox_drain() -> None:
    """Full authorization chain with outbox delivery."""
    kernel, store, outbox, provider = _build_kernel()
    sink = RecordingSink()
    publisher = OutboxPublisher(outbox, sink)

    # 1. Propose
    proposal = _make_proposal()
    result = kernel.propose(proposal, now=NOW)
    assert result.intent.status is IntentStatus.AWAITING_APPROVAL
    assert result.policy_decision.decision is PolicyDecisionValue.REQUIRE_APPROVAL

    # 2. Request and approve
    approval = kernel.request_approval(result.intent.id, requested_for="test-user", now=NOW)
    assert approval.decision is ApprovalDecision.PENDING

    approved = kernel.approve(approval.id, now=NOW + timedelta(seconds=1))
    assert approved.decision is ApprovalDecision.APPROVED

    # Verify intent is now ELIGIBLE
    intent = store.get_intent(result.intent.id)
    assert intent.status is IntentStatus.ELIGIBLE

    # 3. Execute (kernel calls provider, enqueues outbox event)
    outcome = kernel.execute(result.intent.id, now=NOW + timedelta(seconds=2))
    assert outcome.status is IntentStatus.CONFIRMED
    assert outcome.receipt is not None

    # Verify outbox has one event
    assert len(outbox._events) == 1
    event = next(iter(outbox._events.values()))
    assert event.event_type is OutboxEventType.PROVIDER_WRITE
    assert event.action_intent_id == result.intent.id

    # 4. Drain outbox
    drain_result = publisher.publish_batch(owner="test-drain", now=NOW + timedelta(seconds=3))
    assert drain_result.published == 1
    assert drain_result.claimed == 1

    # 5. Verify sink received the event
    assert len(sink.delivered) == 1
    assert sink.delivered[0].action_intent_id == result.intent.id

    # 6. Verify provider has the effect
    assert provider.effect_count() == 1


def test_e2e_no_outbox_when_kernel_has_no_outbox_store() -> None:
    """Kernel without outbox_store skips outbox enqueue silently."""
    store = InMemorySideEffectStore()
    provider = FakeSideEffectProvider()
    kernel = SideEffectKernel(store, provider)  # no outbox_store

    proposal = _make_proposal()
    result = kernel.propose(proposal, now=NOW)
    approval = kernel.request_approval(result.intent.id, requested_for="test-user", now=NOW)
    kernel.approve(approval.id, now=NOW + timedelta(seconds=1))
    outcome = kernel.execute(result.intent.id, now=NOW + timedelta(seconds=2))

    assert outcome.status is IntentStatus.CONFIRMED
    # No outbox store means no events were written.


def test_outbox_drain_with_no_pending_events() -> None:
    """Draining an empty outbox returns zero counts."""
    outbox = InMemoryOutboxStore()
    sink = RecordingSink()
    publisher = OutboxPublisher(outbox, sink)

    result = publisher.publish_batch(owner="test-drain", now=NOW)
    assert result.claimed == 0
    assert result.published == 0
    assert len(sink.delivered) == 0


def test_outbox_drain_respects_available_at() -> None:
    """Events with future available_at are not claimed."""
    from careerops.application.outbox import PendingOutboxEvent

    outbox = InMemoryOutboxStore()
    outbox.enqueue(
        PendingOutboxEvent(
            event_id=uuid4(),
            event_key=f"future/{uuid4()}",
            action_intent_id=uuid4(),
            payload_version_id=uuid4(),
            event_type=OutboxEventType.PROVIDER_WRITE,
            available_at=NOW + timedelta(hours=1),
        )
    )
    sink = RecordingSink()
    publisher = OutboxPublisher(outbox, sink)

    result = publisher.publish_batch(owner="test-drain", now=NOW)
    assert result.claimed == 0


def test_multiple_outbox_events_drained_in_batch() -> None:
    """Multiple enqueued events are drained in a single batch."""
    kernel, _store, outbox, _provider = _build_kernel()
    sink = RecordingSink()
    publisher = OutboxPublisher(outbox, sink)

    # Execute two intents
    for _ in range(2):
        proposal = _make_proposal()
        result = kernel.propose(proposal, now=NOW)
        approval = kernel.request_approval(result.intent.id, requested_for="test-user", now=NOW)
        kernel.approve(approval.id, now=NOW + timedelta(seconds=1))
        kernel.execute(result.intent.id, now=NOW + timedelta(seconds=2))

    assert len(outbox._events) == 2

    drain_result = publisher.publish_batch(owner="test-drain", now=NOW + timedelta(seconds=3))
    assert drain_result.published == 2
    assert len(sink.delivered) == 2
