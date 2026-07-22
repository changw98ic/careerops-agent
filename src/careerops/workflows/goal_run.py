from __future__ import annotations

from contextlib import suppress
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

from pydantic import JsonValue
from temporalio import workflow
from temporalio.common import RetryPolicy

from careerops.workflows.goal_run_contracts import (
    GOAL_RUN_CHECKPOINT_ACTIVITY,
    GOAL_RUN_CLAIM_ACTIVITY,
    GOAL_RUN_COMPLETE_SOURCE_ACTIVITY,
    GOAL_RUN_DISCOVERY_ACTIVITY,
    GOAL_RUN_DISPATCH_ACTIVITY,
    GOAL_RUN_ENSURE_ACTIVITY,
    GOAL_RUN_FAIL_SOURCE_ACTIVITY,
    GOAL_RUN_INGEST_PUBLIC_ATS_ACTIVITY,
    GOAL_RUN_LOAD_ACTIVITY,
    GOAL_RUN_MATCH_ACTIVITY,
    GOAL_RUN_PREPARE_DRAFTS_ACTIVITY,
    GOAL_RUN_RECONCILE_ACTIVITY,
    GOAL_RUN_REQUEST_REVIEW_ACTIVITY,
    GoalRunCancelSignal,
    GoalRunCheckpointCommand,
    GoalRunCheckpointResult,
    GoalRunClaimCommand,
    GoalRunClaimResult,
    GoalRunCompleteCommand,
    GoalRunDiscoveryCommand,
    GoalRunDiscoveryResult,
    GoalRunDiscoveryState,
    GoalRunDomainCommand,
    GoalRunEnsureCommand,
    GoalRunFailCommand,
    GoalRunIngestCommand,
    GoalRunIngestResult,
    GoalRunInput,
    GoalRunLoadCommand,
    GoalRunMode,
    GoalRunPhase,
    GoalRunProgress,
    GoalRunResult,
    GoalRunResumeSignal,
    GoalRunReviewDecision,
    GoalRunReviewRequestCommand,
    GoalRunReviewRequestResult,
    GoalRunReviewSignal,
    GoalRunSnapshot,
    GoalRunStatus,
    GoalRunStatusView,
    GoalRunStepOutcome,
    GoalRunStepResult,
)

_ACTIVITY_START_TO_CLOSE = timedelta(seconds=90)
_ACTIVITY_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)
_MAX_RESUME_ATTEMPTS = 3
_CONTROL_PLANE_POLL_INTERVAL = timedelta(seconds=30)
_EXTERNAL_RECEIPT_POLL_INTERVAL = timedelta(seconds=30)
_MAX_EXTERNAL_RECEIPT_POLLS = 120
_TERMINAL_STATUSES = frozenset(
    {
        GoalRunStatus.RECONCILIATION_REQUIRED,
        GoalRunStatus.COMPLETED,
        GoalRunStatus.FAILED,
        GoalRunStatus.CANCELLED,
        GoalRunStatus.REJECTED,
    }
)


@workflow.defn
class GoalRunWorkflow:
    """Replay-safe GoalRun orchestrator over a Postgres control-plane projection.

    Temporal owns ordering and timers. Postgres owns operator-visible state, authenticated
    decisions and optimistic fencing. Signals only wake this workflow; they are never treated as
    authorization, and every operator command is re-read from the owner-scoped database record.
    """

    def __init__(self) -> None:
        self._goal_run_id: UUID | None = None
        self._owner_user_id: UUID | None = None
        self._phase = GoalRunPhase.INITIALIZING
        self._status = GoalRunStatus.STARTING
        self._version = 0
        self._continuation_count = 0
        self._resume_attempts = 0
        self._pending_review_item_id: UUID | None = None
        self._last_error_code: str | None = None
        self._review_signal: GoalRunReviewSignal | None = None
        self._resume_signal: GoalRunResumeSignal | None = None
        self._cancel_signal: GoalRunCancelSignal | None = None
        self._ignored_signals = 0
        self._load_count = 0
        self._mode = GoalRunMode.GMAIL_DISPATCH

    @workflow.run
    async def run(self, request: GoalRunInput) -> GoalRunResult:
        self._goal_run_id = request.goal_run_id
        self._owner_user_id = request.owner_user_id
        self._phase = request.next_phase
        self._version = request.expected_version
        self._continuation_count = request.continuation_count
        self._resume_attempts = request.resume_attempts
        self._mode = request.mode
        progress = request.progress
        claim: GoalRunClaimResult | None = None
        discovery: GoalRunDiscoveryResult | None = None
        reconciliation_poll_count = 0

        snapshot = await self._ensure_run(request)
        self._apply_snapshot(snapshot)
        if snapshot.status in _TERMINAL_STATUSES:
            return self._terminal_result(progress, reason_code="already_terminal")

        phase = request.next_phase
        if phase is GoalRunPhase.INITIALIZING:
            phase = GoalRunPhase.SELECTING_SOURCE

        while True:
            external = await self._authorized_external_state_if_signaled(request)
            if external is not None and external.status in _TERMINAL_STATUSES:
                return self._terminal_result(progress, reason_code="operator_command")

            try:
                if phase is GoalRunPhase.SELECTING_SOURCE:
                    if not await self._checkpoint(
                        request,
                        phase=phase,
                        status=GoalRunStatus.RUNNING,
                        outcome="source_selection_ready",
                        checkpoint={"source_id": request.source_id},
                        marker="selected",
                    ):
                        return self._terminal_result(progress, reason_code="operator_command")
                    phase = GoalRunPhase.CLAIMING_SOURCE
                    continue

                if phase is GoalRunPhase.CLAIMING_SOURCE:
                    self._phase = phase
                    if not await self._checkpoint_started(request, phase):
                        return self._terminal_result(progress, reason_code="operator_command")
                    claimed = await workflow.execute_activity(
                        GOAL_RUN_CLAIM_ACTIVITY,
                        GoalRunClaimCommand(
                            goal_run_id=request.goal_run_id,
                            registry_id=request.registry_id,
                            source_id=request.source_id,
                        ),
                        result_type=GoalRunClaimResult,
                        activity_id=self._activity_id("claim-source"),
                        start_to_close_timeout=_ACTIVITY_START_TO_CLOSE,
                        retry_policy=_ACTIVITY_RETRY_POLICY,
                    )
                    claim = claimed
                    progress = replace(
                        progress,
                        source_row_id=claimed.source_row_id,
                        crawler_run_id=claimed.run_id,
                        source_id=claimed.source_id,
                    )
                    if not await self._checkpoint(
                        request,
                        phase=phase,
                        status=GoalRunStatus.RUNNING,
                        outcome="source_claimed",
                        checkpoint={
                            "source_id": claimed.source_id,
                            "source_row_id": str(claimed.source_row_id),
                            "crawler_run_id": str(claimed.run_id),
                        },
                        marker="claimed",
                    ):
                        return self._terminal_result(progress, reason_code="operator_command")
                    phase = GoalRunPhase.DISCOVERY
                    continue

                if phase is GoalRunPhase.DISCOVERY:
                    if claim is None:
                        raise RuntimeError("source claim is unavailable")
                    self._phase = phase
                    if not await self._checkpoint_started(request, phase):
                        return self._terminal_result(progress, reason_code="operator_command")
                    discovered = await workflow.execute_activity(
                        GOAL_RUN_DISCOVERY_ACTIVITY,
                        GoalRunDiscoveryCommand(
                            goal_run_id=request.goal_run_id,
                            owner_user_id=request.owner_user_id,
                            registry_id=claim.registry_id,
                            source_id=claim.source_id,
                            source_row_id=claim.source_row_id,
                            run_id=claim.run_id,
                            worker_id=claim.lease_owner,
                            lease_token=claim.lease_token,
                        ),
                        result_type=GoalRunDiscoveryResult,
                        activity_id=self._activity_id("discover"),
                        start_to_close_timeout=_ACTIVITY_START_TO_CLOSE,
                        retry_policy=_ACTIVITY_RETRY_POLICY,
                    )
                    discovery = discovered
                    progress = replace(
                        progress,
                        output_manifest_sha256=discovered.output_manifest_sha256,
                        crawler_execution_request_id=(discovered.crawler_execution_request_id),
                        crawler_execution_result_id=discovered.crawler_execution_result_id,
                        crawler_execution_outbox_event_id=(
                            discovered.crawler_execution_outbox_event_id
                        ),
                    )
                    if discovered.state is not GoalRunDiscoveryState.READY:
                        return await self._handle_unready_discovery(
                            request,
                            progress=progress,
                            claim=claim,
                            discovery=discovered,
                        )
                    if not await self._checkpoint(
                        request,
                        phase=phase,
                        status=GoalRunStatus.RUNNING,
                        outcome="discovery_completed",
                        checkpoint={
                            "result": discovered.result,
                            "crawler_execution_request_id": str(
                                discovered.crawler_execution_request_id
                            ),
                            "crawler_execution_result_id": str(
                                discovered.crawler_execution_result_id
                            ),
                            "crawler_execution_outbox_event_id": str(
                                discovered.crawler_execution_outbox_event_id
                            ),
                            "output_manifest_sha256": discovered.output_manifest_sha256,
                        },
                        marker="discovered",
                    ):
                        return self._terminal_result(progress, reason_code="operator_command")
                    phase = GoalRunPhase.COMPLETE_SOURCE
                    continue

                if phase is GoalRunPhase.COMPLETE_SOURCE:
                    if claim is None or discovery is None:
                        raise RuntimeError("source discovery state is unavailable")
                    self._phase = phase
                    if not await self._checkpoint_started(request, phase):
                        return self._terminal_result(progress, reason_code="operator_command")
                    completed = await workflow.execute_activity(
                        GOAL_RUN_COMPLETE_SOURCE_ACTIVITY,
                        GoalRunCompleteCommand(
                            registry_id=claim.registry_id,
                            source_id=claim.source_id,
                            run_id=discovery.run_id,
                            source_row_id=discovery.source_row_id,
                            worker_id=claim.lease_owner,
                            lease_token=claim.lease_token,
                            output_manifest_sha256=discovery.output_manifest_sha256,
                            cursor=discovery.cursor,
                            result=discovery.result,
                        ),
                        result_type=str,
                        activity_id=self._activity_id("complete-source"),
                        start_to_close_timeout=_ACTIVITY_START_TO_CLOSE,
                        retry_policy=_ACTIVITY_RETRY_POLICY,
                    )
                    if completed != "ok":
                        raise RuntimeError("source completion returned an invalid receipt")
                    if not await self._checkpoint(
                        request,
                        phase=phase,
                        status=GoalRunStatus.RUNNING,
                        outcome="source_completed",
                        checkpoint={"crawler_run_id": str(claim.run_id)},
                        marker="source-completed",
                    ):
                        return self._terminal_result(progress, reason_code="operator_command")
                    phase = GoalRunPhase.CANONICAL_INGEST
                    continue

                if phase is GoalRunPhase.CANONICAL_INGEST:
                    domain = self._domain_command(request, progress)
                    self._phase = phase
                    if not await self._checkpoint_started(request, phase):
                        return self._terminal_result(progress, reason_code="operator_command")
                    ingest = await workflow.execute_activity(
                        GOAL_RUN_INGEST_PUBLIC_ATS_ACTIVITY,
                        GoalRunIngestCommand(
                            goal_run_id=domain.goal_run_id,
                            owner_user_id=domain.owner_user_id,
                            registry_id=domain.registry_id,
                            source_id=domain.source_id,
                            source_row_id=domain.source_row_id,
                            run_id=domain.crawler_run_id,
                            max_records=domain.max_records,
                        ),
                        result_type=GoalRunIngestResult,
                        activity_id=self._activity_id("canonical-ingest"),
                        start_to_close_timeout=_ACTIVITY_START_TO_CLOSE,
                        retry_policy=_ACTIVITY_RETRY_POLICY,
                    )
                    progress = replace(
                        progress,
                        observed_records=ingest.observed_records,
                        inserted_versions=ingest.inserted_versions,
                        reused_versions=ingest.reused_versions,
                    )
                    if not await self._checkpoint(
                        request,
                        phase=phase,
                        status=GoalRunStatus.RUNNING,
                        outcome="canonical_ingest_completed",
                        checkpoint={
                            "observed_records": ingest.observed_records,
                            "inserted_versions": ingest.inserted_versions,
                            "reused_versions": ingest.reused_versions,
                        },
                        marker="ingested",
                    ):
                        return self._terminal_result(progress, reason_code="operator_command")
                    phase = GoalRunPhase.MATCHING
                    continue

                if phase is GoalRunPhase.MATCHING:
                    step = await self._execute_domain_step(
                        request,
                        progress,
                        phase=phase,
                        activity_name=GOAL_RUN_MATCH_ACTIVITY,
                        activity_marker="match-jobs",
                    )
                    blocked = await self._handle_step_result(
                        request,
                        progress,
                        phase=phase,
                        step=step,
                    )
                    if blocked is not None:
                        terminal, phase = blocked
                        if terminal:
                            return self._terminal_result(progress, reason_code=step.reason_code)
                        continue
                    if step.output_sha256 is None:
                        raise RuntimeError("matching result is missing an immutable digest")
                    progress = replace(
                        progress,
                        match_snapshot_sha256=step.output_sha256,
                    )
                    phase = GoalRunPhase.DRAFT_PREPARATION
                    continue

                if phase is GoalRunPhase.DRAFT_PREPARATION:
                    step = await self._execute_domain_step(
                        request,
                        progress,
                        phase=phase,
                        activity_name=GOAL_RUN_PREPARE_DRAFTS_ACTIVITY,
                        activity_marker="prepare-drafts",
                    )
                    blocked = await self._handle_step_result(
                        request,
                        progress,
                        phase=phase,
                        step=step,
                    )
                    if blocked is not None:
                        terminal, phase = blocked
                        if terminal:
                            return self._terminal_result(progress, reason_code=step.reason_code)
                        continue
                    if step.output_sha256 is None:
                        raise RuntimeError("reviewable draft is missing an immutable digest")
                    review = await self._request_review(request, step)
                    progress = replace(
                        progress,
                        review_item_id=review.review_item_id,
                        review_snapshot_sha256=review.snapshot_sha256,
                    )
                    approved, terminal = await self._wait_for_review(request, progress)
                    if terminal:
                        reason_code = (
                            "pre_application_package_approved"
                            if request.mode is GoalRunMode.PRE_APPLICATION_ONLY
                            and self._status is GoalRunStatus.COMPLETED
                            else "operator_review"
                        )
                        return self._terminal_result(progress, reason_code=reason_code)
                    if not approved:
                        raise RuntimeError("review wait ended without an approval")
                    self._continue_as_new(
                        request,
                        progress=progress,
                        next_phase=GoalRunPhase.DISPATCH,
                        resume_attempts=0,
                    )

                if phase is GoalRunPhase.DISPATCH:
                    if request.mode is GoalRunMode.PRE_APPLICATION_ONLY:
                        raise RuntimeError("pre-application GoalRun cannot enter dispatch")
                    step = await self._execute_domain_step(
                        request,
                        progress,
                        phase=phase,
                        activity_name=GOAL_RUN_DISPATCH_ACTIVITY,
                        activity_marker="dispatch",
                    )
                    blocked = await self._handle_step_result(
                        request,
                        progress,
                        phase=phase,
                        step=step,
                    )
                    if blocked is not None:
                        terminal, phase = blocked
                        if terminal:
                            return self._terminal_result(progress, reason_code=step.reason_code)
                        continue
                    phase = GoalRunPhase.RECONCILIATION
                    continue

                if phase is GoalRunPhase.RECONCILIATION:
                    if request.mode is GoalRunMode.PRE_APPLICATION_ONLY:
                        raise RuntimeError("pre-application GoalRun cannot enter reconciliation")
                    step = await self._execute_domain_step(
                        request,
                        progress,
                        phase=phase,
                        activity_name=GOAL_RUN_RECONCILE_ACTIVITY,
                        activity_marker=f"reconcile-{reconciliation_poll_count}",
                        checkpoint_started=reconciliation_poll_count == 0,
                    )
                    if step.outcome is GoalRunStepOutcome.PENDING_EXTERNAL:
                        reconciliation_poll_count += 1
                        if reconciliation_poll_count > _MAX_EXTERNAL_RECEIPT_POLLS:
                            if not await self._checkpoint(
                                request,
                                phase=GoalRunPhase.RECONCILIATION,
                                status=GoalRunStatus.RECONCILIATION_REQUIRED,
                                outcome="external_receipt_timeout",
                                checkpoint={
                                    "reason_code": step.reason_code,
                                    "poll_count": reconciliation_poll_count,
                                    "output_sha256": step.output_sha256,
                                },
                                marker="external-receipt-timeout",
                            ):
                                return self._terminal_result(
                                    progress,
                                    reason_code="operator_command",
                                )
                            return self._terminal_result(
                                progress,
                                reason_code="external_receipt_timeout",
                            )
                        self._phase = GoalRunPhase.RECONCILIATION
                        self._status = GoalRunStatus.RUNNING
                        await workflow.sleep(_EXTERNAL_RECEIPT_POLL_INTERVAL)
                        continue
                    blocked = await self._handle_step_result(
                        request,
                        progress,
                        phase=phase,
                        step=step,
                    )
                    if blocked is not None:
                        terminal, phase = blocked
                        if terminal:
                            return self._terminal_result(progress, reason_code=step.reason_code)
                        continue
                    if not await self._checkpoint(
                        request,
                        phase=GoalRunPhase.COMPLETED,
                        status=GoalRunStatus.COMPLETED,
                        outcome="goal_run_completed",
                        checkpoint={
                            "observed_records": progress.observed_records,
                            "inserted_versions": progress.inserted_versions,
                            "reused_versions": progress.reused_versions,
                        },
                        marker="completed",
                    ):
                        return self._terminal_result(progress, reason_code="operator_command")
                    return self._terminal_result(progress, reason_code="completed")

                raise RuntimeError("goal run phase cannot be resumed")
            except Exception as exc:
                error_code = _safe_error_code(exc)
                self._last_error_code = error_code
                if claim is not None and phase in {
                    GoalRunPhase.DISCOVERY,
                    GoalRunPhase.COMPLETE_SOURCE,
                }:
                    await self._best_effort_fail_source(claim, error_code)
                    claim = None
                    discovery = None
                    progress = replace(
                        progress,
                        source_row_id=None,
                        crawler_run_id=None,
                        output_manifest_sha256=None,
                    )
                    phase = GoalRunPhase.CLAIMING_SOURCE

                if self._resume_attempts >= _MAX_RESUME_ATTEMPTS:
                    await self._checkpoint(
                        request,
                        phase=GoalRunPhase.FAILED,
                        status=GoalRunStatus.FAILED,
                        outcome="resume_budget_exhausted",
                        checkpoint={"error_code": error_code},
                        marker="failed",
                    )
                    return self._terminal_result(progress, reason_code=error_code)

                await self._checkpoint(
                    request,
                    phase=GoalRunPhase.BLOCKED,
                    status=GoalRunStatus.BLOCKED,
                    outcome="retryable_activity_failure",
                    checkpoint={
                        "error_code": error_code,
                        "resume_phase": phase.value,
                    },
                    marker=f"blocked-{phase.value}",
                )
                resumed, terminal = await self._wait_for_resume(request)
                if terminal:
                    return self._terminal_result(progress, reason_code="operator_command")
                if not resumed:
                    raise RuntimeError("resume wait ended without an authorized resume") from exc
                self._resume_attempts += 1

    @workflow.signal
    def review(self, signal: GoalRunReviewSignal) -> None:
        if self._review_signal is not None:
            self._ignored_signals += 1
            return
        if (
            self._pending_review_item_id is not None
            and signal.review_item_id != self._pending_review_item_id
        ):
            self._ignored_signals += 1
            return
        self._review_signal = signal

    @workflow.signal
    def resume(self, signal: GoalRunResumeSignal) -> None:
        if self._resume_signal is not None:
            self._ignored_signals += 1
            return
        self._resume_signal = signal

    @workflow.signal
    def cancel(self, signal: GoalRunCancelSignal) -> None:
        if self._cancel_signal is not None:
            self._ignored_signals += 1
            return
        self._cancel_signal = signal

    @workflow.query
    def status(self) -> GoalRunStatusView:
        return GoalRunStatusView(
            goal_run_id=self._goal_run_id,
            phase=self._phase,
            status=self._status,
            version=self._version,
            pending_review_item_id=self._pending_review_item_id,
            last_error_code=self._last_error_code,
            continuation_count=self._continuation_count,
            resume_attempts=self._resume_attempts,
        )

    @workflow.query
    def ignored_signals(self) -> int:
        return self._ignored_signals

    async def _ensure_run(self, request: GoalRunInput) -> GoalRunSnapshot:
        info = workflow.info()
        return await workflow.execute_activity(
            GOAL_RUN_ENSURE_ACTIVITY,
            GoalRunEnsureCommand(
                goal_run_id=request.goal_run_id,
                owner_user_id=request.owner_user_id,
                registry_id=request.registry_id,
                source_id=request.source_id,
                max_records=request.max_records,
                fencing_token=request.fencing_token,
                temporal_workflow_id=info.workflow_id,
                idempotency_key=f"goal-run:{request.goal_run_id}:create",
                trace_id=self._trace_id(request.goal_run_id),
                mode=request.mode,
            ),
            result_type=GoalRunSnapshot,
            activity_id="goal-ensure-run",
            start_to_close_timeout=_ACTIVITY_START_TO_CLOSE,
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )

    async def _checkpoint_started(
        self,
        request: GoalRunInput,
        phase: GoalRunPhase,
    ) -> bool:
        return await self._checkpoint(
            request,
            phase=phase,
            status=GoalRunStatus.RUNNING,
            outcome="started",
            checkpoint={},
            marker="started",
        )

    async def _checkpoint(
        self,
        request: GoalRunInput,
        *,
        phase: GoalRunPhase,
        status: GoalRunStatus,
        outcome: str,
        checkpoint: dict[str, JsonValue],
        marker: str,
    ) -> bool:
        self._phase = phase
        self._status = status
        try:
            result = await workflow.execute_activity(
                GOAL_RUN_CHECKPOINT_ACTIVITY,
                GoalRunCheckpointCommand(
                    goal_run_id=request.goal_run_id,
                    owner_user_id=request.owner_user_id,
                    expected_version=self._version,
                    fencing_token=request.fencing_token,
                    phase=phase,
                    status=status,
                    outcome=outcome,
                    checkpoint=checkpoint,
                    idempotency_key=self._idempotency_key(request, phase, marker),
                    trace_id=self._trace_id(request.goal_run_id),
                ),
                result_type=GoalRunCheckpointResult,
                activity_id=self._activity_id(f"checkpoint-{phase.value}-{marker}"),
                start_to_close_timeout=_ACTIVITY_START_TO_CLOSE,
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
        except Exception:
            snapshot = await self._load_run(request)
            if snapshot.status in _TERMINAL_STATUSES:
                self._apply_snapshot(snapshot)
                return False
            raise
        self._version = result.version
        self._phase = result.phase
        self._status = result.status
        return True

    async def _execute_domain_step(
        self,
        request: GoalRunInput,
        progress: GoalRunProgress,
        *,
        phase: GoalRunPhase,
        activity_name: str,
        activity_marker: str,
        checkpoint_started: bool = True,
    ) -> GoalRunStepResult:
        self._phase = phase
        if checkpoint_started and not await self._checkpoint_started(request, phase):
            return GoalRunStepResult(
                outcome=GoalRunStepOutcome.BLOCKED_CONFIGURATION,
                reason_code="operator_terminal",
            )
        return await workflow.execute_activity(
            activity_name,
            self._domain_command(request, progress),
            result_type=GoalRunStepResult,
            activity_id=self._activity_id(activity_marker),
            start_to_close_timeout=_ACTIVITY_START_TO_CLOSE,
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )

    async def _handle_step_result(
        self,
        request: GoalRunInput,
        progress: GoalRunProgress,
        *,
        phase: GoalRunPhase,
        step: GoalRunStepResult,
    ) -> tuple[bool, GoalRunPhase] | None:
        if step.outcome is GoalRunStepOutcome.SUCCEEDED:
            if not await self._checkpoint(
                request,
                phase=phase,
                status=GoalRunStatus.RUNNING,
                outcome=step.reason_code,
                checkpoint={
                    "output_sha256": step.output_sha256,
                    "details": step.details,
                },
                marker="succeeded",
            ):
                return True, phase
            return None

        reconciliation_required = step.outcome is GoalRunStepOutcome.RECONCILIATION_REQUIRED
        status = (
            GoalRunStatus.RECONCILIATION_REQUIRED
            if reconciliation_required
            else GoalRunStatus.BLOCKED
        )
        checkpoint_phase = (
            GoalRunPhase.RECONCILIATION if reconciliation_required else GoalRunPhase.BLOCKED
        )
        if not await self._checkpoint(
            request,
            phase=checkpoint_phase,
            status=status,
            outcome=step.reason_code,
            checkpoint={
                "blocked_phase": phase.value,
                "output_sha256": step.output_sha256,
                "details": step.details,
            },
            marker=f"blocked-{phase.value}",
        ):
            return True, phase
        if reconciliation_required:
            # An ambiguous provider outcome is an explicit stop for this run. Generic resume
            # must never replay dispatch; a provider-specific reconciliation command can create
            # a separately authorized follow-up run.
            return True, GoalRunPhase.RECONCILIATION
        resumed, terminal = await self._wait_for_resume(request)
        if terminal:
            return True, phase
        if not resumed:
            raise RuntimeError("resume wait ended without an authorized resume")
        self._resume_attempts += 1
        if self._resume_attempts > _MAX_RESUME_ATTEMPTS:
            if not await self._checkpoint(
                request,
                phase=GoalRunPhase.FAILED,
                status=GoalRunStatus.FAILED,
                outcome="resume_budget_exhausted",
                checkpoint={"blocked_phase": phase.value},
                marker="failed",
            ):
                return True, phase
            return True, phase
        self._continue_as_new(
            request,
            progress=progress,
            next_phase=phase,
            resume_attempts=self._resume_attempts,
        )

    async def _request_review(
        self,
        request: GoalRunInput,
        step: GoalRunStepResult,
    ) -> GoalRunReviewRequestResult:
        digest = step.output_sha256
        if digest is None:
            raise RuntimeError("review request is missing snapshot digest")
        self._phase = GoalRunPhase.REVIEW
        self._status = GoalRunStatus.WAITING_REVIEW
        review_kind_value = step.details.get("review_kind")
        review_kind = (
            review_kind_value
            if isinstance(review_kind_value, str) and review_kind_value
            else "goal_run_application_review.v1"
        )
        result = await workflow.execute_activity(
            GOAL_RUN_REQUEST_REVIEW_ACTIVITY,
            GoalRunReviewRequestCommand(
                goal_run_id=request.goal_run_id,
                owner_user_id=request.owner_user_id,
                expected_version=self._version,
                fencing_token=request.fencing_token,
                review_kind=review_kind,
                review_payload=step.details,
                snapshot_sha256=digest,
                idempotency_key=self._idempotency_key(
                    request,
                    GoalRunPhase.REVIEW,
                    "request",
                ),
                trace_id=self._trace_id(request.goal_run_id),
            ),
            result_type=GoalRunReviewRequestResult,
            activity_id=self._activity_id("request-review"),
            start_to_close_timeout=_ACTIVITY_START_TO_CLOSE,
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )
        self._version = result.version
        self._pending_review_item_id = result.review_item_id
        return result

    async def _wait_for_review(
        self,
        request: GoalRunInput,
        progress: GoalRunProgress,
    ) -> tuple[bool, bool]:
        self._phase = GoalRunPhase.REVIEW
        self._status = GoalRunStatus.WAITING_REVIEW
        self._pending_review_item_id = progress.review_item_id
        while True:
            # Postgres is command authority. A DB-first operator command can commit even if the
            # best-effort Temporal wake signal is temporarily unavailable.
            with suppress(TimeoutError):
                await workflow.wait_condition(
                    lambda: self._review_signal is not None or self._cancel_signal is not None,
                    timeout=_CONTROL_PLANE_POLL_INTERVAL,
                    timeout_summary="goal-review-db-poll",
                )
            snapshot = await self._load_run(request)
            self._apply_snapshot(snapshot)
            if snapshot.status in {
                GoalRunStatus.CANCELLED,
                GoalRunStatus.REJECTED,
                GoalRunStatus.FAILED,
            }:
                return False, True
            if request.mode is GoalRunMode.PRE_APPLICATION_ONLY:
                if (
                    snapshot.status is GoalRunStatus.COMPLETED
                    and snapshot.phase is GoalRunPhase.COMPLETED
                    and snapshot.review_decision is GoalRunReviewDecision.APPROVE
                ):
                    return True, True
                if (
                    snapshot.status is GoalRunStatus.RUNNING
                    and snapshot.review_decision is GoalRunReviewDecision.APPROVE
                ):
                    # A pre-application approval must be terminal in Postgres. If an old or
                    # inconsistent database function exposes dispatch-authorized state, close
                    # the workflow path before any provider activity can run.
                    await self._checkpoint(
                        request,
                        phase=GoalRunPhase.BLOCKED,
                        status=GoalRunStatus.BLOCKED,
                        outcome="pre_application_approval_not_terminal",
                        checkpoint={
                            "reason_code": "BLOCKED_PREAPPLICATION_APPROVAL_NOT_TERMINAL",
                        },
                        marker="preapp-approval-fail-closed",
                    )
                    return False, True
                self._review_signal = None
                self._cancel_signal = None
                self._ignored_signals += 1
                continue
            if (
                snapshot.status is GoalRunStatus.RUNNING
                and snapshot.review_decision is GoalRunReviewDecision.APPROVE
            ):
                return True, False
            self._review_signal = None
            self._cancel_signal = None
            self._ignored_signals += 1

    async def _wait_for_resume(self, request: GoalRunInput) -> tuple[bool, bool]:
        while True:
            # The durable command may be ahead of a lost wake signal; polling avoids an
            # indefinitely sleeping workflow without treating a signal as authorization.
            with suppress(TimeoutError):
                await workflow.wait_condition(
                    lambda: self._resume_signal is not None or self._cancel_signal is not None,
                    timeout=_CONTROL_PLANE_POLL_INTERVAL,
                    timeout_summary="goal-resume-db-poll",
                )
            snapshot = await self._load_run(request)
            self._apply_snapshot(snapshot)
            if snapshot.status in _TERMINAL_STATUSES:
                return False, True
            if snapshot.status is GoalRunStatus.RUNNING:
                self._resume_signal = None
                return True, False
            self._resume_signal = None
            self._cancel_signal = None
            self._ignored_signals += 1

    async def _authorized_external_state_if_signaled(
        self,
        request: GoalRunInput,
    ) -> GoalRunSnapshot | None:
        if (
            self._cancel_signal is None
            and self._review_signal is None
            and self._resume_signal is None
        ):
            return None
        snapshot = await self._load_run(request)
        self._apply_snapshot(snapshot)
        if snapshot.status not in _TERMINAL_STATUSES:
            self._cancel_signal = None
            self._review_signal = None
            self._resume_signal = None
        return snapshot

    async def _load_run(self, request: GoalRunInput) -> GoalRunSnapshot:
        self._load_count += 1
        return await workflow.execute_activity(
            GOAL_RUN_LOAD_ACTIVITY,
            GoalRunLoadCommand(
                goal_run_id=request.goal_run_id,
                owner_user_id=request.owner_user_id,
            ),
            result_type=GoalRunSnapshot,
            activity_id=self._activity_id(f"load-{self._load_count}"),
            start_to_close_timeout=_ACTIVITY_START_TO_CLOSE,
            retry_policy=_ACTIVITY_RETRY_POLICY,
        )

    async def _best_effort_fail_source(
        self,
        claim: GoalRunClaimResult,
        error_code: str,
    ) -> None:
        try:
            await workflow.execute_activity(
                GOAL_RUN_FAIL_SOURCE_ACTIVITY,
                GoalRunFailCommand(
                    registry_id=claim.registry_id,
                    source_id=claim.source_id,
                    run_id=claim.run_id,
                    source_row_id=claim.source_row_id,
                    worker_id=claim.lease_owner,
                    lease_token=claim.lease_token,
                    error_code=error_code,
                ),
                result_type=str,
                activity_id=self._activity_id("fail-source"),
                start_to_close_timeout=_ACTIVITY_START_TO_CLOSE,
                retry_policy=_ACTIVITY_RETRY_POLICY,
            )
        except Exception:
            # The GoalRun checkpoint below remains the durable operator-visible failure. A stale
            # lease can legitimately prevent a second source-run failure write.
            return

    async def _handle_unready_discovery(
        self,
        request: GoalRunInput,
        *,
        progress: GoalRunProgress,
        claim: GoalRunClaimResult,
        discovery: GoalRunDiscoveryResult,
    ) -> GoalRunResult:
        await self._best_effort_fail_source(claim, discovery.result)
        cleared_progress = replace(
            progress,
            source_row_id=None,
            crawler_run_id=None,
            output_manifest_sha256=None,
        )
        checkpoint: dict[str, JsonValue] = {
            "blocked_phase": GoalRunPhase.DISCOVERY.value,
            "discovery_state": discovery.state.value,
            "reason_code": discovery.result,
            "crawler_execution_request_id": (
                str(discovery.crawler_execution_request_id)
                if discovery.crawler_execution_request_id is not None
                else None
            ),
            "crawler_execution_result_id": (
                str(discovery.crawler_execution_result_id)
                if discovery.crawler_execution_result_id is not None
                else None
            ),
        }
        if discovery.state is GoalRunDiscoveryState.RECONCILIATION_REQUIRED:
            await self._checkpoint(
                request,
                phase=GoalRunPhase.RECONCILIATION,
                status=GoalRunStatus.RECONCILIATION_REQUIRED,
                outcome=discovery.result,
                checkpoint=checkpoint,
                marker="crawler-reconciliation-required",
            )
            return self._terminal_result(
                cleared_progress,
                reason_code=discovery.result,
            )

        await self._checkpoint(
            request,
            phase=GoalRunPhase.BLOCKED,
            status=GoalRunStatus.BLOCKED,
            outcome=discovery.result,
            checkpoint=checkpoint,
            marker=f"crawler-{discovery.state.value}",
        )
        resumed, terminal = await self._wait_for_resume(request)
        if terminal:
            return self._terminal_result(cleared_progress, reason_code="operator_command")
        if not resumed:
            raise RuntimeError("crawler review wait ended without an authorized resume")
        self._resume_attempts += 1
        if self._resume_attempts > _MAX_RESUME_ATTEMPTS:
            await self._checkpoint(
                request,
                phase=GoalRunPhase.FAILED,
                status=GoalRunStatus.FAILED,
                outcome="resume_budget_exhausted",
                checkpoint={"blocked_phase": GoalRunPhase.DISCOVERY.value},
                marker="failed",
            )
            return self._terminal_result(
                cleared_progress,
                reason_code="resume_budget_exhausted",
            )
        self._continue_as_new(
            request,
            progress=cleared_progress,
            next_phase=GoalRunPhase.CLAIMING_SOURCE,
            resume_attempts=self._resume_attempts,
        )
        raise RuntimeError("continue-as-new returned unexpectedly")

    def _domain_command(
        self,
        request: GoalRunInput,
        progress: GoalRunProgress,
    ) -> GoalRunDomainCommand:
        if (
            progress.source_id is None
            or progress.source_row_id is None
            or progress.crawler_run_id is None
        ):
            raise RuntimeError("canonical source progress is unavailable")
        return GoalRunDomainCommand(
            goal_run_id=request.goal_run_id,
            owner_user_id=request.owner_user_id,
            registry_id=request.registry_id,
            source_id=progress.source_id,
            source_row_id=progress.source_row_id,
            crawler_run_id=progress.crawler_run_id,
            max_records=request.max_records,
            expected_match_snapshot_sha256=progress.match_snapshot_sha256,
        )

    def _continue_as_new(
        self,
        request: GoalRunInput,
        *,
        progress: GoalRunProgress,
        next_phase: GoalRunPhase,
        resume_attempts: int,
    ) -> None:
        workflow.continue_as_new(
            replace(
                request,
                expected_version=self._version,
                next_phase=next_phase,
                continuation_count=self._continuation_count + 1,
                resume_attempts=resume_attempts,
                progress=progress,
            )
        )

    def _apply_snapshot(self, snapshot: GoalRunSnapshot) -> None:
        if self._goal_run_id is not None and snapshot.goal_run_id != self._goal_run_id:
            raise RuntimeError("GoalRun snapshot identity mismatch")
        if self._owner_user_id is not None and snapshot.owner_user_id != self._owner_user_id:
            raise RuntimeError("GoalRun snapshot owner mismatch")
        if snapshot.mode is not self._mode:
            raise RuntimeError("GoalRun snapshot mode mismatch")
        self._version = snapshot.version
        self._phase = snapshot.phase
        self._status = snapshot.status
        self._pending_review_item_id = snapshot.pending_review_item_id
        self._last_error_code = snapshot.last_error_code

    def _terminal_result(
        self,
        progress: GoalRunProgress,
        *,
        reason_code: str,
    ) -> GoalRunResult:
        goal_run_id = self._goal_run_id
        if goal_run_id is None:
            raise RuntimeError("GoalRun identity is unavailable")
        return GoalRunResult(
            goal_run_id=goal_run_id,
            status=self._status,
            current_phase=self._phase,
            version=self._version,
            source_id=progress.source_id,
            source_row_id=progress.source_row_id,
            crawler_run_id=progress.crawler_run_id,
            observed_records=progress.observed_records,
            inserted_versions=progress.inserted_versions,
            reused_versions=progress.reused_versions,
            reason_code=reason_code,
            mode=self._mode,
            completion_kind=(
                "pre_application_package_approved"
                if self._mode is GoalRunMode.PRE_APPLICATION_ONLY
                and self._status is GoalRunStatus.COMPLETED
                else None
            ),
        )

    def _activity_id(self, marker: str) -> str:
        return (f"goal-{self._continuation_count}-{self._resume_attempts}-{marker}")[:240]

    def _idempotency_key(
        self,
        request: GoalRunInput,
        phase: GoalRunPhase,
        marker: str,
    ) -> str:
        return (
            f"goal-run:{request.goal_run_id}:{self._continuation_count}:"
            f"{self._resume_attempts}:{phase.value}:{marker}"
        )[:240]

    @staticmethod
    def _trace_id(goal_run_id: UUID) -> str:
        return f"goal-run:{goal_run_id}"


def _safe_error_code(exc: Exception) -> str:
    name = exc.__class__.__name__.lower()
    if name in {"activityerror", "timeouterror", "cancellederror"}:
        return f"temporal_{name}"
    return "goal_step_failed"
