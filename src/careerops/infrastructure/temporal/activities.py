from __future__ import annotations

from typing import Protocol

from temporalio import activity

from careerops.workflows.smoke_contracts import (
    SMOKE_COMPLETED_ACTIVITY,
    SMOKE_STARTED_ACTIVITY,
    SmokeActivityReceipt,
    SmokeCompletionCommand,
    SmokeStartCommand,
)


class SmokeActivitySink(Protocol):
    """Activity-side adapter; implementations may perform external I/O."""

    async def record_started(self, command: SmokeStartCommand) -> str: ...

    async def record_completed(self, command: SmokeCompletionCommand) -> str: ...


class NoOpSmokeActivitySink:
    """M0 sink that emits stable receipts without external side effects."""

    async def record_started(self, command: SmokeStartCommand) -> str:
        return f"started:{command.operation_id}"

    async def record_completed(self, command: SmokeCompletionCommand) -> str:
        return f"completed:{command.idempotency_key}"


class SmokeActivities:
    def __init__(self, sink: SmokeActivitySink | None = None) -> None:
        self._sink = sink or NoOpSmokeActivitySink()

    @activity.defn(name=SMOKE_STARTED_ACTIVITY)
    async def record_started(self, command: SmokeStartCommand) -> SmokeActivityReceipt:
        activity.logger.info("recording recoverable smoke start")
        receipt_id = await self._sink.record_started(command)
        return SmokeActivityReceipt(receipt_id=receipt_id)

    @activity.defn(name=SMOKE_COMPLETED_ACTIVITY)
    async def record_completed(self, command: SmokeCompletionCommand) -> SmokeActivityReceipt:
        activity.logger.info("recording recoverable smoke completion")
        receipt_id = await self._sink.record_completed(command)
        return SmokeActivityReceipt(receipt_id=receipt_id)
