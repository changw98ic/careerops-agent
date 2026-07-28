"""AgentRunWorkflow: durable agent-execution orchestration.

Workflow ID: ``agent-run:{candidate_id}:{logical_run_id}``
Task queue:  ``careerops-agent``

The workflow executes five activities in sequence:

1. resolve_agent_context   — candidate-scoped read, 30s timeout
2. run_deterministic_stage — deterministic repo read, 60s timeout, 2 retries
3. invoke_model_stage      — model gateway call, 90s timeout, 1 retry
4. persist_agent_stage     — DB + audit write, 30s timeout, CAS retry
5. reconcile_agent_run     — terminal projection, 30s timeout, no blind retry

Signals:
- ``stop``            — idempotent cancellation (USER_STOPPED / POLICY_REVOKED / OPERATOR_STOPPED)
- ``refresh_context`` — accepted while queued/running/waiting_review

Query:
- ``status`` — redacted execution snapshot (no worker ID, prompts, or lease tokens)

Reference: openspec/changes/agent-first-console-experience/docs/agent-console/temporal-contract.md
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from careerops.workflows.agent_contracts import (
    INVOKE_MODEL_STAGE_ACTIVITY,
    PERSIST_AGENT_STAGE_ACTIVITY,
    RECONCILE_AGENT_RUN_ACTIVITY,
    RESOLVE_AGENT_CONTEXT_ACTIVITY,
    RUN_DETERMINISTIC_STAGE_ACTIVITY,
    ActivityReceipt,
    AgentRunStatus,
    AgentRunWorkflowInput,
    AgentRunWorkflowResult,
    InvokeModelStageInput,
    PersistAgentStageInput,
    ReconcileAgentRunInput,
    RefreshContextSignal,
    ResolveAgentContextInput,
    RunDeterministicStageInput,
    StopSignal,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STAGES: tuple[str, ...] = (
    "resolve_agent_context",
    "run_deterministic_stage",
    "invoke_model_stage",
    "persist_agent_stage",
    "reconcile_agent_run",
)

# ---------------------------------------------------------------------------
# Activity timeouts
# ---------------------------------------------------------------------------

_RESOLVE_CONTEXT_TIMEOUT = timedelta(seconds=30)
_DETERMINISTIC_TIMEOUT = timedelta(seconds=60)
_MODEL_TIMEOUT = timedelta(seconds=90)
_PERSIST_TIMEOUT = timedelta(seconds=30)
_RECONCILE_TIMEOUT = timedelta(seconds=30)

# ---------------------------------------------------------------------------
# Retry policies
# ---------------------------------------------------------------------------

# resolve_agent_context: no retry on CONTEXT_STALE
_RESOLVE_RETRY = RetryPolicy(maximum_attempts=1)

# run_deterministic_stage: at most 2 retries with jitter
_DETERMINISTIC_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)

# invoke_model_stage: at most 1 retry, never after consent/policy drift
_MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=2,
)

# persist_agent_stage: CAS retry (same receipt only)
_PERSIST_RETRY = RetryPolicy(
    initial_interval=timedelta(milliseconds=500),
    maximum_interval=timedelta(seconds=5),
    maximum_attempts=3,
)

# reconcile_agent_run: no blind retry
_RECONCILE_RETRY = RetryPolicy(maximum_attempts=1)

# ---------------------------------------------------------------------------
# Heartbeat interval
# ---------------------------------------------------------------------------

_HEARTBEAT_INTERVAL = timedelta(seconds=10)

# ---------------------------------------------------------------------------
# Cancel-ack deadline
# ---------------------------------------------------------------------------

_CANCEL_ACK_TIMEOUT = timedelta(seconds=30)


@workflow.defn
class AgentRunWorkflow:
    """Durable agent-execution workflow.

    Five sequential activities with signals for stop and context refresh.
    All I/O is performed in activities; workflow code is deterministic.
    """

    def __init__(self) -> None:
        # Mutable workflow state (replayed on replay).
        self._stop: StopSignal | None = None
        self._refresh: RefreshContextSignal | None = None
        self._execution_state: str = "queued"
        self._capability_state: str = "enabled"
        self._review_state: str = "not_required"
        self._current_attempt: int = 1
        self._latest_stage: str = ""
        self._completed_stages: int = 0
        self._failure_code: str | None = None
        self._last_event_key: str = ""

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    @workflow.signal
    def stop(self, signal: StopSignal) -> None:
        """Idempotent stop signal.  Late stops after terminal are ignored."""
        if self._stop is not None:
            return
        self._stop = signal
        self._execution_state = "cancel_requested"

    @workflow.signal
    def refresh_context(self, signal: RefreshContextSignal) -> None:
        """Accept a new context while the run is active."""
        if self._execution_state not in ("queued", "running", "waiting_review"):
            return
        self._refresh = signal

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    @workflow.query
    def status(self) -> AgentRunStatus:
        """Redacted status snapshot — no worker ID, prompts, or lease tokens."""
        return AgentRunStatus(
            run_id="",  # Redacted; the API fills this from the workflow ID.
            execution_state=self._execution_state,
            capability_state=self._capability_state,
            review_state=self._review_state,
            current_attempt=self._current_attempt,
            latest_stage=self._latest_stage,
            completed=self._completed_stages,
            total=len(_STAGES),
            retryable=False,
            failure_code=self._failure_code,
            trace_id="",  # Redacted from the query projection.
        )

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, request: AgentRunWorkflowInput) -> AgentRunWorkflowResult:
        workflow.logger.info(
            "AgentRunWorkflow started: candidate=%s logical_run=%s operation=%s",
            request.candidate_id,
            request.logical_run_id,
            request.operation,
        )

        self._execution_state = "running"

        attempt_id = workflow.info().workflow_id  # Reused as a stable anchor.
        lease_epoch = 1
        cancel_epoch = 0

        # ---- Stage 1: resolve_agent_context ----
        resolve_receipt = await self._execute_resolve_context(
            request,
            attempt_id,
            lease_epoch,
            cancel_epoch,
        )
        if self._stop is not None:
            return await self._reconcile_terminal(
                request,
                attempt_id,
                lease_epoch,
                cancel_epoch,
                "cancelled",
            )
        self._advance("resolve_agent_context", resolve_receipt)

        # ---- Stage 2: run_deterministic_stage ----
        det_receipt = await self._execute_deterministic_stage(
            request,
            attempt_id,
            lease_epoch,
            cancel_epoch,
        )
        if self._stop is not None:
            return await self._reconcile_terminal(
                request,
                attempt_id,
                lease_epoch,
                cancel_epoch,
                "cancelled",
            )
        self._advance("run_deterministic_stage", det_receipt)

        # ---- Stage 3: invoke_model_stage ----
        model_receipt = await self._execute_model_stage(
            request,
            attempt_id,
            lease_epoch,
            cancel_epoch,
        )
        if self._stop is not None:
            return await self._reconcile_terminal(
                request,
                attempt_id,
                lease_epoch,
                cancel_epoch,
                "cancelled",
            )
        self._advance("invoke_model_stage", model_receipt)

        # ---- Stage 4: persist_agent_stage ----
        persist_receipt = await self._execute_persist_stage(
            request,
            attempt_id,
            lease_epoch,
            cancel_epoch,
            model_receipt,
        )
        self._advance("persist_agent_stage", persist_receipt)

        # ---- Stage 5: reconcile_agent_run ----
        return await self._reconcile_terminal(
            request,
            attempt_id,
            lease_epoch,
            cancel_epoch,
            "succeeded",
        )

    # ------------------------------------------------------------------
    # Stage executors
    # ------------------------------------------------------------------

    async def _execute_resolve_context(
        self,
        request: AgentRunWorkflowInput,
        attempt_id: str,
        lease_epoch: int,
        cancel_epoch: int,
    ) -> ActivityReceipt:
        self._latest_stage = "resolve_agent_context"
        receipt: ActivityReceipt = await workflow.execute_activity(
            RESOLVE_AGENT_CONTEXT_ACTIVITY,
            ResolveAgentContextInput(
                context_id=request.context_id,
                operation=request.operation,
                expected_digest=request.context_digest,
            ),
            result_type=ActivityReceipt,
            start_to_close_timeout=_RESOLVE_CONTEXT_TIMEOUT,
            retry_policy=_RESOLVE_RETRY,
            heartbeat_timeout=_HEARTBEAT_INTERVAL,
        )
        return receipt

    async def _execute_deterministic_stage(
        self,
        request: AgentRunWorkflowInput,
        attempt_id: str,
        lease_epoch: int,
        cancel_epoch: int,
    ) -> ActivityReceipt:
        self._latest_stage = "run_deterministic_stage"
        receipt: ActivityReceipt = await workflow.execute_activity(
            RUN_DETERMINISTIC_STAGE_ACTIVITY,
            RunDeterministicStageInput(
                stage="normalize",
                context_digest=request.context_digest,
            ),
            result_type=ActivityReceipt,
            start_to_close_timeout=_DETERMINISTIC_TIMEOUT,
            retry_policy=_DETERMINISTIC_RETRY,
            heartbeat_timeout=_HEARTBEAT_INTERVAL,
        )
        return receipt

    async def _execute_model_stage(
        self,
        request: AgentRunWorkflowInput,
        attempt_id: str,
        lease_epoch: int,
        cancel_epoch: int,
    ) -> ActivityReceipt:
        self._latest_stage = "invoke_model_stage"
        receipt: ActivityReceipt = await workflow.execute_activity(
            INVOKE_MODEL_STAGE_ACTIVITY,
            InvokeModelStageInput(
                operation=request.operation,
                context_digest=request.context_digest,
                consent_id=request.consent_id,
                preflight_id=request.logical_run_id,
                field_set_hash=request.request_fingerprint,
                provider_policy_version=request.policy_version,
            ),
            result_type=ActivityReceipt,
            start_to_close_timeout=_MODEL_TIMEOUT,
            retry_policy=_MODEL_RETRY,
            heartbeat_timeout=_HEARTBEAT_INTERVAL,
        )
        return receipt

    async def _execute_persist_stage(
        self,
        request: AgentRunWorkflowInput,
        attempt_id: str,
        lease_epoch: int,
        cancel_epoch: int,
        prior_receipt: ActivityReceipt,
    ) -> ActivityReceipt:
        self._latest_stage = "persist_agent_stage"
        receipt: ActivityReceipt = await workflow.execute_activity(
            PERSIST_AGENT_STAGE_ACTIVITY,
            PersistAgentStageInput(
                stage=prior_receipt.stage,
                event_key=prior_receipt.event_key,
                sequence=prior_receipt.sequence,
                outcome=prior_receipt.outcome,
                redacted_payload=dict(prior_receipt.output),
                result_digest=str(prior_receipt.output.get("response_digest", "")),
            ),
            result_type=ActivityReceipt,
            start_to_close_timeout=_PERSIST_TIMEOUT,
            retry_policy=_PERSIST_RETRY,
            heartbeat_timeout=_HEARTBEAT_INTERVAL,
        )
        return receipt

    async def _reconcile_terminal(
        self,
        request: AgentRunWorkflowInput,
        attempt_id: str,
        lease_epoch: int,
        cancel_epoch: int,
        terminal_state: str,
    ) -> AgentRunWorkflowResult:
        """Execute reconcile_agent_run and return the workflow result."""
        if self._stop is not None and terminal_state != "cancelled":
            terminal_state = "cancelled"

        self._latest_stage = "reconcile_agent_run"
        receipt: ActivityReceipt = await workflow.execute_activity(
            RECONCILE_AGENT_RUN_ACTIVITY,
            ReconcileAgentRunInput(
                expected_attempt_id=attempt_id,
                expected_lease_epoch=lease_epoch,
                expected_cancel_epoch=cancel_epoch,
                terminal_state=terminal_state,
            ),
            result_type=ActivityReceipt,
            start_to_close_timeout=_RECONCILE_TIMEOUT,
            retry_policy=_RECONCILE_RETRY,
            heartbeat_timeout=_HEARTBEAT_INTERVAL,
        )
        self._advance("reconcile_agent_run", receipt)

        output = receipt.output
        return AgentRunWorkflowResult(
            run_id=str(output.get("reconciled_event_key", "")),
            candidate_id=request.candidate_id,
            logical_run_id=request.logical_run_id,
            execution_state=str(output.get("execution_state", terminal_state)),
            capability_state=str(output.get("capability_state", self._capability_state)),
            review_state=str(output.get("review_state", self._review_state)),
            current_attempt=int(str(output.get("current_attempt", self._current_attempt))),
            last_event_key=str(output.get("reconciled_event_key", "")),
            trace_id=request.trace_id,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _advance(self, stage: str, receipt: ActivityReceipt) -> None:
        """Update mutable workflow state from an activity receipt."""
        self._completed_stages += 1
        self._last_event_key = receipt.event_key
        if receipt.outcome == "failed":
            self._failure_code = str(receipt.output.get("failure_code"))
        next_state = receipt.next_state
        if next_state:
            self._execution_state = next_state
