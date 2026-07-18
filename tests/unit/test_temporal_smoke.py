from careerops.infrastructure.temporal.activities import NoOpSmokeActivitySink
from careerops.workflows.smoke import RecoverableSmokeWorkflow
from careerops.workflows.smoke_contracts import (
    SmokeCompletionCommand,
    SmokePhase,
    SmokeReleaseSignal,
    SmokeStartCommand,
)


def test_first_release_signal_wins_and_later_signals_are_observable() -> None:
    workflow = RecoverableSmokeWorkflow()

    workflow.release(SmokeReleaseSignal(request_id="release-1"))
    workflow.release(SmokeReleaseSignal(request_id="release-1"))
    workflow.release(SmokeReleaseSignal(request_id="conflicting-release"))

    status = workflow.status()
    assert status.phase is SmokePhase.RELEASED
    assert status.release_request_id == "release-1"
    assert status.ignored_release_signals == 2


def test_default_activity_sink_returns_stable_idempotent_receipts() -> None:
    async def exercise() -> None:
        sink = NoOpSmokeActivitySink()
        start = SmokeStartCommand(operation_id="operation-1")
        completion = SmokeCompletionCommand(
            operation_id="operation-1",
            release_request_id="release-1",
            idempotency_key="completion-1",
        )

        assert await sink.record_started(start) == await sink.record_started(start)
        assert await sink.record_completed(completion) == await sink.record_completed(completion)

    import asyncio

    asyncio.run(exercise())
