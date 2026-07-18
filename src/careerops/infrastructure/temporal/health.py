from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, cast

from temporalio.api.enums.v1.task_queue_pb2 import (
    TASK_QUEUE_TYPE_ACTIVITY,
    TASK_QUEUE_TYPE_WORKFLOW,
    TaskQueueType,
)
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import (
    DescribeTaskQueueRequest,
    DescribeTaskQueueResponse,
)
from temporalio.client import Client

from careerops.infrastructure.temporal.worker import TemporalWorkerSettings

_DEFAULT_HEALTH_TIMEOUT_SECONDS = 3.0
_WORKFLOW_TASK_QUEUE_TYPE = TASK_QUEUE_TYPE_WORKFLOW
_ACTIVITY_TASK_QUEUE_TYPE = TASK_QUEUE_TYPE_ACTIVITY


class TemporalWorkflowService(Protocol):
    async def describe_task_queue(
        self,
        req: DescribeTaskQueueRequest,
        retry: bool = False,
        metadata: Mapping[str, str | bytes] | None = None,
        timeout: timedelta | None = None,
    ) -> DescribeTaskQueueResponse: ...


class TemporalHealthClient(Protocol):
    @property
    def workflow_service(self) -> TemporalWorkflowService: ...


TemporalClientFactory = Callable[[TemporalWorkerSettings], Awaitable[TemporalHealthClient]]


@dataclass(frozen=True, slots=True)
class TemporalWorkerHealthResult:
    healthy: bool
    reason: str
    workflow_pollers: int
    activity_pollers: int


async def check_worker_health(
    settings: TemporalWorkerSettings,
    *,
    timeout_seconds: float = _DEFAULT_HEALTH_TIMEOUT_SECONDS,
    client_factory: TemporalClientFactory | None = None,
) -> TemporalWorkerHealthResult:
    """Verify that this task queue has both workflow and activity pollers.

    When the worker identity is configured, both task-queue types must be polled
    by that exact identity. Otherwise at least one poller per task type is enough.
    """

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    factory = client_factory or _connect_client
    try:
        async with asyncio.timeout(timeout_seconds):
            client = await factory(settings)
            workflow_identities = await _poller_identities(
                client.workflow_service,
                settings=settings,
                task_queue_type=_WORKFLOW_TASK_QUEUE_TYPE,
                timeout_seconds=timeout_seconds,
            )
            activity_identities = await _poller_identities(
                client.workflow_service,
                settings=settings,
                task_queue_type=_ACTIVITY_TASK_QUEUE_TYPE,
                timeout_seconds=timeout_seconds,
            )
    except TimeoutError:
        return TemporalWorkerHealthResult(False, "Temporal worker health check timed out", 0, 0)
    except Exception as error:
        return TemporalWorkerHealthResult(
            False,
            f"Temporal worker health check failed: {type(error).__name__}",
            0,
            0,
        )

    return _evaluate_pollers(settings, workflow_identities, activity_identities)


async def _connect_client(settings: TemporalWorkerSettings) -> TemporalHealthClient:
    return cast(
        "TemporalHealthClient",
        await Client.connect(
            settings.target,
            namespace=settings.namespace,
            identity="careerops-worker-health",
        ),
    )


async def _poller_identities(
    workflow_service: TemporalWorkflowService,
    *,
    settings: TemporalWorkerSettings,
    task_queue_type: TaskQueueType.ValueType,
    timeout_seconds: float,
) -> tuple[str, ...]:
    response = await workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=settings.namespace,
            task_queue=TaskQueue(name=settings.task_queue),
            task_queue_type=task_queue_type,
            report_pollers=True,
        ),
        timeout=timedelta(seconds=timeout_seconds),
    )
    return tuple(poller.identity for poller in response.pollers if poller.identity)


def _evaluate_pollers(
    settings: TemporalWorkerSettings,
    workflow_identities: Sequence[str],
    activity_identities: Sequence[str],
) -> TemporalWorkerHealthResult:
    workflow_count = len(workflow_identities)
    activity_count = len(activity_identities)
    expected_identity = settings.identity

    if expected_identity is not None:
        missing: list[str] = []
        if expected_identity not in workflow_identities:
            missing.append("workflow")
        if expected_identity not in activity_identities:
            missing.append("activity")
        if missing:
            return TemporalWorkerHealthResult(
                False,
                f"configured worker identity is not polling {', '.join(missing)} tasks",
                workflow_count,
                activity_count,
            )
        return TemporalWorkerHealthResult(
            True,
            "configured worker identity is polling workflow and activity tasks",
            workflow_count,
            activity_count,
        )

    missing = []
    if workflow_count == 0:
        missing.append("workflow")
    if activity_count == 0:
        missing.append("activity")
    if missing:
        return TemporalWorkerHealthResult(
            False,
            f"task queue has no {', '.join(missing)} pollers",
            workflow_count,
            activity_count,
        )
    return TemporalWorkerHealthResult(
        True,
        "task queue has workflow and activity pollers",
        workflow_count,
        activity_count,
    )


def _timeout_from_environment(environ: Mapping[str, str] | None = None) -> float:
    values = os.environ if environ is None else environ
    raw_value = values.get("CAREEROPS_TEMPORAL_WORKER_HEALTH_TIMEOUT_SECONDS")
    if raw_value is None:
        return _DEFAULT_HEALTH_TIMEOUT_SECONDS
    try:
        timeout_seconds = float(raw_value)
    except ValueError as error:
        raise ValueError("Temporal worker health timeout must be numeric") from error
    if timeout_seconds <= 0:
        raise ValueError("Temporal worker health timeout must be positive")
    return timeout_seconds


async def _amain() -> int:
    try:
        settings = TemporalWorkerSettings.from_environment()
        timeout_seconds = _timeout_from_environment()
        result = await check_worker_health(settings, timeout_seconds=timeout_seconds)
    except ValueError as error:
        print(f"temporal worker health: {error}", file=sys.stderr)
        return 2

    stream = sys.stdout if result.healthy else sys.stderr
    print(f"temporal worker health: {result.reason}", file=stream)
    return 0 if result.healthy else 1


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
