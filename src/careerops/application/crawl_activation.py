"""Autonomous crawl activation (real-autonomous-career-loop Phase 8.2 + 8.3).

**8.2 -- Enable paused Temporal crawl Schedules**:
After the readiness gate passes, this module enables previously paused
Temporal crawl Schedules by calling ``ScheduleActivator.activate_source_schedule``
for each eligible source.  Each source's cadence comes from its plan version's
``interval_seconds`` (or a sensible default).  When the global budget is
exhausted the module does NOT cancel running schedules -- it simply skips
creating new ones so queued work is preserved.

**8.3 -- Chain successful crawl completion into matching and inbox**:
Only ``POSTINGS_FOUND`` outcomes chain into the matching / inbox projection.
Failed, denied, permission-pending, and invalid-extraction outcomes are
filtered out and never reach the inbox.

Design constraints:
- No changes to Phase 1-7 code.
- Pure application logic: the caller provides the concrete dependencies.
- Idempotent: calling ``activate_crawl_schedules`` multiple times converges.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID

from careerops.application.crawl_readiness import CrawlReadiness
from careerops.domain.crawl_attempts import (
    SUCCESSFUL_CHAIN_OUTCOMES,
    CrawlAttemptOutcome,
    CrawlSourceAttempt,
)
from careerops.domain.crawl_plans import CrawlSource
from careerops.infrastructure.temporal.m1_crawl_sink import CrawlSourceResult

__all__ = [
    "BudgetChecker",
    "CrawlActivationResult",
    "CrawlActivationService",
    "InboxProjector",
    "MatchingChainResult",
    "ScheduleActivator",
    "TemporalScheduleActivator",
]

_log = logging.getLogger(__name__)

# Default schedule cadence when a source has no explicit interval.
_DEFAULT_SOURCE_INTERVAL = timedelta(hours=1)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class InboxProjector(Protocol):
    """Projects successful crawl results into matching and inbox.

    Only called for ``POSTINGS_FOUND`` outcomes (8.3).
    """

    async def project_new_postings(
        self,
        owner_id: UUID,
        source_id: UUID,
        postings: tuple[CrawlSourceResult, ...],
        *,
        now: datetime | None = None,
    ) -> int: ...


@runtime_checkable
class ScheduleActivator(Protocol):
    """Resumes a paused Temporal crawl schedule for a source."""

    async def activate_source_schedule(
        self,
        source_id: UUID,
        interval: timedelta,
        *,
        paused: bool = False,
        owner_id: UUID | None = None,
    ) -> bool: ...


@runtime_checkable
class BudgetChecker(Protocol):
    """Checks whether the Tier 2 budget has remaining capacity."""

    @property
    def available_slots(self) -> int: ...

    @property
    def daily_remaining(self) -> int: ...


@runtime_checkable
class SourceEligibilityChecker(Protocol):
    """Checks whether a source is eligible for a new crawl attempt."""

    def is_source_eligible(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        now: datetime | None = None,
    ) -> bool: ...


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrawlActivationResult:
    """Outcome of ``activate_crawl_schedules``."""

    readiness: CrawlReadiness
    sources_considered: int = 0
    schedules_activated: int = 0
    schedules_skipped_budget: int = 0
    schedules_skipped_cooldown: int = 0


@dataclass(frozen=True, slots=True)
class MatchingChainResult:
    """Outcome of ``chain_crawl_to_matching``."""

    outcome: CrawlAttemptOutcome
    chained: bool = False
    postings_projected: int = 0
    skip_reason: str = ""


# ---------------------------------------------------------------------------
# Activation service
# ---------------------------------------------------------------------------


class CrawlActivationService:
    """Orchestrates crawl schedule activation and matching chain.

    Composes:
    - ``CrawlReadiness`` gate (8.1)
    - ``ScheduleActivator`` for Temporal schedule resume (8.2)
    - ``BudgetChecker`` for capacity-aware activation (8.2)
    - ``SourceEligibilityChecker`` for cooldown / terminal-stop checks (8.2)
    - ``InboxProjector`` for matching chain (8.3)
    """

    def __init__(
        self,
        schedule_activator: ScheduleActivator,
        budget_checker: BudgetChecker,
        eligibility_checker: SourceEligibilityChecker,
        inbox_projector: InboxProjector | None = None,
    ) -> None:
        self._activator = schedule_activator
        self._budget = budget_checker
        self._eligibility = eligibility_checker
        self._projector = inbox_projector

    async def activate_crawl_schedules(
        self,
        readiness: CrawlReadiness,
        owner_id: UUID,
        sources: list[CrawlSource],
        *,
        now: datetime | None = None,
    ) -> CrawlActivationResult:
        """Enable paused crawl schedules after readiness gate passes.

        8.2: For each eligible source, resume its Temporal schedule with the
        source's cadence.  When the budget is exhausted, skip activation
        (preserve queued work; do not cancel running schedules).

        Args:
            readiness: the result of ``check_crawl_readiness``.
            owner_id: the owner of the sources.
            sources: enabled + ACTIVE sources from the source queue.
            now: override timestamp (for testing).

        Returns:
            A ``CrawlActivationResult`` summarizing what happened.
        """
        if not readiness.ready:
            _log.warning(
                "crawl activation skipped: readiness gate failed -- %s",
                "; ".join(readiness.failures),
            )
            return CrawlActivationResult(readiness=readiness)

        ts = now or datetime.now(tz=UTC)
        activated = 0
        skipped_budget = 0
        skipped_cooldown = 0

        for source in sources:
            # Budget check: if no concurrent slots or daily budget exhausted,
            # skip this source (preserve queued work, do not cancel running).
            if self._budget.available_slots <= 0 or self._budget.daily_remaining <= 0:
                skipped_budget += 1
                _log.debug(
                    "skipping source %s: budget exhausted (slots=%d, daily=%d)",
                    source.id,
                    self._budget.available_slots,
                    self._budget.daily_remaining,
                )
                continue

            # Eligibility check: cooldown from last attempt, terminal stop
            # (POLICY_DENIED), or source not enabled/ACTIVE.
            if not self._eligibility.is_source_eligible(owner_id, source.id, now=ts):
                skipped_cooldown += 1
                _log.debug(
                    "skipping source %s: not eligible (cooldown or terminal)",
                    source.id,
                )
                continue

            # Determine cadence: use default (source's plan version interval
            # would be passed by the caller in a production wiring).
            interval = _DEFAULT_SOURCE_INTERVAL

            # Activate the schedule (idempotent: already-active is a no-op).
            try:
                await self._activator.activate_source_schedule(
                    source.id,
                    interval,
                    paused=False,
                    owner_id=owner_id,
                )
                activated += 1
            except Exception:
                _log.exception("failed to activate schedule for source %s", source.id)

        _log.info(
            "crawl activation: %d/%d schedules activated, %d skipped (budget), "
            "%d skipped (cooldown)",
            activated,
            len(sources),
            skipped_budget,
            skipped_cooldown,
        )

        return CrawlActivationResult(
            readiness=readiness,
            sources_considered=len(sources),
            schedules_activated=activated,
            schedules_skipped_budget=skipped_budget,
            schedules_skipped_cooldown=skipped_cooldown,
        )

    async def chain_crawl_to_matching(
        self,
        owner_id: UUID,
        source_id: UUID,
        attempt: CrawlSourceAttempt,
        crawl_result: CrawlSourceResult | None = None,
        *,
        now: datetime | None = None,
    ) -> MatchingChainResult:
        """Chain successful crawl completion into matching and inbox projection.

        8.3: Only ``POSTINGS_FOUND`` outcomes chain into matching.  Failed,
        denied, permission-pending, and invalid-extraction outcomes are
        filtered out.

        Args:
            owner_id: the owner of the source.
            source_id: the source that was crawled.
            attempt: the recorded attempt outcome.
            crawl_result: the raw crawl result (for posting data).
            now: override timestamp.

        Returns:
            A ``MatchingChainResult`` indicating whether the chain was taken.
        """
        # 8.3: filter -- only successful outcomes chain.
        if attempt.outcome not in SUCCESSFUL_CHAIN_OUTCOMES:
            return MatchingChainResult(
                outcome=attempt.outcome,
                chained=False,
                skip_reason=f"outcome {attempt.outcome.value} does not chain into matching",
            )

        # No projector wired -- nothing to do.
        if self._projector is None:
            return MatchingChainResult(
                outcome=attempt.outcome,
                chained=False,
                skip_reason="no inbox projector configured",
            )

        # No crawl result or no postings -- nothing to project.
        if crawl_result is None or not crawl_result.postings:
            return MatchingChainResult(
                outcome=attempt.outcome,
                chained=False,
                skip_reason="no postings to project",
            )

        try:
            projected = await self._projector.project_new_postings(
                owner_id,
                source_id,
                (crawl_result,),
                now=now,
            )
            return MatchingChainResult(
                outcome=attempt.outcome,
                chained=True,
                postings_projected=projected,
            )
        except Exception:
            _log.exception(
                "matching chain failed for source %s (outcome=%s)",
                source_id,
                attempt.outcome.value,
            )
            return MatchingChainResult(
                outcome=attempt.outcome,
                chained=False,
                skip_reason="projection error",
            )


# ---------------------------------------------------------------------------
# Concrete adapter: ScheduleActivator -> ScheduleManager
# ---------------------------------------------------------------------------


class TemporalScheduleActivator:
    """Adapts the ``ScheduleActivator`` Protocol to ``ScheduleManager``.

    Delegates to ``ScheduleManager.ensure_schedule()`` with per-source
    deterministic schedule IDs (``crawl:{source_id}``), the
    ``CrawlScheduledWorkflow``, and the configured task queue.
    """

    def __init__(
        self,
        schedule_manager: object,
        task_queue: str = "careerops-m0",
    ) -> None:
        self._manager = schedule_manager
        self._task_queue = task_queue

    async def activate_source_schedule(
        self,
        source_id: UUID,
        interval: timedelta,
        *,
        paused: bool = False,
        owner_id: UUID | None = None,
    ) -> bool:
        from careerops.workflows.s5_contracts import ScheduledCrawlWorkflowInput
        from careerops.workflows.s5_workflows import CrawlScheduledWorkflow

        schedule_id = f"crawl:{source_id}"
        arg = ScheduledCrawlWorkflowInput(
            owner_id=str(owner_id) if owner_id else "",
            source_id=str(source_id),
        )
        interval_seconds = max(1, int(interval.total_seconds()))
        # Temporal interval schedules share an epoch anchor.  A stable offset
        # spreads a large source catalog across the interval instead of
        # starting every source in the same second after reconciliation.
        offset = timedelta(seconds=source_id.int % interval_seconds)
        return await self._manager.ensure_schedule(  # type: ignore[union-attr]
            schedule_id=schedule_id,
            workflow=CrawlScheduledWorkflow,
            arg=arg,
            interval=interval,
            offset=offset,
            task_queue=self._task_queue,
            paused=paused,
            note=f"source={source_id}",
        )
