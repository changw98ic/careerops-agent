from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from careerops.infrastructure.temporal.activities import SmokeActivities
from careerops.infrastructure.temporal.worker import TemporalWorkerSettings, build_worker
from careerops.workflows.smoke import RecoverableSmokeWorkflow
from careerops.workflows.smoke_contracts import (
    SmokeCompletionCommand,
    SmokePhase,
    SmokeReleaseSignal,
    SmokeStartCommand,
    SmokeWorkflowInput,
    SmokeWorkflowResult,
)

pytestmark = pytest.mark.integration


@dataclass
class CountingSink:
    starts: list[SmokeStartCommand] = field(default_factory=list)
    completions: list[SmokeCompletionCommand] = field(default_factory=list)

    async def record_started(self, command: SmokeStartCommand) -> str:
        self.starts.append(command)
        return f"started:{command.operation_id}"

    async def record_completed(self, command: SmokeCompletionCommand) -> str:
        self.completions.append(command)
        return f"completed:{command.idempotency_key}"


def test_signal_wait_survives_worker_restart_and_history_replays() -> None:
    asyncio.run(_exercise_recovery_and_replay())


async def _exercise_recovery_and_replay() -> None:
    environment = await _start_test_environment_or_skip()
    async with environment:
        task_queue = f"careerops-smoke-{uuid4()}"
        settings = TemporalWorkerSettings(task_queue=task_queue)
        sink = CountingSink()
        activities = SmokeActivities(sink)

        async with build_worker(
            environment.client,
            settings,
            activities=activities,
            max_cached_workflows=0,
        ):
            handle = await environment.client.start_workflow(
                RecoverableSmokeWorkflow.run,
                SmokeWorkflowInput(
                    operation_id="operation-1",
                    completion_idempotency_key="completion-1",
                ),
                id=f"recoverable-smoke-{uuid4()}",
                task_queue=task_queue,
            )
            async with asyncio.timeout(10):
                while True:
                    status = await handle.query(RecoverableSmokeWorkflow.status)
                    if status.phase is SmokePhase.WAITING_FOR_RELEASE:
                        break
                    await asyncio.sleep(0.01)

        # The server records both signals while no worker is available. A fresh
        # worker then reconstructs state from history and completes only once.
        signal = SmokeReleaseSignal(request_id="release-1")
        await handle.signal(RecoverableSmokeWorkflow.release, signal)
        await handle.signal(RecoverableSmokeWorkflow.release, signal)

        async with build_worker(
            environment.client,
            settings,
            activities=activities,
            max_cached_workflows=0,
        ):
            async with asyncio.timeout(20):
                result: SmokeWorkflowResult = await handle.result()

        assert result.release_request_id == "release-1"
        assert result.completion_receipt_id == "completed:completion-1"
        assert len(sink.starts) == 1
        assert len(sink.completions) == 1

        history = await handle.fetch_history()
        scheduled_completion_activities = sum(
            event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
            and event.activity_task_scheduled_event_attributes.activity_id == "record-completed"
            for event in history.events
        )
        assert scheduled_completion_activities == 1

        replay_result = await Replayer(workflows=[RecoverableSmokeWorkflow]).replay_workflow(
            history
        )
        assert replay_result.replay_failure is None


async def _start_test_environment_or_skip() -> WorkflowEnvironment:
    binary = os.environ.get("CAREEROPS_TEMPORAL_TEST_SERVER_BINARY")
    if binary is not None and not Path(binary).is_file():
        pytest.skip(f"Temporal test-server binary does not exist: {binary}")
    try:
        return await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=binary,
        )
    except (OSError, RuntimeError) as error:
        pytest.skip(f"Temporal SDK test server is unavailable: {error}")
