from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta

import pytest
from temporalio.api.enums.v1.task_queue_pb2 import (
    TASK_QUEUE_TYPE_ACTIVITY,
    TASK_QUEUE_TYPE_WORKFLOW,
)
from temporalio.api.taskqueue.v1 import PollerInfo
from temporalio.api.workflowservice.v1 import (
    DescribeTaskQueueRequest,
    DescribeTaskQueueResponse,
)

from careerops.infrastructure.temporal.health import (
    TemporalWorkerHealthResult,
    _amain,
    check_worker_health,
)
from careerops.infrastructure.temporal.worker import TemporalWorkerSettings

WORKFLOW_TYPE = TASK_QUEUE_TYPE_WORKFLOW
ACTIVITY_TYPE = TASK_QUEUE_TYPE_ACTIVITY


class FakeWorkflowService:
    def __init__(
        self,
        *,
        workflow_pollers: tuple[str, ...] = ("worker-1",),
        activity_pollers: tuple[str, ...] = ("worker-1",),
    ) -> None:
        self._pollers = {
            WORKFLOW_TYPE: workflow_pollers,
            ACTIVITY_TYPE: activity_pollers,
        }
        self.requests: list[DescribeTaskQueueRequest] = []
        self.timeouts: list[timedelta | None] = []

    async def describe_task_queue(
        self,
        req: DescribeTaskQueueRequest,
        retry: bool = False,
        metadata: Mapping[str, str | bytes] | None = None,
        timeout: timedelta | None = None,
    ) -> DescribeTaskQueueResponse:
        assert retry is False
        assert metadata is None
        self.requests.append(req)
        self.timeouts.append(timeout)
        return DescribeTaskQueueResponse(
            pollers=[
                PollerInfo(identity=identity)
                for identity in self._pollers.get(req.task_queue_type, ())
            ]
        )


class FakeTemporalClient:
    def __init__(self, service: FakeWorkflowService) -> None:
        self._service = service

    @property
    def workflow_service(self) -> FakeWorkflowService:
        return self._service


def test_worker_health_describes_workflow_and_activity_task_queues() -> None:
    service = FakeWorkflowService()
    settings = TemporalWorkerSettings(
        target="temporal.internal:7233",
        namespace="careerops",
        task_queue="careerops-m0",
    )

    async def factory(received: TemporalWorkerSettings) -> FakeTemporalClient:
        assert received == settings
        return FakeTemporalClient(service)

    result = asyncio.run(
        check_worker_health(settings, timeout_seconds=1.25, client_factory=factory)
    )

    assert result == TemporalWorkerHealthResult(
        healthy=True,
        reason="task queue has workflow and activity pollers",
        workflow_pollers=1,
        activity_pollers=1,
    )
    assert [request.task_queue_type for request in service.requests] == [
        WORKFLOW_TYPE,
        ACTIVITY_TYPE,
    ]
    assert [request.namespace for request in service.requests] == ["careerops", "careerops"]
    assert [request.task_queue.name for request in service.requests] == [
        "careerops-m0",
        "careerops-m0",
    ]
    assert service.timeouts == [timedelta(seconds=1.25), timedelta(seconds=1.25)]


def test_worker_health_requires_both_task_types_for_configured_identity() -> None:
    service = FakeWorkflowService(
        workflow_pollers=("expected-worker", "other-worker"),
        activity_pollers=("other-worker",),
    )
    settings = TemporalWorkerSettings(identity="expected-worker")

    async def factory(_settings: TemporalWorkerSettings) -> FakeTemporalClient:
        return FakeTemporalClient(service)

    result = asyncio.run(check_worker_health(settings, client_factory=factory))

    assert result.healthy is False
    assert result.reason == "configured worker identity is not polling activity tasks"
    assert result.workflow_pollers == 2
    assert result.activity_pollers == 1


def test_worker_health_fails_when_either_task_type_has_no_pollers() -> None:
    service = FakeWorkflowService(workflow_pollers=(), activity_pollers=("worker-1",))
    settings = TemporalWorkerSettings()

    async def factory(_settings: TemporalWorkerSettings) -> FakeTemporalClient:
        return FakeTemporalClient(service)

    result = asyncio.run(check_worker_health(settings, client_factory=factory))

    assert result.healthy is False
    assert result.reason == "task queue has no workflow pollers"
    assert result.workflow_pollers == 0
    assert result.activity_pollers == 1


def test_worker_health_times_out_without_leaking_configuration() -> None:
    settings = TemporalWorkerSettings(target="temporal.internal:7233")

    async def factory(_settings: TemporalWorkerSettings) -> FakeTemporalClient:
        await asyncio.sleep(0.05)
        raise AssertionError("unreachable")

    result = asyncio.run(
        check_worker_health(settings, timeout_seconds=0.001, client_factory=factory)
    )

    assert result == TemporalWorkerHealthResult(
        healthy=False,
        reason="Temporal worker health check timed out",
        workflow_pollers=0,
        activity_pollers=0,
    )
    assert settings.target not in result.reason


def test_worker_health_reports_non_sensitive_exception_type_only() -> None:
    settings = TemporalWorkerSettings(target="temporal.internal:7233")

    async def factory(_settings: TemporalWorkerSettings) -> FakeTemporalClient:
        raise RuntimeError("secret temporal endpoint detail")

    result = asyncio.run(check_worker_health(settings, client_factory=factory))

    assert result.healthy is False
    assert result.reason == "Temporal worker health check failed: RuntimeError"
    assert "secret" not in result.reason
    assert settings.target not in result.reason


def test_worker_health_cli_exit_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_check(
        settings: TemporalWorkerSettings,
        *,
        timeout_seconds: float,
    ) -> TemporalWorkerHealthResult:
        assert settings.target == "temporal.internal:7233"
        assert timeout_seconds == 1.5
        return TemporalWorkerHealthResult(
            True,
            "task queue has workflow and activity pollers",
            1,
            1,
        )

    monkeypatch.setenv("CAREEROPS_TEMPORAL_ADDRESS", "temporal.internal:7233")
    monkeypatch.setenv("CAREEROPS_TEMPORAL_WORKER_HEALTH_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setattr("careerops.infrastructure.temporal.health.check_worker_health", fake_check)

    exit_code = asyncio.run(_amain())

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "temporal worker health: task queue has workflow and activity pollers\n"
    assert captured.err == ""


def test_worker_health_cli_fails_closed_on_bad_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CAREEROPS_TEMPORAL_WORKER_HEALTH_TIMEOUT_SECONDS", "0")

    exit_code = asyncio.run(_amain())

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "temporal worker health: Temporal worker health timeout must be positive\n"
    )
