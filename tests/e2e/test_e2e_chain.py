"""M7 E2E: Full authorization chain replay verification.

Covers M7.3: Complete side-effect chain from Intent through Receipt and Audit.
Verifies that every external side-effect is replayable:
Intent -> Payload -> Policy -> Approval -> Attempt -> Receipt -> Audit.

Also verifies the DoD checklist item:
"All external side-effects are replayable: Intent, Payload, Policy, Approval,
Attempt, Receipt, Audit."
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from careerops.application.gmail_send import (
    AutoSendConfig,
    GmailSendService,
    SendRequest,
)
from careerops.application.release_qualification import (
    generate_release_qualification,
    is_qualification_valid,
)
from careerops.application.scheduling import (
    CreateProposalInput,
    FakeCalendarEventProvider,
    FakeFreeBusyProvider,
    InMemoryCalendarEventStore,
    InMemoryConflictReviewStore,
    InMemoryInterviewRecordStore,
    InMemoryScheduleProposalStore,
    SchedulingService,
)
from careerops.application.side_effect_kernel import (
    InMemoryAuditWriter,
    ProposalInput,
    SideEffectKernel,
)
from careerops.domain.scheduling import SchedulingRules, TimeSlot
from careerops.domain.send import SendCategory
from careerops.domain.side_effects import (
    ApprovalDecision,
    IntentStatus,
    PolicyDecisionValue,
    ReceiptFinalState,
)
from careerops.infrastructure.database.side_effect_memory import InMemorySideEffectStore
from careerops.integrations.fake_side_effect_provider import FakeSideEffectProvider

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
FUTURE = NOW + timedelta(hours=2)


def build_kernel() -> tuple[SideEffectKernel, FakeSideEffectProvider, InMemoryAuditWriter]:
    store = InMemorySideEffectStore()
    provider = FakeSideEffectProvider()
    audit = InMemoryAuditWriter()
    kernel = SideEffectKernel(store, provider, audit_writer=audit)
    return kernel, provider, audit


class TestFullChainReplay:
    """Verify the complete authorization chain is replayable for every effect."""

    def test_email_send_full_chain_replay(self) -> None:
        """Email send: Intent -> Payload -> Policy -> Approval -> Attempt -> Receipt -> Audit."""
        kernel, _provider, _audit = build_kernel()

        # 1. Propose (creates Intent + Payload + Policy)
        proposal = ProposalInput(
            action_kind="send_email",
            resource_type="application",
            resource_id=uuid4(),
            idempotency_key="e2e-email-1",
            created_by="agent-1",
            target={"to": "recruiter@company.com"},
            payload={"subject": "Re: Role", "body": "Thank you"},
            evidence_refs=("evidence:resume:v1",),
            trusted_facts={"capability_released": True, "target_allowlisted": True},
        )
        result = kernel.propose(proposal, now=NOW)
        intent_id = result.intent.id

        # Verify Intent created
        assert result.intent.action_kind == "send_email"
        assert result.intent.idempotency_key == "e2e-email-1"

        # Verify Payload is immutable with hash
        assert len(result.payload_version.payload_hash) == 64
        assert result.payload_version.version == 1

        # Verify Policy decision recorded
        assert result.policy_decision.decision is PolicyDecisionValue.REQUIRE_APPROVAL
        assert result.policy_decision.payload_hash == result.payload_version.payload_hash

        # 2. Request and grant Approval
        approval = kernel.request_approval(intent_id, requested_for="user-1", now=NOW)
        assert approval.decision is ApprovalDecision.PENDING
        kernel.approve(approval.id, now=NOW)

        # 3. Execute (creates Attempt + Receipt)
        outcome = kernel.execute(intent_id, now=NOW)
        assert outcome.status is IntentStatus.CONFIRMED
        assert outcome.receipt is not None
        assert outcome.receipt.final_state is ReceiptFinalState.SUCCEEDED

        # 4. Replay: full chain is available from the Approval page
        replay = kernel.replay(intent_id)
        assert replay.intent.id == intent_id
        assert len(replay.payload_versions) >= 1
        assert len(replay.policy_decisions) >= 1
        assert len(replay.approvals) >= 1
        assert len(replay.attempts) >= 1
        assert len(replay.receipts) >= 1
        assert len(replay.audit_events) >= 2  # proposed + approved + executed

        # Verify audit chain completeness
        event_types = {e.event_type for e in replay.audit_events}
        assert "side_effect_proposed" in event_types
        assert "side_effect_approved" in event_types
        assert "side_effect_executed" in event_types

    def test_calendar_event_full_chain_replay(self) -> None:
        """Calendar event: User Action -> Policy -> Approval -> Intent -> Receipt -> Audit."""
        store = InMemorySideEffectStore()
        provider = FakeSideEffectProvider()
        audit = InMemoryAuditWriter()
        kernel = SideEffectKernel(store, provider, audit_writer=audit)

        freebusy = FakeFreeBusyProvider()
        event_store = InMemoryCalendarEventStore()
        proposal_store = InMemoryScheduleProposalStore()
        interview_store = InMemoryInterviewRecordStore()
        conflict_store = InMemoryConflictReviewStore()
        cal_provider = FakeCalendarEventProvider()

        service = SchedulingService(
            kernel=kernel,
            freebusy_provider=freebusy,
            event_provider=cal_provider,
            event_store=event_store,
            proposal_store=proposal_store,
            interview_store=interview_store,
            conflict_store=conflict_store,
        )

        # Use a time within Shanghai working hours (9:00-17:00 CST = 01:00-09:00 UTC)
        # NOW_SCHED is 00:00 UTC = 08:00 Shanghai; slot at 02:00 UTC = 10:00 Shanghai
        now_sched = datetime(2026, 7, 22, 0, 0, 0, tzinfo=UTC)
        slot_start = now_sched + timedelta(hours=2)  # 02:00 UTC = 10:00 Shanghai
        slot = TimeSlot(
            start_utc=slot_start,
            end_utc=slot_start + timedelta(hours=1),
            source_timezone="Asia/Shanghai",
            duration_minutes=60,
        )
        rules = SchedulingRules(
            buffer_minutes=15,
            minimum_notice_minutes=60,
            max_events_per_day=3,
            max_events_per_week=10,
        )

        # 1. Create proposal
        proposal_input = CreateProposalInput(
            application_id=uuid4(),
            canonical_job_id=uuid4(),
            candidate_slots=(slot,),
            calendar_ids=("careerops-interviews",),
            rules=rules,
            created_by="user-1",
        )
        proposal = service.create_proposal(proposal_input, now=now_sched)

        # 2. Mark recruiter confirmed
        proposal = service.mark_recruiter_confirmed(proposal.id, confirmed_at=now_sched)

        # 3. Select slot
        proposal = service.select_slot(proposal.id, slot, now=now_sched)

        # 4. User clicks "re-verify and create"
        result = service.reverify_and_create_event(proposal.id, user_id="user-1", now=now_sched)

        # Verify the chain: event was created through the kernel
        assert result.calendar_event is not None
        assert result.calendar_event.status.value == "created"

        # Verify replay is available
        replay = kernel.replay(result.intent_id)
        assert replay.intent.action_kind == "create_calendar_event"
        assert len(replay.payload_versions) >= 1
        assert len(replay.policy_decisions) >= 1
        assert len(replay.receipts) >= 1

    def test_m6_send_full_chain_with_four_layer_gate(self) -> None:
        """M6 send with four-layer gate: full chain replay."""
        kernel, _provider, audit = build_kernel()

        # Enable all four layers
        service = GmailSendService(
            kernel,
            audit_writer=audit,
            global_kill_switch_off=True,
            release_qualification_valid=True,
            auto_send_config=AutoSendConfig(
                enabled_categories=frozenset({SendCategory.DELIVERY_CONFIRMATION})
            ),
        )

        request = SendRequest(
            account_email="jobsearch@example.com",
            recipient="recruiter@company.com",
            subject="Re: Software Engineer Role",
            body="Thank you for confirming my application.",
            thread_id="thread-123",
            in_reply_to="msg-456",
            references=("msg-456",),
            category=SendCategory.DELIVERY_CONFIRMATION,
            inbound_message_id="msg-456",
            application_id=uuid4(),
            evidence_refs=("evidence:app:1",),
            trusted_facts={
                "has_source_contact": True,
                "sender_trust_level": "high",
                "contact_domain_matches": True,
            },
        )

        # Submit with auto-send enabled
        outcome = service.submit_send(request, account_opt_in=True, now=NOW)
        assert outcome.auto_send is True
        # M5A policy requires approval for external writes; auto_send flag means
        # the four-layer gate passed, but the kernel still requires explicit approval.
        assert outcome.status is IntentStatus.AWAITING_APPROVAL

        # Approve through the kernel (auto-send means approval is automatic)
        kernel_instance = kernel
        approval = kernel_instance.request_approval(
            outcome.intent_id, requested_for="auto-send-system", now=NOW
        )
        kernel_instance.approve(approval.id, now=NOW)

        # Execute
        exec_outcome = service.execute_send(outcome.intent_id, now=NOW)
        assert exec_outcome.status is IntentStatus.CONFIRMED

        # Verify full chain replay
        replay = kernel.replay(outcome.intent_id)
        assert len(replay.payload_versions) >= 1
        assert len(replay.policy_decisions) >= 1
        assert len(replay.attempts) >= 1
        assert len(replay.receipts) >= 1
        assert len(replay.audit_events) >= 1


class TestReleaseQualificationGate:
    """Verify Release Qualification controls auto-send capability."""

    def test_qualification_generation_and_validation(self) -> None:
        """Valid qualification passes; changed commit invalidates."""
        record = generate_release_qualification(
            commit_sha="abc1234def5678",
            migration_head="0042_add_send_tables",
            dataset_versions=("discovery:v1", "email:v2"),
            test_suite_results={"unit": True, "contract": True, "security": True},
            security_scan_passed=True,
            coverage_percent=82.5,
            generated_at=NOW,
        )

        # Valid with same state
        valid, reasons = is_qualification_valid(
            record,
            current_commit_sha="abc1234def5678",
            current_migration_head="0042_add_send_tables",
            current_dataset_versions=("discovery:v1", "email:v2"),
            now=NOW + timedelta(days=1),
        )
        assert valid is True
        assert reasons == ()

        # Invalidated by commit change
        valid, reasons = is_qualification_valid(
            record,
            current_commit_sha="new_commit_sha",
            current_migration_head="0042_add_send_tables",
            current_dataset_versions=("discovery:v1", "email:v2"),
            now=NOW + timedelta(days=1),
        )
        assert valid is False
        assert "COMMIT_SHA_CHANGED" in reasons

    def test_qualification_expired(self) -> None:
        """Expired qualification is invalid."""
        record = generate_release_qualification(
            commit_sha="abc1234def5678",
            migration_head="0042_add_send_tables",
            dataset_versions=("discovery:v1",),
            test_suite_results={"unit": True},
            security_scan_passed=True,
            coverage_percent=80.0,
            generated_at=NOW,
            validity_days=30,
        )

        valid, reasons = is_qualification_valid(
            record,
            current_commit_sha="abc1234def5678",
            current_migration_head="0042_add_send_tables",
            current_dataset_versions=("discovery:v1",),
            now=NOW + timedelta(days=31),
        )
        assert valid is False
        assert "QUALIFICATION_EXPIRED" in reasons

    def test_qualification_invalidation_disables_auto_send(self) -> None:
        """When qualification is invalid, auto-send is denied."""
        kernel, _provider, _audit = build_kernel()
        service = GmailSendService(
            kernel,
            global_kill_switch_off=True,
            release_qualification_valid=False,  # Invalid qualification
            auto_send_config=AutoSendConfig(
                enabled_categories=frozenset({SendCategory.DELIVERY_CONFIRMATION})
            ),
        )

        from careerops.domain.send import SendEligibilityContext

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

    def test_qualification_requires_all_tests_passing(self) -> None:
        """Cannot generate qualification with failing tests."""
        with pytest.raises(ValueError, match="test suites failed"):
            generate_release_qualification(
                commit_sha="abc1234def5678",
                migration_head="0042",
                dataset_versions=("v1",),
                test_suite_results={"unit": True, "security": False},
                security_scan_passed=True,
                coverage_percent=80.0,
                generated_at=NOW,
            )

    def test_qualification_requires_security_scan(self) -> None:
        """Cannot generate qualification without passing security scan."""
        with pytest.raises(ValueError, match="security scan"):
            generate_release_qualification(
                commit_sha="abc1234def5678",
                migration_head="0042",
                dataset_versions=("v1",),
                test_suite_results={"unit": True},
                security_scan_passed=False,
                coverage_percent=80.0,
                generated_at=NOW,
            )

    def test_qualification_requires_coverage_threshold(self) -> None:
        """Cannot generate qualification below 75% coverage."""
        with pytest.raises(ValueError, match="coverage"):
            generate_release_qualification(
                commit_sha="abc1234def5678",
                migration_head="0042",
                dataset_versions=("v1",),
                test_suite_results={"unit": True},
                security_scan_passed=True,
                coverage_percent=74.9,
                generated_at=NOW,
            )
