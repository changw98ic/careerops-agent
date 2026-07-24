"""Concurrency tests for the M5A side-effect kernel.

Verifies that:
1. ``get_or_create_pending_approval`` is idempotent under concurrent access.
2. ``next_attempt_ordinal`` never produces duplicate ordinals under concurrent access.

Uses ``ThreadPoolExecutor`` to simulate concurrent callers against the
in-memory store (single-process safe via RLock).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from careerops.application.side_effect_kernel import (
    ProposalInput,
    SideEffectKernel,
)
from careerops.domain.side_effects import (
    ApprovalDecision,
    ApprovalRequest,
    SideEffectAttempt,
)
from careerops.infrastructure.database.side_effect_memory import (
    InMemorySideEffectStore,
)
from careerops.integrations.fake_side_effect_provider import FakeSideEffectProvider

NOW = datetime(2026, 1, 1, tzinfo=UTC)

THREAD_COUNT = 10


def _make_proposal(**overrides: object) -> ProposalInput:
    defaults = {
        "action_kind": "send_email",
        "resource_type": "email_thread",
        "resource_id": uuid4(),
        "idempotency_key": f"key-{uuid4().hex}",
        "created_by": "test",
        "target": {"to": "recruiter@example.com"},
        "payload": {"subject": "Hello", "body": "World"},
        "evidence_refs": ("evidence:1",),
        "trusted_facts": {"capability_released": True, "target_allowlisted": True},
        "authenticated": True,
    }
    defaults.update(overrides)
    return ProposalInput(**defaults)  # type: ignore[arg-type]


class TestConcurrentGetOrCreateApproval:
    """10 threads call get_or_create_pending_approval concurrently -> 1 approval."""

    def test_single_approval_under_contention(self) -> None:
        store = InMemorySideEffectStore()
        provider = FakeSideEffectProvider()
        kernel = SideEffectKernel(store, provider)

        proposal = _make_proposal()
        result = kernel.propose(proposal, now=NOW)
        intent_id = result.intent.id

        approvals: list[ApprovalRequest] = []

        def _get_or_create() -> ApprovalRequest:
            return kernel.get_or_create_pending_approval(
                intent_id, requested_for="reviewer", now=NOW
            )

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
            futures = [pool.submit(_get_or_create) for _ in range(THREAD_COUNT)]
            for future in as_completed(futures):
                approvals.append(future.result())

        # All callers must get the same approval (same id).
        approval_ids = {a.id for a in approvals}
        assert len(approval_ids) == 1, f"expected 1 unique approval, got {len(approval_ids)}"
        # The single approval must be PENDING.
        assert approvals[0].decision is ApprovalDecision.PENDING

    def test_single_approval_store_level(self) -> None:
        """Test the store's get_or_create_pending_approval directly."""
        store = InMemorySideEffectStore()

        # Seed an intent so the store has something to work with.
        from careerops.domain.side_effects import ActionIntent, IntentStatus

        intent = ActionIntent(
            id=uuid4(),
            action_kind="send_email",
            resource_type="email_thread",
            resource_id=uuid4(),
            idempotency_key=f"key-{uuid4().hex}",
            status=IntentStatus.PROPOSED,
            current_payload_version_id=None,
            created_by="test",
            created_at=NOW,
            updated_at=NOW,
        )
        store.insert_intent(intent)

        payload_version_id = uuid4()
        policy_decision_id = uuid4()
        expires_at = NOW + timedelta(seconds=600)

        approvals: list[ApprovalRequest] = []

        def _get_or_create() -> ApprovalRequest:
            return store.get_or_create_pending_approval(
                intent.id,
                payload_version_id=payload_version_id,
                policy_decision_id=policy_decision_id,
                requested_for="reviewer",
                now=NOW,
                expires_at=expires_at,
            )

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
            futures = [pool.submit(_get_or_create) for _ in range(THREAD_COUNT)]
            for future in as_completed(futures):
                approvals.append(future.result())

        approval_ids = {a.id for a in approvals}
        assert len(approval_ids) == 1
        assert approvals[0].decision is ApprovalDecision.PENDING
        assert approvals[0].action_intent_id == intent.id


class TestConcurrentNextAttemptOrdinal:
    """Concurrent get_ordinal + insert_attempt must never produce duplicate ordinals.

    The in-memory store uses RLock on each individual method, but the kernel's
    ``self._lock`` wraps the full ``execute`` path (get_ordinal + insert_attempt).
    This test simulates the kernel's composite operation under the store's lock
    to verify ordinal uniqueness.
    """

    def test_no_duplicate_ordinals(self) -> None:
        store = InMemorySideEffectStore()

        # Seed an intent.
        from careerops.domain.side_effects import (
            ActionIntent,
            AttemptState,
            IntentStatus,
        )

        intent = ActionIntent(
            id=uuid4(),
            action_kind="send_email",
            resource_type="email_thread",
            resource_id=uuid4(),
            idempotency_key=f"key-{uuid4().hex}",
            status=IntentStatus.PROPOSED,
            current_payload_version_id=None,
            created_by="test",
            created_at=NOW,
            updated_at=NOW,
        )
        store.insert_intent(intent)

        # In-memory store has no FK enforcement, so we can skip outbox seeding.
        ordinals: list[int] = []

        def _get_and_insert() -> int:
            # Simulate the kernel's composite operation under the store's lock.
            with store._lock:
                ordinal = store.next_attempt_ordinal(intent.id)
                attempt = SideEffectAttempt(
                    id=uuid4(),
                    action_intent_id=intent.id,
                    outbox_event_id=intent.id,
                    ordinal=ordinal,
                    state=AttemptState.STARTED,
                    request_fingerprint=f"fp-{uuid4().hex}",
                    started_at=NOW,
                )
                store.insert_attempt(attempt)
            return ordinal

        with ThreadPoolExecutor(max_workers=THREAD_COUNT) as pool:
            futures = [pool.submit(_get_and_insert) for _ in range(THREAD_COUNT)]
            for future in as_completed(futures):
                ordinals.append(future.result())

        # All ordinals must be unique: 1..THREAD_COUNT.
        assert len(ordinals) == THREAD_COUNT
        assert len(set(ordinals)) == THREAD_COUNT, f"duplicate ordinals detected: {ordinals}"
        assert min(ordinals) == 1
        assert max(ordinals) == THREAD_COUNT
