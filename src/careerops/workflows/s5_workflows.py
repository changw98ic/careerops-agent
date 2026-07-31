"""Section 5 Temporal workflows: manual and scheduled crawl runs (task 5.4).

Two workflows:

- :class:`CrawlRunWorkflow` — manual run-now.  The API endpoint creates a
  PENDING run via ``CrawlRunService.run_now`` and then starts this workflow
  with the ``owner_id`` + ``run_id``.  The workflow executes the crawl
  activity and returns the terminal result.

- :class:`CrawlScheduledWorkflow` — scheduled trigger.  The workflow first
  creates a PENDING run (via the ``create_scheduled_run`` activity, which
  calls ``CrawlRunService.run_now``) and then executes it.  If the plan is
  paused or has no eligible sources, the workflow returns a ``skipped``
  result without executing.

Retry semantics:
- ``create_scheduled_run``: transient DB errors are retryable (maximum 3
  attempts); ``InvalidStateError`` (paused/no sources) is non-retryable
  and produces a ``skipped`` result.
- ``execute_crawl_run``: all errors are non-retryable (the service handles
  per-source failures internally; top-level errors mean the run is already
  terminal or in an inconsistent state).

All I/O is performed in Activities.  Workflow code is deterministic (no
direct DB or network access).
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from careerops.workflows.s5_contracts import (
    CREATE_SCHEDULED_RUN_ACTIVITY,
    EXECUTE_CRAWL_RUN_ACTIVITY,
    CrawlRunExecuteInput,
    CrawlRunExecuteResult,
    CrawlRunWorkflowInput,
    CrawlRunWorkflowResult,
    CreateScheduledRunInput,
    CreateScheduledRunResult,
    ScheduledCrawlWorkflowInput,
)

# Activity timeouts.
# A single public ATS board can legitimately contain several thousand jobs.
# In production the activity persists and projects every posting, so ten
# minutes is not a safe upper bound for large boards (Anduril alone currently
# exposes more than two thousand postings). Temporal keeps the activity
# durable while the client/acceptance process may disconnect.
_EXECUTE_TIMEOUT = timedelta(minutes=45)
_CREATE_RUN_TIMEOUT = timedelta(seconds=30)

# Retry policy for create_scheduled_run (transient DB errors).
_CREATE_RUN_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)

# Retry policy for execute_crawl_run (non-retryable by default; the activity
# raises ApplicationError(non_retryable=True) for all errors).
_EXECUTE_RETRY = RetryPolicy(
    maximum_attempts=1,
)


@workflow.defn
class CrawlRunWorkflow:
    """Manual run-now workflow.

    The API endpoint creates a PENDING run and starts this workflow with
    ``owner_id`` + ``run_id``.  The workflow executes the crawl activity
    and returns the terminal result.
    """

    @workflow.run
    async def run(self, request: CrawlRunWorkflowInput) -> CrawlRunWorkflowResult:
        workflow.logger.info(
            "CrawlRunWorkflow started: owner=%s run=%s",
            request.owner_id,
            request.run_id,
        )

        exec_result: CrawlRunExecuteResult = await workflow.execute_activity(
            EXECUTE_CRAWL_RUN_ACTIVITY,
            CrawlRunExecuteInput(
                owner_id=request.owner_id,
                run_id=request.run_id,
            ),
            result_type=CrawlRunExecuteResult,
            start_to_close_timeout=_EXECUTE_TIMEOUT,
            retry_policy=_EXECUTE_RETRY,
        )

        return CrawlRunWorkflowResult(
            run_id=exec_result.run_id,
            state=exec_result.state,
            counters=dict(exec_result.counters),
            error_category=exec_result.error_category,
            next_eligible_at=exec_result.next_eligible_at,
        )


@workflow.defn
class CrawlScheduledWorkflow:
    """Scheduled crawl trigger workflow.

    Creates a PENDING run for the active plan (via
    ``create_scheduled_run`` activity) and then executes it.  If the plan
    is paused or has no eligible sources, returns a ``skipped`` result.
    """

    @workflow.run
    async def run(self, request: ScheduledCrawlWorkflowInput) -> CrawlRunWorkflowResult:
        workflow.logger.info("CrawlScheduledWorkflow started: owner=%s", request.owner_id)

        # Step 1: create (or reuse) a PENDING run.
        create_result: CreateScheduledRunResult = await workflow.execute_activity(
            CREATE_SCHEDULED_RUN_ACTIVITY,
            CreateScheduledRunInput(
                owner_id=request.owner_id,
                source_id=request.source_id,
            ),
            result_type=CreateScheduledRunResult,
            start_to_close_timeout=_CREATE_RUN_TIMEOUT,
            retry_policy=_CREATE_RUN_RETRY,
        )

        if not create_result.run_created or create_result.run_id is None:
            workflow.logger.info(
                "Scheduled run skipped for owner %s: %s",
                request.owner_id,
                create_result.error,
            )
            return CrawlRunWorkflowResult(
                run_id="",
                state="skipped",
                skipped=True,
                skip_reason=create_result.error,
            )

        # Step 2: execute the crawl run.
        exec_result: CrawlRunExecuteResult = await workflow.execute_activity(
            EXECUTE_CRAWL_RUN_ACTIVITY,
            CrawlRunExecuteInput(
                owner_id=request.owner_id,
                run_id=create_result.run_id,
            ),
            result_type=CrawlRunExecuteResult,
            start_to_close_timeout=_EXECUTE_TIMEOUT,
            retry_policy=_EXECUTE_RETRY,
        )

        return CrawlRunWorkflowResult(
            run_id=exec_result.run_id,
            state=exec_result.state,
            counters=dict(exec_result.counters),
            error_category=exec_result.error_category,
            next_eligible_at=exec_result.next_eligible_at,
        )
