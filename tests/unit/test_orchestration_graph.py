"""End-to-end graph flow tests for the CareerOps orchestration layer.

Covers the plan v0.4 §5 acceptance contract for ``test_orchestration_graph.py``:

- invoke → interrupt at ``review_gate`` (``get_state().next == ("review_gate",)``).
- ``Command(resume={"action":"approve",...})`` routes to ``send``, the kernel
  records an APPROVED decision, and ``kernel.replay(intent_id).approvals[0]``
  reflects the decision. Exactly one approval exists (resume did not duplicate).
- reject route reaches END without invoking ``send``.
- v1 ``disabled`` model adapter yields an empty/error match result that is
  never a positive match (the human reviewer at ``review_gate`` is the fallback).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from careerops.application.side_effect_kernel import SideEffectKernel
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
from careerops.orchestration.kernel_adapter import CapabilityResolver

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


def _sample_job() -> RawJobDTO:
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


def _sample_contact() -> ContactDTO:
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


class _DemoCapabilityResolver:
    """Test-only capability resolver that satisfies the policy triple.

    Production v1 reads ``capability_released`` from settings and the contact's
    ``publicly_listed`` flag; tests hard-wire True so the policy returns
    REQUIRE_APPROVAL (rather than DENY) and the gate can actually interrupt.
    """

    def for_send_batch(self, drafts: tuple[DraftDTO, ...]) -> Capability:  # type: ignore[override]
        return Capability(
            resource_id=UUID(int=1),
            target={"to": "hiring@example.com"},
            trusted_facts={
                "capability_released": True,
                "target_allowlisted": True,
            },
            evidence_refs=("evidence:resume:v1",),
            authenticated=True,
        )


def _build_graph() -> tuple[
    object,
    SideEffectKernel,
    FakeSideEffectProvider,
    InMemorySideEffectStore,
]:
    store = InMemorySideEffectStore()
    provider = FakeSideEffectProvider()
    kernel = SideEffectKernel(store, provider)

    def crawler() -> tuple[RawJobDTO, ...]:
        return (_sample_job(),)

    def extractor(jobs: tuple[RawJobDTO, ...]) -> tuple[ContactDTO, ...]:
        del jobs
        return (_sample_contact(),)

    graph = build_graph(
        crawler=crawler,
        extractor=extractor,
        resume_text="I know python, fastapi, postgres, react, 5+ years",
        model_client=DisabledModelAdapter(),
        kernel=kernel,
        review_mapping=InMemoryReviewMappingStore(),
        capability_resolver=_DemoCapabilityResolver(),
        checkpointer=MemorySaver(),
    )
    return graph, kernel, provider, store


@pytest.fixture()
def graph_bundle() -> tuple[
    object,
    SideEffectKernel,
    FakeSideEffectProvider,
    InMemorySideEffectStore,
]:
    return _build_graph()


def _drive_to_review(graph: object, cfg: Mapping[str, object]) -> dict[str, str]:
    graph.invoke({"requested_for": "user-1"}, cfg)  # type: ignore[attr-defined]
    state = graph.get_state(cfg)  # type: ignore[attr-defined]
    assert state.next == ("review_gate",), state.next
    interrupt_payload = state.tasks[0].interrupts[0].value
    assert isinstance(interrupt_payload, dict)
    approval_id = interrupt_payload["approval_id"]
    intent_id = interrupt_payload["intent_id"]
    assert isinstance(approval_id, str)
    assert isinstance(intent_id, str)
    return {"approval_id": approval_id, "intent_id": intent_id}


class TestInvokeInterruptsAtReviewGate:
    def test_invoke_pauses_at_review_gate(
        self,
        graph_bundle: tuple[
            object, SideEffectKernel, FakeSideEffectProvider, InMemorySideEffectStore
        ],
    ) -> None:
        graph, _kernel, _provider, _store = graph_bundle
        cfg = {"configurable": {"thread_id": "t1"}}
        ids = _drive_to_review(graph, cfg)
        state = graph.get_state(cfg)  # type: ignore[attr-defined]
        # drafts must be non-empty by the time review_gate interrupts.
        # LangGraph deserializes tuples to lists on read; accept either shape.
        drafts = state.values.get("drafts", ())
        assert isinstance(drafts, (tuple, list))
        assert len(drafts) >= 1
        assert drafts[0]["recipient"] == "hiring@example.com"
        # disabled-model match is advisory only: recommendation must not be "apply"
        matches = state.values.get("matches", ())
        assert isinstance(matches, (tuple, list))
        if matches:
            assert matches[0].get("recommendation") != "apply"
            # This graph fixture predates the capability-specific resolver
            # seam; the matcher must fail closed before reaching the disabled
            # provider and expose only the bounded capability error.
            assert matches[0].get("error") == "model capability unavailable"
        # IDs are JSON-safe strings (not UUID objects)
        UUID(ids["approval_id"])  # parses cleanly
        UUID(ids["intent_id"])


class TestApproveRoutesToSend:
    def test_approve_executes_send_and_marks_approval_approved(
        self,
        graph_bundle: tuple[
            object, SideEffectKernel, FakeSideEffectProvider, InMemorySideEffectStore
        ],
    ) -> None:
        graph, kernel, _provider, _store = graph_bundle
        cfg = {"configurable": {"thread_id": "t2"}}
        ids = _drive_to_review(graph, cfg)

        graph.invoke(  # type: ignore[attr-defined]
            Command(resume={"action": "approve", "approval_id": ids["approval_id"]}),
            cfg,
        )
        final = graph.get_state(cfg)  # type: ignore[attr-defined]
        assert final.next == ()

        receipts = final.values.get("send_receipts", ())
        assert isinstance(receipts, (tuple, list))
        assert len(receipts) == 1
        assert receipts[0]["final_state"] == "confirmed"
        assert receipts[0]["approval_id"] == ids["approval_id"]

        # kernel replay: APPROVED, single approval, single receipt.
        from careerops.domain.side_effects import ApprovalDecision

        replay = kernel.replay(UUID(ids["intent_id"]))
        assert replay.approvals[0].decision is ApprovalDecision.APPROVED
        assert len(replay.approvals) == 1
        assert len(replay.receipts) == 1


class TestRejectRoutesToEnd:
    def test_reject_does_not_send(
        self,
        graph_bundle: tuple[
            object, SideEffectKernel, FakeSideEffectProvider, InMemorySideEffectStore
        ],
    ) -> None:
        graph, kernel, _provider, _store = graph_bundle
        cfg = {"configurable": {"thread_id": "t3"}}
        ids = _drive_to_review(graph, cfg)

        graph.invoke(  # type: ignore[attr-defined]
            Command(resume={"action": "reject", "approval_id": ids["approval_id"]}),
            cfg,
        )
        final = graph.get_state(cfg)  # type: ignore[attr-defined]
        assert final.next == ()
        # No send receipts on reject
        assert final.values.get("send_receipts", ()) == ()

        from careerops.domain.side_effects import ApprovalDecision

        replay = kernel.replay(UUID(ids["intent_id"]))
        assert replay.approvals[0].decision is ApprovalDecision.REJECTED
        assert len(replay.approvals) == 1
        assert len(replay.receipts) == 0


class TestResumeDoesNotDuplicateApproval:
    def test_resume_rerun_only_one_pending_approval(
        self,
        graph_bundle: tuple[
            object, SideEffectKernel, FakeSideEffectProvider, InMemorySideEffectStore
        ],
    ) -> None:
        """PoC-1 confirmed resume re-runs review_gate; this asserts that path
        leaves exactly one PENDING approval before resume (the
        ``get_or_create_pending_approval`` atomic find-or-create contract)."""
        graph, kernel, _provider, _store = graph_bundle
        cfg = {"configurable": {"thread_id": "t4"}}
        ids = _drive_to_review(graph, cfg)

        replay = kernel.replay(UUID(ids["intent_id"]))
        assert len(replay.approvals) == 1, (
            f"expected exactly 1 PENDING approval before resume, got {len(replay.approvals)}"
        )


class TestCapabilityResolverProtocol:
    def test_capability_resolver_protocol_is_structural(self) -> None:
        # The Protocol must accept any object implementing for_send_batch.
        resolver: CapabilityResolver = _DemoCapabilityResolver()
        cap = resolver.for_send_batch(())
        assert cap.authenticated is True
        assert cap.evidence_refs
        assert cap.trusted_facts["capability_released"] is True
        assert cap.trusted_facts["target_allowlisted"] is True
