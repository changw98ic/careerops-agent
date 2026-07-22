from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from careerops.workflows.goal_run import GoalRunWorkflow
from careerops.workflows.goal_run_contracts import (
    GoalRunCancelSignal,
    GoalRunCheckpointResult,
    GoalRunClaimCommand,
    GoalRunClaimResult,
    GoalRunDiscoveryCommand,
    GoalRunDiscoveryResult,
    GoalRunDiscoveryState,
    GoalRunEnsureCommand,
    GoalRunIngestResult,
    GoalRunInput,
    GoalRunMode,
    GoalRunPhase,
    GoalRunProgress,
    GoalRunResumeSignal,
    GoalRunReviewDecision,
    GoalRunReviewRequestResult,
    GoalRunReviewSignal,
    GoalRunSnapshot,
    GoalRunStatus,
    GoalRunStepOutcome,
    GoalRunStepResult,
)

pytestmark = pytest.mark.integration

SHA_A = "a" * 64
SHA_B = "b" * 64


def test_goal_run_workflow_replays_completed_history_deterministically() -> None:
    asyncio.run(_exercise_completed_history_replay())


def test_goal_run_workflow_retries_discovery_with_same_goal_run_id() -> None:
    asyncio.run(_exercise_discovery_retry_idempotency())


@pytest.mark.parametrize(
    "discovery_state",
    [GoalRunDiscoveryState.WAITING_REVIEW, GoalRunDiscoveryState.FAILED],
)
def test_goal_run_workflow_reclaims_after_non_ready_discovery(
    discovery_state: GoalRunDiscoveryState,
) -> None:
    asyncio.run(_exercise_non_ready_discovery_resume(discovery_state))


def test_goal_run_workflow_waits_for_review_signal_before_dispatching() -> None:
    asyncio.run(_exercise_review_signal_wait())


def test_preapplication_approval_completes_without_dispatch_or_reconciliation() -> None:
    asyncio.run(_exercise_preapplication_approval_is_terminal())


def test_goal_run_workflow_polls_db_first_review_approval_without_signal() -> None:
    asyncio.run(_exercise_review_poll_without_signal())


def test_goal_run_workflow_cancel_wins_over_rejected_review_race() -> None:
    asyncio.run(_exercise_cancel_review_race())


def test_goal_run_workflow_resumes_from_blocked_configuration() -> None:
    asyncio.run(_exercise_blocked_resume())


def test_goal_run_workflow_survives_worker_restart_and_continues_as_new() -> None:
    asyncio.run(_exercise_restart_and_continue_as_new())


def test_goal_run_workflow_stops_when_reconciliation_is_required() -> None:
    asyncio.run(_exercise_reconciliation_required())


def test_goal_run_workflow_waits_automatically_for_external_receipt() -> None:
    asyncio.run(_exercise_pending_external_receipt())


async def _exercise_completed_history_replay() -> None:
    harness = _GoalRunHarness()
    environment = await _start_test_environment_or_skip()
    async with environment:
        handle, result = await _run_to_completion(environment, harness)
        histories = await _fetch_full_history(environment, handle)

        assert result.status is GoalRunStatus.COMPLETED
        assert result.current_phase is GoalRunPhase.COMPLETED
        assert result.reason_code == "completed"

        replay_result = await Replayer(
            workflows=[GoalRunWorkflow],
            data_converter=pydantic_data_converter,
        ).replay_workflow(histories[-1])
        assert replay_result.replay_failure is None


async def _exercise_discovery_retry_idempotency() -> None:
    harness = _GoalRunHarness(fail_once={"goal.run_discovery"})
    environment = await _start_test_environment_or_skip()
    async with environment:
        _handle, result = await _run_to_completion(environment, harness)

        assert result.status is GoalRunStatus.COMPLETED
        assert harness.attempts["goal.run_discovery"] == 2
        first, second = harness.commands["goal.run_discovery"]
        assert first.goal_run_id == second.goal_run_id
        assert first.lease_token == second.lease_token
        assert first.source_row_id == second.source_row_id


async def _exercise_non_ready_discovery_resume(
    discovery_state: GoalRunDiscoveryState,
) -> None:
    harness = _GoalRunHarness(discovery_state=discovery_state)
    environment = await _start_test_environment_or_skip()
    async with environment:
        task_queue = f"goal-run-workflow-{uuid4()}"
        async with _worker(environment, task_queue, harness):
            handle = await _start_goal_run(environment, task_queue, harness)
            await _wait_for_snapshot(harness, GoalRunStatus.BLOCKED)

            assert harness.snapshot is not None
            assert harness.snapshot.phase is GoalRunPhase.BLOCKED
            assert harness.attempts["goal.claim_source"] == 1
            assert harness.attempts["goal.run_discovery"] == 1
            assert harness.attempts["goal.fail_source"] == 1
            assert "goal.complete_source" not in harness.commands
            assert "goal.ingest_public_ats" not in harness.commands

            blocked_checkpoint = next(
                command
                for command in harness.commands["goal.checkpoint"]
                if _phase(_field(command, "phase")) is GoalRunPhase.BLOCKED
            )
            assert _status(_field(blocked_checkpoint, "status")) is GoalRunStatus.BLOCKED
            assert _field(blocked_checkpoint, "checkpoint")["discovery_state"] == (
                discovery_state.value
            )

            load_attempts = harness.attempts.get("goal.load_run", 0)
            await environment.sleep(timedelta(seconds=31))
            await _wait_for_attempts(harness, "goal.load_run", load_attempts + 1)
            assert harness.attempts["goal.claim_source"] == 1
            assert harness.attempts["goal.run_discovery"] == 1

            harness.discovery_state = GoalRunDiscoveryState.READY
            harness.resume(phase=GoalRunPhase.CLAIMING_SOURCE)
            await handle.signal(
                GoalRunWorkflow.resume,
                GoalRunResumeSignal(command_id=uuid4()),
            )
            await _wait_for_snapshot(harness, GoalRunStatus.WAITING_REVIEW)

            assert harness.attempts["goal.claim_source"] == 2
            assert harness.attempts["goal.run_discovery"] == 2
            assert harness.attempts["goal.complete_source"] == 1
            assert harness.attempts["goal.ingest_public_ats"] == 1

            harness.approve_review()
            await handle.signal(GoalRunWorkflow.review, harness.review_signal())
            result = await handle.result()

        assert result.status is GoalRunStatus.COMPLETED


async def _exercise_review_signal_wait() -> None:
    harness = _GoalRunHarness()
    environment = await _start_test_environment_or_skip()
    async with environment:
        task_queue = f"goal-run-workflow-{uuid4()}"
        async with _worker(environment, task_queue, harness):
            handle = await _start_goal_run(environment, task_queue, harness)
            await _wait_for_snapshot(harness, GoalRunStatus.WAITING_REVIEW)

            assert "goal.dispatch_goal" not in harness.commands
            harness.approve_review()
            await handle.signal(GoalRunWorkflow.review, harness.review_signal())
            result = await handle.result()

        assert result.status is GoalRunStatus.COMPLETED
        assert result.mode is GoalRunMode.GMAIL_DISPATCH
        assert result.completion_kind is None
        assert harness.commands["goal.dispatch_goal"]
        assert harness.commands["goal.reconcile_goal"]


async def _exercise_preapplication_approval_is_terminal() -> None:
    harness = _GoalRunHarness(mode=GoalRunMode.PRE_APPLICATION_ONLY)
    environment = await _start_test_environment_or_skip()
    async with environment:
        task_queue = f"goal-run-workflow-{uuid4()}"
        async with _worker(environment, task_queue, harness):
            handle = await _start_goal_run(
                environment,
                task_queue,
                harness,
                mode=GoalRunMode.PRE_APPLICATION_ONLY,
            )
            await _wait_for_snapshot(harness, GoalRunStatus.WAITING_REVIEW)

            assert "goal.dispatch_goal" not in harness.commands
            assert "goal.reconcile_goal" not in harness.commands
            assert _field(
                harness.commands["goal.prepare_drafts"][0],
                "expected_match_snapshot_sha256",
            ) == SHA_A
            assert _field(
                harness.commands["goal.request_review"][0],
                "review_kind",
            ) == "goal_run_pre_application_review.v1"

            harness.approve_review()
            await handle.signal(GoalRunWorkflow.review, harness.review_signal())
            result = await handle.result()

        assert result.status is GoalRunStatus.COMPLETED
        assert result.current_phase is GoalRunPhase.COMPLETED
        assert result.mode is GoalRunMode.PRE_APPLICATION_ONLY
        assert result.completion_kind == "pre_application_package_approved"
        assert "goal.dispatch_goal" not in harness.commands
        assert "goal.reconcile_goal" not in harness.commands


async def _exercise_review_poll_without_signal() -> None:
    harness = _GoalRunHarness()
    environment = await _start_test_environment_or_skip()
    async with environment:
        task_queue = f"goal-run-workflow-{uuid4()}"
        async with _worker(environment, task_queue, harness):
            handle = await _start_goal_run(environment, task_queue, harness)
            await _wait_for_snapshot(harness, GoalRunStatus.WAITING_REVIEW)
            load_attempts_before_approval = harness.attempts.get("goal.load_run", 0)

            harness.approve_review()
            async with asyncio.timeout(10):
                result = await handle.result()

        histories = await _fetch_full_history(environment, handle)

        assert result.status is GoalRunStatus.COMPLETED
        assert harness.attempts["goal.load_run"] > load_attempts_before_approval
        assert any(
            event.event_type == EventType.EVENT_TYPE_TIMER_STARTED
            and event.timer_started_event_attributes.start_to_fire_timeout.seconds == 30
            for history in histories
            for event in history.events
        )
        assert not any(
            event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
            for history in histories
            for event in history.events
        )


async def _exercise_cancel_review_race() -> None:
    harness = _GoalRunHarness()
    environment = await _start_test_environment_or_skip()
    async with environment:
        task_queue = f"goal-run-workflow-{uuid4()}"
        async with _worker(environment, task_queue, harness):
            handle = await _start_goal_run(environment, task_queue, harness)
            await _wait_for_snapshot(harness, GoalRunStatus.WAITING_REVIEW)

            harness.cancel()
            harness.reject_review()
            await asyncio.gather(
                handle.signal(GoalRunWorkflow.cancel, GoalRunCancelSignal(command_id=uuid4())),
                handle.signal(GoalRunWorkflow.review, harness.review_signal()),
            )
            result = await handle.result()

        assert result.status is GoalRunStatus.CANCELLED
        assert result.current_phase is GoalRunPhase.CANCELLED
        assert "goal.dispatch_goal" not in harness.commands


async def _exercise_blocked_resume() -> None:
    harness = _GoalRunHarness(dispatch_enabled=False)
    environment = await _start_test_environment_or_skip()
    async with environment:
        task_queue = f"goal-run-workflow-{uuid4()}"
        async with _worker(environment, task_queue, harness):
            handle = await _start_goal_run(
                environment,
                task_queue,
                harness,
                next_phase=GoalRunPhase.DISPATCH,
                progress=GoalRunProgress(
                    source_row_id=harness.source_row_id,
                    crawler_run_id=harness.crawler_run_id,
                    source_id="public-ats",
                    output_manifest_sha256=SHA_A,
                    observed_records=2,
                    inserted_versions=1,
                    reused_versions=1,
                    review_item_id=harness.review_item_id,
                    review_snapshot_sha256=harness.snapshot_sha256,
                ),
            )
            await _wait_for_snapshot(harness, GoalRunStatus.BLOCKED)

            harness.dispatch_enabled = True
            harness.resume()
            await handle.signal(GoalRunWorkflow.resume, GoalRunResumeSignal(command_id=uuid4()))
            result = await handle.result()

        assert result.status is GoalRunStatus.COMPLETED
        assert len(harness.commands["goal.dispatch_goal"]) == 2


async def _exercise_restart_and_continue_as_new() -> None:
    harness = _GoalRunHarness()
    environment = await _start_test_environment_or_skip()
    async with environment:
        task_queue = f"goal-run-workflow-{uuid4()}"
        async with _worker(environment, task_queue, harness):
            handle = await _start_goal_run(environment, task_queue, harness)
            await _wait_for_snapshot(harness, GoalRunStatus.WAITING_REVIEW)

        harness.approve_review()
        await handle.signal(GoalRunWorkflow.review, harness.review_signal())

        async with _worker(environment, task_queue, harness):
            result = await handle.result()

        histories = await _fetch_full_history(environment, handle)
        continued_as_new = any(
            event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW
            for history in histories
            for event in history.events
        )

        assert result.status is GoalRunStatus.COMPLETED
        assert result.current_phase is GoalRunPhase.COMPLETED
        assert continued_as_new


async def _exercise_reconciliation_required() -> None:
    harness = _GoalRunHarness(
        matching_outcome=GoalRunStepOutcome.RECONCILIATION_REQUIRED,
    )
    environment = await _start_test_environment_or_skip()
    async with environment:
        task_queue = f"goal-run-workflow-{uuid4()}"
        async with _worker(environment, task_queue, harness):
            handle = await _start_goal_run(environment, task_queue, harness)
            async with asyncio.timeout(10):
                result = await handle.result()

        assert result.status is GoalRunStatus.RECONCILIATION_REQUIRED
        assert result.current_phase is GoalRunPhase.RECONCILIATION
        assert result.reason_code == "matching_reconciliation_required"
        assert harness.snapshot is not None
        assert harness.snapshot.phase is GoalRunPhase.RECONCILIATION
        assert harness.snapshot.status is GoalRunStatus.RECONCILIATION_REQUIRED
        assert harness.attempts["goal.match_jobs"] == 1
        assert "goal.load_run" not in harness.commands
        assert "goal.prepare_drafts" not in harness.commands
        assert "goal.request_review" not in harness.commands
        assert "goal.dispatch_goal" not in harness.commands
        assert "goal.reconcile_goal" not in harness.commands

        checkpoint_commands = harness.commands["goal.checkpoint"]
        assert any(
            _phase(_field(command, "phase")) is GoalRunPhase.RECONCILIATION
            and _status(_field(command, "status")) is GoalRunStatus.RECONCILIATION_REQUIRED
            for command in checkpoint_commands
        )
        assert not any(
            _phase(_field(command, "phase")) in {GoalRunPhase.BLOCKED, GoalRunPhase.COMPLETED}
            for command in checkpoint_commands
        )


async def _exercise_pending_external_receipt() -> None:
    harness = _GoalRunHarness(pending_reconciliation_polls=2)
    environment = await _start_test_environment_or_skip()
    async with environment:
        task_queue = f"goal-run-workflow-{uuid4()}"
        async with _worker(environment, task_queue, harness):
            handle = await _start_goal_run(environment, task_queue, harness)
            await _wait_for_snapshot(harness, GoalRunStatus.WAITING_REVIEW)
            harness.approve_review()
            await handle.signal(GoalRunWorkflow.review, harness.review_signal())
            result = await handle.result()

        assert result.status is GoalRunStatus.COMPLETED
        assert harness.attempts["goal.dispatch_goal"] == 1
        assert harness.attempts["goal.reconcile_goal"] == 3
        assert harness.attempts.get("goal.load_run", 0) == 1


async def _run_to_completion(
    environment: WorkflowEnvironment,
    harness: _GoalRunHarness,
) -> tuple[Any, Any]:
    task_queue = f"goal-run-workflow-{uuid4()}"
    async with _worker(environment, task_queue, harness):
        handle = await _start_goal_run(environment, task_queue, harness)
        await _wait_for_snapshot(harness, GoalRunStatus.WAITING_REVIEW)
        harness.approve_review()
        await handle.signal(GoalRunWorkflow.review, harness.review_signal())
        result = await handle.result()
    return handle, result


async def _start_goal_run(
    environment: WorkflowEnvironment,
    task_queue: str,
    harness: _GoalRunHarness,
    *,
    next_phase: GoalRunPhase = GoalRunPhase.INITIALIZING,
    progress: GoalRunProgress | None = None,
    mode: GoalRunMode | None = None,
) -> Any:
    request_kwargs: dict[str, Any] = {
        "goal_run_id": harness.goal_run_id,
        "owner_user_id": harness.owner_user_id,
        "registry_id": harness.registry_id,
        "fencing_token": harness.fencing_token,
        "source_id": "public-ats",
        "max_records": 10,
        "expected_version": 0,
        "next_phase": next_phase,
        "progress": progress or GoalRunProgress(),
    }
    if mode is not None:
        request_kwargs["mode"] = mode
    request = GoalRunInput(**request_kwargs)
    return await environment.client.start_workflow(
        GoalRunWorkflow.run,
        request,
        id=f"goal-run-workflow-{harness.goal_run_id}",
        task_queue=task_queue,
    )


def _worker(
    environment: WorkflowEnvironment,
    task_queue: str,
    harness: _GoalRunHarness,
) -> Worker:
    return Worker(
        environment.client,
        task_queue=task_queue,
        workflows=[GoalRunWorkflow],
        activities=harness.activities(),
        max_cached_workflows=0,
    )


async def _wait_for_status(handle: Any, expected: GoalRunStatus) -> None:
    async with asyncio.timeout(10):
        while True:
            status = await handle.query(GoalRunWorkflow.status)
            if status.status is expected:
                return
            await asyncio.sleep(0.01)


async def _wait_for_snapshot(harness: _GoalRunHarness, expected: GoalRunStatus) -> None:
    try:
        async with asyncio.timeout(10):
            while True:
                if harness.snapshot is not None and harness.snapshot.status is expected:
                    return
                await asyncio.sleep(0.01)
    except TimeoutError as error:
        raise AssertionError(
            f"timed out waiting for {expected}; "
            f"snapshot={harness.snapshot}; attempts={harness.attempts}"
        ) from error


async def _wait_for_attempts(
    harness: _GoalRunHarness,
    activity_name: str,
    expected: int,
) -> None:
    async with asyncio.timeout(10):
        while harness.attempts.get(activity_name, 0) < expected:
            await asyncio.sleep(0.01)


async def _fetch_full_history(environment: WorkflowEnvironment, handle: Any) -> list[Any]:
    histories = []
    workflow_id = handle.id
    run_id = handle.result_run_id
    while True:
        run_handle = environment.client.get_workflow_handle(workflow_id, run_id=run_id)
        history = await run_handle.fetch_history()
        histories.append(history)
        next_run_id = _continued_as_new_run_id(history)
        if next_run_id is None:
            return histories
        run_id = next_run_id


def _continued_as_new_run_id(history: Any) -> str | None:
    for event in history.events:
        if event.event_type != EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW:
            continue
        attrs = event.workflow_execution_continued_as_new_event_attributes
        return attrs.new_execution_run_id
    return None


async def _wait_for_continued_handle(environment: WorkflowEnvironment, handle: Any) -> Any:
    async with asyncio.timeout(10):
        while True:
            history = await handle.fetch_history()
            run_id = _continued_as_new_run_id(history)
            if run_id is not None:
                return environment.client.get_workflow_handle(handle.id, run_id=run_id)
            await asyncio.sleep(0.01)


async def _start_test_environment_or_skip() -> WorkflowEnvironment:
    binary = os.environ.get("CAREEROPS_TEMPORAL_TEST_SERVER_BINARY")
    if binary is not None and not Path(binary).is_file():
        pytest.skip(f"Temporal test-server binary does not exist: {binary}")
    try:
        return await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            test_server_existing_path=binary,
        )
    except (OSError, RuntimeError) as error:
        pytest.skip(f"Temporal SDK test server is unavailable: {error}")


@dataclass
class _GoalRunHarness:
    mode: GoalRunMode = GoalRunMode.GMAIL_DISPATCH
    dispatch_enabled: bool = True
    discovery_state: GoalRunDiscoveryState = GoalRunDiscoveryState.READY
    matching_outcome: GoalRunStepOutcome = GoalRunStepOutcome.SUCCEEDED
    pending_reconciliation_polls: int = 0
    fail_once: set[str] = field(default_factory=set)
    goal_run_id: UUID = field(default_factory=uuid4)
    owner_user_id: UUID = field(default_factory=uuid4)
    registry_id: UUID = field(default_factory=uuid4)
    fencing_token: UUID = field(default_factory=uuid4)
    source_row_id: UUID = field(default_factory=uuid4)
    crawler_run_id: UUID = field(default_factory=uuid4)
    crawler_execution_request_id: UUID = field(default_factory=uuid4)
    crawler_execution_result_id: UUID = field(default_factory=uuid4)
    crawler_execution_outbox_event_id: UUID = field(default_factory=uuid4)
    review_item_id: UUID = field(default_factory=uuid4)
    snapshot_sha256: str = SHA_B
    version: int = 0
    attempts: dict[str, int] = field(default_factory=dict)
    commands: dict[str, list[Any]] = field(default_factory=dict)
    snapshot: GoalRunSnapshot | None = None

    def activities(self) -> list[Any]:
        return [
            self.ensure_run,
            self.load_run,
            self.checkpoint,
            self.claim_source,
            self.run_discovery,
            self.complete_source,
            self.fail_source,
            self.ingest_public_ats,
            self.match_jobs,
            self.prepare_drafts,
            self.request_review,
            self.dispatch_goal,
            self.reconcile_goal,
        ]

    def approve_review(self) -> None:
        if self.mode is GoalRunMode.PRE_APPLICATION_ONLY:
            self._set_snapshot(
                phase=GoalRunPhase.COMPLETED,
                status=GoalRunStatus.COMPLETED,
                review_decision=GoalRunReviewDecision.APPROVE,
                pending_review_item_id=self.review_item_id,
                pending_review_snapshot_sha256=self.snapshot_sha256,
            )
            return
        self._set_snapshot(
            phase=GoalRunPhase.REVIEW,
            status=GoalRunStatus.RUNNING,
            review_decision=GoalRunReviewDecision.APPROVE,
            pending_review_item_id=self.review_item_id,
            pending_review_snapshot_sha256=self.snapshot_sha256,
        )

    def reject_review(self) -> None:
        if self._snapshot().status is GoalRunStatus.CANCELLED:
            return
        self._set_snapshot(
            phase=GoalRunPhase.REJECTED,
            status=GoalRunStatus.REJECTED,
            review_decision=GoalRunReviewDecision.REJECT,
            pending_review_item_id=self.review_item_id,
            pending_review_snapshot_sha256=self.snapshot_sha256,
        )

    def cancel(self) -> None:
        self._set_snapshot(phase=GoalRunPhase.CANCELLED, status=GoalRunStatus.CANCELLED)

    def resume(self, *, phase: GoalRunPhase = GoalRunPhase.DISPATCH) -> None:
        self._set_snapshot(phase=phase, status=GoalRunStatus.RUNNING)

    def review_signal(self) -> GoalRunReviewSignal:
        return GoalRunReviewSignal(
            command_id=uuid4(),
            review_item_id=self.review_item_id,
            snapshot_sha256=self.snapshot_sha256,
        )

    @activity.defn(name="goal.ensure_run")
    async def ensure_run(self, command: GoalRunEnsureCommand) -> GoalRunSnapshot:
        self._record("goal.ensure_run", command)
        if self.snapshot is None:
            self.snapshot = GoalRunSnapshot(
                goal_run_id=command.goal_run_id,
                owner_user_id=command.owner_user_id,
                version=self.version,
                fencing_token=command.fencing_token,
                phase=GoalRunPhase.INITIALIZING,
                status=GoalRunStatus.STARTING,
                source_id=command.source_id,
                mode=self.mode,
            )
        return self._snapshot()

    @activity.defn(name="goal.load_run")
    async def load_run(self, command: Any) -> GoalRunSnapshot:
        self._record("goal.load_run", command)
        return self._snapshot()

    @activity.defn(name="goal.checkpoint")
    async def checkpoint(self, command: Any) -> GoalRunCheckpointResult:
        self._record("goal.checkpoint", command)
        if _field(command, "expected_version") != self.version:
            raise RuntimeError("stale_goal_run_version")
        phase = _phase(_field(command, "phase"))
        status = _status(_field(command, "status"))
        self._set_snapshot(phase=phase, status=status)
        return GoalRunCheckpointResult(
            goal_run_id=_field(command, "goal_run_id"),
            version=self.version,
            phase=phase,
            status=status,
        )

    @activity.defn(name="goal.claim_source")
    async def claim_source(self, command: GoalRunClaimCommand) -> GoalRunClaimResult:
        self._record("goal.claim_source", command)
        return GoalRunClaimResult(
            registry_id=command.registry_id,
            run_id=self.crawler_run_id,
            source_row_id=self.source_row_id,
            source_id=command.source_id or "public-ats",
            lease_token=self.fencing_token,
            lease_owner="goal-run-test-worker",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    @activity.defn(name="goal.run_discovery")
    async def run_discovery(self, command: GoalRunDiscoveryCommand) -> GoalRunDiscoveryResult:
        self._record("goal.run_discovery", command)
        self._maybe_fail("goal.run_discovery")
        if self.discovery_state is not GoalRunDiscoveryState.READY:
            return GoalRunDiscoveryResult(
                source_row_id=command.source_row_id,
                run_id=command.run_id,
                state=self.discovery_state,
                output_manifest_sha256=None,
                cursor=None,
                result=f"discovery_{self.discovery_state.value}",
                crawler_execution_request_id=self.crawler_execution_request_id,
                crawler_execution_result_id=self.crawler_execution_result_id,
                crawler_execution_outbox_event_id=self.crawler_execution_outbox_event_id,
            )
        return GoalRunDiscoveryResult(
            source_row_id=command.source_row_id,
            run_id=command.run_id,
            state=GoalRunDiscoveryState.READY,
            output_manifest_sha256=SHA_A,
            cursor="cursor-2",
            result="discovered",
            crawler_execution_request_id=self.crawler_execution_request_id,
            crawler_execution_result_id=self.crawler_execution_result_id,
            crawler_execution_outbox_event_id=self.crawler_execution_outbox_event_id,
        )

    @activity.defn(name="goal.complete_source")
    async def complete_source(self, command: Any) -> str:
        self._record("goal.complete_source", command)
        return "ok"

    @activity.defn(name="goal.fail_source")
    async def fail_source(self, command: Any) -> str:
        self._record("goal.fail_source", command)
        return "ok"

    @activity.defn(name="goal.ingest_public_ats")
    async def ingest_public_ats(self, command: Any) -> GoalRunIngestResult:
        self._record("goal.ingest_public_ats", command)
        return GoalRunIngestResult(observed_records=2, inserted_versions=1, reused_versions=1)

    @activity.defn(name="goal.match_jobs")
    async def match_jobs(self, command: Any) -> GoalRunStepResult:
        self._record("goal.match_jobs", command)
        return GoalRunStepResult(
            outcome=self.matching_outcome,
            reason_code=(
                "matched"
                if self.matching_outcome is GoalRunStepOutcome.SUCCEEDED
                else "matching_reconciliation_required"
            ),
            output_sha256=SHA_A,
        )

    @activity.defn(name="goal.prepare_drafts")
    async def prepare_drafts(self, command: Any) -> GoalRunStepResult:
        self._record("goal.prepare_drafts", command)
        return GoalRunStepResult(
            outcome=GoalRunStepOutcome.SUCCEEDED,
            reason_code="drafts_prepared",
            output_sha256=self.snapshot_sha256,
            details=(
                {"review_kind": "goal_run_pre_application_review.v1"}
                if self.mode is GoalRunMode.PRE_APPLICATION_ONLY
                else {}
            ),
        )

    @activity.defn(name="goal.request_review")
    async def request_review(
        self,
        command: Any,
    ) -> GoalRunReviewRequestResult:
        self._record("goal.request_review", command)
        self.snapshot_sha256 = _field(command, "snapshot_sha256")
        self._set_snapshot(
            phase=GoalRunPhase.REVIEW,
            status=GoalRunStatus.WAITING_REVIEW,
            pending_review_item_id=self.review_item_id,
            pending_review_snapshot_sha256=self.snapshot_sha256,
        )
        return GoalRunReviewRequestResult(
            goal_run_id=_field(command, "goal_run_id"),
            review_item_id=self.review_item_id,
            snapshot_sha256=self.snapshot_sha256,
            version=self.version,
        )

    @activity.defn(name="goal.dispatch_goal")
    async def dispatch_goal(self, command: Any) -> GoalRunStepResult:
        self._record("goal.dispatch_goal", command)
        if not self.dispatch_enabled:
            return GoalRunStepResult(
                outcome=GoalRunStepOutcome.BLOCKED_CONFIGURATION,
                reason_code="dispatch_disabled",
                output_sha256=SHA_A,
            )
        return GoalRunStepResult(
            outcome=GoalRunStepOutcome.SUCCEEDED,
            reason_code="dispatched",
            output_sha256=SHA_A,
        )

    @activity.defn(name="goal.reconcile_goal")
    async def reconcile_goal(self, command: Any) -> GoalRunStepResult:
        self._record("goal.reconcile_goal", command)
        if self.attempts["goal.reconcile_goal"] <= self.pending_reconciliation_polls:
            return GoalRunStepResult(
                outcome=GoalRunStepOutcome.PENDING_EXTERNAL,
                reason_code="waiting_for_provider_receipt",
                output_sha256=SHA_A,
            )
        return GoalRunStepResult(
            outcome=GoalRunStepOutcome.SUCCEEDED,
            reason_code="reconciled",
            output_sha256=SHA_A,
        )

    def _record(self, name: str, command: Any) -> None:
        self.attempts[name] = self.attempts.get(name, 0) + 1
        self.commands.setdefault(name, []).append(command)

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail_once and self.attempts[name] == 1:
            raise RuntimeError(f"transient failure from {name}")

    def _snapshot(self) -> GoalRunSnapshot:
        if self.snapshot is None:
            self.snapshot = GoalRunSnapshot(
                goal_run_id=self.goal_run_id,
                owner_user_id=self.owner_user_id,
                version=self.version,
                fencing_token=self.fencing_token,
                phase=GoalRunPhase.INITIALIZING,
                status=GoalRunStatus.STARTING,
                source_id="public-ats",
                mode=self.mode,
            )
        return self.snapshot

    def _set_snapshot(
        self,
        *,
        phase: GoalRunPhase,
        status: GoalRunStatus,
        review_decision: GoalRunReviewDecision | None = None,
        pending_review_item_id: UUID | None = None,
        pending_review_snapshot_sha256: str | None = None,
    ) -> None:
        current = self._snapshot()
        self.version += 1
        self.snapshot = replace(
            current,
            version=self.version,
            phase=phase,
            status=status,
            review_decision=review_decision,
            pending_review_item_id=pending_review_item_id,
            pending_review_snapshot_sha256=pending_review_snapshot_sha256,
        )


def _field(command: Any, name: str) -> Any:
    if isinstance(command, dict):
        return command[name]
    return getattr(command, name)


def _phase(value: Any) -> GoalRunPhase:
    if isinstance(value, GoalRunPhase):
        return value
    return GoalRunPhase(value)


def _status(value: Any) -> GoalRunStatus:
    if isinstance(value, GoalRunStatus):
        return value
    return GoalRunStatus(value)
