"""Durable audit and user-attention adapters for crawl permissions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from careerops.agent_console.contracts import ActionKind, ActionState
from careerops.application.audit import AuditActorType, AuditEventDraft
from careerops.application.crawl_permission_service import (
    PermissionAuditEvent,
    PermissionDecision,
)
from careerops.application.notification_service import NotificationEvent
from careerops.domain.crawl_attempts import CrawlSourcePermission
from careerops.infrastructure.database.agent_console_repo import (
    AgentActionRecord,
    AgentActionRepository,
)
from careerops.infrastructure.database.audit import PostgresAuditWriterEngine
from careerops.infrastructure.database.postgres_notification_repo import (
    PostgresNotificationRepository,
)


class PostgresPermissionAuditSink:
    """Append each permission transition to the database hash chain."""

    def __init__(self, writer: PostgresAuditWriterEngine) -> None:
        self._writer = writer

    def record(self, event: PermissionAuditEvent) -> None:
        self._writer.append(
            AuditEventDraft(
                event_type=f"crawl_permission.{event.decision.value}",
                actor_type=(
                    AuditActorType.USER
                    if event.decision
                    in {
                        PermissionDecision.GRANTED,
                        PermissionDecision.DENIED,
                        PermissionDecision.REVOKED,
                    }
                    else AuditActorType.WORKER
                ),
                actor_id=str(event.owner_id),
                resource_type="crawl_permission",
                resource_id=event.permission_id,
                trace_id=f"crawl-permission:{event.permission_id}",
                event_data={
                    "source_id": str(event.source_id),
                    "prior_state": event.prior_state.value,
                    "new_state": event.new_state.value,
                    "reason": event.reason,
                },
                occurred_at=event.occurred_at or datetime.now(UTC),
            )
        )


class PostgresPermissionAttentionSink:
    """Create one persistent action and notification for a pending request."""

    def __init__(
        self,
        actions: AgentActionRepository,
        notifications: PostgresNotificationRepository,
    ) -> None:
        self._actions = actions
        self._notifications = notifications

    def pending(
        self,
        permission: CrawlSourcePermission,
        *,
        source_name: str,
    ) -> None:
        now = permission.created_at or datetime.now(UTC)
        self._actions.upsert(
            AgentActionRecord(
                id=permission.id,
                candidate_id=permission.owner_id,
                action_key=f"crawl-permission:{permission.id}",
                source_event_key=permission.id,
                queue_version=1,
                state=ActionState.PROPOSED,
                kind=ActionKind.CRAWL_PERMISSION,
                reason_code="CRAWL_LOGIN_REQUIRED",
                deterministic_rank=0,
                source_refs=(
                    {"type": "crawl_source", "id": str(permission.source_id)},
                ),
                expires_at=permission.expires_at,
                created_at=now,
                updated_at=now,
            )
        )
        self._notifications.save(
            NotificationEvent(
                id=uuid4(),
                user_id=str(permission.owner_id),
                event_type="crawl_permission",
                payload={
                    "permission_id": str(permission.id),
                    "source_id": str(permission.source_id),
                    "source_name": source_name,
                    "target_route": "/crawl-plans",
                },
                created_at=now,
            )
        )

    def resolved(
        self,
        permission: CrawlSourcePermission,
        *,
        decision: PermissionDecision,
    ) -> None:
        action = self._actions.find_by_key(
            permission.owner_id,
            f"crawl-permission:{permission.id}",
        )
        if action is None or action.state is not ActionState.PROPOSED:
            return
        target = (
            ActionState.COMPLETED
            if decision is PermissionDecision.GRANTED
            else ActionState.DISMISSED
        )
        self._actions.transition_state(
            permission.owner_id,
            action.id,
            from_state=ActionState.PROPOSED,
            to_state=target,
            now=permission.updated_at or datetime.now(UTC),
        )
