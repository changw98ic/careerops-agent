"""Startup bootstrap for the self-driving trigger loop (Phase 2).

On API startup this idempotently registers the Temporal Schedules that drive
the autonomous loop:

- ``outbox-drain``   -- re-deliver pending outbox events (always; internal).
- ``approval-sweep`` -- expire stale human-review approvals (always; internal).
- ``mail-sync:<candidate_id>`` -- incremental inbound Gmail pull, one schedule
  per connected mail account (only when ``google_oauth_enabled`` is on AND
  connected accounts exist).
- per-source crawl   -- via ``CrawlActivationService`` (only when the crawl
  readiness gate passes): for EVERY candidate, registers each eligible
  source's schedule and resumes it, bounded by the Tier 2 budget and
  per-source eligibility.  Each candidate id doubles as that candidate's
  crawl owner id, and only the sources selected by that candidate's ACTIVE
  crawl plan are activated (the ``job_sources`` registry is a global table
  with no owner column, so the candidate's plan is what scopes the loop).
  Candidates without an active plan get no crawl schedules.
- legacy cleanup    -- deletes the pre-multi-candidate ``crawl:{source_id}``
  schedules and the fixed ``mail-sync`` schedule (when the injected
  ``ScheduleManager`` can list schedules), so upgraded deployments do not
  double-run old schedules next to the per-candidate ones.

Everything is idempotent (``ScheduleManager.ensure_schedule`` reconciles), so
calling this on every API restart converges without duplicate side effects.
Candidate/account identity is resolved defensively from existing data (the
``candidates`` and ``email_accounts`` tables); it does NOT depend on the
console-login layer.

Fail-closed: a missing dependency or failed gate skips that one schedule and
logs the reason; the call never raises (the API must still serve).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

import sqlalchemy as sa

from careerops.application.crawl_readiness import check_crawl_readiness
from careerops.infrastructure.database.schema import candidates, email_accounts

_log = logging.getLogger(__name__)

# Cadence defaults (tasks.md 2026-07-30 plan).
MAIL_SYNC_INTERVAL = timedelta(minutes=5)
OUTBOX_DRAIN_INTERVAL = timedelta(seconds=30)
APPROVAL_SWEEP_INTERVAL = timedelta(seconds=60)

MAIN_TASK_QUEUE = "careerops-m0"

# Pre-multi-candidate schedule formats removed by this branch. The cleanup step
# deletes them so upgraded deployments do not double-run:
# - ``crawl:{source_id}`` (one bare uuid after ``crawl:``) -- the candidate was
#   absent from the id, so the last candidate to bootstrap overwrote everyone
#   else's schedule. New format: ``crawl:{candidate_id}:{source_id}``.
# - ``mail-sync`` (fixed id, no candidate) -- new format: ``mail-sync:{candidate_id}``.
_LEGACY_CRAWL_ID_RE = re.compile(r"^crawl:[0-9a-fA-F-]{36}$")
_LEGACY_MAIL_SYNC_ID = "mail-sync"


def is_legacy_schedule_id(schedule_id: str) -> bool:
    """Return True for schedule ids that predate the multi-candidate format.

    The legacy crawl schedule id is exactly ``crawl:{uuid}`` (no candidate in
    the id); the legacy mail-sync schedule is the fixed ``mail-sync`` id.
    """
    if schedule_id == _LEGACY_MAIL_SYNC_ID:
        return True
    return bool(_LEGACY_CRAWL_ID_RE.fullmatch(schedule_id))


@dataclass
class BootstrapReport:
    """Outcome of one bootstrap pass; safe to surface on app.state."""

    mail_registered: bool = False
    outbox_registered: bool = False
    sweep_registered: bool = False
    crawl_schedules_considered: int = 0
    crawl_activated: int = 0
    legacy_schedules_deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"mail={self.mail_registered} outbox={self.outbox_registered} "
            f"sweep={self.sweep_registered} crawl={self.crawl_activated}/"
            f"{self.crawl_schedules_considered} legacy_deleted="
            f"{len(self.legacy_schedules_deleted)} skipped={self.skipped}"
        )


def _list_all_candidates(engine) -> list[UUID]:
    """Return the id of every candidate row (each id is a crawl owner).

    The ``candidates`` table is the source of truth for the candidate set;
    crawl plans / mail accounts are scoped per candidate and need not exist.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(candidates.c.id).order_by(candidates.c.id)
        ).all()
    return [UUID(str(row[0])) for row in rows]


def _list_connected_mail_accounts(engine) -> list[tuple[UUID, UUID]]:
    """Return ``(candidate_id, account_id)`` for every connected account.

    Only ``connection_state = 'connected'`` accounts are usable by the
    mail-sync worker (anything else raises ``AccountNotConnectedError``), so
    fail-closed: register a schedule only for accounts that can sync.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(email_accounts.c.candidate_id, email_accounts.c.id)
            .where(
                email_accounts.c.candidate_id.isnot(None),
                email_accounts.c.connection_state == "connected",
            )
            .order_by(email_accounts.c.candidate_id, email_accounts.c.id)
        ).all()
    return [(UUID(str(row[0])), UUID(str(row[1]))) for row in rows]


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
    # One schedule per connected account, keyed by the owning candidate
    # (dedicated-account model: at most one account per candidate).
    if getattr(settings, "google_oauth_enabled", False):
        accounts = _list_connected_mail_accounts(runtime.database)
        if accounts:
            for candidate_id, account_id in accounts:
                await schedule_manager.ensure_schedule(
                    schedule_id=f"mail-sync:{candidate_id}",
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

    # --- legacy-schedule cleanup (only when the manager can enumerate) ---
    # Upgraded deployments may still hold pre-multi-candidate schedules
    # (``crawl:{source_id}`` and the fixed ``mail-sync``); delete them so they
    # do not double-run next to the per-candidate schedules registered below.
    list_ids = getattr(schedule_manager, "list_schedule_ids", None)
    if callable(list_ids):
        try:
            existing_ids = await list_ids()
            for schedule_id in existing_ids:
                if is_legacy_schedule_id(schedule_id):
                    await schedule_manager.delete(schedule_id)
                    report.legacy_schedules_deleted.append(schedule_id)
                    _log.info("trigger-loop bootstrap: deleted legacy schedule %s", schedule_id)
        except Exception:  # cleanup is best-effort; never crash the API
            _log.exception("trigger-loop bootstrap: legacy-schedule cleanup failed")

    # --- crawl: register + activate eligible sources after the readiness gate ---
    if crawl_activation_service is None:
        report.skipped.append("crawl: activation service not provided")
        _log.info("trigger-loop bootstrap: %s", report)
        return report

    candidate_ids = _list_all_candidates(runtime.database)
    if not candidate_ids:
        report.skipped.append("crawl: no candidates")
        _log.info("trigger-loop bootstrap: %s", report)
        return report

    # Readiness gate is global (migrations, crawler, permissions, budgets) --
    # compute once and reuse for every candidate.
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

    plan_repo = getattr(runtime, "crawl_plan_repo", None)
    sources_considered = 0
    schedules_activated = 0
    for candidate_id in candidate_ids:
        # Per-candidate isolation: one candidate's plan read / selection /
        # activation failure must not abort the whole round (log + continue).
        try:
            # The source registry is GLOBAL (no owner column), so the
            # candidate's ACTIVE plan is what scopes the loop: only sources
            # selected by that candidate's plan get schedules. A candidate
            # without an active plan gets no crawl schedules.
            active_plan = plan_repo.get_active_for(candidate_id) if plan_repo is not None else None
            if active_plan is None:
                report.skipped.append(f"crawl: candidate {candidate_id} has no active plan")
                continue
            plan_source_ids = set(getattr(active_plan, "sources", ()) or ())
            sources = runtime.source_queue_service.select_sources(candidate_id)
            sources = [s for s in sources if s.id in plan_source_ids]
            result = await crawl_activation_service.activate_crawl_schedules(
                readiness, candidate_id, sources
            )
            sources_considered += result.sources_considered
            schedules_activated += result.schedules_activated
        except Exception:  # one candidate must not kill the round
            _log.exception("trigger-loop bootstrap: crawl registration failed for %s", candidate_id)
            report.skipped.append(f"crawl: candidate {candidate_id} failed (see log)")
    report.crawl_schedules_considered = sources_considered
    report.crawl_activated = schedules_activated
    _log.info("trigger-loop bootstrap: %s", report)
    return report
