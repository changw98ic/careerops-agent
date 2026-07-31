"""Crawl execution service (Section 5, tasks 5.1-5.3, 5.5-5.7).

end-to-end-career-application-loop. Orchestrates the actual crawl execution
for a PENDING run record: loads the run + bound plan-version snapshot,
evaluates crawl policy for each source, calls the crawl adapter sink, ingests
postings with provenance (crawl_run_id + plan_version_id), accumulates
counters, applies backoff/stop rules, and transitions the run to a terminal
state.

This module is the "what Section 5 Temporal activities (task 5.4) or a direct
in-process call invokes". It does NOT own scheduling or retry — Temporal (or
the manual run-now path) calls :meth:`CrawlExecutionService.execute` once per
run attempt.

Iron Rules honored:
- 1 (every source request goes through crawl_policy BEFORE fetch).
- 2 (provenance: every ingested version records crawl_run_id + plan_version_id).
- 3 (fail-closed: unknown/unavailable dependencies deny the source).
- 4 (idempotent ingest: reuses m1_crawl_sink ON CONFLICT DO NOTHING; counters
  are derived from the ``is_new_posting`` / ``is_new_version`` flags).
- 8 (additive: does NOT rewrite m1_crawl_sink.ingest_posting dedup logic).

Section 5 extensions (tasks 5.2, 5.3, 5.7):
- Every source request goes through crawl_policy.evaluate() BEFORE the fetch
  (the policy is authoritative; the execution service does NOT bypass it).
- Unknown or unavailable crawl policy dependencies (e.g. DNS resolver down,
  terms DB unavailable) fail-closed: the source is skipped and counted as
  failed. The caller can inspect ``error_category`` for the reason.
- Source-specific backoff/stop rules: on 403 (blocked), 429 (rate limit),
  CAPTCHA/login-wall signals, explicit terms blocks, parser drift, or repeated
  failures -> stop that domain and record ``next_eligible_at`` on the run.

Server-side ownership (Iron Rule 2): the service takes a server-resolved
``owner_id`` (the authenticated candidate). A missing repository surfaces as
``DependencyNotReadyError`` (503) at the route via ``require_repository``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from careerops.api.errors import InvalidStateError
from careerops.application.backoff_policy import (
    BackoffDecision,
    BackoffPolicy,
    BackoffReason,
    BackoffState,
    SourceFetchOutcome,
)
from careerops.application.bounded_tier2 import (
    BoundedTier2Orchestrator,
    Tier2RoutingDecision,
    Tier2RunConfig,
)
from careerops.application.crawl_permission_service import CrawlPermissionService
from careerops.application.crawl_policy import evaluate_crawl_policy
from careerops.application.outcome_classifier import ClassificationInput, classify_outcome
from careerops.application.source_queue import SourceQueueService
from careerops.domain.crawl import (
    CrawlDecision,
    CrawlPolicyDecision,
    CrawlPolicyInput,
    CrawlRunState,
)
from careerops.domain.crawl_attempts import CrawlAttemptOutcome
from careerops.domain.crawl_plans import (
    CrawlExecutorMode,
    CrawlPlanRepository,
    CrawlRun,
    CrawlRunCounters,
    CrawlRunRepository,
    CrawlSourceRepository,
    CrawlSourceState,
)
from careerops.infrastructure.temporal.m1_crawl_sink import (
    CrawlSourceResult,
    RealCrawlActivitySink,
)
from careerops.workflows.m1_contracts import CrawlJobSourceInput

logger = logging.getLogger(__name__)

__all__ = ["CrawlExecutionService"]

# ``None`` in ``CrawlPerRunLimits`` means the service default, not unlimited
# work. These bounds keep an accidentally incomplete plan from turning into an
# unbounded network or ingest operation.
DEFAULT_MAX_POSTINGS_PER_SOURCE = 500
DEFAULT_MAX_SOURCES = 200
DEFAULT_TIMEOUT_SECONDS = 600


class CrawlDownstreamProjector(Protocol):
    """Persist matching and inbox projections for changed canonical jobs."""

    def project(
        self,
        candidate_id: UUID,
        canonical_job_ids: set[UUID],
        *,
        now: datetime,
    ) -> int: ...


def _extract_domain(url: str) -> str:
    """Extract hostname from a URL for policy evaluation."""
    parts = urlsplit(url)
    return parts.hostname or ""


class CrawlExecutionService:
    """Execute a PENDING crawl run through to terminal state.

    Takes the same repository Protocols the Section-4 services use
    (:class:`CrawlRunRepository`, :class:`CrawlPlanRepository`,
    :class:`CrawlSourceRepository`) plus the crawl adapter sink
    (:class:`RealCrawlActivitySink`) and a policy callback (defaults to
    :func:`evaluate_crawl_policy`).

    ``execute`` is the single entry point: it loads the PENDING run + bound
    plan-version snapshot, iterates the plan's sources through policy + fetch +
    ingest, accumulates counters, applies backoff/stop rules, and transitions
    the run to a terminal state. The caller (Temporal activity or manual path)
    owns the retry / cancellation semantics.
    """

    def __init__(
        self,
        run_repository: CrawlRunRepository,
        plan_repository: CrawlPlanRepository,
        source_repository: CrawlSourceRepository,
        sink: RealCrawlActivitySink,
        *,
        policy_fn: Callable[[CrawlPolicyInput], CrawlPolicyDecision] | None = None,
        backoff_policy: BackoffPolicy | None = None,
        source_queue: SourceQueueService | None = None,
        permission_service: CrawlPermissionService | None = None,
        tier2: BoundedTier2Orchestrator | None = None,
        downstream_projector: CrawlDownstreamProjector | None = None,
    ) -> None:
        self._runs = run_repository
        self._plans = plan_repository
        self._sources = source_repository
        self._sink = sink
        self._policy_fn = policy_fn or evaluate_crawl_policy
        self._backoff = backoff_policy or BackoffPolicy()
        self._source_queue = source_queue
        self._permission_service = permission_service
        self._tier2 = tier2
        self._downstream = downstream_projector

    async def execute(
        self,
        owner_id: UUID,
        run_id: UUID,
        *,
        now: datetime | None = None,
    ) -> CrawlRun:
        """Execute a PENDING crawl run through to terminal state.

        Steps:
        1. Load the PENDING run record + bound plan-version snapshot.
        2. Transition run to RUNNING.
        3. For each source in the plan: evaluate policy -> if allowed,
           call sink.crawl_source_with_signals; evaluate backoff signals;
           for each crawled posting, call sink.ingest_posting WITH
           crawl_run_id + plan_version_id.
        4. Accumulate counters from ingest results.
        5. On completion or failure, update run to terminal status +
           counters + ended_at + error_category + next_eligible_at.

        Raises :class:`InvalidStateError` if the run is not PENDING.
        Returns the terminal run record.
        """
        started_at = now or datetime.now(tz=UTC)

        # Step 1: load the run (ownership-scoped via plan-version join).
        run = self._runs.get_by_id(owner_id, run_id)
        if run.state is not CrawlRunState.PENDING:
            raise InvalidStateError(
                f"crawl run {run_id} is in state {run.state.value}; "
                "only PENDING runs can be executed"
            )

        # Load the bound plan-version snapshot.
        plan_version = self._plans.get_by_id(owner_id, run.plan_version_id)

        # Step 2: transition to RUNNING.
        run = self._runs.update_terminal(
            owner_id,
            run_id,
            state=CrawlRunState.RUNNING,
            started_at=started_at,
            now=started_at,
        )

        counters = CrawlRunCounters()
        error_category = ""
        earliest_next_eligible: datetime | None = None
        timeout_seconds = (
            run.limits.timeout_seconds
            if run.limits.timeout_seconds is not None
            else DEFAULT_TIMEOUT_SECONDS
        )
        # The persisted ``started_at`` may be supplied by a replay or a test
        # fixture. Budget enforcement is wall-clock execution time, so anchor
        # the deadline to the activity's actual start rather than a historical
        # provenance timestamp.
        deadline = datetime.now(tz=UTC) + timedelta(seconds=timeout_seconds)
        max_sources = (
            run.limits.max_sources if run.limits.max_sources is not None else DEFAULT_MAX_SOURCES
        )
        max_postings_per_source = (
            run.limits.max_postings_per_source
            if run.limits.max_postings_per_source is not None
            else DEFAULT_MAX_POSTINGS_PER_SOURCE
        )
        timed_out = False
        # Per-domain backoff state for this run.
        domain_backoff: dict[str, BackoffState] = {}
        changed_canonical_job_ids: set[UUID] = set()

        try:
            # Step 3: iterate the run's immutable source snapshot. The
            # fallback preserves compatibility with old in-memory fixtures
            # that predate ``CrawlRun.source_set``.
            source_ids = run.source_set or plan_version.sources
            source_ids = source_ids[: max(0, max_sources)]

            for source_id in source_ids:
                if datetime.now(tz=UTC) >= deadline:
                    timed_out = True
                    error_category = "run_timeout"
                    break
                source = self._sources.get_by_id(owner_id, source_id)

                # Skip sources that are not enabled and ACTIVE (spec: "User
                # pauses a source ... prevents new runs from starting for
                # that source"). This is a safety net — the run-now service
                # already filters eligible sources; a source that was paused
                # between run creation and execution is skipped here.
                if not source.enabled or source.state is not CrawlSourceState.ACTIVE:
                    logger.info(
                        "skipping source %s (enabled=%s, state=%s)",
                        source_id,
                        source.enabled,
                        source.state.value,
                    )
                    continue

                domain = _extract_domain(source.base_url)

                # Check per-domain backoff: if this domain is still in
                # backoff from a previous source in this run, skip it.
                backoff_state = domain_backoff.get(domain, BackoffState())
                if (
                    backoff_state.next_eligible_at is not None
                    and backoff_state.next_eligible_at > started_at
                ):
                    logger.info(
                        "skipping source %s: domain %s in backoff until %s",
                        source_id,
                        domain,
                        backoff_state.next_eligible_at.isoformat(),
                    )
                    counters = dataclasses.replace(counters, failed=counters.failed + 1)
                    continue

                # Iron Rule 1: every source request goes through crawl_policy
                # BEFORE fetch. Iron Rule 3: fail-closed on unknown deps.
                policy_input = CrawlPolicyInput(
                    source_url=source.base_url,
                    terms_status=source.terms_status.value,
                    domain=domain,
                )
                try:
                    policy_result = self._policy_fn(policy_input)
                except Exception:
                    # Iron Rule 3: fail-closed. If the policy evaluation itself
                    # raises (e.g. DNS resolver down, terms DB unavailable),
                    # treat the source as denied.
                    logger.exception(
                        "crawl_policy raised for source %s; failing closed",
                        source_id,
                    )
                    counters = dataclasses.replace(counters, failed=counters.failed + 1)
                    if not error_category:
                        error_category = "dependency_unavailable"
                    continue

                if policy_result.decision is not CrawlDecision.ALLOW:
                    logger.info(
                        "policy denied source %s: %s (%s)",
                        source_id,
                        policy_result.decision.value,
                        policy_result.reason,
                    )
                    # Record error category from the policy decision so the
                    # UI shows an actionable reason.
                    if not error_category:
                        error_category = policy_result.decision.value
                    counters = dataclasses.replace(counters, failed=counters.failed + 1)

                    # On terms_blocked, mark the source as BLOCKED.
                    if policy_result.decision is CrawlDecision.DENY_BLOCKED:
                        try:
                            self._sources.update_state(
                                owner_id,
                                source_id,
                                state=CrawlSourceState.BLOCKED,
                                now=started_at,
                            )
                        except Exception:
                            logger.exception("failed to mark source %s as BLOCKED", source_id)
                    continue

                # Fetch via the crawl adapter sink with signals.
                crawl_input = CrawlJobSourceInput(
                    source_id=str(source.id),
                    company_id=str(source.company_id),
                    company_name="",  # not needed for fetch
                    source_type=source.source_type.value,
                    base_url=source.base_url,
                    # Tier 1 is always an unauthenticated public probe. Tier 2
                    # is entered only after the recorded outcome is eligible.
                    executor_mode=CrawlExecutorMode.HTTP.value,
                )
                try:
                    remaining = (deadline - datetime.now(tz=UTC)).total_seconds()
                    if remaining <= 0:
                        raise TimeoutError
                    async with asyncio.timeout(remaining):
                        result = await self._sink.crawl_source_with_signals(crawl_input)
                except TimeoutError:
                    if self._source_queue is not None:
                        self._source_queue.record_attempt(
                            owner_id,
                            source_id,
                            CrawlSourceResult(postings=(), status_code=0),
                            policy_decision=policy_result.decision,
                            has_adapter=self._sink.supports_source_type(
                                source.source_type.value
                            ),
                            timed_out=True,
                            crawl_run_id=run.id,
                            now=started_at,
                        )
                    timed_out = True
                    error_category = "run_timeout"
                    break
                except Exception:
                    logger.exception("crawl_source failed for source %s", source_id)
                    if self._source_queue is not None:
                        self._source_queue.record_attempt(
                            owner_id,
                            source_id,
                            CrawlSourceResult(postings=(), status_code=0),
                            policy_decision=policy_result.decision,
                            has_adapter=self._sink.supports_source_type(
                                source.source_type.value
                            ),
                            transport_error=True,
                            crawl_run_id=run.id,
                            now=started_at,
                        )
                    counters = dataclasses.replace(counters, failed=counters.failed + 1)
                    if not error_category:
                        error_category = "source_fetch_error"
                    # Record consecutive failure for backoff.
                    backoff_state = self._backoff.record_outcome(
                        backoff_state,
                        BackoffDecision(should_stop=False),
                        success=False,
                    )
                    domain_backoff[domain] = backoff_state
                    continue

                # A configured ego source whose public structured probe
                # produced no postings is dynamic evidence, not VERIFIED_EMPTY.
                if (
                    source.executor_mode is CrawlExecutorMode.EGO
                    and not result.postings
                    and result.status_code == 200
                    and not result.expected_fields_missing
                ):
                    result = CrawlSourceResult(
                        postings=result.postings,
                        status_code=result.status_code,
                        body_prefix=result.body_prefix,
                        expected_fields_missing=("dynamic_rendering_required",),
                    )

                # Record the Tier 1 outcome before any Tier 2 escalation.
                tier1_outcome = classify_outcome(
                    ClassificationInput(
                        result=result,
                        policy_decision=policy_result.decision,
                        has_adapter=self._sink.supports_source_type(
                            source.source_type.value
                        ),
                    )
                )
                if self._source_queue is not None:
                    self._source_queue.record_attempt(
                        owner_id,
                        source_id,
                        result,
                        policy_decision=policy_result.decision,
                        has_adapter=self._sink.supports_source_type(
                            source.source_type.value
                        ),
                        crawl_run_id=run.id,
                        now=started_at,
                    )

                if tier1_outcome in (
                    CrawlAttemptOutcome.AUTH_REQUIRED,
                    CrawlAttemptOutcome.DYNAMIC_OR_UNSUPPORTED,
                ):
                    routing = (
                        self._tier2.should_enter_tier2(
                            owner_id,
                            source_id,
                            tier1_outcome=tier1_outcome,
                        )
                        if self._tier2 is not None
                        else Tier2RoutingDecision.DENY
                    )

                    if (
                        tier1_outcome is CrawlAttemptOutcome.AUTH_REQUIRED
                        and routing is Tier2RoutingDecision.DENY
                        and self._permission_service is not None
                    ):
                        self._permission_service.request_permission(
                            owner_id,
                            source_id,
                            login_evidence=result.body_prefix[:1000],
                            domain_scope=domain,
                            disclosed_terms={
                                "purpose": "read-only job discovery",
                                "frequency": f"every {plan_version.interval_seconds} seconds",
                                "max_browser_actions": 30,
                                "max_duration_seconds": 300,
                                "max_consecutive_empty_pages": 3,
                            },
                            now=started_at,
                        )
                        counters = dataclasses.replace(
                            counters, failed=counters.failed + 1
                        )
                        continue

                    if routing is not Tier2RoutingDecision.DENY and self._tier2 is not None:
                        tier2_result = await self._tier2.run_source(
                            Tier2RunConfig(
                                source_id=str(source_id),
                                base_url=source.base_url,
                                owner_id=owner_id,
                                crawl_run_id=run.id,
                                plan_version_id=plan_version.id,
                                authenticated=(
                                    routing
                                    is Tier2RoutingDecision.SKIP_AUTHENTICATED
                                ),
                            )
                        )
                        counters = dataclasses.replace(
                            counters,
                            discovered=(
                                counters.discovered + tier2_result.postings_new
                            ),
                            updated=(
                                counters.updated + tier2_result.postings_updated
                            ),
                            failed=(
                                counters.failed
                                + (1 if tier2_result.error else 0)
                            ),
                        )
                        changed_canonical_job_ids.update(
                            tier2_result.canonical_job_ids
                        )
                        if self._source_queue is not None:
                            self._source_queue.record_tier2_attempt(
                                owner_id,
                                source_id,
                                outcome=self._tier2.outcome_for_stop_reason(
                                    tier2_result.stop_reason,
                                    tier2_result.postings_found,
                                ),
                                action_count=tier2_result.action_count,
                                crawl_run_id=run.id,
                                evidence_summary={
                                    "stop_reason": tier2_result.stop_reason.value,
                                    "duration_seconds": tier2_result.duration_s,
                                    "consecutive_empty_pages": (
                                        tier2_result.consecutive_empty_pages
                                    ),
                                    "authenticated": (
                                        routing
                                        is Tier2RoutingDecision.SKIP_AUTHENTICATED
                                    ),
                                },
                            )
                        continue

                # Evaluate backoff/stop signals from the fetch outcome.
                fetch_outcome = SourceFetchOutcome(
                    domain=domain,
                    status_code=result.status_code,
                    body_prefix=result.body_prefix,
                    terms_status=source.terms_status.value,
                    jobs_found=len(result.postings),
                    expected_fields_missing=result.expected_fields_missing,
                    consecutive_failures=backoff_state.consecutive_failures,
                )
                backoff_decision = self._backoff.evaluate(fetch_outcome, now=started_at)

                if backoff_decision.should_stop:
                    logger.info(
                        "backoff triggered for source %s: %s",
                        source_id,
                        backoff_decision.reason.value if backoff_decision.reason else "unknown",
                    )
                    if not error_category:
                        error_category = (
                            backoff_decision.reason.value if backoff_decision.reason else "backoff"
                        )
                    counters = dataclasses.replace(counters, failed=counters.failed + 1)
                    # Update domain backoff state.
                    backoff_state = self._backoff.record_outcome(
                        backoff_state, backoff_decision, success=False
                    )
                    domain_backoff[domain] = backoff_state
                    # Track the earliest next_eligible_at for the run.
                    if backoff_decision.next_eligible_at is not None and (
                        earliest_next_eligible is None
                        or backoff_decision.next_eligible_at < earliest_next_eligible
                    ):
                        earliest_next_eligible = backoff_decision.next_eligible_at

                    # On 403 or terms_blocked, mark the source as BLOCKED.
                    if backoff_decision.reason in (
                        BackoffReason.BLOCKED_403,
                        BackoffReason.TERMS_BLOCKED,
                    ):
                        try:
                            self._sources.update_state(
                                owner_id,
                                source_id,
                                state=CrawlSourceState.BLOCKED,
                                now=started_at,
                            )
                        except Exception:
                            logger.exception("failed to mark source %s as BLOCKED", source_id)
                    continue

                # Backoff clear — reset domain state on success.
                backoff_state = self._backoff.record_outcome(
                    backoff_state, backoff_decision, success=True
                )
                domain_backoff[domain] = backoff_state

                # Step 4: ingest each posting with provenance.
                for posting in result.postings[: max(0, max_postings_per_source)]:
                    try:
                        remaining = (deadline - datetime.now(tz=UTC)).total_seconds()
                        if remaining <= 0:
                            raise TimeoutError
                        async with asyncio.timeout(remaining):
                            ingest_result = await self._sink.ingest_posting(
                                posting,
                                crawl_run_id=run.id,
                                plan_version_id=plan_version.id,
                            )
                    except TimeoutError:
                        timed_out = True
                        error_category = "run_timeout"
                        break
                    except Exception:
                        logger.exception(
                            "ingest_posting failed for source %s external_id=%s",
                            source_id,
                            posting.external_id,
                        )
                        counters = dataclasses.replace(counters, failed=counters.failed + 1)
                        continue

                    if ingest_result.get("is_new_posting"):
                        counters = dataclasses.replace(counters, discovered=counters.discovered + 1)
                    elif ingest_result.get("is_new_version"):
                        counters = dataclasses.replace(counters, updated=counters.updated + 1)
                    canonical_job_id = ingest_result.get("canonical_job_id")
                    if canonical_job_id and (
                        ingest_result.get("is_new_posting")
                        or ingest_result.get("is_new_version")
                    ):
                        changed_canonical_job_ids.add(UUID(str(canonical_job_id)))

                if timed_out:
                    break

            # Step 5: terminal result. Source failures are represented by the
            # counters and bounded category; a budget exhaustion is distinct
            # from a source failure so callers can offer a safe retry action.
            ended_at = datetime.now(tz=UTC)
            if (
                not timed_out
                and changed_canonical_job_ids
                and self._downstream is not None
            ):
                self._downstream.project(
                    owner_id,
                    changed_canonical_job_ids,
                    now=ended_at,
                )
            run = self._runs.update_terminal(
                owner_id,
                run_id,
                state=CrawlRunState.TIMEOUT if timed_out else CrawlRunState.SUCCEEDED,
                counters=counters,
                error_category=error_category,
                ended_at=ended_at,
                next_eligible_at=earliest_next_eligible,
                now=ended_at,
            )

        except Exception:
            # Step 5: failed — record whatever counters were accumulated.
            ended_at = datetime.now(tz=UTC)
            if not error_category:
                error_category = "execution_error"
            try:
                run = self._runs.update_terminal(
                    owner_id,
                    run_id,
                    state=CrawlRunState.FAILED,
                    counters=counters,
                    error_category=error_category,
                    ended_at=ended_at,
                    next_eligible_at=earliest_next_eligible,
                    now=ended_at,
                )
            except Exception:
                logger.exception("failed to update run %s to terminal state", run_id)
            raise

        return run
