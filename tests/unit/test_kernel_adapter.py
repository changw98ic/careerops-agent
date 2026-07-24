"""review_gate integration edges not covered by Stage 1 (plan v0.4 §2.5, §5).

Stage 1 (``test_review_idempotency.py`` + ``test_orchestration_graph.py``)
covers the graph-flow happy path, kernel-level idempotency, the edit loop, and
policy fail-closed. This file covers the EDGE cases of the review_gate surface
that Stage 1 did not exercise directly:

- ``parse_review_decision`` validation (every constrained-schema branch).
- ``drafts_payload_hash`` stability + sensitivity (the propose idempotency root).
- ``review_gate`` pre-interrupt guards (empty drafts / missing requested_for).
- ``InMemoryReviewMappingStore`` conflict / claim / complete edges.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from careerops.application.side_effect_kernel import SideEffectKernel
from careerops.infrastructure.database.side_effect_memory import InMemorySideEffectStore
from careerops.integrations.fake_side_effect_provider import FakeSideEffectProvider
from careerops.orchestration.kernel_adapter import (
    Capability,
    ReviewDecisionError,
    drafts_payload_hash,
    parse_review_decision,
    review_gate,
    review_idempotency_key,
)
from careerops.orchestration.mapping_store import (
    InMemoryReviewMappingStore,
    MappingConflictError,
    MappingNotFoundError,
    MappingRecord,
)
from careerops.orchestration.state import DraftDTO

APPROVAL = UUID(int=42)


def _drafts(*, body: str = "v0", recipient: str = "hiring@example.com") -> tuple[DraftDTO, ...]:
    return (
        DraftDTO(
            id="d1",
            job_external_id="job-1",
            recipient=recipient,
            subject="Application",
            body=body,
            revision=0,
        ),
    )


# ---------------------------------------------------------------------------
# parse_review_decision — every constrained-schema branch
# ---------------------------------------------------------------------------


class TestParseReviewDecisionApprovalReject:
    def test_approve_parses_cleanly(self) -> None:
        decision = parse_review_decision(
            {"action": "approve", "approval_id": str(APPROVAL)},
            expected_approval_id=APPROVAL,
        )
        assert decision.action == "approve"
        assert decision.approval_id == APPROVAL
        assert decision.edited_payload is None

    def test_reject_parses_cleanly(self) -> None:
        decision = parse_review_decision(
            {"action": "reject", "approval_id": str(APPROVAL)},
            expected_approval_id=APPROVAL,
        )
        assert decision.action == "reject"

    def test_non_dict_payload_rejected(self) -> None:
        with pytest.raises(ReviewDecisionError, match="JSON object"):
            parse_review_decision("approve", expected_approval_id=APPROVAL)  # type: ignore[arg-type]

    def test_invalid_action_rejected(self) -> None:
        with pytest.raises(ReviewDecisionError, match="action"):
            parse_review_decision(
                {"action": "delete", "approval_id": str(APPROVAL)},
                expected_approval_id=APPROVAL,
            )

    def test_approval_id_not_string_rejected(self) -> None:
        with pytest.raises(ReviewDecisionError, match="approval_id must be a string"):
            parse_review_decision(
                {"action": "approve", "approval_id": 42},
                expected_approval_id=APPROVAL,
            )

    def test_approval_id_not_uuid_rejected(self) -> None:
        with pytest.raises(ReviewDecisionError, match="not a valid UUID"):
            parse_review_decision(
                {"action": "approve", "approval_id": "not-a-uuid"},
                expected_approval_id=APPROVAL,
            )

    def test_approval_id_mismatch_rejected(self) -> None:
        other = UUID(int=99)
        with pytest.raises(ReviewDecisionError, match="does not match"):
            parse_review_decision(
                {"action": "approve", "approval_id": str(other)},
                expected_approval_id=APPROVAL,
            )

    def test_approve_carrying_edited_drafts_rejected(self) -> None:
        with pytest.raises(ReviewDecisionError, match="must not carry"):
            parse_review_decision(
                {
                    "action": "approve",
                    "approval_id": str(APPROVAL),
                    "edited_drafts": [{"id": "d1", "subject": "s", "body": "b"}],
                },
                expected_approval_id=APPROVAL,
            )


class TestParseReviewDecisionEdit:
    def test_edit_with_valid_payload_parses(self) -> None:
        decision = parse_review_decision(
            {
                "action": "edit",
                "approval_id": str(APPROVAL),
                "edited_drafts": [{"id": "d1", "subject": "new", "body": "new body"}],
            },
            expected_approval_id=APPROVAL,
        )
        assert decision.action == "edit"
        assert decision.edited_payload is not None
        drafts = decision.edited_payload.get("drafts")
        assert drafts is not None
        assert len(drafts) >= 1
        assert drafts[0].get("subject") == "new"

    def test_edit_without_edited_drafts_rejected(self) -> None:
        with pytest.raises(ReviewDecisionError, match="edit requires"):
            parse_review_decision(
                {"action": "edit", "approval_id": str(APPROVAL)},
                expected_approval_id=APPROVAL,
            )

    def test_edit_with_empty_edited_drafts_rejected(self) -> None:
        with pytest.raises(ReviewDecisionError, match="edit requires"):
            parse_review_decision(
                {"action": "edit", "approval_id": str(APPROVAL), "edited_drafts": []},
                expected_approval_id=APPROVAL,
            )

    def test_edit_item_not_dict_rejected(self) -> None:
        with pytest.raises(ReviewDecisionError, match="must be an object"):
            parse_review_decision(
                {
                    "action": "edit",
                    "approval_id": str(APPROVAL),
                    "edited_drafts": ["not-a-dict"],
                },
                expected_approval_id=APPROVAL,
            )

    def test_edit_item_forbidden_keys_rejected(self) -> None:
        # Recipient / target changes must re-enter policy via a fresh proposal;
        # the edit schema allows ONLY id / subject / body.
        with pytest.raises(ReviewDecisionError, match="forbidden keys"):
            parse_review_decision(
                {
                    "action": "edit",
                    "approval_id": str(APPROVAL),
                    "edited_drafts": [
                        {"id": "d1", "subject": "s", "body": "b", "recipient": "evil@example.com"}
                    ],
                },
                expected_approval_id=APPROVAL,
            )

    def test_edit_item_id_must_be_string(self) -> None:
        with pytest.raises(ReviewDecisionError, match=r"item\.id must be a string"):
            parse_review_decision(
                {
                    "action": "edit",
                    "approval_id": str(APPROVAL),
                    "edited_drafts": [{"id": 5, "subject": "s", "body": "b"}],
                },
                expected_approval_id=APPROVAL,
            )

    def test_edit_item_subject_must_be_string(self) -> None:
        with pytest.raises(ReviewDecisionError, match=r"item\.subject must be a string"):
            parse_review_decision(
                {
                    "action": "edit",
                    "approval_id": str(APPROVAL),
                    "edited_drafts": [{"id": "d1", "subject": 7, "body": "b"}],
                },
                expected_approval_id=APPROVAL,
            )

    def test_edit_item_body_must_be_string(self) -> None:
        with pytest.raises(ReviewDecisionError, match=r"item\.body must be a string"):
            parse_review_decision(
                {
                    "action": "edit",
                    "approval_id": str(APPROVAL),
                    "edited_drafts": [{"id": "d1", "subject": "s", "body": None}],
                },
                expected_approval_id=APPROVAL,
            )


# ---------------------------------------------------------------------------
# drafts_payload_hash + review_idempotency_key
# ---------------------------------------------------------------------------


class TestDraftsPayloadHash:
    def test_stable_for_same_drafts(self) -> None:
        assert drafts_payload_hash(_drafts()) == drafts_payload_hash(_drafts())

    def test_subject_change_changes_hash(self) -> None:
        d1 = _drafts()
        d2 = (
            DraftDTO(
                id="d1",
                job_external_id="job-1",
                recipient="hiring@example.com",
                subject="Different",
                body="v0",
                revision=0,
            ),
        )
        assert drafts_payload_hash(d1) != drafts_payload_hash(d2)

    def test_body_change_changes_hash(self) -> None:
        assert drafts_payload_hash(_drafts(body="v0")) != drafts_payload_hash(_drafts(body="v1"))

    def test_recipient_change_changes_hash(self) -> None:
        assert drafts_payload_hash(_drafts()) != drafts_payload_hash(
            _drafts(recipient="other@example.com")
        )

    def test_order_matters(self) -> None:
        a = (
            DraftDTO(id="d1", recipient="a@example.com", subject="s", body="b", revision=0),
            DraftDTO(id="d2", recipient="b@example.com", subject="s", body="b", revision=0),
        )
        b = (a[1], a[0])
        assert drafts_payload_hash(a) != drafts_payload_hash(b)


class TestReviewIdempotencyKey:
    def test_revision_change_changes_key(self) -> None:
        drafts = _drafts()
        k0 = review_idempotency_key(requested_for="u1", review_revision=0, drafts=drafts)
        k1 = review_idempotency_key(requested_for="u1", review_revision=1, drafts=drafts)
        assert k0 != k1
        assert "r0" in k0 and "r1" in k1

    def test_user_change_changes_key(self) -> None:
        drafts = _drafts()
        ka = review_idempotency_key(requested_for="u1", review_revision=0, drafts=drafts)
        kb = review_idempotency_key(requested_for="u2", review_revision=0, drafts=drafts)
        assert ka != kb


# ---------------------------------------------------------------------------
# review_gate pre-interrupt guards
# ---------------------------------------------------------------------------


def _kernel() -> SideEffectKernel:
    return SideEffectKernel(InMemorySideEffectStore(), FakeSideEffectProvider())


class _StubResolver:
    def for_send_batch(self, drafts: tuple[DraftDTO, ...]) -> Capability:
        del drafts
        return Capability(
            resource_id=UUID(int=1),
            target={"to": "hiring@example.com"},
            trusted_facts={"capability_released": True, "target_allowlisted": True},
            evidence_refs=("evidence:1",),
            authenticated=True,
        )


class TestReviewGatePreInterruptGuards:
    def test_missing_requested_for_raises(self) -> None:
        state = {"drafts": _drafts()}  # no requested_for
        with pytest.raises(ReviewDecisionError, match="requested_for"):
            review_gate(
                state,  # type: ignore[arg-type]
                kernel=_kernel(),
                review_mapping=InMemoryReviewMappingStore(),
                capability_resolver=_StubResolver(),
            )

    def test_empty_drafts_raises(self) -> None:
        state = {"requested_for": "user-1", "drafts": ()}
        with pytest.raises(ReviewDecisionError, match="empty drafts"):
            review_gate(
                state,  # type: ignore[arg-type]
                kernel=_kernel(),
                review_mapping=InMemoryReviewMappingStore(),
                capability_resolver=_StubResolver(),
            )

    def test_missing_drafts_key_raises(self) -> None:
        state = {"requested_for": "user-1"}
        with pytest.raises(ReviewDecisionError, match="empty drafts"):
            review_gate(
                state,  # type: ignore[arg-type]
                kernel=_kernel(),
                review_mapping=InMemoryReviewMappingStore(),
                capability_resolver=_StubResolver(),
            )


# ---------------------------------------------------------------------------
# InMemoryReviewMappingStore edges
# ---------------------------------------------------------------------------


def _record(
    *,
    approval_id: UUID | None = None,
    thread_id: str = "t1",
    intent_id: UUID | None = None,
    requested_for: str = "user-1",
) -> MappingRecord:
    return MappingRecord(
        thread_id=thread_id,
        approval_id=approval_id or uuid4(),
        intent_id=intent_id or uuid4(),
        requested_for=requested_for,
        completed_at=None,
    )


class TestMappingStorePutIfAbsent:
    def test_first_insert_returns_record(self) -> None:
        store = InMemoryReviewMappingStore()
        record = _record()
        assert store.put_if_absent(record) == record

    def test_idempotent_re_insert_returns_existing(self) -> None:
        """review_gate resume re-runs and re-inserts the SAME mapping; the store
        must treat that as a no-op (return the existing record)."""
        store = InMemoryReviewMappingStore()
        record = _record()
        first = store.put_if_absent(record)
        second = store.put_if_absent(record)
        assert first == second == record

    def test_conflict_on_different_record_same_approval(self) -> None:
        store = InMemoryReviewMappingStore()
        approval = uuid4()
        store.put_if_absent(_record(approval_id=approval, thread_id="t1"))
        with pytest.raises(MappingConflictError):
            store.put_if_absent(_record(approval_id=approval, thread_id="t-other"))


class TestMappingStoreLookup:
    def test_get_by_approval_returns_record(self) -> None:
        store = InMemoryReviewMappingStore()
        record = _record()
        store.put_if_absent(record)
        assert store.get_by_approval(record.approval_id) == record

    def test_get_by_approval_missing_raises(self) -> None:
        store = InMemoryReviewMappingStore()
        with pytest.raises(MappingNotFoundError):
            store.get_by_approval(uuid4())


class TestMappingStoreClaimComplete:
    def test_claim_then_complete_then_claim_conflicts(self) -> None:
        """First resume claim wins; a second claim AFTER completion is rejected
        (the graph's own decide_approval is separately idempotent, but the claim
        gives the API a deterministic first-writer-wins response)."""
        store = InMemoryReviewMappingStore()
        record = _record()
        store.put_if_absent(record)

        claimed = store.claim_resume(record.approval_id)
        assert claimed == record

        completed_at = datetime.now(tz=UTC).isoformat()
        completed = store.complete_resume(record.approval_id, completed_at=completed_at)
        assert completed.completed_at == completed_at

        with pytest.raises(MappingConflictError):
            store.claim_resume(record.approval_id)

    def test_claim_missing_raises(self) -> None:
        store = InMemoryReviewMappingStore()
        with pytest.raises(MappingNotFoundError):
            store.claim_resume(uuid4())

    def test_complete_missing_raises(self) -> None:
        store = InMemoryReviewMappingStore()
        with pytest.raises(MappingNotFoundError):
            store.complete_resume(uuid4(), completed_at="now")
