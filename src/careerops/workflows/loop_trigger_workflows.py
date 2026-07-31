"""Thin trigger workflows that let a Temporal Schedule fire an activity.

``drain_outbox``, ``sweep_expired_approvals`` and ``fetch_and_sync`` are
activity-only paths; a Temporal Schedule can only fire a *workflow*, so each
gets a zero/one-arg wrapper that calls exactly one activity. This mirrors the
existing ``CrawlScheduledWorkflow`` pattern in ``s5_workflows.py``.

The activity-name constants are imported lazily inside ``run`` (not at module
top) to avoid an import cycle: importing
``careerops.infrastructure.temporal.activities`` runs the ``temporal`` package
initializer, which loads ``worker``, which imports this module. Deferring the
constant lookup to workflow-execution time breaks that cycle.

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

_OUTBOX_TIMEOUT = timedelta(seconds=60)
_MAIL_SYNC_TIMEOUT = timedelta(seconds=120)


@workflow.defn
class OutboxDrainWorkflow:
    """Fire ``drain_outbox`` on a schedule to re-deliver pending outbox events."""

    @workflow.run
    async def run(self) -> dict[str, int]:
        from careerops.infrastructure.temporal.activities import OUTBOX_DRAIN_ACTIVITY

        return await workflow.execute_activity(
            OUTBOX_DRAIN_ACTIVITY,
            start_to_close_timeout=_OUTBOX_TIMEOUT,
        )


@workflow.defn
class ApprovalSweepWorkflow:
    """Fire ``sweep_expired_approvals`` on a schedule (expire stale approvals)."""

    @workflow.run
    async def run(self) -> int:
        from careerops.infrastructure.temporal.activities import APPROVAL_SWEEPER_ACTIVITY

        return await workflow.execute_activity(
            APPROVAL_SWEEPER_ACTIVITY,
            start_to_close_timeout=_OUTBOX_TIMEOUT,
        )


@workflow.defn
class MailSyncTriggerWorkflow:
    """Fire ``fetch_and_sync`` on a schedule to pull inbound Gmail incrementally."""

    @workflow.run
    async def run(self, request: MailSyncFetchInput) -> MailSyncFetchResult:
        # MAIL_SYNC_FETCH_ACTIVITY is a plain string constant safe to import
        # at module top (mail_sync_contracts has no infrastructure dependency).
        return await workflow.execute_activity(
            MAIL_SYNC_FETCH_ACTIVITY,
            request,
            start_to_close_timeout=_MAIL_SYNC_TIMEOUT,
        )
