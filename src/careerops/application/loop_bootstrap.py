"""Startup bootstrap for the self-driving trigger loop (Phase 2).

On API startup this idempotently registers the Temporal Schedules that drive
the autonomous loop:

- ``outbox-drain``   -- re-deliver pending outbox events (always; internal).
- ``approval-sweep`` -- expire stale human-review approvals (always; internal).
- ``mail-sync``      -- incremental inbound Gmail pull (only when
  ``google_oauth_enabled`` is on AND a mail account exists).
- per-source crawl   -- via ``CrawlActivationService`` (only when the crawl
  readiness gate passes): registers each eligible source's schedule and
  resumes it, bounded by the Tier 2 budget and per-source eligibility.

Everything is idempotent (``ScheduleManager.ensure_schedule`` reconciles), so
calling this on every API restart converges without duplicate side effects.
Owner/account identity is resolved defensively from existing data (crawl plan
owner / mail account); it does NOT depend on the console-login layer.

Fail-closed: a missing dependency or failed gate skips that one schedule and
logs the reason; the call never raises (the API must still serve).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

import sqlalchemy as sa

from careerops.application.crawl_readiness import check_crawl_readiness
from careerops.infrastructure.database.schema import crawl_plan_versions, email_accounts

_log = logging.getLogger(__name__)

# Cadence defaults (tasks.md 2026-07-30 plan).
MAIL_SYNC_INTERVAL = timedelta(minutes=5)
OUTBOX_DRAIN_INTERVAL = timedelta(seconds=30)
APPROVAL_SWEEP_INTERVAL = timedelta(seconds=60)

MAIN_TASK_QUEUE = "careerops-m0"


@dataclass
class BootstrapReport:
    """Outcome of one bootstrap pass; safe to surface on app.state."""

    mail_registered: bool = False
    outbox_registered: bool = False
    sweep_registered: bool = False
    crawl_schedules_considered: int = 0
    crawl_activated: int = 0
    skipped: list[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"mail={self.mail_registered} outbox={self.outbox_registered} "
            f"sweep={self.sweep_registered} crawl={self.crawl_activated}/"
            f"{self.crawl_schedules_considered} skipped={self.skipped}"
        )


def _resolve_owner_id(engine) -> UUID | None:
    """Return the single crawl owner, or None if there is not exactly one."""
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(crawl_plan_versions.c.owner_id).distinct().limit(2)
        ).all()
    if len(rows) != 1:
        return None
    return UUID(str(rows[0][0]))


def _resolve_mail_account(engine) -> tuple[UUID, UUID] | None:
    """Return ``(candidate_id, account_id)`` for the single mail account."""
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(email_accounts.c.candidate_id, email_accounts.c.id).limit(2)
        ).all()
    if len(rows) != 1:
        return None
    return UUID(str(rows[0][0])), UUID(str(rows[0][1]))


async def bootstrap_trigger_loop(
    settings,
    runtime,
    *,
    schedule_manager,
    crawl_activation_service=None,
) -> BootstrapReport:
    """Reconcile all managed schedules. Never raises.

    Args:
        settings: process ``Settings`` (reads ``google_oauth_enabled``).
        runtime: ``RuntimeResources`` (provides ``database`` engine, the crawl
            source queue, the Tier 2 budget, and the permission repo).
        schedule_manager: an injected ``ScheduleManager`` (so tests can fake it).
        crawl_activation_service: optional ``CrawlActivationService``. When
            ``None``, crawl schedules are skipped (mail/outbox/sweep still run).
    """
    from careerops.workflows.loop_trigger_workflows import (
        ApprovalSweepWorkflow,
        MailSyncTriggerWorkflow,
        OutboxDrainWorkflow,
    )
    from careerops.workflows.mail_sync_contracts import MailSyncFetchInput

    report = BootstrapReport()

    # --- outbox drain + approval sweep: always (pure internal cleanup) ---
    await schedule_manager.ensure_schedule(
        schedule_id="outbox-drain",
        workflow=OutboxDrainWorkflow,
        arg=None,
        interval=OUTBOX_DRAIN_INTERVAL,
        task_queue=MAIN_TASK_QUEUE,
        note="drain pending outbox events",
    )
    report.outbox_registered = True
    await schedule_manager.ensure_schedule(
        schedule_id="approval-sweep",
        workflow=ApprovalSweepWorkflow,
        arg=None,
        interval=APPROVAL_SWEEP_INTERVAL,
        task_queue=MAIN_TASK_QUEUE,
        note="expire stale human-review approvals",
    )
    report.sweep_registered = True

    # --- mail sync: gated on oauth flag + a connected account existing ---
    if getattr(settings, "google_oauth_enabled", False):
        account = _resolve_mail_account(runtime.database)
        if account is not None:
            candidate_id, account_id = account
            await schedule_manager.ensure_schedule(
                schedule_id="mail-sync",
                workflow=MailSyncTriggerWorkflow,
                arg=MailSyncFetchInput(
                    candidate_id=str(candidate_id),
                    account_id=str(account_id),
                    start_history_id="",
                ),
                interval=MAIL_SYNC_INTERVAL,
                task_queue=MAIN_TASK_QUEUE,
                note="incremental inbound mail sync",
            )
            report.mail_registered = True
        else:
            report.skipped.append("mail-sync: no mail account configured")
    else:
        report.skipped.append("mail-sync: google_oauth_enabled is off")

    # --- crawl: register + activate eligible sources after the readiness gate ---
    if crawl_activation_service is None:
        report.skipped.append("crawl: activation service not provided")
        _log.info("trigger-loop bootstrap: %s", report)
        return report

    owner_id = _resolve_owner_id(runtime.database)
    if owner_id is None:
        report.skipped.append("crawl: no single owner resolved (0 or >1)")
        _log.info("trigger-loop bootstrap: %s", report)
        return report

    readiness = check_crawl_readiness(
        runtime.database,
        budget_max_concurrent_slots=runtime.tier2_budget.max_concurrent_slots,
        budget_daily_action_budget=runtime.tier2_budget.daily_action_budget,
        permission_repository=runtime.crawl_permission_repo,
    )
    if not readiness.ready:
        report.skipped.append(f"crawl: readiness gate failed ({'; '.join(readiness.failures)})")
        _log.info("trigger-loop bootstrap: %s", report)
        return report

    sources = runtime.source_queue_service.select_sources(owner_id)
    result = await crawl_activation_service.activate_crawl_schedules(
        readiness, owner_id, sources
    )
    report.crawl_schedules_considered = result.sources_considered
    report.crawl_activated = result.schedules_activated
    _log.info("trigger-loop bootstrap: %s", report)
    return report
