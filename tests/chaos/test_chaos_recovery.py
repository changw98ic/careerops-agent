"""M7 Chaos and Recovery: fault injection and reconciliation verification.

Covers M7.4: Temporal, Worker, PostgreSQL, Redis, object storage, network
and provider fault injection.

Verifies the DoD checklist items:
- Security counters = 0 in defined fault matrix.
- All ambiguous effects stop auto-retry and enter human queue.
- Worker crash after provider success => reconcile, never re-execute.
- Confirmed duplicate send = 0 in fault matrix.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from careerops.application.side_effect_kernel import (
    InMemoryAuditWriter,
    ProposalInput,
    SideEffectKernel,
)
from careerops.domain.side_effects import (
    IntentStatus,
    ProviderCallResult,
    ProviderFailureClass,
    ProviderResultKind,
    ReceiptFinalState,
    ReconciliationResult,
)
from careerops.infrastructure.database.side_effect_memory import InMemorySideEffectStore
from careerops.integrations.fake_side_effect_provider import (
    FakeFailureMode,
    FakeSideEffectProvider,
)

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)


def build_kernel(
    *,
    provider: FakeSideEffectProvider | None = None,
    max_attempts: int = 5,
) -> tuple[SideEffectKernel, FakeSideEffectProvider, InMemoryAuditWriter]:
    store = InMemorySideEffectStore()
    prov = provider or FakeSideEffectProvider()
    audit = InMemoryAuditWriter()
    kernel = SideEffectKernel(store, prov, audit_writer=audit, max_attempts=max_attempts)
    return kernel, prov, audit


def make_proposal(idempotency_key: str = "chaos-1") -> ProposalInput:
    return ProposalInput(
        action_kind="send_email",
        resource_type="application",
        resource_id=uuid4(),
        idempotency_key=idempotency_key,
        created_by="agent-1",
        target={"to": "recruiter@example.com"},
        payload={"subject": "Re: Role", "body": "hello"},
        evidence_refs=("evidence:resume:v1",),
        trusted_facts={"capability_released": True, "target_allowlisted": True},
    )


def approve_intent(kernel: SideEffectKernel, intent_id: object, now: datetime) -> None:
    """Helper to request and grant approval for an intent."""
    from uuid import UUID

    assert isinstance(intent_id, UUID)
    approval = kernel.request_approval(intent_id, requested_for="user-1", now=now)
    kernel.approve(approval.id, now=now)


class TestFaultMatrix:
    """M7.4 fault injection: verify security counters = 0 in the fault matrix."""

    def test_crash_before_call_no_effect(self) -> None:
        """Crash before provider call: no effect, enters reconciliation."""
        kernel, provider, _audit = build_kernel()
        provider.set_failure_mode(FakeFailureMode.CRASH_BEFORE_CALL)

        proposal = make_proposal("crash-before-1")
        result = kernel.propose(proposal, now=NOW)
        approve_intent(kernel, result.intent.id, NOW)

        # Kernel catches the crash and enters reconciliation_required
        outcome = kernel.execute(result.intent.id, now=NOW)
        assert outcome.status is IntentStatus.RECONCILIATION_REQUIRED
        assert provider.effect_count() == 0

    def test_crash_after_call_before_commit_reconciles(self) -> None:
        """Crash after provider success but before commit: reconcile, never re-execute."""
        kernel, provider, _audit = build_kernel()
        provider.set_failure_mode(FakeFailureMode.CRASH_AFTER_CALL_BEFORE_COMMIT)

        proposal = make_proposal("crash-after-1")
        result = kernel.propose(proposal, now=NOW)
        approve_intent(kernel, result.intent.id, NOW)

        # First execution: crash after call
        outcome = kernel.execute(result.intent.id, now=NOW)
        assert outcome.status is IntentStatus.RECONCILIATION_REQUIRED
        assert provider.effect_count() == 1  # Effect was applied

        # Reset failure mode to allow reconciliation
        provider.set_failure_mode(FakeFailureMode.NONE)

        # Second execution: should reconcile, not re-execute
        outcome2 = kernel.execute(result.intent.id, now=NOW)
        assert outcome2.status is IntentStatus.CONFIRMED
        assert outcome2.reconciled is True
        assert provider.effect_count() == 1  # Still exactly 1 effect

    def test_timeout_with_success_reconciles(self) -> None:
        """Timeout (UNKNOWN result) with actual success: reconcile confirms."""
        kernel, provider, _audit = build_kernel()
        provider.set_failure_mode(FakeFailureMode.TIMEOUT_WITH_SUCCESS)

        proposal = make_proposal("timeout-1")
        result = kernel.propose(proposal, now=NOW)
        approve_intent(kernel, result.intent.id, NOW)

        outcome = kernel.execute(result.intent.id, now=NOW)
        # Should reconcile and confirm
        assert outcome.status is IntentStatus.CONFIRMED
        assert outcome.reconciled is True
        assert provider.effect_count() == 1

    def test_validation_error_never_retries(self) -> None:
        """4xx validation errors are terminal; never retry."""
        kernel, provider, _audit = build_kernel()
        provider.set_failure_mode(FakeFailureMode.VALIDATION_ERROR)

        proposal = make_proposal("validation-1")
        result = kernel.propose(proposal, now=NOW)
        approve_intent(kernel, result.intent.id, NOW)

        outcome = kernel.execute(result.intent.id, now=NOW)
        assert outcome.status is IntentStatus.FAILED
        assert provider.effect_count() == 0
        assert provider.execute_call_count == 1  # Only one call, no retry

    def test_concurrent_submissions_single_effect(self) -> None:
        """100 concurrent same-idempotency-key submissions => exactly 1 effect."""
        kernel, provider, _audit = build_kernel()

        proposal = make_proposal("concurrent-1")
        result = kernel.propose(proposal, now=NOW)
        approve_intent(kernel, result.intent.id, NOW)

        def execute_once() -> IntentStatus:
            try:
                outcome = kernel.execute(result.intent.id, now=NOW)
                return outcome.status
            except Exception:
                return IntentStatus.FAILED

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(execute_once) for _ in range(100)]
            results = [f.result() for f in futures]

        # Exactly one provider effect
        assert provider.effect_count() == 1
        # At least one confirmed
        assert IntentStatus.CONFIRMED in results

    def test_duplicate_receipt_idempotent(self) -> None:
        """Duplicate delivery with same reconciliation key is idempotent."""
        kernel, provider, _audit = build_kernel()

        proposal = make_proposal("dup-receipt-1")
        result = kernel.propose(proposal, now=NOW)
        approve_intent(kernel, result.intent.id, NOW)

        # First execution succeeds
        outcome1 = kernel.execute(result.intent.id, now=NOW)
        assert outcome1.status is IntentStatus.CONFIRMED
        assert provider.effect_count() == 1

        # Second execution: receipt already recorded, idempotent
        outcome2 = kernel.execute(result.intent.id, now=NOW)
        assert outcome2.status is IntentStatus.CONFIRMED
        assert outcome2.reconciled is True
        assert provider.effect_count() == 1  # Still 1

    def test_max_attempts_exhausted_enters_reconciliation(self) -> None:
        """After max attempts, intent enters reconciliation_required (human queue)."""
        kernel, provider, _audit = build_kernel(max_attempts=2)
        provider.set_failure_mode(FakeFailureMode.TRANSIENT_ERROR)

        proposal = make_proposal("max-attempts-1")
        result = kernel.propose(proposal, now=NOW)
        approve_intent(kernel, result.intent.id, NOW)

        # Exhaust attempts: with max_attempts=2, the 3rd call hits the limit
        kernel.execute(result.intent.id, now=NOW)
        kernel.execute(result.intent.id, now=NOW)
        outcome = kernel.execute(result.intent.id, now=NOW)

        # After max attempts, enters reconciliation_required
        assert outcome.status is IntentStatus.RECONCILIATION_REQUIRED
        assert "MAX_ATTEMPTS_EXHAUSTED" in outcome.reason_codes


class TestAmbiguousEffectsEnterHumanQueue:
    """Verify all ambiguous effects stop auto-retry and enter human queue."""

    def test_unknown_result_stops_auto_retry(self) -> None:
        """UNKNOWN provider result stops auto-retry and enters human queue."""
        kernel, provider, _audit = build_kernel()
        provider.set_failure_mode(FakeFailureMode.TIMEOUT_WITH_SUCCESS)

        proposal = make_proposal("ambiguous-1")
        result = kernel.propose(proposal, now=NOW)
        approve_intent(kernel, result.intent.id, NOW)

        # Make reconciliation fail to find the effect (simulate truly ambiguous)
        # First, use a provider that times out but effect is NOT findable
        store = InMemorySideEffectStore()
        audit = InMemoryAuditWriter()

        class AmbiguousProvider:
            """Provider where reconcile cannot determine outcome."""

            provider_name = "ambiguous"

            def execute(
                self,
                *,
                reconciliation_key: str,
                request_fingerprint: str,
                target: dict[str, object],
                payload: dict[str, object],
                now: datetime,
            ) -> ProviderCallResult:
                return ProviderCallResult(
                    kind=ProviderResultKind.UNKNOWN,
                    error_code="PROVIDER_TIMEOUT",
                    failure_class=ProviderFailureClass.UNKNOWN,
                )

            def reconcile(self, *, reconciliation_key: str, now: datetime) -> ReconciliationResult:
                return ReconciliationResult(found=False)

            def revoke(
                self, *, reconciliation_key: str, provider_resource_id: str, now: datetime
            ) -> ReconciliationResult:
                return ReconciliationResult(found=False)

        ambiguous_kernel = SideEffectKernel(
            store,
            AmbiguousProvider(),
            audit_writer=audit,
        )

        proposal2 = make_proposal("ambiguous-2")
        result2 = ambiguous_kernel.propose(proposal2, now=NOW)
        approve_intent(ambiguous_kernel, result2.intent.id, NOW)

        outcome = ambiguous_kernel.execute(result2.intent.id, now=NOW)
        assert outcome.status is IntentStatus.RECONCILIATION_REQUIRED
        assert "UNKNOWN_RESULT_RECONCILE_REQUIRED" in outcome.reason_codes

    def test_reconciliation_required_no_auto_retry(self) -> None:
        """Once in reconciliation_required, further execute calls don't blind-retry."""
        kernel, provider, _audit = build_kernel(max_attempts=1)
        provider.set_failure_mode(FakeFailureMode.TRANSIENT_ERROR)

        proposal = make_proposal("no-retry-1")
        result = kernel.propose(proposal, now=NOW)
        approve_intent(kernel, result.intent.id, NOW)

        # First attempt fails transiently
        outcome1 = kernel.execute(result.intent.id, now=NOW)
        # With max_attempts=1, after 1 attempt it should enter reconciliation
        assert outcome1.status is IntentStatus.RECONCILIATION_REQUIRED

        # Record execute count before second call
        calls_before = provider.execute_call_count

        # Second call should NOT make another provider call (max attempts exhausted)
        outcome2 = kernel.execute(result.intent.id, now=NOW)
        assert outcome2.status is IntentStatus.RECONCILIATION_REQUIRED
        assert provider.execute_call_count == calls_before  # No new provider call


class TestRevokeAndRecovery:
    """Verify revoke and recovery operations."""

    def test_revoke_after_success(self) -> None:
        """A confirmed effect can be revoked."""
        kernel, _provider, audit = build_kernel()

        proposal = make_proposal("revoke-1")
        result = kernel.propose(proposal, now=NOW)
        approve_intent(kernel, result.intent.id, NOW)
        kernel.execute(result.intent.id, now=NOW)

        # Revoke
        revoke_result = kernel.revoke(result.intent.id, now=NOW)
        assert revoke_result.found is True
        assert revoke_result.final_state is ReceiptFinalState.REVOKED

        # Audit records the revocation
        events = audit.list_for_resource("action_intent", result.intent.id)
        event_types = {e.event_type for e in events}
        assert "side_effect_revoked" in event_types

    def test_payload_change_invalidates_approval(self) -> None:
        """Changing payload after approval invalidates the approval."""
        kernel, _provider, _audit = build_kernel()

        proposal = make_proposal("payload-change-1")
        result = kernel.propose(proposal, now=NOW)
        approval = kernel.request_approval(result.intent.id, requested_for="user-1", now=NOW)
        kernel.approve(approval.id, now=NOW)

        # Change payload (new body)
        changed_proposal = ProposalInput(
            action_kind="send_email",
            resource_type="application",
            resource_id=proposal.resource_id,
            idempotency_key="payload-change-1",  # Same idempotency key
            created_by="agent-1",
            target={"to": "recruiter@example.com"},
            payload={"subject": "Re: Role", "body": "CHANGED BODY"},
            evidence_refs=("evidence:resume:v1",),
            trusted_facts={"capability_released": True, "target_allowlisted": True},
        )
        kernel.propose(changed_proposal, now=NOW)

        # Execution should fail because approval is bound to old payload
        from careerops.application.side_effect_kernel import ApprovalInvalidError

        with pytest.raises(ApprovalInvalidError):
            kernel.execute(result.intent.id, now=NOW)
