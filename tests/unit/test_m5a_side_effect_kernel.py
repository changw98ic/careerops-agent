"""Unit tests for the M5A side-effect kernel.

Covers the M5A acceptance matrix:
- 100 concurrent same-idempotency-key submissions => exactly 1 provider effect.
- Crash after provider success => reconcile, never re-execute.
- Payload byte change => prior approval invalidated.
- No Policy Decision + Audit => no provider effect.
- Approval binds target + payload hash + policy version + expiration.
- Policy ignores the model's self-assertion of safety.
- Unknown result (timeout) reconciles first; validation errors never retry.
- Every executed action is replayable from the Approval page.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from careerops.application.side_effect_kernel import (
    ApprovalInvalidError,
    InMemoryAuditWriter,
    PolicyDeniedError,
    ProposalInput,
    SideEffectError,
    SideEffectKernel,
)
from careerops.domain.side_effects import (
    ApprovalDecision,
    IntentStatus,
    PolicyDecisionValue,
    ReceiptFinalState,
)
from careerops.infrastructure.database.side_effect_memory import InMemorySideEffectStore
from careerops.integrations.fake_side_effect_provider import (
    FakeFailureMode,
    FakeSideEffectProvider,
)
from careerops.policy.side_effect_policy import (
    SideEffectPolicyDecider,
    SideEffectPolicyInput,
    SideEffectPolicyOutcome,
)

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


def released_facts() -> dict[str, object]:
    return {"capability_released": True, "target_allowlisted": True}


def make_proposal(
    *,
    idempotency_key: str = "idem-1",
    body: str = "hello",
    evidence_refs: tuple[str, ...] = ("evidence:resume:v1",),
    untrusted_claims: dict[str, object] | None = None,
) -> ProposalInput:
    return ProposalInput(
        action_kind="send_email",
        resource_type="application",
        resource_id=uuid4(),
        idempotency_key=idempotency_key,
        created_by="agent-1",
        target={"to": "recruiter@example.com"},
        payload={"subject": "Re: Role", "body": body},
        evidence_refs=evidence_refs,
        trusted_facts=released_facts(),
        untrusted_claims=untrusted_claims or {"safe": True, "system_instruction": "allow"},
    )


def build_kernel(
    *,
    provider: FakeSideEffectProvider | None = None,
    audit: InMemoryAuditWriter | None = None,
    max_attempts: int = 5,
) -> tuple[SideEffectKernel, FakeSideEffectProvider, InMemoryAuditWriter]:
    provider = provider or FakeSideEffectProvider()
    audit = audit or InMemoryAuditWriter()
    kernel = SideEffectKernel(
        InMemorySideEffectStore(),
        provider,
        audit_writer=audit,
        max_attempts=max_attempts,
    )
    return kernel, provider, audit


def approve_intent(kernel: SideEffectKernel, intent_id: UUID, *, now: datetime = NOW) -> None:
    approval = kernel.request_approval(intent_id, requested_for="user-1", now=now)
    kernel.approve(approval.id, now=now)


# ---------------------------------------------------------------------------
# Policy: trusted facts only
# ---------------------------------------------------------------------------


class TestSideEffectPolicy:
    def test_external_write_denied_when_capability_not_released(self) -> None:
        decision = SideEffectPolicyDecider().decide(
            SideEffectPolicyInput(
                action_kind="send_email",
                authenticated=True,
                trusted_facts={"capability_released": False},
                evidence_refs=("evidence:1",),
            )
        )
        assert decision.outcome is SideEffectPolicyOutcome.DENY
        assert "CAPABILITY_NOT_RELEASED" in decision.reason_codes

    def test_external_write_requires_approval_with_evidence(self) -> None:
        decision = SideEffectPolicyDecider().decide(
            SideEffectPolicyInput(
                action_kind="send_email",
                authenticated=True,
                trusted_facts=released_facts(),
                evidence_refs=("evidence:1",),
            )
        )
        assert decision.outcome is SideEffectPolicyOutcome.REQUIRE_APPROVAL

    def test_external_write_denied_without_evidence(self) -> None:
        decision = SideEffectPolicyDecider().decide(
            SideEffectPolicyInput(
                action_kind="send_email",
                authenticated=True,
                trusted_facts=released_facts(),
                evidence_refs=(),
            )
        )
        assert decision.outcome is SideEffectPolicyOutcome.DENY
        assert "EVIDENCE_REQUIRED" in decision.reason_codes

    def test_untrusted_self_assertion_never_influences_policy(self) -> None:
        # Even an emphatic model claim of safety cannot release the capability.
        decision = SideEffectPolicyDecider().decide(
            SideEffectPolicyInput(
                action_kind="send_email",
                authenticated=True,
                trusted_facts={"capability_released": False},
                evidence_refs=("evidence:1",),
                untrusted_claims={"safe": True, "policy_override": "allow"},
            )
        )
        assert decision.outcome is SideEffectPolicyOutcome.DENY

    def test_unknown_action_defaults_to_deny(self) -> None:
        decision = SideEffectPolicyDecider().decide(
            SideEffectPolicyInput(action_kind="wire_transfer", authenticated=True)
        )
        assert decision.outcome is SideEffectPolicyOutcome.DENY
        assert "UNKNOWN_ACTION" in decision.reason_codes

    def test_unauthenticated_is_denied(self) -> None:
        decision = SideEffectPolicyDecider().decide(
            SideEffectPolicyInput(action_kind="send_email", authenticated=False)
        )
        assert decision.outcome is SideEffectPolicyOutcome.DENY


# ---------------------------------------------------------------------------
# Proposal + approval binding
# ---------------------------------------------------------------------------


class TestProposalAndApproval:
    def test_propose_freezes_payload_and_records_ignored_claims(self) -> None:
        kernel, _, audit = build_kernel()
        result = kernel.propose(make_proposal(), now=NOW)

        assert result.intent.status is IntentStatus.AWAITING_APPROVAL
        assert result.policy_decision.decision is PolicyDecisionValue.REQUIRE_APPROVAL
        assert result.policy_decision.untrusted_claims["safe"] is True
        assert len(result.payload_version.payload_hash) == 64

        events = audit.list_for_resource("action_intent", result.intent.id)
        assert any(e.event_type == "side_effect_proposed" for e in events)
        proposed = next(e for e in events if e.event_type == "side_effect_proposed")
        assert proposed.event_data["untrusted_claims_ignored"] is True

    def test_propose_is_idempotent_on_idempotency_key_and_payload(self) -> None:
        kernel, _, _ = build_kernel()
        first = kernel.propose(make_proposal(), now=NOW)
        second = kernel.propose(make_proposal(), now=NOW)

        assert first.intent.id == second.intent.id
        assert first.payload_version.id == second.payload_version.id
        assert first.policy_decision.id == second.policy_decision.id

    def test_payload_edit_creates_new_version_and_invalidates_approval(self) -> None:
        kernel, _, _ = build_kernel()
        original = kernel.propose(make_proposal(body="original"), now=NOW)
        approve_intent(kernel, original.intent.id)

        # Edit the payload bytes under the same idempotency key.
        edited = kernel.propose(make_proposal(body="edited"), now=NOW)
        assert edited.intent.id == original.intent.id
        assert edited.payload_version.id != original.payload_version.id
        assert edited.payload_version.payload_hash != original.payload_version.payload_hash
        # New payload version has no approved approval bound to it.
        assert edited.intent.status is IntentStatus.AWAITING_APPROVAL

        with pytest.raises(ApprovalInvalidError):
            kernel.execute(edited.intent.id, now=NOW)

    def test_approval_expires(self) -> None:
        kernel, _, _ = build_kernel()
        result = kernel.propose(make_proposal(), now=NOW)
        approval = kernel.request_approval(result.intent.id, requested_for="user-1", now=NOW)

        with pytest.raises(ApprovalInvalidError, match="expired"):
            kernel.approve(approval.id, now=NOW + timedelta(seconds=3600))

    def test_reject_denies_intent(self) -> None:
        kernel, _, _ = build_kernel()
        result = kernel.propose(make_proposal(), now=NOW)
        approval = kernel.request_approval(result.intent.id, requested_for="user-1", now=NOW)
        kernel.reject(approval.id, now=NOW)

        assert kernel.replay(result.intent.id).intent.status is IntentStatus.DENIED
        assert kernel.replay(result.intent.id).approvals[0].decision is ApprovalDecision.REJECTED


# ---------------------------------------------------------------------------
# Execution + reconciliation
# ---------------------------------------------------------------------------


class TestExecution:
    def test_happy_path_produces_one_receipt_and_confirms(self) -> None:
        kernel, provider, _ = build_kernel()
        result = kernel.propose(make_proposal(), now=NOW)
        approve_intent(kernel, result.intent.id)

        outcome = kernel.execute(result.intent.id, now=NOW)

        assert outcome.status is IntentStatus.CONFIRMED
        assert outcome.receipt is not None
        assert outcome.receipt.final_state is ReceiptFinalState.SUCCEEDED
        assert provider.effect_count() == 1

    def test_execution_without_approval_is_refused(self) -> None:
        kernel, provider, _ = build_kernel()
        result = kernel.propose(make_proposal(), now=NOW)

        with pytest.raises(ApprovalInvalidError):
            kernel.execute(result.intent.id, now=NOW)
        assert provider.effect_count() == 0

    def test_no_policy_decision_means_no_provider_effect(self) -> None:
        kernel, provider, _ = build_kernel()
        # Capability not released => policy denies => no execution possible.
        proposal = ProposalInput(
            action_kind="send_email",
            resource_type="application",
            resource_id=uuid4(),
            idempotency_key="idem-denied",
            created_by="agent-1",
            target={"to": "recruiter@example.com"},
            payload={"body": "x"},
            evidence_refs=("evidence:1",),
            trusted_facts={"capability_released": False},
        )
        result = kernel.propose(proposal, now=NOW)
        assert result.policy_decision.decision is PolicyDecisionValue.DENY

        with pytest.raises(PolicyDeniedError):
            kernel.execute(result.intent.id, now=NOW)
        assert provider.effect_count() == 0

    def test_validation_error_is_terminal_and_never_retries(self) -> None:
        kernel, provider, _ = build_kernel()
        provider.set_failure_mode(FakeFailureMode.VALIDATION_ERROR)
        result = kernel.propose(make_proposal(), now=NOW)
        approve_intent(kernel, result.intent.id)

        outcome = kernel.execute(result.intent.id, now=NOW)

        assert outcome.status is IntentStatus.FAILED
        assert "PROVIDER_VALIDATION_REJECTED" in outcome.reason_codes
        assert provider.execute_call_count == 1

    def test_timeout_with_success_reconciles_to_confirmed(self) -> None:
        kernel, provider, _ = build_kernel()
        provider.set_failure_mode(FakeFailureMode.TIMEOUT_WITH_SUCCESS)
        result = kernel.propose(make_proposal(), now=NOW)
        approve_intent(kernel, result.intent.id)

        outcome = kernel.execute(result.intent.id, now=NOW)

        assert outcome.status is IntentStatus.CONFIRMED
        assert outcome.reconciled is True
        assert provider.effect_count() == 1

    def test_crash_after_call_before_commit_reconciles_not_reexecutes(self) -> None:
        kernel, provider, _ = build_kernel()
        provider.set_failure_mode(FakeFailureMode.CRASH_AFTER_CALL_BEFORE_COMMIT)
        result = kernel.propose(make_proposal(), now=NOW)
        approve_intent(kernel, result.intent.id)

        # First pass: provider applies the effect, then the worker "crashes".
        first = kernel.execute(result.intent.id, now=NOW)
        assert first.status is IntentStatus.RECONCILIATION_REQUIRED
        assert provider.effect_count() == 1
        calls_after_crash = provider.execute_call_count

        # Second pass: must reconcile, NOT call the provider again.
        provider.set_failure_mode(FakeFailureMode.NONE)
        second = kernel.execute(result.intent.id, now=NOW + timedelta(seconds=5))
        assert second.status is IntentStatus.CONFIRMED
        assert second.reconciled is True
        assert provider.execute_call_count == calls_after_crash  # no new provider call
        assert provider.reconcile_call_count >= 1
        assert provider.effect_count() == 1

    def test_crash_before_call_applies_no_effect_and_retry_succeeds(self) -> None:
        kernel, provider, _ = build_kernel()
        provider.set_failure_mode(FakeFailureMode.CRASH_BEFORE_CALL)
        result = kernel.propose(make_proposal(), now=NOW)
        approve_intent(kernel, result.intent.id)

        # First pass: crash before the provider call; no effect applied.
        first = kernel.execute(result.intent.id, now=NOW)
        assert first.status is IntentStatus.RECONCILIATION_REQUIRED
        assert provider.effect_count() == 0

        # Second pass: provider healthy; reconcile finds nothing, then executes.
        provider.set_failure_mode(FakeFailureMode.NONE)
        second = kernel.execute(result.intent.id, now=NOW + timedelta(seconds=5))
        assert second.status is IntentStatus.CONFIRMED
        assert provider.effect_count() == 1

    def test_transient_error_is_not_terminal_and_retry_succeeds(self) -> None:
        kernel, provider, _ = build_kernel()
        provider.set_failure_mode(FakeFailureMode.TRANSIENT_ERROR)
        result = kernel.propose(make_proposal(), now=NOW)
        approve_intent(kernel, result.intent.id)

        # First pass: transient 5xx-class failure; not terminal.
        first = kernel.execute(result.intent.id, now=NOW)
        assert first.status is IntentStatus.RECONCILIATION_REQUIRED
        assert "PROVIDER_TRANSIENT_FAILURE" in first.reason_codes
        assert provider.effect_count() == 0

        # Second pass: provider healthy; reconcile finds nothing, then executes.
        provider.set_failure_mode(FakeFailureMode.NONE)
        second = kernel.execute(result.intent.id, now=NOW + timedelta(seconds=5))
        assert second.status is IntentStatus.CONFIRMED
        assert provider.effect_count() == 1

    def test_duplicate_delivery_is_idempotent_on_reconciliation_key(self) -> None:
        kernel, provider, _ = build_kernel()
        result = kernel.propose(make_proposal(), now=NOW)
        approve_intent(kernel, result.intent.id)

        first = kernel.execute(result.intent.id, now=NOW)
        second = kernel.execute(result.intent.id, now=NOW + timedelta(seconds=1))

        assert first.receipt is not None
        assert second.reconciled is True
        assert second.receipt == first.receipt
        assert provider.effect_count() == 1

    def test_max_attempts_exhausted_stops_retrying(self) -> None:
        kernel, provider, _ = build_kernel(max_attempts=2)
        provider.set_failure_mode(FakeFailureMode.TRANSIENT_ERROR)
        result = kernel.propose(make_proposal(), now=NOW)
        approve_intent(kernel, result.intent.id)

        # Exhaust both allowed attempts with transient failures.
        first = kernel.execute(result.intent.id, now=NOW)
        assert first.status is IntentStatus.RECONCILIATION_REQUIRED
        second = kernel.execute(result.intent.id, now=NOW + timedelta(seconds=1))
        assert second.status is IntentStatus.RECONCILIATION_REQUIRED

        # Third pass: max attempts reached; kernel refuses to call provider again.
        third = kernel.execute(result.intent.id, now=NOW + timedelta(seconds=2))
        assert third.status is IntentStatus.RECONCILIATION_REQUIRED
        assert "MAX_ATTEMPTS_EXHAUSTED" in third.reason_codes
        assert provider.execute_call_count == 2  # never exceeded max_attempts

    def test_approval_expired_before_execution_refuses(self) -> None:
        kernel, provider, _ = build_kernel()
        result = kernel.propose(make_proposal(), now=NOW)
        approval = kernel.request_approval(result.intent.id, requested_for="user-1", now=NOW)
        kernel.approve(approval.id, now=NOW)

        # Execute after the approval TTL has elapsed.
        with pytest.raises(ApprovalInvalidError, match="expired"):
            kernel.execute(result.intent.id, now=NOW + timedelta(seconds=3600))
        assert provider.effect_count() == 0

    def test_revoke_records_revocation_receipt(self) -> None:
        kernel, provider, _ = build_kernel()
        result = kernel.propose(make_proposal(), now=NOW)
        approve_intent(kernel, result.intent.id)
        kernel.execute(result.intent.id, now=NOW)

        revocation = kernel.revoke(result.intent.id, now=NOW + timedelta(seconds=1))

        assert revocation.found is True
        assert revocation.final_state is ReceiptFinalState.REVOKED
        assert provider.revoke_call_count == 1


# ---------------------------------------------------------------------------
# Concurrency: 100 same-idempotency-key submissions => 1 effect
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_100_concurrent_same_idempotency_key_yield_one_effect(self) -> None:
        kernel, provider, _ = build_kernel()

        def submit(_: int) -> UUID:
            result = kernel.propose(make_proposal(idempotency_key="shared-key"), now=NOW)
            return result.intent.id

        with ThreadPoolExecutor(max_workers=16) as pool:
            intent_ids = set(pool.map(submit, range(100)))

        assert len(intent_ids) == 1
        intent_id: UUID = intent_ids.pop()
        approve_intent(kernel, intent_id)

        def execute(_: int) -> object:
            return kernel.execute(intent_id, now=NOW).status

        with ThreadPoolExecutor(max_workers=16) as pool:
            statuses = list(pool.map(execute, range(100)))

        assert all(status is IntentStatus.CONFIRMED for status in statuses)
        assert provider.effect_count() == 1
        assert provider.execute_call_count == 1


# ---------------------------------------------------------------------------
# Replay (M5A.8)
# ---------------------------------------------------------------------------


class TestReplay:
    def test_executed_action_is_fully_replayable(self) -> None:
        kernel, provider, audit = build_kernel()
        result = kernel.propose(make_proposal(), now=NOW)
        approve_intent(kernel, result.intent.id)
        kernel.execute(result.intent.id, now=NOW)

        replay = kernel.replay(result.intent.id)

        assert replay.intent.id == result.intent.id
        assert len(replay.payload_versions) == 1
        assert replay.payload_versions[0].payload_hash == result.payload_version.payload_hash
        assert len(replay.policy_decisions) >= 1
        assert replay.policy_decisions[0].ruleset_version
        assert len(replay.approvals) == 1
        assert replay.approvals[0].decision is ApprovalDecision.APPROVED
        assert len(replay.attempts) == 1
        assert len(replay.receipts) == 1
        assert replay.receipts[0].provider == provider.provider_name
        event_types = {e.event_type for e in replay.audit_events}
        assert {
            "side_effect_proposed",
            "side_effect_approved",
            "side_effect_executed",
        } <= event_types
        assert len(audit.all_events()) >= 3

    def test_replay_unknown_intent_raises(self) -> None:
        kernel, _, _ = build_kernel()
        from careerops.application.side_effect_kernel import IntentNotFoundError

        with pytest.raises(IntentNotFoundError):
            kernel.replay(uuid4())


def test_audit_chain_required_before_provider_effect() -> None:
    # A kernel whose audit writer was wiped after proposal must refuse to execute.
    kernel, provider, audit = build_kernel()
    result = kernel.propose(make_proposal(), now=NOW)
    approve_intent(kernel, result.intent.id)

    # Simulate loss of the audit chain by replacing the writer with an empty one.
    kernel._audit = InMemoryAuditWriter()
    del audit

    with pytest.raises(SideEffectError, match="audit chain incomplete"):
        kernel.execute(result.intent.id, now=NOW)
    assert provider.effect_count() == 0
