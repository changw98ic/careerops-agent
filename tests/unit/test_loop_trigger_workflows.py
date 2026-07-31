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


def test_trigger_workflows_accept_the_schedule_arg() -> None:
    """A Temporal Schedule always passes its ``arg`` to ``run``.

    Found in live verification: arg-less ``run(self)`` workflows TypeError when
    the schedule passes ``arg=None``. Every trigger workflow's ``run`` must
    therefore accept at least one parameter (the schedule input).
    """
    import inspect

    from careerops.workflows.loop_trigger_workflows import (
        ApprovalSweepWorkflow,
        MailSyncTriggerWorkflow,
        OutboxDrainWorkflow,
    )

    for cls in (OutboxDrainWorkflow, ApprovalSweepWorkflow, MailSyncTriggerWorkflow):
        run = cls.run
        # @workflow.run may wrap the method; follow __wrapped__ if present.
        target = getattr(run, "__wrapped__", run)
        params = [p for p in inspect.signature(target).parameters if p != "self"]
        assert params, f"{cls.__name__}.run must accept the schedule arg (got none)"


def test_trigger_workflow_module_does_not_import_activities() -> None:
    """The workflow module must stay out of the heavy ``activities`` import graph.

    Found in live verification: importing ``activities`` (top-level or lazily
    inside ``run``) trips the Temporal workflow sandbox
    (``RestrictedWorkflowAccessError`` on urllib) at execution time, and a
    top-level import also cycles through the worker. The activity-name strings
    are local constants instead.
    """
    import sys

    # Ensure a clean import of the workflow module.
    for mod in list(sys.modules):
        if mod.startswith("careerops.workflows.loop_trigger_workflows"):
            del sys.modules[mod]
    activities_name = "careerops.infrastructure.temporal.activities"
    previously_loaded_activities = sys.modules.pop(activities_name, None)

    try:
        import careerops.workflows.loop_trigger_workflows as ltw  # noqa: F401

        assert activities_name not in sys.modules, (
            "loop_trigger_workflows pulled in the activities module -- this breaks "
            "the Temporal workflow sandbox at execution time"
        )
    finally:
        if previously_loaded_activities is not None:
            sys.modules[activities_name] = previously_loaded_activities


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
