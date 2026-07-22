from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from careerops.application.crawler_execution import (
    CrawlerExecutionApprovalDraft,
    CrawlerExecutionApprovalOutcome,
    CrawlerExecutionDecisionState,
    CrawlerExecutionDispatchDecision,
    CrawlerExecutionRequestDraft,
)
from careerops.infrastructure.database.crawler_execution import (
    CrawlerExecutionRepositoryError,
    PostgresCrawlerExecutionRepository,
    compile_query_for_test,
    insert_action_intent_statement,
    insert_payload_version_statement,
    insert_request_statement,
    select_approval_by_request_id_statement,
    select_dispatch_by_execution_key_statement,
    select_pending_requests_statement,
    select_request_by_id_statement,
    select_request_by_idempotency_key_statement,
    update_action_intent_status_statement,
)

NOW = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


def request_draft() -> CrawlerExecutionRequestDraft:
    return CrawlerExecutionRequestDraft(
        request_id=uuid4(),
        owner_user_id=uuid4(),
        action_intent_id=uuid4(),
        payload_version_id=uuid4(),
        payload_hash="0" * 64,
        manifest_path="datasets/manifests/recruitment-crawler-sources.example.json",
        request_artifact_path=(
            "datasets/private/crawler-execution-reviews/00000000-0000-0000-0000-000000000001.json"
        ),
        manifest_sha256="a" * 64,
        request_sha256="b" * 64,
        reviewed_plan_sha256="c" * 64,
        source_ids=["greenhouse-sitemap"],
        reason="crawl reviewed configurable recruitment sources",
        created_at=NOW,
        expires_at=NOW + timedelta(hours=2),
    )


def approval_draft(request: CrawlerExecutionRequestDraft) -> CrawlerExecutionApprovalDraft:
    return CrawlerExecutionApprovalDraft(
        request_id=request.request_id,
        outcome=CrawlerExecutionApprovalOutcome.APPROVE,
        decided_by_user_id=uuid4(),
        decision_reason="reviewed crawler request may be queued",
        decided_at=NOW + timedelta(minutes=5),
        approval_artifact_path=(
            "datasets/private/crawler-execution-reviews/00000000-0000-0000-0000-000000000001.approved.json"
        ),
        approval_artifact_sha256="d" * 64,
    )


class TransactionalConnection:
    def in_transaction(self) -> bool:
        return True


class NonTransactionalConnection:
    def in_transaction(self) -> bool:
        return False


def test_repository_requires_explicit_transaction() -> None:
    with pytest.raises(ValueError, match="explicit transaction"):
        PostgresCrawlerExecutionRepository(NonTransactionalConnection())  # type: ignore[arg-type]


def test_create_request_persists_generic_action_payload_before_crawler_request() -> None:
    draft = request_draft()
    intent_sql = compile_query_for_test(
        insert_action_intent_statement(
            intent_id=draft.action_intent_id,
            draft=draft,
            created_by=str(draft.owner_user_id),
        )
    )
    payload_sql = compile_query_for_test(
        insert_payload_version_statement(draft=draft), literal_binds=False
    )
    request_sql = compile_query_for_test(insert_request_statement(draft), literal_binds=False)

    assert "INSERT INTO careerops.action_intents" in intent_sql
    assert "queue_crawler_execution" in intent_sql
    assert draft.idempotency_key in intent_sql
    assert "status" not in intent_sql
    assert "INSERT INTO careerops.action_payload_versions" in payload_sql
    assert "payload_hash" in payload_sql
    assert "INSERT INTO careerops.crawler_execution_requests" in request_sql
    assert "action_intent_id" in request_sql
    assert "payload_version_id" in request_sql
    assert "reviewed_plan_sha256" in request_sql
    assert "created_at" not in request_sql


def test_action_intent_status_is_transitioned_after_privilege_safe_insert() -> None:
    draft = request_draft()
    sql = compile_query_for_test(
        update_action_intent_status_statement(
            intent_id=draft.action_intent_id,
            status="awaiting_approval",
        )
    )

    assert "UPDATE careerops.action_intents" in sql
    assert "status='awaiting_approval'" in sql.replace(" ", "")


def test_request_retrieval_exposes_full_bindings_for_web_transition() -> None:
    draft = request_draft()
    by_id_sql = compile_query_for_test(select_request_by_id_statement(draft.request_id))
    by_key_sql = compile_query_for_test(
        select_request_by_idempotency_key_statement(draft.idempotency_key)
    )

    assert "careerops.action_intents" in by_id_sql
    assert "careerops.action_payload_versions" in by_id_sql
    assert "manifest_sha256" in by_id_sql
    assert "reviewed_plan_sha256" in by_id_sql
    assert "idempotency_key" in by_key_sql
    assert "FOR UPDATE OF action_intents" in by_key_sql


def test_pending_request_list_is_owner_scoped_and_approval_excluding() -> None:
    owner_user_id = uuid4()
    sql = compile_query_for_test(
        select_pending_requests_statement(owner_user_id=owner_user_id, limit=25)
    )

    assert "FROM careerops.crawler_execution_requests" in sql
    assert "LEFT OUTER JOIN careerops.crawler_execution_approvals" in sql
    assert str(owner_user_id) in sql
    assert "crawler_execution_approvals.id IS NULL" in sql
    assert "LIMIT 25" in sql


def test_approval_and_dispatch_lookups_lock_idempotency_rows() -> None:
    request = request_draft()
    approval_sql = compile_query_for_test(
        select_approval_by_request_id_statement(request.request_id)
    )
    dispatch_sql = compile_query_for_test(
        select_dispatch_by_execution_key_statement(request.execution_key)
    )

    assert "FOR UPDATE OF crawler_execution_approvals" in approval_sql
    assert "approval_artifact_sha256" in approval_sql
    assert "FOR UPDATE OF crawler_execution_dispatches" in dispatch_sql
    assert "outbox_event_id" in dispatch_sql
    assert "execution_key" in dispatch_sql


def test_approval_insert_omits_decided_at_for_database_default() -> None:
    from careerops.infrastructure.database.crawler_execution import _approval_values

    request = request_draft()
    approval = approval_draft(request)

    values = _approval_values(request=request, approval=approval)

    assert values["decision"] == "approved"
    assert "decided_at" not in values


def test_repository_rejects_non_enqueueable_decision_before_side_effects() -> None:
    repo = PostgresCrawlerExecutionRepository(TransactionalConnection())  # type: ignore[arg-type]
    request = request_draft()
    approval = approval_draft(request)
    decision = CrawlerExecutionDispatchDecision(
        CrawlerExecutionDecisionState.STOPPED,
        ("AUTHENTICATED_APPROVAL_REQUIRED",),
        request_id=request.request_id,
    )

    with pytest.raises(CrawlerExecutionRepositoryError, match="not enqueueable"):
        repo.approve_and_enqueue(request, approval, decision)


def test_repository_rejects_divergent_dispatch_and_outbox_keys_before_side_effects() -> None:
    repo = PostgresCrawlerExecutionRepository(TransactionalConnection())  # type: ignore[arg-type]
    request = request_draft()
    approval = approval_draft(request)
    decision = CrawlerExecutionDispatchDecision(
        CrawlerExecutionDecisionState.ENQUEUE_WORKFLOW_SIGNAL,
        ("CRAWLER_EXECUTION_APPROVED_FOR_WORKFLOW",),
        request_id=request.request_id,
        execution_key=request.execution_key,
        outbox_event_key="crawler-execution:" + request.execution_key,
    )

    with pytest.raises(CrawlerExecutionRepositoryError, match="must match"):
        repo.approve_and_enqueue(request, approval, decision)


def test_repository_rejects_local_approval_without_authenticated_actor() -> None:
    repo = PostgresCrawlerExecutionRepository(TransactionalConnection())  # type: ignore[arg-type]
    request = request_draft()
    approval = CrawlerExecutionApprovalDraft(
        request_id=request.request_id,
        outcome=CrawlerExecutionApprovalOutcome.APPROVE,
        decided_by_user_id=None,
        decision_reason="local approval evidence only",
        decided_at=NOW + timedelta(minutes=5),
        approval_artifact_path=(
            "datasets/private/crawler-execution-reviews/00000000-0000-0000-0000-000000000001.approved.json"
        ),
        approval_artifact_sha256="d" * 64,
        local_cli_approval_present=True,
    )
    decision = CrawlerExecutionDispatchDecision(
        CrawlerExecutionDecisionState.ENQUEUE_WORKFLOW_SIGNAL,
        ("CRAWLER_EXECUTION_APPROVED_FOR_WORKFLOW",),
        request_id=request.request_id,
        execution_key="crawler-execution:" + request.execution_key,
        outbox_event_key="crawler-execution:" + request.execution_key,
    )

    with pytest.raises(CrawlerExecutionRepositoryError, match="authenticated approval actor"):
        repo.approve_and_enqueue(request, approval, decision)
