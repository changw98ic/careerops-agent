"""Unit tests for ScheduleManager (Phase 2.4 — idempotent Schedule lifecycle).

Uses a fake Temporal client/handle so the create/update/pause/resume/delete
flow is verified without a live Temporal server. The fake records the final
``Schedule`` state after each update so assertions can check spec + paused.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from temporalio.client import (
    Schedule,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.service import RPCError, RPCStatusCode

from careerops.infrastructure.temporal.schedule_manager import (
    ScheduleDescriptor,
    ScheduleManager,
    crawl_schedule_id,
    gmail_token_refresh_schedule_id,
    mail_sync_schedule_id,
)


# Minimal stubs matching the temporalio client surface ScheduleManager uses.


@dataclass
class _FakeHandle:
    schedule_id: str
    exists: bool
    schedule: Schedule = field(
        default_factory=lambda: Schedule(
            action=None,  # type: ignore[arg-type]
            spec=None,  # type: ignore[arg-type]
            state=ScheduleState(),
        )
    )
    deleted: bool = False
    triggered: int = 0
    updates: list[ScheduleUpdate] = field(default_factory=list)

    async def describe(self, **_: Any) -> Any:
        if not self.exists:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, None)  # type: ignore[arg-type]
        return _Description(schedule=self.schedule)

    async def update(
        self,
        updater: Callable[[ScheduleUpdateInput], ScheduleUpdate | None],
        **_: Any,
    ) -> None:
        if not self.exists:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, None)  # type: ignore[arg-type]
        desc = _Description(schedule=self.schedule)
        result = updater(ScheduleUpdateInput(desc))  # type: ignore[arg-type]
        if result is not None:
            self.updates.append(result)
            self.schedule = result.schedule

    async def delete(self, **_: Any) -> None:
        if not self.exists:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, None)  # type: ignore[arg-type]
        self.deleted = True
        self.exists = False

    async def trigger(self, **_: Any) -> None:
        if not self.exists:
            raise RPCError("not found", RPCStatusCode.NOT_FOUND, None)  # type: ignore[arg-type]
        self.triggered += 1


@dataclass
class _Description:
    schedule: Schedule


class _FakeClient:
    def __init__(self) -> None:
        self.schedules: dict[str, _FakeHandle] = {}
        self.created: list[tuple[str, Schedule]] = []

    def get_schedule_handle(self, schedule_id: str) -> _FakeHandle:
        if schedule_id not in self.schedules:
            self.schedules[schedule_id] = _FakeHandle(schedule_id=schedule_id, exists=False)
        return self.schedules[schedule_id]

    async def create_schedule(self, schedule_id: str, schedule: Schedule, **_: Any) -> _FakeHandle:
        handle = _FakeHandle(
            schedule_id=schedule_id, exists=True, schedule=schedule
        )
        self.schedules[schedule_id] = handle
        self.created.append((schedule_id, schedule))
        return handle


def _manager() -> tuple[ScheduleManager, _FakeClient]:
    client = _FakeClient()
    return ScheduleManager(client), client


# A workflow type name (string) avoids needing a registered Temporal workflow
# definition when constructing the real ScheduleActionStartWorkflow.
_WORKFLOW = "careerops.mail_sync.MailSyncWorkflow"


class TestEnsureSchedule:
    @pytest.mark.asyncio
    async def test_creates_when_absent(self) -> None:
        mgr, client = _manager()
        created = await mgr.ensure_schedule(
            schedule_id="mail-sync:acc-1",
            workflow=_WORKFLOW,
            arg={"x": 1},
            interval=timedelta(minutes=5),
            task_queue="q",
            paused=True,
        )
        assert created is True
        assert "mail-sync:acc-1" in client.schedules
        handle = client.schedules["mail-sync:acc-1"]
        assert handle.schedule.state.paused is True
        assert handle.schedule.spec.intervals[0].every == timedelta(minutes=5)

    @pytest.mark.asyncio
    async def test_updates_when_present(self) -> None:
        mgr, client = _manager()
        await mgr.ensure_schedule(
            schedule_id="s1",
            workflow=_WORKFLOW,
            arg=1,
            interval=timedelta(minutes=5),
            task_queue="q",
            paused=True,
        )
        # Second call changes interval + unpauses -> should update, not create.
        created = await mgr.ensure_schedule(
            schedule_id="s1",
            workflow=_WORKFLOW,
            arg=1,
            interval=timedelta(hours=2),
            task_queue="q",
            paused=False,
        )
        assert created is False
        assert len(client.created) == 1  # only one create
        handle = client.schedules["s1"]
        assert handle.schedule.state.paused is False
        assert handle.schedule.spec.intervals[0].every == timedelta(hours=2)


class TestPauseResume:
    @pytest.mark.asyncio
    async def test_pause_sets_paused_true(self) -> None:
        mgr, _ = _manager()
        await mgr.ensure_schedule(
            schedule_id="s1",
            workflow=_WORKFLOW,
            arg=1,
            interval=timedelta(hours=1),
            task_queue="q",
        )
        ok = await mgr.pause("s1")
        assert ok is True
        desc = await mgr.describe("s1")
        assert desc is not None and desc.paused is True

    @pytest.mark.asyncio
    async def test_resume_sets_paused_false(self) -> None:
        mgr, _ = _manager()
        await mgr.ensure_schedule(
            schedule_id="s1",
            workflow=_WORKFLOW,
            arg=1,
            interval=timedelta(hours=1),
            task_queue="q",
            paused=True,
        )
        ok = await mgr.resume("s1")
        assert ok is True
        desc = await mgr.describe("s1")
        assert desc is not None and desc.paused is False

    @pytest.mark.asyncio
    async def test_pause_absent_returns_false(self) -> None:
        mgr, _ = _manager()
        ok = await mgr.pause("nope")
        assert ok is False


class TestDeleteTrigger:
    @pytest.mark.asyncio
    async def test_delete_removes(self) -> None:
        mgr, client = _manager()
        await mgr.ensure_schedule(
            schedule_id="s1",
            workflow=_WORKFLOW,
            arg=1,
            interval=timedelta(hours=1),
            task_queue="q",
        )
        ok = await mgr.delete("s1")
        assert ok is True
        assert client.schedules["s1"].deleted is True
        assert await mgr.delete("s1") is False  # already gone

    @pytest.mark.asyncio
    async def test_trigger_fires_once(self) -> None:
        mgr, client = _manager()
        await mgr.ensure_schedule(
            schedule_id="s1",
            workflow=_WORKFLOW,
            arg=1,
            interval=timedelta(hours=1),
            task_queue="q",
        )
        assert await mgr.trigger("s1") is True
        assert client.schedules["s1"].triggered == 1
        assert await mgr.trigger("nope") is False


class TestDescribe:
    @pytest.mark.asyncio
    async def test_describe_reports_interval_and_paused(self) -> None:
        mgr, _ = _manager()
        await mgr.ensure_schedule(
            schedule_id="s1",
            workflow=_WORKFLOW,
            arg=1,
            interval=timedelta(minutes=10),
            task_queue="q",
            paused=True,
        )
        desc = await mgr.describe("s1")
        assert isinstance(desc, ScheduleDescriptor)
        assert desc.interval_seconds == 600.0
        assert desc.paused is True

    @pytest.mark.asyncio
    async def test_describe_absent_returns_none(self) -> None:
        mgr, _ = _manager()
        assert await mgr.describe("nope") is None


class TestScheduleIds:
    def test_mail_sync_id_is_deterministic(self) -> None:
        assert mail_sync_schedule_id("acc-1") == "mail-sync:acc-1"

    def test_crawl_id_includes_owner_and_version(self) -> None:
        assert crawl_schedule_id("owner", "v1") == "crawl:owner:v1"

    def test_token_refresh_id_is_constant(self) -> None:
        assert gmail_token_refresh_schedule_id() == "gmail-token-refresh"
