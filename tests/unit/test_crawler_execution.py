from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from careerops.application.crawler_execution import (
    CrawlerExecutionApprovalDraft,
    CrawlerExecutionApprovalOutcome,
    CrawlerExecutionDecisionState,
    CrawlerExecutionDispatchDecision,
    CrawlerExecutionPlanner,
    CrawlerExecutionRejected,
    CrawlerExecutionRequestDraft,
    CrawlerExecutionRequestSummary,
    CrawlerExecutionService,
)

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


def request_draft(**overrides: object) -> CrawlerExecutionRequestDraft:
    values: dict[str, object] = {
        "request_id": uuid4(),
        "owner_user_id": uuid4(),
        "action_intent_id": uuid4(),
        "payload_version_id": uuid4(),
        "payload_hash": "0" * 64,
        "manifest_path": "datasets/manifests/recruitment-crawler-sources.example.json",
        "request_artifact_path": (
            "datasets/private/crawler-execution-reviews/00000000-0000-0000-0000-000000000001.json"
        ),
        "manifest_sha256": "a" * 64,
        "request_sha256": "b" * 64,
        "reviewed_plan_sha256": "c" * 64,
        "source_ids": ["greenhouse-sitemap", "workday-pages"],
        "reason": "crawl reviewed configurable recruitment sources",
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=2),
    }
    values.update(overrides)
    return CrawlerExecutionRequestDraft(**values)  # type: ignore[arg-type]


def approval_draft(
    request_id: UUID,
    **overrides: object,
) -> CrawlerExecutionApprovalDraft:
    values: dict[str, object] = {
        "request_id": request_id,
        "outcome": CrawlerExecutionApprovalOutcome.APPROVE,
        "decided_by_user_id": uuid4(),
        "decision_reason": "reviewed crawler request may be queued",
        "decided_at": NOW + timedelta(minutes=5),
        "approval_artifact_path": (
            "datasets/private/crawler-execution-reviews/00000000-0000-0000-0000-000000000001.approved.json"
        ),
        "approval_artifact_sha256": "d" * 64,
    }
    values.update(overrides)
    return CrawlerExecutionApprovalDraft(**values)  # type: ignore[arg-type]


def test_request_draft_binds_canonical_action_payload_identity() -> None:
    draft = request_draft(source_ids=["workday-pages", "greenhouse-sitemap"])

    assert draft.source_ids == ("greenhouse-sitemap", "workday-pages")
    assert draft.hash_bindings == {
        "manifest": "a" * 64,
        "payload": "0" * 64,
        "request": "b" * 64,
        "reviewed_plan": "c" * 64,
    }
    assert draft.idempotency_key == f"crawler-execution-request:{draft.execution_key}"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"manifest_path": "/tmp/source.json"}, "repository-relative"),
        ({"manifest_path": "datasets/private/source.json"}, "under datasets/manifests"),
        ({"request_artifact_path": "datasets/private/other/review.json"}, "review artifacts"),
        ({"request_sha256": "A" * 64}, "lowercase sha256"),
        ({"source_ids": []}, "must not be empty"),
        ({"source_ids": ["same", "same"]}, "duplicates"),
        ({"expires_at": NOW}, "expire after"),
    ],
)
def test_request_draft_rejects_unbounded_or_mutable_authority(
    override: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        request_draft(**override)


def test_approved_decision_requires_artifact_but_reject_forbids_it() -> None:
    request = request_draft()

    with pytest.raises(ValueError, match="requires an approval artifact"):
        approval_draft(request.request_id, approval_artifact_path=None)

    with pytest.raises(ValueError, match="must not carry an approval artifact"):
        approval_draft(
            request.request_id,
            outcome=CrawlerExecutionApprovalOutcome.REJECT,
        )

    rejection = approval_draft(
        request.request_id,
        outcome=CrawlerExecutionApprovalOutcome.REJECT,
        approval_artifact_path=None,
        approval_artifact_sha256=None,
    )
    assert rejection.approval_artifact_path is None


def test_planner_enqueues_only_authenticated_server_approval() -> None:
    request = request_draft()
    approval = approval_draft(request.request_id)

    decision = CrawlerExecutionPlanner().plan(request, approval, now=NOW + timedelta(minutes=6))

    assert decision.state is CrawlerExecutionDecisionState.ENQUEUE_WORKFLOW_SIGNAL
    assert decision.outbox_event_key == f"crawler-execution:{request.execution_key}"
    assert decision.execution_key == decision.outbox_event_key


def test_local_cli_approval_alone_is_not_execution_authority() -> None:
    request = request_draft()
    approval = approval_draft(
        request.request_id,
        decided_by_user_id=None,
        local_cli_approval_present=True,
    )

    decision = CrawlerExecutionPlanner().plan(request, approval, now=NOW + timedelta(minutes=6))

    assert decision.state is CrawlerExecutionDecisionState.STOPPED
    assert "AUTHENTICATED_APPROVAL_REQUIRED" in decision.reason_codes
    assert "LOCAL_CLI_APPROVAL_NOT_EXECUTION_AUTHORITY" in decision.reason_codes
    assert decision.outbox_event_key is None


def test_unauthenticated_reject_is_not_persistable() -> None:
    request = request_draft()
    approval = approval_draft(
        request.request_id,
        outcome=CrawlerExecutionApprovalOutcome.REJECT,
        decided_by_user_id=None,
        approval_artifact_path=None,
        approval_artifact_sha256=None,
    )

    decision = CrawlerExecutionPlanner().plan(request, approval, now=NOW + timedelta(minutes=6))

    assert decision.state is CrawlerExecutionDecisionState.STOPPED
    assert "AUTHENTICATED_APPROVAL_REQUIRED" in decision.reason_codes


def test_service_loads_request_by_id_before_approve_or_reject() -> None:
    class Store:
        def __init__(self) -> None:
            self.request = request_draft()
            self.approved = False
            self.rejected = False

        def create_request(self, draft: CrawlerExecutionRequestDraft) -> UUID:
            return draft.request_id

        def get_request(self, request_id: UUID) -> CrawlerExecutionRequestDraft | None:
            assert request_id == self.request.request_id
            return self.request

        def list_pending_requests(
            self,
            *,
            owner_user_id: UUID,
            limit: int = 50,
        ) -> tuple[CrawlerExecutionRequestSummary, ...]:
            del owner_user_id, limit
            return ()

        def approve_and_enqueue(
            self,
            request: CrawlerExecutionRequestDraft,
            approval: CrawlerExecutionApprovalDraft,
            decision: CrawlerExecutionDispatchDecision,
        ) -> UUID:
            del request, approval, decision
            self.approved = True
            return UUID("00000000-0000-0000-0000-000000000098")

        def reject(
            self,
            request: CrawlerExecutionRequestDraft,
            approval: CrawlerExecutionApprovalDraft,
            decision: CrawlerExecutionDispatchDecision,
        ) -> UUID:
            del request, approval, decision
            self.rejected = True
            return UUID("00000000-0000-0000-0000-000000000099")

    store = Store()
    service = CrawlerExecutionService(store, store)
    result = service.approve_request(
        store.request.request_id,
        approval_draft(store.request.request_id),
        now=NOW + timedelta(minutes=6),
    )

    assert result == UUID("00000000-0000-0000-0000-000000000098")
    assert store.approved is True


def test_service_refuses_missing_request_without_reconstructing_from_user_input() -> None:
    class Store:
        def create_request(self, draft: CrawlerExecutionRequestDraft) -> UUID:
            return draft.request_id

        def get_request(self, request_id: UUID) -> None:
            del request_id
            return None

        def list_pending_requests(
            self,
            *,
            owner_user_id: UUID,
            limit: int = 50,
        ) -> tuple[CrawlerExecutionRequestSummary, ...]:
            del owner_user_id, limit
            return ()

        def approve_and_enqueue(
            self,
            request: CrawlerExecutionRequestDraft,
            approval: CrawlerExecutionApprovalDraft,
            decision: CrawlerExecutionDispatchDecision,
        ) -> UUID:
            del request, approval, decision
            raise AssertionError("missing request must not enqueue")

        def reject(
            self,
            request: CrawlerExecutionRequestDraft,
            approval: CrawlerExecutionApprovalDraft,
            decision: object,
        ) -> UUID:
            del request, approval, decision
            raise AssertionError("missing request must not reject")

    service = CrawlerExecutionService(Store(), Store())  # type: ignore[arg-type]
    request_id = uuid4()

    with pytest.raises(CrawlerExecutionRejected) as error:
        service.approve_request(request_id, approval_draft(request_id), now=NOW)

    assert error.value.reason_codes == ("CRAWLER_REQUEST_NOT_FOUND",)
