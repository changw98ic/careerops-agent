"""Unit tests for the autonomous A/B approval loop (real-autonomous-career-loop D2).

Covers the load-bearing properties of ``careerops.application.approval_loop``:

- A (drafter) composes a draft on top of the template skeleton.
- B (reviewer) approves -> outcome approved, A's final draft returned.
- B rejects -> A revises with B's feedback; converging within budget -> approved.
- B never approves within 5 rounds -> escalated (no auto-send).
- Model disabled / capability not released / provider error -> escalated
  (default-deny on uncertainty).
- B's request payload NEVER carries A's self-assessment (independence).
"""

from __future__ import annotations

from typing import Any

import pytest

from careerops.application.approval_loop import (
    AB_ACTOR_ID,
    ABApprovalLoop,
    DraftContext,
    DraftResult,
    MAX_ROUNDS,
)
from careerops.model_gateway.base import (
    DisabledModelAdapter,
    StructuredModelRequest,
    StructuredModelResponse,
)
from careerops.orchestration.capability_resolver import (
    CapabilityDecision,
    CapabilityKind,
)

SKELETON = DraftResult(subject="Template subject", body="Template body")
CONTEXT = DraftContext(
    recipient="hiring@example.com",
    job_title="Backend Engineer",
    company="Example",
    resume_summary="Python, FastAPI, 5 years",
)


class _ReleasedResolver:
    def decide(self, capability: CapabilityKind) -> CapabilityDecision:
        return CapabilityDecision(released=True, reason="released")


class _DeniedResolver:
    def decide(self, capability: CapabilityKind) -> CapabilityDecision:
        return CapabilityDecision(released=False, reason="not released")


class _ScriptedModel:
    """Records every invoke and returns scripted responses keyed by call index.

    ``drafts`` and ``reviews`` are consumed in order across rounds. Each round
    uses one draft (A) then one review (B). All requests are captured for
    independence assertions.
    """

    def __init__(
        self,
        *,
        drafts: list[dict[str, object]] | None = None,
        reviews: list[dict[str, object]] | None = None,
        enabled: bool = True,
        raise_on: set[int] | None = None,
    ) -> None:
        self._drafts = drafts or []
        self._reviews = reviews or []
        self._enabled = enabled
        self._raise_on = raise_on or set()
        self.calls: list[StructuredModelRequest] = []
        self._seq = 0

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def invoke(self, request: StructuredModelRequest) -> StructuredModelResponse:  # type: ignore[override]
        seq = self._seq
        self._seq += 1
        self.calls.append(request)
        if seq in self._raise_on:
            raise RuntimeError("provider error")
        if request.task_type == "reply_draft":
            result = self._drafts.pop(0) if self._drafts else {
                "subject": "Refined",
                "body": "Refined body",
            }
        else:
            result = self._reviews.pop(0) if self._reviews else {
                "verdict": "approve",
                "issues": [],
            }
        return StructuredModelResponse(
            task_type=request.task_type,
            result=result,
            confidence=0.9,
            model_id="scripted",
            prompt_version="test",
            is_review_only=True,
            trace_id=request.trace_id,
        )


# ---------------------------------------------------------------------------
# Capability / provider default-deny
# ---------------------------------------------------------------------------


class TestDefaultDeny:
    def test_capability_not_released_escalates_without_invoke(self) -> None:
        client = _ScriptedModel(reviews=[{"verdict": "approve", "issues": []}])
        loop = ABApprovalLoop(client=client, capability_resolver=_DeniedResolver())
        result = loop.run(SKELETON, CONTEXT)
        assert result.outcome == "escalated"
        assert result.rounds == 0
        assert "not released" in result.reason
        # The provider was NEVER called.
        assert client.calls == []

    def test_disabled_provider_escalates_without_invoke(self) -> None:
        loop = ABApprovalLoop(
            client=DisabledModelAdapter(), capability_resolver=_ReleasedResolver()
        )
        result = loop.run(SKELETON, CONTEXT)
        assert result.outcome == "escalated"
        assert result.rounds == 0
        assert result.reason == "model provider disabled"

    def test_no_resolver_escalates(self) -> None:
        client = _ScriptedModel()
        loop = ABApprovalLoop(client=client, capability_resolver=None)
        result = loop.run(SKELETON, CONTEXT)
        assert result.outcome == "escalated"
        assert client.calls == []


# ---------------------------------------------------------------------------
# Happy path: B approves on round 1
# ---------------------------------------------------------------------------


class TestApprovedOnFirstPass:
    def test_b_approves_round1(self) -> None:
        client = _ScriptedModel(
            drafts=[{"subject": "Hello", "body": "I'd like to apply."}],
            reviews=[{"verdict": "approve", "issues": []}],
        )
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        result = loop.run(SKELETON, CONTEXT)
        assert result.outcome == "approved"
        assert result.rounds == 1
        assert result.subject == "Hello"
        assert result.body == "I'd like to apply."
        assert result.reason == "approved by reviewer B"

    def test_exactly_one_a_and_one_b_call_on_convergence(self) -> None:
        client = _ScriptedModel(
            drafts=[{"subject": "s", "body": "b"}],
            reviews=[{"verdict": "approve", "issues": []}],
        )
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        loop.run(SKELETON, CONTEXT)
        assert len(client.calls) == 2
        assert client.calls[0].task_type == "reply_draft"
        assert client.calls[1].task_type == "reply_review"


# ---------------------------------------------------------------------------
# Revision loop: B rejects then approves
# ---------------------------------------------------------------------------


class TestRevisionLoop:
    def test_b_rejects_then_approves(self) -> None:
        client = _ScriptedModel(
            drafts=[
                {"subject": "v1", "body": "v1 body"},
                {"subject": "v2", "body": "v2 body"},
            ],
            reviews=[
                {"verdict": "reject", "issues": ["too casual"]},
                {"verdict": "approve", "issues": []},
            ],
        )
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        result = loop.run(SKELETON, CONTEXT)
        assert result.outcome == "approved"
        assert result.rounds == 2
        assert result.subject == "v2"
        # A's second-round prompt must carry B's prior feedback (revision channel).
        second_a = client.calls[2]  # round 2 draft call
        assert "too casual" in second_a.user_prompt

    def test_b_feedback_is_passed_to_a_not_shared_context(self) -> None:
        """B sees ONLY the draft; B's issues flow back to A as an explicit
        argument on the next round (not shared memory)."""
        client = _ScriptedModel(
            drafts=[{"subject": "s1", "body": "b1"}, {"subject": "s2", "body": "b2"}],
            reviews=[
                {"verdict": "reject", "issues": ["fix tone"]},
                {"verdict": "approve", "issues": []},
            ],
        )
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        loop.run(SKELETON, CONTEXT)
        # Round-1 B request must NOT contain A's reasoning/confidence/resume.
        first_b = client.calls[1]
        assert first_b.task_type == "reply_review"
        assert "resume" not in first_b.user_prompt.lower()
        assert first_b.untrusted_content == ""


# ---------------------------------------------------------------------------
# Termination: 5 rounds without convergence -> escalate
# ---------------------------------------------------------------------------


class TestTermination:
    def test_five_rejections_escalate(self) -> None:
        reviews = [{"verdict": "reject", "issues": ["no"]} for _ in range(MAX_ROUNDS)]
        drafts = [{"subject": f"s{i}", "body": f"b{i}"} for i in range(MAX_ROUNDS)]
        client = _ScriptedModel(drafts=drafts, reviews=reviews)
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        result = loop.run(SKELETON, CONTEXT)
        assert result.outcome == "escalated"
        assert result.rounds == MAX_ROUNDS
        assert "escalated to human review" in result.reason
        # The draft was refined by A (not the template skeleton).
        assert result.subject == f"s{MAX_ROUNDS - 1}"
        assert result.is_review_only is False

    def test_sixth_round_never_happens(self) -> None:
        reviews = [{"verdict": "reject", "issues": ["no"]} for _ in range(MAX_ROUNDS + 2)]
        drafts = [{"subject": f"s{i}", "body": f"b{i}"} for i in range(MAX_ROUNDS + 2)]
        client = _ScriptedModel(drafts=drafts, reviews=reviews)
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        loop.run(SKELETON, CONTEXT)
        # Exactly MAX_ROUNDS * 2 calls (A+B each round), no sixth round.
        assert len(client.calls) == MAX_ROUNDS * 2

    def test_convergence_at_round_five_does_not_escalate(self) -> None:
        reviews = (
            [{"verdict": "reject", "issues": ["no"]} for _ in range(MAX_ROUNDS - 1)]
            + [{"verdict": "approve", "issues": []}]
        )
        drafts = [{"subject": f"s{i}", "body": f"b{i}"} for i in range(MAX_ROUNDS)]
        client = _ScriptedModel(drafts=drafts, reviews=reviews)
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        result = loop.run(SKELETON, CONTEXT)
        assert result.outcome == "approved"
        assert result.rounds == MAX_ROUNDS


# ---------------------------------------------------------------------------
# Provider error mid-loop -> escalate (default-deny)
# ---------------------------------------------------------------------------


class TestProviderError:
    def test_drafter_error_escalates(self) -> None:
        client = _ScriptedModel(
            drafts=[{"subject": "s", "body": "b"}],
            reviews=[{"verdict": "approve", "issues": []}],
            raise_on={0},  # first A call raises
        )
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        result = loop.run(SKELETON, CONTEXT)
        assert result.outcome == "escalated"
        assert result.rounds == 1
        assert result.reason == "drafter returned no usable draft"

    def test_reviewer_error_escalates(self) -> None:
        client = _ScriptedModel(
            drafts=[{"subject": "s", "body": "b"}],
            reviews=[{"verdict": "approve", "issues": []}],
            raise_on={1},  # first B call raises
        )
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        result = loop.run(SKELETON, CONTEXT)
        assert result.outcome == "escalated"
        assert result.reason == "reviewer returned no usable verdict"

    def test_malformed_review_verdict_escalates(self) -> None:
        client = _ScriptedModel(
            drafts=[{"subject": "s", "body": "b"}],
            reviews=[{"verdict": "maybe", "issues": []}],  # invalid verdict
        )
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        result = loop.run(SKELETON, CONTEXT)
        assert result.outcome == "escalated"
        assert result.reason == "reviewer returned no usable verdict"


# ---------------------------------------------------------------------------
# Independence: B's prompt never carries A's self-assessment
# ---------------------------------------------------------------------------


class TestReviewerIndependence:
    def test_b_prompt_has_no_drafter_metadata(self) -> None:
        client = _ScriptedModel(
            drafts=[{"subject": "s", "body": "b"}],
            reviews=[{"verdict": "approve", "issues": []}],
        )
        loop = ABApprovalLoop(client=client, capability_resolver=_ReleasedResolver())
        loop.run(SKELETON, CONTEXT)
        review_request = client.calls[1]
        assert review_request.task_type == "reply_review"
        # B's system prompt is distinct from A's.
        assert "reviewer" in review_request.system_prompt.lower()
        # B receives no untrusted content (judges the draft text only).
        assert review_request.untrusted_content == ""
        # A and B use different schema names.
        draft_request = client.calls[0]
        assert draft_request.schema_name == "reply_draft"
        assert review_request.schema_name == "reply_review"


# ---------------------------------------------------------------------------
# Constants exported for the kernel wiring
# ---------------------------------------------------------------------------


class TestConstants:
    def test_actor_id_and_max_rounds(self) -> None:
        assert AB_ACTOR_ID == "ab_reviewer"
        assert MAX_ROUNDS == 5
