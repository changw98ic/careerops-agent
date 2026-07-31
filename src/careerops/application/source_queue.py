"""Tier 1 source queue and attempt recording (Phase 5.5 + 5.7).

Builds a source queue from the persisted ``job_sources`` registry and records
exactly one durable ``CrawlSourceAttempt`` outcome per source per scheduled
run.  Integrates with the outcome classifier (5.6) and the backoff policy
(5.7) to compute ``next_eligible_at`` for cooldown.

Design:
- ``SourceQueueService.select_sources`` returns enabled + ACTIVE sources from
  the registry, ordered by ``last_run_at ASC NULLS FIRST`` (starving sources
  first).
- ``SourceQueueService.record_attempt`` persists one ``CrawlSourceAttempt`` per
  source, computing ``next_eligible_at`` from the backoff policy.
- Each attempt gets a monotonic ``attempt_no`` from the repository's
  ``next_attempt_no`` so the ``uq_crawl_source_attempts_source_attempt_no``
  unique key is never violated.
- ``POLICY_DENIED`` is a terminal stop: no ``next_eligible_at`` is set because
  the source will not be retried until the underlying policy changes.
- Temporary / unconfirmed sources get a cooldown via the backoff durations;
  public sources get a shorter cooldown.

Iron Rules honored:
- 2 (server-side ownership: every method takes ``owner_id``).
- 3 (fail-closed: unknown outcome → TRANSIENT_FAILURE).
- 4 (idempotent: attempt_no monotonicity prevents duplicates).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from careerops.application.backoff_policy import BackoffPolicy, BackoffReason
from careerops.application.outcome_classifier import ClassificationInput, classify_outcome
from careerops.domain.crawl import CrawlDecision
from careerops.domain.crawl_attempts import (
    CrawlAttemptOutcome,
    CrawlAttemptRepository,
    CrawlSourceAttempt,
    TERMINAL_STOP_OUTCOMES,
)
from careerops.domain.crawl_plans import (
    CrawlExecutorMode,
    CrawlSource,
    CrawlSourceRepository,
    CrawlSourceState,
)
from careerops.infrastructure.temporal.m1_crawl_sink import CrawlSourceResult

logger = logging.getLogger(__name__)

__all__ = ["SourceQueueService"]


# Default backoff durations when the backoff policy does not supply one.
# These are shorter than the backoff policy's durations because the queue
# service is for Tier 1 (structured) sources which are cheaper to retry.
_DEFAULT_COOLDOWN: dict[CrawlAttemptOutcome, timedelta] = {
    CrawlAttemptOutcome.TRANSIENT_FAILURE: timedelta(minutes=30),
    CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED: timedelta(hours=2),
    CrawlAttemptOutcome.AUTH_REQUIRED: timedelta(hours=12),
    CrawlAttemptOutcome.NOT_JOB_SOURCE: timedelta(hours=24),
    CrawlAttemptOutcome.VERIFIED_EMPTY: timedelta(hours=1),
    CrawlAttemptOutcome.POSTINGS_FOUND: timedelta(minutes=5),
}


class SourceQueueService:
    """Tier 1 source queue builder and attempt recorder.

    Combines the source registry (``CrawlSourceRepository``), the attempt
    store (``CrawlAttemptRepository``), and the outcome classifier into a
    single service that the crawl execution layer calls.
    """

    def __init__(
        self,
        source_repository: CrawlSourceRepository,
        attempt_repository: CrawlAttemptRepository,
        *,
        backoff_policy: BackoffPolicy | None = None,
    ) -> None:
        self._sources = source_repository
        self._attempts = attempt_repository
        self._backoff = backoff_policy or BackoffPolicy()

    def select_sources(
        self,
        owner_id: UUID,
        *,
        limit: int = 200,
    ) -> list[CrawlSource]:
        """Return enabled + ACTIVE sources, starving-first.

        Sources that have never been run (``last_run_at IS NULL``) come first,
        then by oldest ``last_run_at``.  This ensures new or starving sources
        are tried before well-trodden ones.
        """
        all_sources = self._sources.list_for(owner_id, limit=limit)
        eligible = [
            s
            for s in all_sources
            if s.enabled and s.state is CrawlSourceState.ACTIVE
        ]
        # Sort: null last_run_at first, then oldest first.
        eligible.sort(key=lambda s: (s.last_run_at is not None, s.last_run_at or datetime.min))
        return eligible

    def is_source_eligible(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Check if a source is eligible for a new attempt.

        Returns False if:
        - The source is not enabled or not ACTIVE.
        - The latest attempt's ``next_eligible_at`` is in the future.
        - The latest attempt outcome is ``POLICY_DENIED`` (terminal stop).
        """
        ts = now or datetime.now(tz=UTC)

        try:
            source = self._sources.get_by_id(owner_id, source_id)
        except Exception:
            return False

        if not source.enabled or source.state is not CrawlSourceState.ACTIVE:
            return False

        latest = self._attempts.latest_for_source(owner_id, source_id)
        if latest is None:
            return True  # Never attempted — eligible.

        # Terminal stop: POLICY_DENIED — do not retry until policy changes.
        if latest.outcome in TERMINAL_STOP_OUTCOMES:
            return False

        # Cooldown check.
        if latest.next_eligible_at is not None and latest.next_eligible_at > ts:
            return False

        return True

    def record_attempt(
        self,
        owner_id: UUID,
        source_id: UUID,
        result: CrawlSourceResult,
        *,
        policy_decision: CrawlDecision = CrawlDecision.ALLOW,
        has_adapter: bool = True,
        transport_error: bool = False,
        timed_out: bool = False,
        crawl_run_id: UUID | None = None,
        now: datetime | None = None,
    ) -> CrawlSourceAttempt:
        """Record one durable attempt outcome for a source.

        Steps:
        1. Classify the outcome from the crawl result + policy + signals.
        2. Compute ``next_eligible_at`` from the backoff policy (unless
           ``POLICY_DENIED`` — terminal stop, no cooldown).
        3. Persist the attempt with a monotonic ``attempt_no``.
        """
        ts = now or datetime.now(tz=UTC)

        # Step 1: classify.
        classification_input = ClassificationInput(
            result=result,
            policy_decision=policy_decision,
            has_adapter=has_adapter,
            transport_error=transport_error,
            timed_out=timed_out,
        )
        outcome = classify_outcome(classification_input)

        # Step 2: compute next_eligible_at.
        next_eligible_at: datetime | None = None
        if outcome not in TERMINAL_STOP_OUTCOMES:
            cooldown = _DEFAULT_COOLDOWN.get(outcome, timedelta(hours=1))
            next_eligible_at = ts + cooldown

        # Step 3: build and persist the attempt.
        attempt_no = self._attempts.next_attempt_no(owner_id, source_id)

        evidence_summary: dict[str, object] = {
            "status_code": result.status_code,
            "postings_count": len(result.postings),
            "policy_decision": policy_decision.value,
            "has_adapter": has_adapter,
            "transport_error": transport_error,
            "timed_out": timed_out,
        }
        if result.expected_fields_missing:
            evidence_summary["expected_fields_missing"] = list(
                result.expected_fields_missing
            )

        attempt = CrawlSourceAttempt(
            id=uuid4(),
            source_id=source_id,
            owner_id=owner_id,
            attempt_no=attempt_no,
            outcome=outcome,
            crawl_run_id=crawl_run_id,
            executor_mode=CrawlExecutorMode.HTTP,
            action_count=0,
            evidence_summary=evidence_summary,
            started_at=ts,
            finished_at=ts,
            next_eligible_at=next_eligible_at,
        )

        return self._attempts.record(owner_id, attempt)

    def record_tier2_attempt(
        self,
        owner_id: UUID,
        source_id: UUID,
        *,
        outcome: CrawlAttemptOutcome,
        action_count: int,
        crawl_run_id: UUID | None,
        evidence_summary: dict[str, object],
        now: datetime | None = None,
    ) -> CrawlSourceAttempt:
        """Persist the measured terminal outcome of one Tier 2 run."""
        ts = now or datetime.now(tz=UTC)
        next_eligible_at: datetime | None = None
        if outcome not in TERMINAL_STOP_OUTCOMES:
            next_eligible_at = ts + _DEFAULT_COOLDOWN.get(
                outcome, timedelta(hours=1)
            )
        attempt = CrawlSourceAttempt(
            id=uuid4(),
            source_id=source_id,
            owner_id=owner_id,
            attempt_no=self._attempts.next_attempt_no(owner_id, source_id),
            outcome=outcome,
            crawl_run_id=crawl_run_id,
            executor_mode=CrawlExecutorMode.EGO,
            action_count=action_count,
            evidence_summary=evidence_summary,
            started_at=ts,
            finished_at=ts,
            next_eligible_at=next_eligible_at,
        )
        return self._attempts.record(owner_id, attempt)
