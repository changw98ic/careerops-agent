"""Section 5 Temporal activities: crawl execution and scheduled-run creation.

Wraps :class:`CrawlExecutionService.execute` and
:class:`CrawlRunService.run_now` as Temporal activities with proper error
classification.

Retry semantics (task 5.4):
- ``CrawlExecutionService.execute`` handles per-source transient failures
  (network timeout, 5xx) internally by counting them as ``failed`` and
  continuing.  The activity-level exception means a top-level failure
  (e.g. DB error during terminal-state update) that is NOT safe to retry
  because the run may already be in a terminal state.  Such errors are
  raised as ``ApplicationError(non_retryable=True)``.
- ``create_scheduled_run`` creates (or reuses via idempotency) a PENDING
  run.  A DB transient error here IS retryable — Temporal will re-invoke
  the activity and ``run_now``'s idempotency ensures no duplicate runs.

Activity implementations are Protocol-typed so tests can substitute NoOp
or mock sinks.  The concrete ``CrawlExecutionService`` and
``CrawlRunService`` are injected at construction time (same pattern as
``M1CrawlActivities``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol
from uuid import UUID

from temporalio import activity
from temporalio.exceptions import ApplicationError

from careerops.api.errors import InvalidStateError
from careerops.domain.crawl import CrawlRunState
from careerops.domain.crawl_plans import CrawlRun
from careerops.workflows.s5_contracts import (
    CREATE_SCHEDULED_RUN_ACTIVITY,
    EXECUTE_CRAWL_RUN_ACTIVITY,
    CrawlRunExecuteInput,
    CrawlRunExecuteResult,
    CreateScheduledRunInput,
    CreateScheduledRunResult,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols (activity-side service seams)
# ---------------------------------------------------------------------------


class CrawlExecutor(Protocol):
    """Activity-side seam for crawl execution."""

    async def execute(
        self,
        owner_id: UUID,
        run_id: UUID,
        *,
        now: datetime | None = None,
    ) -> CrawlRun: ...


class ScheduledRunCreator(Protocol):
    """Activity-side seam for creating a scheduled PENDING run."""

    def run_now(
        self,
        owner_id: UUID,
        *,
        now: datetime | None = None,
    ) -> CrawlRun: ...


# ---------------------------------------------------------------------------
# NoOp sinks (tests / degraded mode)
# ---------------------------------------------------------------------------


class NoOpCrawlExecutor:
    """Returns a synthetic succeeded result without performing I/O."""

    async def execute(
        self,
        owner_id: UUID,
        run_id: UUID,
        *,
        now: datetime | None = None,
    ) -> CrawlRun:
        return CrawlRun(
            id=run_id,
            plan_version_id=UUID("00000000-0000-0000-0000-000000000000"),
            run_identity="noop",
            state=CrawlRunState.SUCCEEDED,
        )


class NoOpScheduledRunCreator:
    """Returns a synthetic PENDING run without performing I/O."""

    def run_now(
        self,
        owner_id: UUID,
        *,
        now: datetime | None = None,
    ) -> CrawlRun:
        from uuid import uuid4

        return CrawlRun(
            id=uuid4(),
            plan_version_id=uuid4(),
            run_identity="noop-scheduled",
            state=CrawlRunState.PENDING,
        )


class _UnavailableCrawlExecutor:
    """Fail closed when a worker was built without the real crawl service."""

    async def execute(
        self,
        owner_id: UUID,
        run_id: UUID,
        *,
        now: datetime | None = None,
    ) -> CrawlRun:
        del owner_id, run_id, now
        raise RuntimeError("crawl executor is not wired")


class _UnavailableScheduledRunCreator:
    """Fail closed when scheduled-run persistence was not injected."""

    def run_now(
        self,
        owner_id: UUID,
        *,
        now: datetime | None = None,
    ) -> CrawlRun:
        del owner_id, now
        raise RuntimeError("scheduled run creator is not wired")


# ---------------------------------------------------------------------------
# Activity bundles (injectable into the worker)
# ---------------------------------------------------------------------------


class S5CrawlExecutionActivities:
    """Activities for crawl execution (task 5.4).

    ``execute_crawl_run`` wraps :class:`CrawlExecutionService.execute`.
    ``create_scheduled_run`` wraps :class:`CrawlRunService.run_now`.
    """

    def __init__(
        self,
        executor: CrawlExecutor | None = None,
        run_creator: ScheduledRunCreator | None = None,
    ) -> None:
        # Explicit NoOp implementations remain available for isolated unit
        # tests, but a production worker with missing wiring must fail closed
        # instead of manufacturing a successful crawl result.
        self._executor: CrawlExecutor = executor or _UnavailableCrawlExecutor()
        self._run_creator: ScheduledRunCreator = run_creator or _UnavailableScheduledRunCreator()

    @activity.defn(name=EXECUTE_CRAWL_RUN_ACTIVITY)
    async def execute_crawl_run(self, request: CrawlRunExecuteInput) -> CrawlRunExecuteResult:
        """Execute a PENDING crawl run through to terminal state.

        All per-source transient failures (network timeout, 5xx, policy
        denials) are handled internally by
        :meth:`CrawlExecutionService.execute` — they appear as non-zero
        ``counters.failed`` in the result, NOT as exceptions.

        An exception from ``execute()`` means a top-level failure (e.g. the
        run was already in a non-PENDING state, or the DB failed during a
        terminal-state update).  These are raised as
        ``ApplicationError(non_retryable=True)`` so Temporal does NOT retry
        — the run is already in a terminal or inconsistent state and a retry
        would either hit the same error or violate the idempotency contract.
        """
        owner_id = UUID(request.owner_id)
        run_id = UUID(request.run_id)

        activity.logger.info("executing crawl run %s for owner %s", run_id, owner_id)

        try:
            run = await self._executor.execute(owner_id, run_id)
        except InvalidStateError as exc:
            # Non-retryable: run is not PENDING (already RUNNING/terminal).
            activity.logger.warning("crawl run %s not executable: %s", run_id, exc)
            raise ApplicationError(str(exc), non_retryable=True, type="InvalidStateError") from exc
        except Exception as exc:
            # Top-level failure: the run may already be in a terminal state.
            # Retrying could create a duplicate or hit the same error.
            activity.logger.exception("crawl run %s failed with unexpected error", run_id)
            raise ApplicationError(
                f"crawl execution failed: {type(exc).__name__}",
                non_retryable=True,
                type=type(exc).__name__,
            ) from exc

        # Extract counters from the domain object.
        counters = {
            "discovered": run.counters.discovered,
            "updated": run.counters.updated,
            "closed": run.counters.closed,
            "failed": run.counters.failed,
        }

        result = CrawlRunExecuteResult(
            run_id=str(run.id),
            state=run.state.value,
            counters=counters,
            error_category=run.error_category,
            next_eligible_at=(run.next_eligible_at.isoformat() if run.next_eligible_at else None),
        )

        activity.logger.info(
            "crawl run %s completed: state=%s discovered=%d updated=%d failed=%d",
            run_id,
            result.state,
            counters["discovered"],
            counters["updated"],
            counters["failed"],
        )
        return result

    @activity.defn(name=CREATE_SCHEDULED_RUN_ACTIVITY)
    async def create_scheduled_run(
        self, request: CreateScheduledRunInput
    ) -> CreateScheduledRunResult:
        """Create (or reuse) a PENDING run for the active plan.

        Calls ``CrawlRunService.run_now`` which is idempotent on the
        plan-version: if a PENDING/RUNNING run already exists, it is
        returned.  A plan without an active version or without eligible
        sources raises ``InvalidStateError`` — the workflow treats this
        as a skip (no run created).
        """
        owner_id = UUID(request.owner_id)
        activity.logger.info("creating scheduled run for owner %s", owner_id)

        try:
            run = self._run_creator.run_now(owner_id)
        except InvalidStateError as exc:
            # Plan paused, no eligible sources, etc. — skip, don't retry.
            activity.logger.info("scheduled run skipped for owner %s: %s", owner_id, exc)
            return CreateScheduledRunResult(run_created=False, error=str(exc))
        except Exception as exc:
            # DB transient error — retryable.  Let Temporal retry.
            activity.logger.exception("create_scheduled_run failed for owner %s", owner_id)
            raise ApplicationError(
                f"failed to create scheduled run: {type(exc).__name__}",
                non_retryable=False,
                type=type(exc).__name__,
            ) from exc

        run_id = str(run.id)
        activity.logger.info(
            "scheduled run created: run_id=%s state=%s",
            run_id,
            run.state.value,
        )
        return CreateScheduledRunResult(run_created=True, run_id=run_id)
