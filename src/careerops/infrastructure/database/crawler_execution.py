from __future__ import annotations

import re
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql.base import Executable

from careerops.application.crawler_execution import (
    CrawlerExecutionApprovalDraft,
    CrawlerExecutionApprovalOutcome,
    CrawlerExecutionDecisionState,
    CrawlerExecutionDispatchDecision,
    CrawlerExecutionRequestDraft,
    CrawlerExecutionRequestSummary,
)
from careerops.application.outbox import OutboxEventType, PendingOutboxEvent
from careerops.infrastructure.database.outbox import PostgresOutboxRepository
from careerops.infrastructure.database.schema import (
    action_intents,
    action_payload_versions,
    crawler_execution_approvals,
    crawler_execution_dispatches,
    crawler_execution_requests,
)

_CREATED_BY = re.compile(r"^[A-Za-z0-9._:@/-]{1,160}$")
_EVENT_KEY = re.compile(r"^[A-Za-z0-9._:/-]{1,255}$")


class CrawlerExecutionRepositoryError(RuntimeError):
    pass


class PostgresCrawlerExecutionRepository:
    """Persist canonical crawler execution request/approval/dispatch rows.

    The repository imports the shared schema tables and requires an explicit transaction. It
    creates the generic action intent + payload version first, stores the crawler request bound to
    that identity, and later enqueues one stable workflow_signal outbox event using the same
    action_intent_id/payload_version_id FK pair.
    """

    def __init__(self, connection: Connection) -> None:
        if not connection.in_transaction():
            raise ValueError("crawler execution repository requires an explicit transaction")
        self._connection = connection

    def create_request(self, draft: CrawlerExecutionRequestDraft) -> UUID:
        inserted_id = self._connection.scalar(
            insert_action_intent_statement(
                intent_id=draft.action_intent_id,
                draft=draft,
                created_by=str(draft.owner_user_id),
            )
        )
        if isinstance(inserted_id, UUID):
            self._connection.execute(
                insert_payload_version_statement(
                    draft=draft,
                )
            )
            self._connection.execute(
                bind_current_payload_statement(
                    intent_id=draft.action_intent_id,
                    payload_version_id=draft.payload_version_id,
                )
            )
            self._connection.execute(insert_request_statement(draft))
            self._connection.execute(
                update_action_intent_status_statement(
                    intent_id=draft.action_intent_id,
                    status="awaiting_approval",
                )
            )
            return draft.request_id

        existing = self.get_request_by_idempotency_key(draft.idempotency_key)
        if existing is None:
            raise RuntimeError("crawler request idempotency conflict did not return a row")
        _assert_matching_request(existing, draft)
        return cast("UUID", existing["request_id"])

    def get_request(self, request_id: UUID) -> CrawlerExecutionRequestDraft | None:
        row = self._select_request_by_id(request_id)
        if row is None:
            return None
        return _request_draft_from_row(row)

    def list_pending_requests(
        self,
        *,
        owner_user_id: UUID,
        limit: int = 50,
    ) -> tuple[CrawlerExecutionRequestSummary, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        rows = (
            self._connection.execute(
                select_pending_requests_statement(owner_user_id=owner_user_id, limit=limit)
            )
            .mappings()
            .all()
        )
        return tuple(_request_summary(row) for row in rows)

    def approve_and_enqueue(
        self,
        request: CrawlerExecutionRequestDraft,
        approval: CrawlerExecutionApprovalDraft,
        decision: CrawlerExecutionDispatchDecision,
    ) -> UUID:
        if decision.state is not CrawlerExecutionDecisionState.ENQUEUE_WORKFLOW_SIGNAL:
            raise CrawlerExecutionRepositoryError("crawler execution decision is not enqueueable")
        if decision.execution_key is None or decision.outbox_event_key is None:
            raise CrawlerExecutionRepositoryError("crawler execution decision is missing keys")
        if decision.execution_key != decision.outbox_event_key:
            raise CrawlerExecutionRepositoryError(
                "crawler execution key must match the outbox event key"
            )
        if not _EVENT_KEY.fullmatch(decision.outbox_event_key):
            raise CrawlerExecutionRepositoryError("crawler outbox event key is not bounded")
        if approval.outcome is not CrawlerExecutionApprovalOutcome.APPROVE:
            raise CrawlerExecutionRepositoryError("only approve decisions can be enqueued")
        if approval.decided_by_user_id is None:
            raise CrawlerExecutionRepositoryError("authenticated approval actor is required")

        request_row = self._lock_request(request.request_id)
        _assert_existing_pending_request(request_row, request)
        self._insert_or_reuse_approval(request=request, approval=approval)
        event_id = PostgresOutboxRepository(self._connection).enqueue_once(
            PendingOutboxEvent(
                event_id=uuid4(),
                event_key=decision.outbox_event_key,
                action_intent_id=request.action_intent_id,
                payload_version_id=request.payload_version_id,
                event_type=OutboxEventType.WORKFLOW_SIGNAL,
                available_at=approval.decided_at,
            )
        )
        dispatch_id = self._insert_or_reuse_dispatch(
            request=request,
            event_id=event_id,
            decision=decision,
        )
        self._connection.execute(
            sa.update(action_intents)
            .where(action_intents.c.id == request.action_intent_id)
            .values(status="processing")
        )
        return dispatch_id

    def reject(
        self,
        request: CrawlerExecutionRequestDraft,
        approval: CrawlerExecutionApprovalDraft,
        decision: CrawlerExecutionDispatchDecision,
    ) -> UUID:
        if decision.state is not CrawlerExecutionDecisionState.REJECTED:
            raise CrawlerExecutionRepositoryError("crawler execution decision is not a rejection")
        if approval.outcome is not CrawlerExecutionApprovalOutcome.REJECT:
            raise CrawlerExecutionRepositoryError("approval outcome is not reject")
        if approval.decided_by_user_id is None:
            raise CrawlerExecutionRepositoryError("authenticated rejection actor is required")

        request_row = self._lock_request(request.request_id)
        _assert_existing_pending_request(request_row, request)
        approval_id = self._insert_or_reuse_approval(request=request, approval=approval)
        self._connection.execute(
            sa.update(action_intents)
            .where(action_intents.c.id == request.action_intent_id)
            .values(status="denied")
        )
        return approval_id

    def get_request_by_idempotency_key(self, idempotency_key: str) -> RowMapping | None:
        return (
            self._connection.execute(select_request_by_idempotency_key_statement(idempotency_key))
            .mappings()
            .one_or_none()
        )

    def _select_request_by_id(self, request_id: UUID) -> RowMapping | None:
        return (
            self._connection.execute(select_request_by_id_statement(request_id))
            .mappings()
            .one_or_none()
        )

    def _lock_request(self, request_id: UUID) -> RowMapping | None:
        return (
            self._connection.execute(select_request_by_id_statement(request_id, for_update=True))
            .mappings()
            .one_or_none()
        )

    def _insert_or_reuse_approval(
        self,
        *,
        request: CrawlerExecutionRequestDraft,
        approval: CrawlerExecutionApprovalDraft,
    ) -> UUID:
        inserted_id = self._connection.scalar(
            postgresql.insert(crawler_execution_approvals)
            .values(_approval_values(request=request, approval=approval))
            .on_conflict_do_nothing(index_elements=[crawler_execution_approvals.c.request_id])
            .returning(crawler_execution_approvals.c.id)
        )
        if isinstance(inserted_id, UUID):
            return inserted_id
        existing = (
            self._connection.execute(select_approval_by_request_id_statement(request.request_id))
            .mappings()
            .one_or_none()
        )
        if existing is None:
            raise RuntimeError("crawler approval idempotency conflict did not return a row")
        _assert_matching_approval(existing, request=request, approval=approval)
        return cast("UUID", existing["id"])

    def _insert_or_reuse_dispatch(
        self,
        *,
        request: CrawlerExecutionRequestDraft,
        event_id: UUID,
        decision: CrawlerExecutionDispatchDecision,
    ) -> UUID:
        inserted_id = self._connection.scalar(
            postgresql.insert(crawler_execution_dispatches)
            .values(
                id=uuid4(),
                request_id=request.request_id,
                action_intent_id=request.action_intent_id,
                outbox_event_id=event_id,
                execution_key=decision.execution_key,
            )
            .on_conflict_do_nothing(index_elements=[crawler_execution_dispatches.c.execution_key])
            .returning(crawler_execution_dispatches.c.id)
        )
        if isinstance(inserted_id, UUID):
            return inserted_id
        existing = (
            self._connection.execute(
                select_dispatch_by_execution_key_statement(cast("str", decision.execution_key))
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:
            raise RuntimeError("crawler dispatch idempotency conflict did not return a row")
        _assert_matching_dispatch(existing, request=request, event_id=event_id, decision=decision)
        return cast("UUID", existing["id"])


def insert_action_intent_statement(
    *,
    intent_id: UUID,
    draft: CrawlerExecutionRequestDraft,
    created_by: str,
) -> Executable:
    _validate_created_by(created_by)
    return (
        postgresql.insert(action_intents)
        .values(
            id=intent_id,
            action_kind="queue_crawler_execution",
            resource_type="crawler_execution_request",
            resource_id=draft.request_id,
            idempotency_key=draft.idempotency_key,
            created_by=created_by,
        )
        .on_conflict_do_nothing(index_elements=[action_intents.c.idempotency_key])
        .returning(action_intents.c.id)
    )


def insert_payload_version_statement(*, draft: CrawlerExecutionRequestDraft) -> Executable:
    return sa.insert(action_payload_versions).values(
        id=draft.payload_version_id,
        action_intent_id=draft.action_intent_id,
        version=1,
        target=dict(draft.target),
        payload=dict(draft.payload),
        attachment_refs=[],
        payload_hash=draft.payload_hash,
    )


def bind_current_payload_statement(
    *,
    intent_id: UUID,
    payload_version_id: UUID,
) -> sa.Update:
    return (
        sa.update(action_intents)
        .where(action_intents.c.id == intent_id)
        .values(current_payload_version_id=payload_version_id)
    )


def update_action_intent_status_statement(
    *,
    intent_id: UUID,
    status: str,
) -> sa.Update:
    return sa.update(action_intents).where(action_intents.c.id == intent_id).values(status=status)


def insert_request_statement(draft: CrawlerExecutionRequestDraft) -> Executable:
    return sa.insert(crawler_execution_requests).values(_request_values(draft))


def select_request_by_id_statement(
    request_id: UUID,
    *,
    for_update: bool = False,
) -> sa.Select[tuple[object, ...]]:
    statement = _request_select().where(crawler_execution_requests.c.id == request_id)
    if for_update:
        statement = statement.with_for_update(of=crawler_execution_requests)
    return statement


def select_request_by_idempotency_key_statement(
    idempotency_key: str,
) -> sa.Select[tuple[object, ...]]:
    return (
        _request_select()
        .where(action_intents.c.idempotency_key == idempotency_key)
        .with_for_update(of=action_intents)
    )


def select_pending_requests_statement(
    *,
    owner_user_id: UUID,
    limit: int = 50,
) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(
            crawler_execution_requests.c.id.label("request_id"),
            crawler_execution_requests.c.owner_user_id,
            crawler_execution_requests.c.manifest_path,
            crawler_execution_requests.c.request_artifact_path,
            crawler_execution_requests.c.manifest_sha256,
            crawler_execution_requests.c.request_sha256,
            crawler_execution_requests.c.reviewed_plan_sha256,
            crawler_execution_requests.c.source_ids,
            crawler_execution_requests.c.reason,
            crawler_execution_requests.c.created_at,
            crawler_execution_requests.c.expires_at,
        )
        .select_from(
            crawler_execution_requests.outerjoin(
                crawler_execution_approvals,
                crawler_execution_approvals.c.request_id == crawler_execution_requests.c.id,
            )
        )
        .where(
            crawler_execution_requests.c.owner_user_id == owner_user_id,
            crawler_execution_approvals.c.id.is_(None),
        )
        .order_by(crawler_execution_requests.c.created_at, crawler_execution_requests.c.id)
        .limit(limit)
    )


def select_approval_by_request_id_statement(request_id: UUID) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(
            crawler_execution_approvals.c.id,
            crawler_execution_approvals.c.request_id,
            crawler_execution_approvals.c.decided_by_user_id,
            crawler_execution_approvals.c.decision,
            crawler_execution_approvals.c.decision_reason,
            crawler_execution_approvals.c.approval_artifact_path,
            crawler_execution_approvals.c.approval_artifact_sha256,
            crawler_execution_approvals.c.decided_at,
        )
        .where(crawler_execution_approvals.c.request_id == request_id)
        .with_for_update(of=crawler_execution_approvals)
    )


def select_dispatch_by_execution_key_statement(
    execution_key: str,
) -> sa.Select[tuple[object, ...]]:
    return (
        sa.select(
            crawler_execution_dispatches.c.id,
            crawler_execution_dispatches.c.request_id,
            crawler_execution_dispatches.c.action_intent_id,
            crawler_execution_dispatches.c.outbox_event_id,
            crawler_execution_dispatches.c.execution_key,
        )
        .where(crawler_execution_dispatches.c.execution_key == execution_key)
        .with_for_update(of=crawler_execution_dispatches)
    )


def _request_select() -> sa.Select[tuple[object, ...]]:
    return sa.select(
        crawler_execution_requests.c.id.label("request_id"),
        crawler_execution_requests.c.owner_user_id,
        crawler_execution_requests.c.action_intent_id,
        crawler_execution_requests.c.payload_version_id,
        crawler_execution_requests.c.payload_hash,
        crawler_execution_requests.c.manifest_path,
        crawler_execution_requests.c.request_artifact_path,
        crawler_execution_requests.c.manifest_sha256,
        crawler_execution_requests.c.request_sha256,
        crawler_execution_requests.c.reviewed_plan_sha256,
        crawler_execution_requests.c.source_ids,
        crawler_execution_requests.c.reason,
        crawler_execution_requests.c.created_at,
        crawler_execution_requests.c.expires_at,
        action_intents.c.idempotency_key,
        action_payload_versions.c.target,
        action_payload_versions.c.payload,
        action_payload_versions.c.attachment_refs,
    ).select_from(
        crawler_execution_requests.join(
            action_intents,
            action_intents.c.id == crawler_execution_requests.c.action_intent_id,
        ).join(
            action_payload_versions,
            sa.and_(
                action_payload_versions.c.action_intent_id
                == crawler_execution_requests.c.action_intent_id,
                action_payload_versions.c.id == crawler_execution_requests.c.payload_version_id,
                action_payload_versions.c.payload_hash == crawler_execution_requests.c.payload_hash,
            ),
        )
    )


def _request_values(draft: CrawlerExecutionRequestDraft) -> dict[str, object]:
    return {
        "id": draft.request_id,
        "owner_user_id": draft.owner_user_id,
        "action_intent_id": draft.action_intent_id,
        "payload_version_id": draft.payload_version_id,
        "payload_hash": draft.payload_hash,
        "manifest_path": draft.manifest_path,
        "request_artifact_path": draft.request_artifact_path,
        "manifest_sha256": draft.manifest_sha256,
        "request_sha256": draft.request_sha256,
        "reviewed_plan_sha256": draft.reviewed_plan_sha256,
        "source_ids": list(draft.source_ids),
        "reason": draft.reason,
        "expires_at": draft.expires_at,
    }


def _approval_values(
    *,
    request: CrawlerExecutionRequestDraft,
    approval: CrawlerExecutionApprovalDraft,
) -> dict[str, object]:
    actor_id = approval.decided_by_user_id
    if actor_id is None:
        raise CrawlerExecutionRepositoryError("authenticated approval actor is required")
    return {
        "id": uuid4(),
        "request_id": request.request_id,
        "decided_by_user_id": actor_id,
        "decision": approval.outcome.value,
        "decision_reason": approval.decision_reason,
        "approval_artifact_path": approval.approval_artifact_path,
        "approval_artifact_sha256": approval.approval_artifact_sha256,
    }


def _request_draft_from_row(row: RowMapping) -> CrawlerExecutionRequestDraft:
    return CrawlerExecutionRequestDraft(
        request_id=cast("UUID", row["request_id"]),
        owner_user_id=cast("UUID", row["owner_user_id"]),
        action_intent_id=cast("UUID", row["action_intent_id"]),
        payload_version_id=cast("UUID", row["payload_version_id"]),
        payload_hash=cast("str", row["payload_hash"]),
        manifest_path=cast("str", row["manifest_path"]),
        request_artifact_path=cast("str", row["request_artifact_path"]),
        manifest_sha256=cast("str", row["manifest_sha256"]),
        request_sha256=cast("str", row["request_sha256"]),
        reviewed_plan_sha256=cast("str", row["reviewed_plan_sha256"]),
        source_ids=tuple(cast("list[str]", row["source_ids"])),
        reason=cast("str", row["reason"]),
        created_at=cast("datetime", row["created_at"]),
        expires_at=cast("datetime", row["expires_at"]),
    )


def _request_summary(row: RowMapping) -> CrawlerExecutionRequestSummary:
    return CrawlerExecutionRequestSummary(
        request_id=cast("UUID", row["request_id"]),
        owner_user_id=cast("UUID", row["owner_user_id"]),
        manifest_path=cast("str", row["manifest_path"]),
        request_artifact_path=cast("str", row["request_artifact_path"]),
        manifest_sha256=cast("str", row["manifest_sha256"]),
        request_sha256=cast("str", row["request_sha256"]),
        reviewed_plan_sha256=cast("str", row["reviewed_plan_sha256"]),
        source_ids=tuple(cast("list[str]", row["source_ids"])),
        reason=cast("str", row["reason"]),
        created_at=cast("datetime", row["created_at"]),
        expires_at=cast("datetime", row["expires_at"]),
    )


def _assert_existing_pending_request(
    existing: RowMapping | None,
    draft: CrawlerExecutionRequestDraft,
) -> None:
    if existing is None:
        raise CrawlerExecutionRepositoryError("crawler execution request is missing")
    _assert_matching_request(existing, draft)


def _assert_matching_request(existing: RowMapping, draft: CrawlerExecutionRequestDraft) -> None:
    expected = {
        "request_id": draft.request_id,
        "owner_user_id": draft.owner_user_id,
        "action_intent_id": draft.action_intent_id,
        "payload_version_id": draft.payload_version_id,
        "payload_hash": draft.payload_hash,
        "manifest_path": draft.manifest_path,
        "request_artifact_path": draft.request_artifact_path,
        "manifest_sha256": draft.manifest_sha256,
        "request_sha256": draft.request_sha256,
        "reviewed_plan_sha256": draft.reviewed_plan_sha256,
        "source_ids": list(draft.source_ids),
        "reason": draft.reason,
        "expires_at": draft.expires_at,
        "idempotency_key": draft.idempotency_key,
    }
    if any(existing[key] != value for key, value in expected.items()):
        raise CrawlerExecutionRepositoryError(
            "crawler request key is already bound to a different request identity"
        )


def _assert_matching_approval(
    existing: RowMapping,
    *,
    request: CrawlerExecutionRequestDraft,
    approval: CrawlerExecutionApprovalDraft,
) -> None:
    expected = {
        "request_id": request.request_id,
        "decided_by_user_id": approval.decided_by_user_id,
        "decision": approval.outcome.value,
        "decision_reason": approval.decision_reason,
        "approval_artifact_path": approval.approval_artifact_path,
        "approval_artifact_sha256": approval.approval_artifact_sha256,
    }
    if any(existing[key] != value for key, value in expected.items()):
        raise CrawlerExecutionRepositoryError(
            "crawler approval is already bound to a different approval identity"
        )


def _assert_matching_dispatch(
    existing: RowMapping,
    *,
    request: CrawlerExecutionRequestDraft,
    event_id: UUID,
    decision: CrawlerExecutionDispatchDecision,
) -> None:
    expected = {
        "request_id": request.request_id,
        "action_intent_id": request.action_intent_id,
        "outbox_event_id": event_id,
        "execution_key": decision.execution_key,
    }
    if any(existing[key] != value for key, value in expected.items()):
        raise CrawlerExecutionRepositoryError(
            "crawler dispatch key is already bound to a different dispatch identity"
        )


def _validate_created_by(value: str) -> None:
    if not _CREATED_BY.fullmatch(value):
        raise ValueError("created_by must be a bounded actor identifier")


def compile_query_for_test(
    statement: sa.ClauseElement | Executable,
    *,
    literal_binds: bool = True,
) -> str:
    return str(
        cast("sa.ClauseElement", statement).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": literal_binds},
        )
    )


__all__ = [
    "CrawlerExecutionRepositoryError",
    "PostgresCrawlerExecutionRepository",
    "bind_current_payload_statement",
    "compile_query_for_test",
    "insert_action_intent_statement",
    "insert_payload_version_statement",
    "insert_request_statement",
    "select_approval_by_request_id_statement",
    "select_dispatch_by_execution_key_statement",
    "select_pending_requests_statement",
    "select_request_by_id_statement",
    "select_request_by_idempotency_key_statement",
    "update_action_intent_status_statement",
]
