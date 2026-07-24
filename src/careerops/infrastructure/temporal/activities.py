from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from temporalio import activity

from careerops.application.outbox import OutboxPublisher, PublishBatchResult
from careerops.application.side_effect_kernel import SideEffectKernel
from careerops.workflows.smoke_contracts import (
    SMOKE_COMPLETED_ACTIVITY,
    SMOKE_STARTED_ACTIVITY,
    SmokeActivityReceipt,
    SmokeCompletionCommand,
    SmokeStartCommand,
)

OUTBOX_DRAIN_ACTIVITY = "outbox_drain"
APPROVAL_SWEEPER_ACTIVITY = "approval_sweeper"


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


class OutboxDrainActivities:
    """Activities that drain pending outbox events via ``OutboxPublisher``."""

    def __init__(self, publisher: OutboxPublisher) -> None:
        self._publisher = publisher

    @activity.defn(name=OUTBOX_DRAIN_ACTIVITY)
    async def drain_outbox(self) -> dict[str, int]:
        now = datetime.now(UTC)
        result: PublishBatchResult = self._publisher.publish_batch(
            owner="temporal-outbox-drain", now=now
        )
        activity.logger.info(
            "outbox drain: claimed=%d published=%d deferred=%d failed=%d",
            result.claimed,
            result.published,
            result.deferred,
            result.failed,
        )
        return {
            "claimed": result.claimed,
            "published": result.published,
            "deferred": result.deferred,
            "failed": result.failed,
        }


class ApprovalSweeperActivities:
    """Activities that expire stale PENDING approvals."""

    def __init__(self, kernel: SideEffectKernel) -> None:
        self._kernel = kernel

    @activity.defn(name=APPROVAL_SWEEPER_ACTIVITY)
    async def sweep_expired_approvals(self) -> int:
        now = datetime.now(UTC)
        expired = self._kernel.expire_pending_approvals(now=now)
        count = len(expired)
        activity.logger.info("approval sweeper expired %d approvals", count)
        return count
