"""Thin trigger workflows that let a Temporal Schedule fire an activity.

``drain_outbox``, ``sweep_expired_approvals`` and ``fetch_and_sync`` are
activity-only paths; a Temporal Schedule can only fire a *workflow*, so each
gets a zero/one-arg wrapper that calls exactly one activity. This mirrors the
existing ``CrawlScheduledWorkflow`` pattern in ``s5_workflows.py``.

Two constraints shaped this module:

1. **No import of ``careerops.infrastructure.temporal.activities``.** That
   module pulls in the outbox / side-effect kernel (and transitively
   ``urllib``). Importing it -- whether at module top or lazily inside ``run``
   -- breaks the Temporal workflow sandbox (``RestrictedWorkflowAccessError``)
   at execution time, and a top-level import also cycles through the worker.
   So the two activity-name strings are defined locally below, mirroring the
   ``@activity.defn(name=...)`` values in ``activities.py``.

2. **``run`` accepts the schedule arg.** A Schedule always passes its ``arg``
   to ``run``, so the arg-less triggers accept and ignore it.

These workflows are registered on the ``careerops-m0`` task queue (see
``worker.build_worker``); the schedules that fire them are reconciled by
``application.loop_bootstrap.bootstrap_trigger_loop`` on API startup.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

from careerops.workflows.mail_sync_contracts import (
    MAIL_SYNC_FETCH_ACTIVITY,
    MailSyncFetchInput,
    MailSyncFetchResult,
)

# Activity name strings -- mirrors of the ``@activity.defn(name=...)`` values
# in ``careerops.infrastructure.temporal.activities``. Kept local (not imported)
# so this workflow module never enters the heavy activities import graph; see
# the module docstring constraint #1.
OUTBOX_DRAIN_ACTIVITY = "outbox_drain"
APPROVAL_SWEEPER_ACTIVITY = "approval_sweeper"

_OUTBOX_TIMEOUT = timedelta(seconds=60)
_MAIL_SYNC_TIMEOUT = timedelta(seconds=120)


@workflow.defn
class OutboxDrainWorkflow:
    """Fire ``drain_outbox`` on a schedule to re-deliver pending outbox events.

    ``arg`` is the schedule input Temporal always passes to a scheduled
    workflow's ``run``; this trigger has no input, so it is accepted and
    ignored (passing ``arg=None`` from the bootstrap).
    """

    @workflow.run
    async def run(self, arg: object = None) -> dict[str, int]:
        return await workflow.execute_activity(
            OUTBOX_DRAIN_ACTIVITY,
            start_to_close_timeout=_OUTBOX_TIMEOUT,
        )


@workflow.defn
class ApprovalSweepWorkflow:
    """Fire ``sweep_expired_approvals`` on a schedule (expire stale approvals).

    See ``OutboxDrainWorkflow``: ``arg`` is the ignored schedule input.
    """

    @workflow.run
    async def run(self, arg: object = None) -> int:
        return await workflow.execute_activity(
            APPROVAL_SWEEPER_ACTIVITY,
            start_to_close_timeout=_OUTBOX_TIMEOUT,
        )


@workflow.defn
class MailSyncTriggerWorkflow:
    """Fire ``fetch_and_sync`` on a schedule to pull inbound Gmail incrementally."""

    @workflow.run
    async def run(self, request: MailSyncFetchInput) -> MailSyncFetchResult:
        return await workflow.execute_activity(
            MAIL_SYNC_FETCH_ACTIVITY,
            request,
            start_to_close_timeout=_MAIL_SYNC_TIMEOUT,
        )
