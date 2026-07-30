"""End-to-end graph flow tests for the CareerOps orchestration layer.

Covers the real-autonomous-career-loop Phase 3 A/B approval contract for
``test_orchestration_graph.py``:

- The per-message human ``interrupt`` is GONE. ``review_gate`` now runs the
  autonomous A/B approval loop inline.
- With a reviewer-B stub that APPROVES, the graph sends autonomously through
  the side-effect chain, the kernel records an APPROVED decision whose audit
  actor is AGENT (task 3.7), and exactly one approval + one receipt exist.
- With a reviewer-B stub that always REJECTS, the loop exhausts its 5-round
  budget, ESCALATES (no send), and the approval stays PENDING for the
  human-review fallback (default-deny on non-convergence).
- With the model DISABLED (``DisabledModelAdapter``), every draft escalates
  immediately (default-deny when the model is unavailable); no send, approval
  PENDING.
- v1 ``disabled`` model adapter yields an empty/error match result that is
  never a positive match.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import MemorySaver

from careerops.application.approval_loop import AB_ACTOR_ID
from careerops.application.side_effect_kernel import SideEffectKernel
from careerops.application.audit import AuditActorType
from careerops.infrastructure.database.side_effect_memory import InMemorySideEffectStore
from careerops.integrations.fake_side_effect_provider import FakeSideEffectProvider
from careerops.model_gateway.base import (
    DisabledModelAdapter,
    StructuredModelRequest,
    StructuredModelResponse,
)
from careerops.orchestration import (
    Capability,
    ContactDTO,
    DraftDTO,
    InMemoryReviewMappingStore,
    RawJobDTO,
    build_graph,
)
from careerops.orchestration.capability_resolver import (
    CapabilityDecision,
    CapabilityKind,
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
    """Test-only capability resolver that satisfies BOTH protocols.

    - ``for_send_batch`` satisfies the review_gate policy triple (returns
      REQUIRE_APPROVAL rather than DENY so the gate can run).
    - ``decide`` releases MODEL_TAILORING so the A/B loop actually invokes the
      model (production uses ``SettingsCapabilityResolver`` which also has both).
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

    def decide(self, capability: CapabilityKind) -> CapabilityDecision:  # type: ignore[override]
        # Release the model capability so the A/B loop runs in tests.
        return CapabilityDecision(released=True, reason="released for test")


class _FakeModelClient:
    """Controllable StructuredModelClient for the A/B loop.

    - Drafter (A) calls always return a refined draft.
    - Reviewer (B) calls return ``verdict`` (approve/reject) on every round,
      so a reject verdict forces the loop to exhaust its budget and escalate.
    """

    def __init__(self, *, verdict: str = "approve") -> None:
        self._verdict = verdict

    @property
    def is_enabled(self) -> bool:
        return True

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:  # type: ignore[override]
        if request.task_type == "reply_draft":
            result: dict[str, object] = {
                "subject": "Application — Backend Engineer",
                "body": "Hello, I'd like to apply for the Backend Engineer role.",
            }
        else:  # reply_review
            issues: list[str] = [] if self._verdict == "approve" else ["tone too casual"]
            result = {"verdict": self._verdict, "issues": issues}
        return StructuredModelResponse(
            task_type=request.task_type,
            result=result,
            confidence=0.9,
            model_id="fake-ab",
            prompt_version="test-v1",
            is_review_only=True,
            trace_id=request.trace_id,
        )


def _build_graph(
    *, model_client: object = None, verdict: str = "approve"
) -> tuple[
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

    if model_client is None:
        model_client = _FakeModelClient(verdict=verdict)

    graph = build_graph(
        crawler=crawler,
        extractor=extractor,
        resume_text="I know python, fastapi, postgres, react, 5+ years",
        model_client=model_client,  # type: ignore[arg-type]
        kernel=kernel,
        review_mapping=InMemoryReviewMappingStore(),
        capability_resolver=_DemoCapabilityResolver(),
        checkpointer=MemorySaver(),
    )
    return graph, kernel, provider, store


def _run_to_completion(graph: object, cfg: Mapping[str, object]) -> None:
    graph.invoke({"requested_for": "user-1"}, cfg)  # type: ignore[attr-defined]


@pytest.fixture()
def graph_bundle() -> tuple[
    object,
    SideEffectKernel,
    FakeSideEffectProvider,
    InMemorySideEffectStore,
]:
    return _build_graph(verdict="approve")


class TestABApprovalSendsAutonomously:
    def test_approved_draft_is_sent_and_audit_is_agent(
        self,
        graph_bundle: tuple[
            object, SideEffectKernel, FakeSideEffectProvider, InMemorySideEffectStore
        ],
    ) -> None:
        graph, kernel, _provider, store = graph_bundle
        cfg = {"configurable": {"thread_id": "t1"}}
        _run_to_completion(graph, cfg)
        final = graph.get_state(cfg)  # type: ignore[attr-defined]
        # Graph reached END (no pending nodes).
        assert final.next == ()
        # A/B approved -> ab_outcome recorded, send executed.
        assert final.values.get("ab_outcome") == "approved"

        receipts = final.values.get("send_receipts", ())
        assert isinstance(receipts, (tuple, list))
        assert len(receipts) == 1
        assert receipts[0]["final_state"] == "confirmed"

        # kernel replay: APPROVED, single approval, single receipt.
        intent_id = final.values.get("pending_intent_id")
        assert isinstance(intent_id, str)
        from careerops.domain.side_effects import ApprovalDecision

        replay = kernel.replay(UUID(intent_id))
        assert replay.approvals[0].decision is ApprovalDecision.APPROVED
        assert len(replay.approvals) == 1
        assert len(replay.receipts) == 1

        # task 3.7: the approval audit event is AGENT-initiated by the A/B loop.
        events = store._audit_events if hasattr(store, "_audit_events") else ()
        # The kernel holds the audit writer; inspect via the replay/list path.
        audit_events = kernel._audit.list_for_resource(  # type: ignore[attr-defined]
            "action_intent", UUID(intent_id)
        )
        approved_events = [e for e in audit_events if e.event_type == "side_effect_approved"]
        assert len(approved_events) == 1
        assert approved_events[0].actor_type is AuditActorType.AGENT
        assert approved_events[0].actor_id == AB_ACTOR_ID

        # drafts were refined by A (subject changed from the template skeleton).
        drafts = final.values.get("drafts", ())
        assert isinstance(drafts, (tuple, list))
        assert drafts[0]["subject"] == "Application — Backend Engineer"


class TestABRejectionEscalates:
    def test_rejection_exhausts_budget_and_does_not_send(self) -> None:
        graph, kernel, _provider, _store = _build_graph(verdict="reject")
        cfg = {"configurable": {"thread_id": "t2"}}
        _run_to_completion(graph, cfg)
        final = graph.get_state(cfg)  # type: ignore[attr-defined]
        assert final.next == ()
        # No autonomous send on non-convergence (default-deny).
        assert final.values.get("send_receipts", ()) == ()
        assert final.values.get("ab_outcome") == "escalated"

        intent_id = final.values.get("pending_intent_id")
        assert isinstance(intent_id, str)
        from careerops.domain.side_effects import ApprovalDecision

        replay = kernel.replay(UUID(intent_id))
        # Approval stays PENDING so the human-review fallback can still decide it.
        assert replay.approvals[0].decision is ApprovalDecision.PENDING
        assert len(replay.approvals) == 1
        assert len(replay.receipts) == 0


class TestDisabledModelEscalates:
    def test_disabled_provider_escalates_without_send(self) -> None:
        graph, kernel, _provider, _store = _build_graph(
            model_client=DisabledModelAdapter()
        )
        cfg = {"configurable": {"thread_id": "t3"}}
        _run_to_completion(graph, cfg)
        final = graph.get_state(cfg)  # type: ignore[attr-defined]
        assert final.next == ()
        # Default-deny when the model is unavailable: no send, escalated.
        assert final.values.get("send_receipts", ()) == ()
        assert final.values.get("ab_outcome") == "escalated"

        intent_id = final.values.get("pending_intent_id")
        assert isinstance(intent_id, str)
        from careerops.domain.side_effects import ApprovalDecision

        replay = kernel.replay(UUID(intent_id))
        assert replay.approvals[0].decision is ApprovalDecision.PENDING


class TestSingleApprovalPerIntent:
    def test_only_one_approval_exists_after_run(
        self,
        graph_bundle: tuple[
            object, SideEffectKernel, FakeSideEffectProvider, InMemorySideEffectStore
        ],
    ) -> None:
        graph, kernel, _provider, _store = graph_bundle
        cfg = {"configurable": {"thread_id": "t4"}}
        _run_to_completion(graph, cfg)
        final = graph.get_state(cfg)  # type: ignore[attr-defined]
        intent_id = final.values.get("pending_intent_id")
        assert isinstance(intent_id, str)
        replay = kernel.replay(UUID(intent_id))
        assert len(replay.approvals) == 1, (
            f"expected exactly 1 approval, got {len(replay.approvals)}"
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
