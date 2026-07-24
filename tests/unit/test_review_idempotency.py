"""Review-gate idempotency + kernel policy tests (plan v0.4 §5).

Verifies the four safety properties called out in the plan:

1. Resume re-runs ``review_gate`` but does NOT create a duplicate PENDING
   approval (the ``get_or_create_pending_approval`` atomic contract).
2. Concurrent resume of the same intent yields a single PENDING approval.
3. Repeated ``decide_approval`` calls are idempotent (first call wins; later
   calls return the same final state without mutating the record or
   re-firing audit).
4. ``edit`` action rejects the current approval and forces the next
   ``review_gate`` pass to propose a new intent (different idempotency key
   due to revision + drafts hash change).
5. Policy fail-closed: any of ``capability_released`` / ``target_allowlisted`` /
   non-empty ``evidence_refs`` missing -> policy DENY, kernel refuses to even
   create an approval (existing kernel behavior, unchanged).
"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from careerops.application.side_effect_kernel import (
    ApprovalInvalidError,
    PolicyDeniedError,
    ProposalInput,
    SideEffectKernel,
)
from careerops.domain.side_effects import (
    ActionIntent,
    ApprovalDecision,
    IntentStatus,
    PolicyDecisionRecord,
)
from careerops.infrastructure.database.side_effect_memory import InMemorySideEffectStore
from careerops.integrations.fake_side_effect_provider import FakeSideEffectProvider
from careerops.model_gateway.base import DisabledModelAdapter
from careerops.orchestration import (
    Capability,
    ContactDTO,
    DraftDTO,
    InMemoryReviewMappingStore,
    RawJobDTO,
    build_graph,
)
from careerops.orchestration.kernel_adapter import (
    review_idempotency_key,
)

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


def _job() -> RawJobDTO:
    return RawJobDTO(
        external_id="job-1",
        title="Backend Engineer",
        location="SF",
        url="https://example.com/jobs/1",
        description="Python and FastAPI role",
        source_url="https://example.com/jobs/1",
        fetched_at=NOW.isoformat(),
        response_hash="abc",
        parser_version="test-v1",
        raw_data={"description": "Python and FastAPI role"},
    )


def _contact() -> ContactDTO:
    return ContactDTO(
        email="hiring@example.com",
        platform="greenhouse",
        company_hint="Example",
        post_type="hiring",
        context="We are hiring",
        source_file="greenhouse.json",
        publicly_listed=True,
        extracted_at=NOW.isoformat(),
    )


def _capability(trusted: dict[str, object] | None = None) -> Capability:
    return Capability(
        resource_id=UUID(int=1),
        target={"to": "hiring@example.com"},
        trusted_facts=trusted or {"capability_released": True, "target_allowlisted": True},
        evidence_refs=("evidence:resume:v1",),
        authenticated=True,
    )


class _ScriptedCapabilityResolver:
    """Returns a fixed capability; used by the happy-path graph tests."""

    def __init__(self, cap: Capability | None = None) -> None:
        self._cap = cap or _capability()

    def for_send_batch(self, drafts: tuple[DraftDTO, ...]) -> Capability:  # type: ignore[override]
        return self._cap


def _build_graph(
    *, capability: Capability | None = None
) -> tuple[
    object,
    SideEffectKernel,
    FakeSideEffectProvider,
    InMemorySideEffectStore,
    InMemoryReviewMappingStore,
]:
    store = InMemorySideEffectStore()
    provider = FakeSideEffectProvider()
    kernel = SideEffectKernel(store, provider, approval_ttl_seconds=3600)
    review_mapping = InMemoryReviewMappingStore()

    def crawler() -> tuple[RawJobDTO, ...]:
        return (_job(),)

    def extractor(jobs: tuple[RawJobDTO, ...]) -> tuple[ContactDTO, ...]:
        del jobs
        return (_contact(),)

    graph = build_graph(
        crawler=crawler,
        extractor=extractor,
        resume_text="I know python, fastapi, postgres, react, 5+ years",
        model_client=DisabledModelAdapter(),
        kernel=kernel,
        review_mapping=review_mapping,
        capability_resolver=_ScriptedCapabilityResolver(capability),
        checkpointer=MemorySaver(),
    )
    return graph, kernel, provider, store, review_mapping


def _drive_to_review(graph: object, cfg: Mapping[str, object]) -> dict[str, str]:
    graph.invoke({"requested_for": "user-1"}, cfg)  # type: ignore[attr-defined]
    state = graph.get_state(cfg)  # type: ignore[attr-defined]
    assert state.next == ("review_gate",), state.next
    iv = state.tasks[0].interrupts[0].value
    assert isinstance(iv, dict)
    return {"approval_id": iv["approval_id"], "intent_id": iv["intent_id"]}


# ---------------------------------------------------------------------------
# 1. resume does not duplicate approval
# ---------------------------------------------------------------------------


class TestResumeDoesNotDuplicateApproval:
    def test_one_pending_approval_before_resume(self) -> None:
        graph, kernel, _p, _s, _m = _build_graph()
        cfg = {"configurable": {"thread_id": "t1"}}
        ids = _drive_to_review(graph, cfg)
        replay = kernel.replay(UUID(ids["intent_id"]))
        assert len(replay.approvals) == 1
        assert replay.approvals[0].decision is ApprovalDecision.PENDING

    def test_resume_rerun_keeps_one_approval(self) -> None:
        """PoC-1 confirmed resume re-runs review_gate from the top. The
        proposal's idempotency key (revision + drafts hash) AND
        ``get_or_create_pending_approval`` together must keep the approval
        count at exactly 1 even though the node runs twice.

        This is verified by counting approvals BEFORE resume (1) and after
        a successful approve-resume (still 1: the existing approval was
        promoted to APPROVED, not duplicated).
        """
        graph, kernel, _p, _s, _m = _build_graph()
        cfg = {"configurable": {"thread_id": "t-rerun"}}
        ids = _drive_to_review(graph, cfg)
        # Before resume: 1 PENDING approval.
        pre = kernel.replay(UUID(ids["intent_id"]))
        assert len(pre.approvals) == 1
        assert pre.approvals[0].decision is ApprovalDecision.PENDING

        graph.invoke(  # type: ignore[attr-defined]
            Command(resume={"action": "approve", "approval_id": ids["approval_id"]}),
            cfg,
        )
        # After resume: still 1 approval, now APPROVED.
        post = kernel.replay(UUID(ids["intent_id"]))
        assert len(post.approvals) == 1
        assert post.approvals[0].decision is ApprovalDecision.APPROVED


# ---------------------------------------------------------------------------
# 2. concurrent get_or_create yields one pending approval
# ---------------------------------------------------------------------------


class TestConcurrentGetOrCreate:
    def test_concurrent_get_or_create_returns_one_pending(self) -> None:
        """Direct kernel call: 10 threads racing get_or_create_pending_approval
        on the same intent must observe exactly one PENDING approval."""
        store = InMemorySideEffectStore()
        kernel = SideEffectKernel(store, FakeSideEffectProvider())
        # Propose first so an intent exists.
        proposal = ProposalInput(
            action_kind="send_email",
            resource_type="email_thread",
            resource_id=UUID(int=1),
            idempotency_key="idem-concurrent",
            created_by="test",
            target={"to": "hiring@example.com"},
            payload={"subject": "s", "body": "b"},
            trusted_facts={
                "capability_released": True,
                "target_allowlisted": True,
            },
            evidence_refs=("evidence:1",),
            authenticated=True,
        )
        result = kernel.propose(proposal, now=NOW)

        def call() -> object:
            return kernel.get_or_create_pending_approval(
                result.intent.id, requested_for="user-1", now=NOW
            )

        with ThreadPoolExecutor(max_workers=10) as ex:
            approvals = list(ex.map(lambda _: call(), range(10)))
        # All threads must return the SAME approval id.
        assert len({a.id for a in approvals}) == 1  # type: ignore[attr-defined]
        replay = kernel.replay(result.intent.id)
        assert len(replay.approvals) == 1


# ---------------------------------------------------------------------------
# 3. decide_approval idempotency
# ---------------------------------------------------------------------------


class TestDecideApprovalIdempotent:
    def test_repeat_approve_is_idempotent(self) -> None:
        store = InMemorySideEffectStore()
        kernel = SideEffectKernel(store, FakeSideEffectProvider())
        result = kernel.propose(
            ProposalInput(
                action_kind="send_email",
                resource_type="email_thread",
                resource_id=UUID(int=1),
                idempotency_key="idem-decide-approve",
                created_by="test",
                target={"to": "hiring@example.com"},
                payload={"subject": "s", "body": "b"},
                trusted_facts={
                    "capability_released": True,
                    "target_allowlisted": True,
                },
                evidence_refs=("evidence:1",),
                authenticated=True,
            ),
            now=NOW,
        )
        approval = kernel.get_or_create_pending_approval(
            result.intent.id, requested_for="user-1", now=NOW
        )
        first = kernel.decide_approval(
            approval.id, action="approve", requested_for="user-1", now=NOW
        )
        second = kernel.decide_approval(
            approval.id, action="approve", requested_for="user-1", now=NOW
        )
        # Second call returns the existing APPROVED record unchanged.
        assert first.decision is ApprovalDecision.APPROVED
        assert second.decision is ApprovalDecision.APPROVED
        assert first.decided_at == second.decided_at

    def test_repeat_reject_is_idempotent(self) -> None:
        store = InMemorySideEffectStore()
        kernel = SideEffectKernel(store, FakeSideEffectProvider())
        result = kernel.propose(
            ProposalInput(
                action_kind="send_email",
                resource_type="email_thread",
                resource_id=UUID(int=1),
                idempotency_key="idem-decide-reject",
                created_by="test",
                target={"to": "hiring@example.com"},
                payload={"subject": "s", "body": "b"},
                trusted_facts={
                    "capability_released": True,
                    "target_allowlisted": True,
                },
                evidence_refs=("evidence:1",),
                authenticated=True,
            ),
            now=NOW,
        )
        approval = kernel.get_or_create_pending_approval(
            result.intent.id, requested_for="user-1", now=NOW
        )
        first = kernel.decide_approval(
            approval.id, action="reject", requested_for="user-1", now=NOW
        )
        second = kernel.decide_approval(
            approval.id, action="reject", requested_for="user-1", now=NOW
        )
        assert first.decision is ApprovalDecision.REJECTED
        assert second.decision is ApprovalDecision.REJECTED
        assert first.decided_at == second.decided_at

    def test_owner_mismatch_rejected(self) -> None:
        store = InMemorySideEffectStore()
        kernel = SideEffectKernel(store, FakeSideEffectProvider())
        result = kernel.propose(
            ProposalInput(
                action_kind="send_email",
                resource_type="email_thread",
                resource_id=UUID(int=1),
                idempotency_key="idem-owner",
                created_by="test",
                target={"to": "hiring@example.com"},
                payload={"subject": "s", "body": "b"},
                trusted_facts={
                    "capability_released": True,
                    "target_allowlisted": True,
                },
                evidence_refs=("evidence:1",),
                authenticated=True,
            ),
            now=NOW,
        )
        approval = kernel.get_or_create_pending_approval(
            result.intent.id, requested_for="user-1", now=NOW
        )
        with pytest.raises(ApprovalInvalidError, match="requested_for"):
            kernel.decide_approval(
                approval.id, action="approve", requested_for="user-evil", now=NOW
            )


# ---------------------------------------------------------------------------
# 4. edit creates a new intent + rejects old approval
# ---------------------------------------------------------------------------


class TestEditCreatesNewIntent:
    def test_edit_rejects_old_and_changes_idempotency_key(self) -> None:
        """Verifying the building block of the edit flow at the helper level:

        the ``review_idempotency_key`` embeds revision + drafts hash, so when
        the user edits, ``review_revision`` bumps and the new review_gate pass
        proposes a fresh intent. The old approval stays REJECTED for replay."""
        drafts_v0 = (
            {
                "id": "d1",
                "job_external_id": "job-1",
                "recipient": "hiring@example.com",
                "subject": "Application: Backend",
                "body": "v0 body",
            },
        )
        drafts_v1 = (
            {
                "id": "d1",
                "job_external_id": "job-1",
                "recipient": "hiring@example.com",
                "subject": "Application: Backend",
                "body": "edited body",  # changed
            },
        )
        key_v0 = review_idempotency_key(
            requested_for="user-1",
            review_revision=0,
            drafts=drafts_v0,  # type: ignore[arg-type]
        )
        key_v1_edited = review_idempotency_key(
            requested_for="user-1",
            review_revision=1,
            drafts=drafts_v1,  # type: ignore[arg-type]
        )
        # Different revisions -> different keys.
        assert key_v0 != key_v1_edited

    def test_full_edit_flow_two_intents(self) -> None:
        """End-to-end edit: review_gate rejects the first approval, loops to
        draft, comes back with revision+1 and proposes a NEW intent."""
        graph, kernel, _p, _s, _m = _build_graph()
        cfg = {"configurable": {"thread_id": "t-edit"}}
        ids_v0 = _drive_to_review(graph, cfg)
        intent_v0 = UUID(ids_v0["intent_id"])

        # Send an edit decision: subject/body for draft d1.
        draft = graph.get_state(cfg).values.get("drafts")[0]  # type: ignore[attr-defined]
        draft_id = draft["id"]
        graph.invoke(  # type: ignore[attr-defined]
            Command(
                resume={
                    "action": "edit",
                    "approval_id": ids_v0["approval_id"],
                    "edited_drafts": [{"id": draft_id, "subject": "Edited", "body": "Edited body"}],
                }
            ),
            cfg,
        )
        # After the edit pass, review_gate interrupts AGAIN on a NEW approval.
        state2 = graph.get_state(cfg)  # type: ignore[attr-defined]
        assert state2.next == ("review_gate",), state2.next
        ids_v1 = state2.tasks[0].interrupts[0].value
        assert isinstance(ids_v1, dict)
        # New intent_id is different.
        assert ids_v1["intent_id"] != ids_v0["intent_id"]

        # The old intent's approval is REJECTED.
        replay_v0 = kernel.replay(intent_v0)
        assert replay_v0.approvals[0].decision is ApprovalDecision.REJECTED


# ---------------------------------------------------------------------------
# 5. policy fail-closed
# ---------------------------------------------------------------------------


class TestPolicyFailClosed:
    """Policy (``policy/side_effect_policy.py``) rules are UNCHANGED by Stage 1.

    ``propose`` records a DENY decision in the policy record and sets the
    intent status to DENIED; ``request_approval`` (and therefore
    ``get_or_create_pending_approval``) then raises ``PolicyDeniedError`` when
    asked to create an approval on a DENIED intent. This test class verifies
    the three fail-closed invariants remain in force after the new APIs ship.
    """

    def _propose(
        self,
        kernel: SideEffectKernel,
        *,
        trusted_facts: dict[str, object],
        evidence_refs: tuple[str, ...],
        idempotency_key: str,
    ) -> tuple[ActionIntent, PolicyDecisionRecord]:
        result = kernel.propose(
            ProposalInput(
                action_kind="send_email",
                resource_type="email_thread",
                resource_id=uuid4(),
                idempotency_key=idempotency_key,
                created_by="test",
                target={"to": "hiring@example.com"},
                payload={"subject": "s", "body": "b"},
                trusted_facts=trusted_facts,
                evidence_refs=evidence_refs,
                authenticated=True,
            ),
            now=NOW,
        )
        return result.intent, result.policy_decision

    def test_missing_capability_released_denies(self) -> None:
        store = InMemorySideEffectStore()
        kernel = SideEffectKernel(store, FakeSideEffectProvider())
        from careerops.domain.side_effects import PolicyDecisionValue

        intent, decision = self._propose(
            kernel,
            trusted_facts={"target_allowlisted": True},  # missing capability
            evidence_refs=("evidence:1",),
            idempotency_key="no-cap",
        )
        assert decision.decision is PolicyDecisionValue.DENY
        assert "CAPABILITY_NOT_RELEASED" in decision.reason_codes
        assert intent.status is IntentStatus.DENIED
        # Approval creation must fail closed.
        with pytest.raises(PolicyDeniedError):
            kernel.get_or_create_pending_approval(
                intent.id,
                requested_for="user-1",
                now=NOW,  # type: ignore[attr-defined]
            )

    def test_missing_target_allowlisted_denies(self) -> None:
        store = InMemorySideEffectStore()
        kernel = SideEffectKernel(store, FakeSideEffectProvider())
        from careerops.domain.side_effects import PolicyDecisionValue

        intent, decision = self._propose(
            kernel,
            trusted_facts={"capability_released": True},  # missing target
            evidence_refs=("evidence:1",),
            idempotency_key="no-target",
        )
        assert decision.decision is PolicyDecisionValue.DENY
        assert "TARGET_NOT_ALLOWLISTED" in decision.reason_codes
        assert intent.status is IntentStatus.DENIED

    def test_empty_evidence_refs_denies(self) -> None:
        store = InMemorySideEffectStore()
        kernel = SideEffectKernel(store, FakeSideEffectProvider())
        from careerops.domain.side_effects import PolicyDecisionValue

        intent, decision = self._propose(
            kernel,
            trusted_facts={
                "capability_released": True,
                "target_allowlisted": True,
            },
            evidence_refs=(),  # missing evidence
            idempotency_key="no-evidence",
        )
        assert decision.decision is PolicyDecisionValue.DENY
        assert "EVIDENCE_REQUIRED" in decision.reason_codes
        assert intent.status is IntentStatus.DENIED

    def test_denied_intent_cannot_get_approval(self) -> None:
        store = InMemorySideEffectStore()
        kernel = SideEffectKernel(store, FakeSideEffectProvider())
        intent, _decision = self._propose(
            kernel,
            trusted_facts={"capability_released": True, "target_allowlisted": True},
            evidence_refs=(),
            idempotency_key="deny-then-approval",
        )
        assert intent.status is IntentStatus.DENIED
        with pytest.raises(PolicyDeniedError):
            kernel.get_or_create_pending_approval(
                intent.id,
                requested_for="user-1",
                now=NOW,  # type: ignore[attr-defined]
            )

    def test_untrusted_claims_ignored_by_policy(self) -> None:
        """ADR 0006 invariant: model self-assertion is never an input."""
        store = InMemorySideEffectStore()
        kernel = SideEffectKernel(store, FakeSideEffectProvider())
        from careerops.domain.side_effects import PolicyDecisionValue

        result = kernel.propose(
            ProposalInput(
                action_kind="send_email",
                resource_type="email_thread",
                resource_id=uuid4(),
                idempotency_key="untrusted-claims",
                created_by="test",
                target={"to": "hiring@example.com"},
                payload={"subject": "s", "body": "b"},
                trusted_facts={
                    "capability_released": True,
                    "target_allowlisted": True,
                },
                evidence_refs=("evidence:1",),
                # Model LIES: "safe=True". Policy MUST ignore this.
                untrusted_claims={"safe": True, "target_bypass": True},
                authenticated=True,
            ),
            now=NOW,
        )
        # The intent is eligible for approval (REQUIRE_APPROVAL), proving the
        # policy relied on trusted_facts, not the lie.
        assert result.policy_decision.decision is PolicyDecisionValue.REQUIRE_APPROVAL


# ---------------------------------------------------------------------------
# expiry / ttl still works through decide_approval
# ---------------------------------------------------------------------------


class TestApprovalExpiryThroughDecideApproval:
    def test_expired_approval_cannot_be_decided(self) -> None:
        store = InMemorySideEffectStore()
        kernel = SideEffectKernel(store, FakeSideEffectProvider(), approval_ttl_seconds=1)
        result = kernel.propose(
            ProposalInput(
                action_kind="send_email",
                resource_type="email_thread",
                resource_id=uuid4(),
                idempotency_key="expire-decide",
                created_by="test",
                target={"to": "hiring@example.com"},
                payload={"subject": "s", "body": "b"},
                trusted_facts={
                    "capability_released": True,
                    "target_allowlisted": True,
                },
                evidence_refs=("evidence:1",),
                authenticated=True,
            ),
            now=NOW,
        )
        approval = kernel.get_or_create_pending_approval(
            result.intent.id, requested_for="user-1", now=NOW
        )
        # Move 10 seconds past expiry.
        with pytest.raises(ApprovalInvalidError, match="expired"):
            kernel.decide_approval(
                approval.id,
                action="approve",
                requested_for="user-1",
                now=NOW + timedelta(seconds=10),
            )
