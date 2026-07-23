"""M7 DoD (Definition of Done) verification tests.

Verifies the complete DoD checklist from plan section 16:
1. All external side-effects replayable: Intent -> Payload -> Policy -> Approval
   -> Attempt -> Receipt -> Audit.
2. Security counters = 0 in defined fault matrix.
3. All ambiguous effects stop auto-retry and enter human queue.
4. Auto-send default OFF; only valid Release Qualification + explicit user opt-in
   enables it.
5. Release Qualification invalidation auto-disables auto-send.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from careerops.application.gmail_send import (
    AutoSendConfig,
    GmailSendService,
)
from careerops.application.release_qualification import (
    ReleaseQualificationRecord,
    generate_release_qualification,
    is_qualification_valid,
)
from careerops.application.side_effect_kernel import (
    InMemoryAuditWriter,
    ProposalInput,
    SideEffectKernel,
)
from careerops.domain.send import (
    PERMANENT_DENY_CATEGORIES,
    SendCategory,
    SendEligibilityContext,
)
from careerops.domain.side_effects import (
    IntentStatus,
    ReceiptFinalState,
)
from careerops.infrastructure.database.side_effect_memory import InMemorySideEffectStore
from careerops.integrations.fake_side_effect_provider import (
    FakeFailureMode,
    FakeSideEffectProvider,
)

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)


def build_kernel(
    *, max_attempts: int = 5
) -> tuple[SideEffectKernel, FakeSideEffectProvider, InMemoryAuditWriter]:
    store = InMemorySideEffectStore()
    provider = FakeSideEffectProvider()
    audit = InMemoryAuditWriter()
    kernel = SideEffectKernel(store, provider, audit_writer=audit, max_attempts=max_attempts)
    return kernel, provider, audit


class TestDoDReplayableChain:
    """DoD: All external side-effects are replayable."""

    def test_full_chain_replay_intent_payload_policy_approval_attempt_receipt_audit(
        self,
    ) -> None:
        """Every executed action has Intent, Payload, Policy, Approval, Attempt,
        Receipt, and Audit records available for replay."""
        kernel, _provider, _audit = build_kernel()

        proposal = ProposalInput(
            action_kind="send_email",
            resource_type="application",
            resource_id=uuid4(),
            idempotency_key="dod-replay-1",
            created_by="agent-1",
            target={"to": "recruiter@company.com"},
            payload={"subject": "Re: Role", "body": "Thank you"},
            evidence_refs=("evidence:resume:v1",),
            trusted_facts={"capability_released": True, "target_allowlisted": True},
        )
        result = kernel.propose(proposal, now=NOW)
        intent_id = result.intent.id

        # Approve
        approval = kernel.request_approval(intent_id, requested_for="user-1", now=NOW)
        kernel.approve(approval.id, now=NOW)

        # Execute
        outcome = kernel.execute(intent_id, now=NOW)
        assert outcome.status is IntentStatus.CONFIRMED

        # Replay: all chain elements present
        replay = kernel.replay(intent_id)
        assert replay.intent.id == intent_id  # Intent
        assert len(replay.payload_versions) >= 1  # Payload
        assert len(replay.policy_decisions) >= 1  # Policy
        assert len(replay.approvals) >= 1  # Approval
        assert len(replay.attempts) >= 1  # Attempt
        assert len(replay.receipts) >= 1  # Receipt
        assert len(replay.audit_events) >= 2  # Audit

        # Verify chain integrity
        pv = replay.payload_versions[0]
        pd = replay.policy_decisions[0]
        ap = replay.approvals[0]
        assert pd.payload_hash == pv.payload_hash  # Policy bound to payload
        assert ap.payload_version_id == pv.id  # Approval bound to payload version
        assert replay.receipts[0].final_state is ReceiptFinalState.SUCCEEDED

    def test_no_provider_effect_without_policy_and_audit(self) -> None:
        """No Policy Decision + Audit => no provider effect."""
        kernel, provider, _audit = build_kernel()

        # Propose with unknown action (will be denied)
        proposal = ProposalInput(
            action_kind="unknown_action",
            resource_type="application",
            resource_id=uuid4(),
            idempotency_key="dod-no-policy-1",
            created_by="agent-1",
            target={"to": "recruiter@company.com"},
            payload={"subject": "Re: Role", "body": "hello"},
            evidence_refs=("evidence:1",),
            trusted_facts={"capability_released": True, "target_allowlisted": True},
        )
        result = kernel.propose(proposal, now=NOW)
        assert result.intent.status is IntentStatus.DENIED
        assert provider.effect_count() == 0


class TestDoDSecurityCounters:
    """DoD: Security counters = 0 in defined fault matrix."""

    def test_confirmed_duplicate_send_zero_in_fault_matrix(self) -> None:
        """Confirmed duplicate send = 0 across all fault modes."""
        for mode in [
            FakeFailureMode.CRASH_AFTER_CALL_BEFORE_COMMIT,
            FakeFailureMode.TIMEOUT_WITH_SUCCESS,
        ]:
            kernel, provider, _audit = build_kernel()
            provider.set_failure_mode(mode)

            proposal = ProposalInput(
                action_kind="send_email",
                resource_type="application",
                resource_id=uuid4(),
                idempotency_key=f"dod-dup-{mode.value}",
                created_by="agent-1",
                target={"to": "recruiter@company.com"},
                payload={"subject": "Re: Role", "body": "hello"},
                evidence_refs=("evidence:1",),
                trusted_facts={"capability_released": True, "target_allowlisted": True},
            )
            result = kernel.propose(proposal, now=NOW)
            approval = kernel.request_approval(result.intent.id, requested_for="u", now=NOW)
            kernel.approve(approval.id, now=NOW)

            # First execution (may crash/timeout)
            kernel.execute(result.intent.id, now=NOW)

            # Reset failure mode for reconciliation
            provider.set_failure_mode(FakeFailureMode.NONE)

            # Second execution (reconcile)
            kernel.execute(result.intent.id, now=NOW)

            # Exactly 1 provider effect, never a duplicate
            assert provider.effect_count() == 1, f"Duplicate in mode {mode.value}"

    def test_unapproved_high_risk_send_zero(self) -> None:
        """High-risk unapproved send = 0."""
        kernel, provider, _audit = build_kernel()
        service = GmailSendService(
            kernel,
            global_kill_switch_off=True,
            release_qualification_valid=True,
            auto_send_config=AutoSendConfig(
                enabled_categories=frozenset({SendCategory.DELIVERY_CONFIRMATION})
            ),
        )

        # Try to auto-send a PERMANENT_DENY category
        for category in PERMANENT_DENY_CATEGORIES:
            context = SendEligibilityContext(
                category=category,
                has_source_contact=True,
                sender_trust_level="high",
                has_application_linkage=True,
                contact_domain_matches=True,
            )
            can_send, _reasons = service.can_auto_send(context=context, account_opt_in=True)
            assert can_send is False, f"Category {category} should never auto-send"

        assert provider.effect_count() == 0


class TestDoDAmbiguousEffects:
    """DoD: All ambiguous effects stop auto-retry and enter human queue."""

    def test_ambiguous_enters_reconciliation_required(self) -> None:
        """Ambiguous provider results enter reconciliation_required, no auto-retry."""
        kernel, provider, _audit = build_kernel(max_attempts=1)
        provider.set_failure_mode(FakeFailureMode.TRANSIENT_ERROR)

        proposal = ProposalInput(
            action_kind="send_email",
            resource_type="application",
            resource_id=uuid4(),
            idempotency_key="dod-ambiguous-1",
            created_by="agent-1",
            target={"to": "recruiter@company.com"},
            payload={"subject": "Re: Role", "body": "hello"},
            evidence_refs=("evidence:1",),
            trusted_facts={"capability_released": True, "target_allowlisted": True},
        )
        result = kernel.propose(proposal, now=NOW)
        approval = kernel.request_approval(result.intent.id, requested_for="u", now=NOW)
        kernel.approve(approval.id, now=NOW)

        # First attempt fails
        outcome = kernel.execute(result.intent.id, now=NOW)
        assert outcome.status is IntentStatus.RECONCILIATION_REQUIRED

        # Further calls do NOT make new provider calls (no auto-retry)
        calls_before = provider.execute_call_count
        outcome2 = kernel.execute(result.intent.id, now=NOW)
        assert outcome2.status is IntentStatus.RECONCILIATION_REQUIRED
        assert provider.execute_call_count == calls_before


class TestDoDAutoSendDefaultOff:
    """DoD: Auto-send default OFF; only valid RQ + explicit opt-in enables it."""

    def test_auto_send_default_off_all_layers(self) -> None:
        """With default config, auto-send is always denied."""
        kernel, _provider, _audit = build_kernel()
        # Default: kill switch ON (not off), no qualification, no opt-in
        service = GmailSendService(kernel)

        context = SendEligibilityContext(
            category=SendCategory.DELIVERY_CONFIRMATION,
            has_source_contact=True,
            sender_trust_level="high",
            has_application_linkage=True,
            contact_domain_matches=True,
        )
        can_send, reasons = service.can_auto_send(context=context, account_opt_in=False)
        assert can_send is False
        assert "GLOBAL_KILL_SWITCH_ACTIVE" in reasons

    def test_auto_send_requires_all_four_layers(self) -> None:
        """Auto-send requires: kill switch off + valid RQ + opt-in + policy allow."""
        kernel, _provider, _audit = build_kernel()

        # Only 3 of 4 layers satisfied
        service = GmailSendService(
            kernel,
            global_kill_switch_off=True,
            release_qualification_valid=True,
            auto_send_config=AutoSendConfig(
                enabled_categories=frozenset({SendCategory.DELIVERY_CONFIRMATION})
            ),
        )
        context = SendEligibilityContext(
            category=SendCategory.DELIVERY_CONFIRMATION,
            has_source_contact=True,
            sender_trust_level="high",
            has_application_linkage=True,
            contact_domain_matches=True,
        )

        # Missing account opt-in
        can_send, reasons = service.can_auto_send(context=context, account_opt_in=False)
        assert can_send is False
        assert "ACCOUNT_OPT_IN_MISSING" in reasons

        # All 4 layers satisfied
        can_send, reasons = service.can_auto_send(context=context, account_opt_in=True)
        assert can_send is True
        assert reasons == ()

    def test_config_rejects_auto_send_enabled(self) -> None:
        """Settings validator rejects auto_send_enabled=True (requires M7 RQ)."""
        from careerops.config import Settings

        with pytest.raises(ValueError, match="M7"):
            Settings(auto_send_enabled=True)


class TestDoDReleaseQualificationInvalidation:
    """DoD: Release Qualification invalidation auto-disables auto-send."""

    def test_commit_change_invalidates_qualification(self) -> None:
        """Deployment version change invalidates qualification."""
        record = generate_release_qualification(
            commit_sha="abc1234def5678",
            migration_head="0042",
            dataset_versions=("v1",),
            test_suite_results={"unit": True, "security": True},
            security_scan_passed=True,
            coverage_percent=82.0,
            generated_at=NOW,
        )
        valid, reasons = is_qualification_valid(
            record,
            current_commit_sha="different_commit",
            current_migration_head="0042",
            current_dataset_versions=("v1",),
            now=NOW,
        )
        assert valid is False
        assert "COMMIT_SHA_CHANGED" in reasons

    def test_migration_change_invalidates_qualification(self) -> None:
        """Migration head change invalidates qualification."""
        record = generate_release_qualification(
            commit_sha="abc1234def5678",
            migration_head="0042",
            dataset_versions=("v1",),
            test_suite_results={"unit": True},
            security_scan_passed=True,
            coverage_percent=82.0,
            generated_at=NOW,
        )
        valid, reasons = is_qualification_valid(
            record,
            current_commit_sha="abc1234def5678",
            current_migration_head="0043_new_migration",
            current_dataset_versions=("v1",),
            now=NOW,
        )
        assert valid is False
        assert "MIGRATION_HEAD_CHANGED" in reasons

    def test_dataset_change_invalidates_qualification(self) -> None:
        """Dataset version change invalidates qualification."""
        record = generate_release_qualification(
            commit_sha="abc1234def5678",
            migration_head="0042",
            dataset_versions=("discovery:v1", "email:v1"),
            test_suite_results={"unit": True},
            security_scan_passed=True,
            coverage_percent=82.0,
            generated_at=NOW,
        )
        valid, reasons = is_qualification_valid(
            record,
            current_commit_sha="abc1234def5678",
            current_migration_head="0042",
            current_dataset_versions=("discovery:v2", "email:v1"),
            now=NOW,
        )
        assert valid is False
        assert "DATASET_VERSIONS_CHANGED" in reasons

    def test_expired_qualification_disables_auto_send(self) -> None:
        """Expired qualification means auto-send is denied."""
        kernel, _provider, _audit = build_kernel()
        service = GmailSendService(
            kernel,
            global_kill_switch_off=True,
            release_qualification_valid=False,  # Expired/invalid
            auto_send_config=AutoSendConfig(
                enabled_categories=frozenset({SendCategory.DELIVERY_CONFIRMATION})
            ),
        )
        context = SendEligibilityContext(
            category=SendCategory.DELIVERY_CONFIRMATION,
            has_source_contact=True,
            sender_trust_level="high",
            has_application_linkage=True,
            contact_domain_matches=True,
        )
        can_send, reasons = service.can_auto_send(context=context, account_opt_in=True)
        assert can_send is False
        assert "RELEASE_QUALIFICATION_INVALID" in reasons

    def test_qualification_record_is_immutable(self) -> None:
        """Release Qualification record is a frozen dataclass (immutable)."""
        record = generate_release_qualification(
            commit_sha="abc1234def5678",
            migration_head="0042",
            dataset_versions=("v1",),
            test_suite_results={"unit": True},
            security_scan_passed=True,
            coverage_percent=82.0,
            generated_at=NOW,
        )
        with pytest.raises(AttributeError):
            record.commit_sha = "tampered"  # type: ignore[misc]

    def test_qualification_tamper_detection(self) -> None:
        """Record hash mismatch is detected as tampering at construction time."""
        record = generate_release_qualification(
            commit_sha="abc1234def5678",
            migration_head="0042",
            dataset_versions=("v1",),
            test_suite_results={"unit": True},
            security_scan_passed=True,
            coverage_percent=82.0,
            generated_at=NOW,
        )
        # Attempting to create a tampered record with wrong hash raises ValueError
        with pytest.raises(ValueError, match="tampered"):
            ReleaseQualificationRecord(
                commit_sha="tampered_sha",
                migration_head="0042",
                dataset_versions=("v1",),
                test_suite_results={"unit": True},
                security_scan_passed=True,
                coverage_percent=82.0,
                generated_at=NOW,
                expires_at=NOW + timedelta(days=30),
                record_hash=record.record_hash,  # Hash from original, won't match
            )

        # A record without a hash can be checked via is_qualification_valid
        no_hash_record = ReleaseQualificationRecord(
            commit_sha="abc1234def5678",
            migration_head="0042",
            dataset_versions=("v1",),
            test_suite_results={"unit": True},
            security_scan_passed=True,
            coverage_percent=82.0,
            generated_at=NOW,
            expires_at=NOW + timedelta(days=30),
            record_hash="",  # No hash stored
        )
        # Valid when hash is empty (no tamper check possible)
        valid, _reasons = is_qualification_valid(
            no_hash_record,
            current_commit_sha="abc1234def5678",
            current_migration_head="0042",
            current_dataset_versions=("v1",),
            now=NOW,
        )
        assert valid is True
