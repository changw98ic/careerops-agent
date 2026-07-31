"""Registration + shape tests for the Phase 2 trigger workflows.

These workflows are thin activity wrappers; the load-bearing assertion is that
``build_worker`` registers them on the ``careerops-m0`` task queue so the
schedules fired by ``loop_bootstrap`` have a worker that can execute them.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from temporalio.client import Client


def test_trigger_workflows_are_temporal_defs() -> None:
    from careerops.workflows.loop_trigger_workflows import (
        ApprovalSweepWorkflow,
        MailSyncTriggerWorkflow,
        OutboxDrainWorkflow,
    )

    for cls in (OutboxDrainWorkflow, ApprovalSweepWorkflow, MailSyncTriggerWorkflow):
        # @workflow.defn attaches this marker; absence means the decorator was lost.
        assert hasattr(cls, "__temporal_workflow_definition"), cls.__name__


def test_build_worker_registers_trigger_workflows(monkeypatch) -> None:
    import careerops.infrastructure.temporal.worker as worker_mod

    captured: dict = {}

    class FakeWorker:
        def __init__(self, client, **kwargs) -> None:
            captured["workflows"] = kwargs.get("workflows", [])

    monkeypatch.setattr(worker_mod, "Worker", FakeWorker)
    client = MagicMock(spec=Client)
    worker_mod.build_worker(client, worker_mod.TemporalWorkerSettings())

    names = {getattr(w, "__name__", "") for w in captured["workflows"]}
    assert "OutboxDrainWorkflow" in names
    assert "ApprovalSweepWorkflow" in names
    assert "MailSyncTriggerWorkflow" in names
