from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from careerops.workflows.smoke_contracts import (
    SMOKE_COMPLETED_ACTIVITY,
    SMOKE_STARTED_ACTIVITY,
    SmokeActivityReceipt,
    SmokeCompletionCommand,
    SmokePhase,
    SmokeReleaseSignal,
    SmokeStartCommand,
    SmokeStatus,
    SmokeWorkflowInput,
    SmokeWorkflowResult,
)

_ACTIVITY_START_TO_CLOSE = timedelta(seconds=30)
_ACTIVITY_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=5),
    maximum_attempts=3,
)


@workflow.defn
class RecoverableSmokeWorkflow:
    """Minimal durable workflow used to prove worker crash recovery.

    The first release signal wins. Signals only mutate replayed workflow state;
    the single completion activity is scheduled by ``run`` after that state is
    observed. External completion adapters must deduplicate on the supplied
    idempotency key because Temporal activities are at-least-once.
    """

    def __init__(self) -> None:
        self._phase = SmokePhase.STARTING
        self._release: SmokeReleaseSignal | None = None
        self._ignored_release_signals = 0

    @workflow.run
    async def run(self, request: SmokeWorkflowInput) -> SmokeWorkflowResult:
        start_receipt = await workflow.execute_activity(
            SMOKE_STARTED_ACTIVITY,
            SmokeStartCommand(operation_id=request.operation_id),
            result_type=SmokeActivityReceipt,
            activity_id="record-started",
            start_to_close_timeout=_ACTIVITY_START_TO_CLOSE,
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        self._phase = SmokePhase.WAITING_FOR_RELEASE
        await workflow.wait_condition(lambda: self._release is not None)

        release = self._release
        if release is None:  # Narrows the type without introducing non-determinism.
            raise RuntimeError("release condition resolved without a release signal")

        self._phase = SmokePhase.COMPLETING
        completion_receipt = await workflow.execute_activity(
            SMOKE_COMPLETED_ACTIVITY,
            SmokeCompletionCommand(
                operation_id=request.operation_id,
                release_request_id=release.request_id,
                idempotency_key=request.completion_idempotency_key,
            ),
            result_type=SmokeActivityReceipt,
            activity_id="record-completed",
            start_to_close_timeout=_ACTIVITY_START_TO_CLOSE,
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        self._phase = SmokePhase.COMPLETED
        return SmokeWorkflowResult(
            operation_id=request.operation_id,
            release_request_id=release.request_id,
            start_receipt_id=start_receipt.receipt_id,
            completion_receipt_id=completion_receipt.receipt_id,
        )

    @workflow.signal
    def release(self, signal: SmokeReleaseSignal) -> None:
        if self._release is not None:
            self._ignored_release_signals += 1
            return
        self._release = signal
        self._phase = SmokePhase.RELEASED

    @workflow.query
    def status(self) -> SmokeStatus:
        return SmokeStatus(
            phase=self._phase,
            release_request_id=self._release.request_id if self._release else None,
            ignored_release_signals=self._ignored_release_signals,
        )
