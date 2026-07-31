"""Phase 9 notification service + action projection tests.

Tests:
- NotificationService: notify, get_pending, mark_delivered, convenience helpers.
- SSEChannel: publish/subscribe, shutdown.
- SSE endpoint: returns streaming response.
- ActionProjectionBuilder: real build from AgentActionRepository.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from careerops.agent_console.action_projection import (
    ActionProjectionBuilder,
    _record_to_action_item,
)
from careerops.agent_console.contracts import (
    ActionKind,
    ActionSourceRef,
    ActionState,
    SourceRefType,
)
from careerops.application.notification_service import (
    NotificationEvent,
    NotificationService,
    SSEChannel,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sse_channel() -> SSEChannel:
    return SSEChannel()


@pytest.fixture()
def notification_service(sse_channel: SSEChannel) -> NotificationService:
    return NotificationService(sse_channel)


@pytest.fixture()
def user_id() -> str:
    return str(uuid4())


# ---------------------------------------------------------------------------
# NotificationService tests
# ---------------------------------------------------------------------------


class TestNotificationService:
    def test_notify_persists_to_outbox(
        self, notification_service: NotificationService, user_id: str
    ) -> None:
        event = notification_service.notify(user_id, "system", {"msg": "hello"})
        assert event.user_id == user_id
        assert event.event_type == "system"
        assert event.payload == {"msg": "hello"}
        assert not event.delivered

        pending = notification_service.get_pending(user_id)
        assert len(pending) == 1
        assert pending[0].id == event.id

    def test_get_pending_returns_only_undelivered(
        self, notification_service: NotificationService, user_id: str
    ) -> None:
        e1 = notification_service.notify(user_id, "system", {"n": 1})
        e2 = notification_service.notify(user_id, "system", {"n": 2})
        notification_service.mark_delivered(user_id, e1.id)

        pending = notification_service.get_pending(user_id)
        assert len(pending) == 1
        assert pending[0].id == e2.id

    def test_mark_delivered_removes_from_pending(
        self, notification_service: NotificationService, user_id: str
    ) -> None:
        event = notification_service.notify(user_id, "system", {"x": 1})
        assert len(notification_service.get_pending(user_id)) == 1

        notification_service.mark_delivered(user_id, event.id)
        assert len(notification_service.get_pending(user_id)) == 0

    def test_mark_delivered_unknown_id_is_noop(
        self, notification_service: NotificationService, user_id: str
    ) -> None:
        notification_service.notify(user_id, "system", {"x": 1})
        notification_service.mark_delivered(user_id, uuid4())
        assert len(notification_service.get_pending(user_id)) == 1

    def test_get_pending_unknown_user_returns_empty(
        self, notification_service: NotificationService
    ) -> None:
        assert notification_service.get_pending("nonexistent") == []

    def test_notify_crawl_progress(
        self, notification_service: NotificationService, user_id: str
    ) -> None:
        crawl_run_id = uuid4()
        event = notification_service.notify_crawl_progress(
            user_id, crawl_run_id, "fetching", postings_count=5, source_name="greenhouse"
        )
        assert event.event_type == "crawl_progress"
        assert event.payload["crawl_run_id"] == str(crawl_run_id)
        assert event.payload["phase"] == "fetching"
        assert event.payload["postings_count"] == 5
        assert event.payload["source_name"] == "greenhouse"

    def test_notify_agent_progress(
        self, notification_service: NotificationService, user_id: str
    ) -> None:
        agent_run_id = uuid4()
        event = notification_service.notify_agent_progress(
            user_id, agent_run_id, "completed", action_count=3, message="done"
        )
        assert event.event_type == "agent_progress"
        assert event.payload["agent_run_id"] == str(agent_run_id)
        assert event.payload["phase"] == "completed"
        assert event.payload["action_count"] == 3
        assert event.payload["message"] == "done"


# ---------------------------------------------------------------------------
# SSEChannel tests
# ---------------------------------------------------------------------------


class TestSSEChannel:
    @pytest.mark.asyncio
    async def test_publish_subscribe(self, sse_channel: SSEChannel) -> None:
        user = "test-user"
        event = NotificationEvent(
            id=uuid4(),
            user_id=user,
            event_type="system",
            payload={"msg": "hi"},
            created_at=datetime.now(UTC),
        )

        received: list[str] = []

        async def collect():
            async for chunk in sse_channel.subscribe(user):
                received.append(chunk)
                break  # only collect one event

        # Publish before subscribing — the event is lost (no persistent queue).
        # So we subscribe first, then publish in a separate task.
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)  # let subscribe register
        sse_channel.publish(user, event)
        await asyncio.wait_for(task, timeout=2.0)

        assert len(received) == 1
        assert f"event: system" in received[0]
        assert "hi" in received[0]

    @pytest.mark.asyncio
    async def test_shutdown_sends_sentinel(self, sse_channel: SSEChannel) -> None:
        user = "test-user"
        received: list[str | None] = []

        async def collect():
            async for chunk in sse_channel.subscribe(user):
                received.append(chunk)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        sse_channel.shutdown(user)
        await asyncio.wait_for(task, timeout=2.0)

        # The generator should have exited (sentinel received).
        assert task.done()

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, sse_channel: SSEChannel) -> None:
        user = "test-user"
        received_a: list[str] = []
        received_b: list[str] = []

        async def collect_a():
            async for chunk in sse_channel.subscribe(user):
                received_a.append(chunk)
                break

        async def collect_b():
            async for chunk in sse_channel.subscribe(user):
                received_b.append(chunk)
                break

        task_a = asyncio.create_task(collect_a())
        task_b = asyncio.create_task(collect_b())
        await asyncio.sleep(0.05)

        event = NotificationEvent(
            id=uuid4(),
            user_id=user,
            event_type="test",
            payload={},
            created_at=datetime.now(UTC),
        )
        sse_channel.publish(user, event)
        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2.0)

        assert len(received_a) == 1
        assert len(received_b) == 1


# ---------------------------------------------------------------------------
# SSE endpoint integration test
# ---------------------------------------------------------------------------


class TestNotificationRoutes:
    def test_pending_endpoint_is_public(self) -> None:
        """The recovery endpoint is public (the session/CSRF auth gate was
        removed — auth-rm Task 9). candidate_id comes from the path."""
        from types import SimpleNamespace

        from careerops.api.app import create_app

        app = create_app()
        # auth-rm I1: per-candidate routers carry the path_candidate_id
        # existence gate; let the test candidate id through (the gate's 404
        # branch is covered by its own unit + contract tests).
        app.state.candidate_service = SimpleNamespace(
            get=lambda candidate_id: SimpleNamespace(id=candidate_id)
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(f"/api/v1/candidates/{uuid4()}/notifications")
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "count": 0}

    def test_stream_endpoint_is_public(self) -> None:
        """The SSE stream endpoint is public (no session gate anymore).

        The stream path is verified through the OpenAPI spec rather than an
        actual streamed request: the endpoint never terminates (heartbeat
        keepalives forever), so any HTTP client request against it blocks
        indefinitely in this environment (starlette 1.x TestClient cannot
        complete a request to an infinite stream).
        """
        from careerops.api.app import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        spec = client.get("/api/v1/openapi.json").json()
        paths = spec["paths"]
        assert "/api/v1/candidates/{candidate_id}/notifications/stream" in paths
        assert "/api/v1/candidates/{candidate_id}/notifications" in paths

    def test_notification_service_wired_on_app_state(self) -> None:
        """Verify NotificationService is wired on app.state in create_app."""
        from careerops.api.app import create_app

        app = create_app()
        svc = getattr(app.state, "notification_service", None)
        assert svc is not None
        assert isinstance(svc, NotificationService)

    def test_sse_channel_wired_on_app_state(self) -> None:
        """Verify SSEChannel is wired on app.state in create_app."""
        from careerops.api.app import create_app

        app = create_app()
        channel = getattr(app.state, "sse_channel", None)
        assert channel is not None
        assert isinstance(channel, SSEChannel)


# ---------------------------------------------------------------------------
# ActionProjectionBuilder real build tests
# ---------------------------------------------------------------------------


class _FakeActionRecord:
    """Minimal stand-in for AgentActionRecord."""

    def __init__(
        self,
        action_key: str = "test-action",
        kind: ActionKind = ActionKind.RESUME_REVIEW,
        reason_code: str = "pending_review",
        deterministic_rank: int = 0,
        state: ActionState = ActionState.PROPOSED,
        source_refs: tuple[dict[str, str], ...] = (),
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
        source_event_key: UUID | None = None,
        context_id: UUID | None = None,
    ) -> None:
        self.action_key = action_key
        self.kind = kind
        self.reason_code = reason_code
        self.deterministic_rank = deterministic_rank
        self.state = state
        self.source_refs = source_refs
        self.created_at = created_at or datetime.now(UTC)
        self.expires_at = expires_at
        self.source_event_key = source_event_key or uuid4()
        self.context_id = context_id


class _FakeActionStore:
    """In-memory action store for testing."""

    def __init__(self, records: list[_FakeActionRecord] | None = None) -> None:
        self._records = records or []

    def list_queue(
        self,
        candidate_id: UUID,
        *,
        states: tuple[ActionState, ...] | None = None,
        limit: int = 3,
    ) -> list[_FakeActionRecord]:
        results = self._records
        if states is not None:
            results = [r for r in results if r.state in states]
        return results[:limit]


class TestActionProjectionBuilder:
    def test_build_returns_empty_when_no_repo(self) -> None:
        builder = ActionProjectionBuilder()
        candidate_id = uuid4()
        projection = builder.build(candidate_id)
        assert projection.items == ()
        assert projection.queue_version == 1

    def test_build_returns_empty_when_repo_has_no_records(self) -> None:
        store = _FakeActionStore([])
        builder = ActionProjectionBuilder(agent_action_repo=store)
        projection = builder.build(uuid4())
        assert projection.items == ()

    def test_build_maps_records_to_action_items(self) -> None:
        rec = _FakeActionRecord(
            action_key="resume-review-1",
            kind=ActionKind.RESUME_REVIEW,
            reason_code="pending_review",
            deterministic_rank=0,
            state=ActionState.PROPOSED,
            source_refs=({"type": "resume", "id": "r1"},),
        )
        store = _FakeActionStore([rec])
        builder = ActionProjectionBuilder(agent_action_repo=store)
        projection = builder.build(uuid4())

        assert len(projection.items) == 1
        item = projection.items[0]
        assert item.action_key == "resume-review-1"
        assert item.kind == ActionKind.RESUME_REVIEW
        assert item.title == "Review Resume"
        assert item.priority == "high"
        assert item.target_route == "/resumes"
        assert item.state == ActionState.PROPOSED
        assert len(item.source_refs) == 1
        assert item.source_refs[0].type == SourceRefType.RESUME

    def test_build_caps_at_limit(self) -> None:
        records = [
            _FakeActionRecord(action_key=f"action-{i}", deterministic_rank=i)
            for i in range(5)
        ]
        store = _FakeActionStore(records)
        builder = ActionProjectionBuilder(agent_action_repo=store)
        projection = builder.build(uuid4())
        assert len(projection.items) == 3  # _ACTION_QUEUE_LIMIT

    def test_build_filters_non_proposed_snoozed(self) -> None:
        rec_proposed = _FakeActionRecord(action_key="a1", state=ActionState.PROPOSED)
        rec_snoozed = _FakeActionRecord(action_key="a2", state=ActionState.SNOOZED)
        rec_dismissed = _FakeActionRecord(action_key="a3", state=ActionState.DISMISSED)
        store = _FakeActionStore([rec_proposed, rec_snoozed, rec_dismissed])
        builder = ActionProjectionBuilder(agent_action_repo=store)
        projection = builder.build(uuid4())
        keys = {item.action_key for item in projection.items}
        assert "a1" in keys
        assert "a2" in keys
        assert "a3" not in keys

    def test_find_action_returns_item(self) -> None:
        rec = _FakeActionRecord(action_key="target-action")
        store = _FakeActionStore([rec])
        builder = ActionProjectionBuilder(agent_action_repo=store)
        found = builder.find_action(uuid4(), "target-action")
        assert found is not None
        assert found.action_key == "target-action"

    def test_find_action_returns_none_for_missing(self) -> None:
        store = _FakeActionStore([])
        builder = ActionProjectionBuilder(agent_action_repo=store)
        found = builder.find_action(uuid4(), "nonexistent")
        assert found is None


class TestRecordToActionItem:
    def test_maps_all_fields(self) -> None:
        now = datetime.now(UTC)
        ctx_id = uuid4()
        src_event = uuid4()
        rec = _FakeActionRecord(
            action_key="test-key",
            kind=ActionKind.JOB_MATCHING,
            reason_code="match_found",
            deterministic_rank=1,
            state=ActionState.PROPOSED,
            source_refs=({"type": "job", "id": "j1", "version": "2"},),
            created_at=now,
            expires_at=None,
            source_event_key=src_event,
            context_id=ctx_id,
        )
        item = _record_to_action_item(rec, now=now)
        assert item is not None
        assert item.action_key == "test-key"
        assert item.kind == ActionKind.JOB_MATCHING
        assert item.title == "Review Job Matches"
        assert item.reason_code == "match_found"
        assert item.priority == "high"  # rank 1 -> high
        assert item.target_route == "/inbox"
        assert item.deterministic_rank == 1
        assert item.prerequisites.status == "ready"
        assert item.prerequisites.missing == []
        assert item.context_id == str(ctx_id)
        assert len(item.source_refs) == 1
        assert item.source_refs[0].type == SourceRefType.JOB
        assert item.source_refs[0].id == "j1"
        assert item.source_refs[0].version == 2
        assert item.source_event_key == str(src_event)
        assert item.source_freshness == now
        assert item.state == ActionState.PROPOSED
        assert item.created_at == now
        assert item.expires_at is None

    def test_returns_none_for_bad_record(self) -> None:
        assert _record_to_action_item(object()) is None

    def test_priority_mapping(self) -> None:
        rec_high = _FakeActionRecord(deterministic_rank=0)
        assert _record_to_action_item(rec_high).priority == "high"  # type: ignore[union-attr]

        rec_high2 = _FakeActionRecord(deterministic_rank=1)
        assert _record_to_action_item(rec_high2).priority == "high"  # type: ignore[union-attr]

        rec_medium = _FakeActionRecord(deterministic_rank=2)
        assert _record_to_action_item(rec_medium).priority == "medium"  # type: ignore[union-attr]

    def test_source_refs_empty(self) -> None:
        rec = _FakeActionRecord(source_refs=())
        item = _record_to_action_item(rec)
        assert item is not None
        assert item.source_refs == []

    def test_context_id_none(self) -> None:
        rec = _FakeActionRecord(context_id=None)
        item = _record_to_action_item(rec)
        assert item is not None
        assert item.context_id == ""
