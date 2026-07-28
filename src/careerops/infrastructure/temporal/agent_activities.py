"""AgentRunWorkflow activities: all five stages of the agent-execution lifecycle.

Activities are the I/O boundary.  Workflow code never touches the network,
database, or model gateway directly.  Each activity receives a typed input
and returns an ``ActivityReceipt`` whose ``output`` is activity-specific.

Activity side effects and timeouts (from the temporal contract):
- resolve_agent_context   : 30s, no retry on CONTEXT_STALE
- run_deterministic_stage : 60s, at most 2 retries with jitter
- invoke_model_stage      : 90s, at most 1 retry, never after consent/policy drift
- persist_agent_stage     : 30s, CAS retry (same receipt only)
- reconcile_agent_run     : 30s, no blind retry

Activity implementations are Protocol-typed so tests can substitute NoOp
sinks.  Concrete service dependencies are injected at construction time.

Reference: openspec/changes/agent-first-console-experience/docs/agent-console/temporal-contract.md
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from careerops.workflows.agent_contracts import (
    INVOKE_MODEL_STAGE_ACTIVITY,
    PERSIST_AGENT_STAGE_ACTIVITY,
    RECONCILE_AGENT_RUN_ACTIVITY,
    RESOLVE_AGENT_CONTEXT_ACTIVITY,
    RUN_DETERMINISTIC_STAGE_ACTIVITY,
    ActivityReceipt,
    InvokeModelStageInput,
    InvokeModelStageOutput,
    PersistAgentStageInput,
    PersistAgentStageOutput,
    ReconcileAgentRunInput,
    ReconcileAgentRunOutput,
    ResolveAgentContextInput,
    ResolveAgentContextOutput,
    RunDeterministicStageInput,
    RunDeterministicStageOutput,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols (activity-side service seams)
# ---------------------------------------------------------------------------


class AgentContextResolver(Protocol):
    """Resolves and validates an agent context."""

    async def resolve(
        self,
        context_id: str,
        operation: str,
        expected_digest: str,
    ) -> ResolveAgentContextOutput: ...


class DeterministicStageRunner(Protocol):
    """Runs a deterministic (non-model) stage."""

    async def run_stage(
        self,
        stage: str,
        context_digest: str,
        input_refs: list[dict[str, Any]],
    ) -> RunDeterministicStageOutput: ...


class ModelStageInvoker(Protocol):
    """Invokes the model gateway for an agent stage."""

    async def invoke(
        self,
        operation: str,
        context_digest: str,
        consent_id: str,
        preflight_id: str,
        field_set_hash: str,
        provider_policy_version: str,
    ) -> InvokeModelStageOutput: ...


class AgentStagePersister(Protocol):
    """Persists a stage event with CAS semantics."""

    async def persist(
        self,
        stage: str,
        event_key: str,
        sequence: int,
        outcome: str,
        redacted_payload: dict[str, Any],
        result_digest: str,
    ) -> PersistAgentStageOutput: ...


class AgentRunReconciler(Protocol):
    """Reconciles an agent run to its terminal state."""

    async def reconcile(
        self,
        expected_attempt_id: str,
        expected_lease_epoch: int,
        expected_cancel_epoch: int,
        terminal_state: str,
    ) -> ReconcileAgentRunOutput: ...


# ---------------------------------------------------------------------------
# NoOp sinks (tests / degraded mode)
# ---------------------------------------------------------------------------


class NoOpAgentContextResolver:
    """Returns a synthetic resolved context without performing I/O."""

    async def resolve(
        self,
        context_id: str,
        operation: str,
        expected_digest: str,
    ) -> ResolveAgentContextOutput:
        return ResolveAgentContextOutput(
            context_id=context_id,
            source_version_snapshot="noop-v1",
            source_digests=[expected_digest],
            allowed_operation=operation,
            state="active",
            expires_at="2099-12-31T23:59:59Z",
        )


class NoOpDeterministicStageRunner:
    """Returns a synthetic deterministic stage result."""

    async def run_stage(
        self,
        stage: str,
        context_digest: str,
        input_refs: list[dict[str, Any]],
    ) -> RunDeterministicStageOutput:
        return RunDeterministicStageOutput(
            stage=stage,
            result_digest=context_digest,
            verdict="pass",
        )


class NoOpModelStageInvoker:
    """Returns a synthetic model stage result."""

    async def invoke(
        self,
        operation: str,
        context_digest: str,
        consent_id: str,
        preflight_id: str,
        field_set_hash: str,
        provider_policy_version: str,
    ) -> InvokeModelStageOutput:
        return InvokeModelStageOutput(
            provider_id="noop-provider",
            model_id="noop-model",
            response_digest="a" * 64,
            schema_version="schema-v1",
            prompt_version="prompt-v1",
            field_set_hash=field_set_hash,
        )


class NoOpAgentStagePersister:
    """Returns a synthetic persist receipt."""

    async def persist(
        self,
        stage: str,
        event_key: str,
        sequence: int,
        outcome: str,
        redacted_payload: dict[str, Any],
        result_digest: str,
    ) -> PersistAgentStageOutput:
        return PersistAgentStageOutput(
            stage_event_id=str(uuid.uuid4()),
            event_key=event_key,
            sequence=sequence,
            stored_state=outcome,
            audit_receipt_id=str(uuid.uuid4()),
        )


class NoOpAgentRunReconciler:
    """Returns a synthetic reconciled state."""

    async def reconcile(
        self,
        expected_attempt_id: str,
        expected_lease_epoch: int,
        expected_cancel_epoch: int,
        terminal_state: str,
    ) -> ReconcileAgentRunOutput:
        return ReconcileAgentRunOutput(
            execution_state=terminal_state,
            capability_state="enabled",
            review_state="not_required",
            current_attempt=1,
            reconciled_event_key=str(uuid.uuid4()),
        )


# ---------------------------------------------------------------------------
# Unavailable stubs (fail-closed when services not injected)
# ---------------------------------------------------------------------------


class _UnavailableAgentContextResolver:
    async def resolve(
        self,
        context_id: str,
        operation: str,
        expected_digest: str,
    ) -> ResolveAgentContextOutput:
        raise RuntimeError("agent context resolver is not wired")


class _UnavailableDeterministicStageRunner:
    async def run_stage(
        self,
        stage: str,
        context_digest: str,
        input_refs: list[dict[str, Any]],
    ) -> RunDeterministicStageOutput:
        raise RuntimeError("deterministic stage runner is not wired")


class _UnavailableModelStageInvoker:
    async def invoke(
        self,
        operation: str,
        context_digest: str,
        consent_id: str,
        preflight_id: str,
        field_set_hash: str,
        provider_policy_version: str,
    ) -> InvokeModelStageOutput:
        raise RuntimeError("model stage invoker is not wired")


class _UnavailableAgentStagePersister:
    async def persist(
        self,
        stage: str,
        event_key: str,
        sequence: int,
        outcome: str,
        redacted_payload: dict[str, Any],
        result_digest: str,
    ) -> PersistAgentStageOutput:
        raise RuntimeError("agent stage persister is not wired")


class _UnavailableAgentRunReconciler:
    async def reconcile(
        self,
        expected_attempt_id: str,
        expected_lease_epoch: int,
        expected_cancel_epoch: int,
        terminal_state: str,
    ) -> ReconcileAgentRunOutput:
        raise RuntimeError("agent run reconciler is not wired")


# ---------------------------------------------------------------------------
# Activity bundle (injectable into the worker)
# ---------------------------------------------------------------------------


class AgentActivities:
    """All five AgentRunWorkflow activities.

    Service dependencies are injected at construction time.  When running
    without injection (tests, degraded mode), NoOp stubs are used.
    A production worker with missing wiring must fail closed.
    """

    def __init__(
        self,
        context_resolver: AgentContextResolver | None = None,
        deterministic_runner: DeterministicStageRunner | None = None,
        model_invoker: ModelStageInvoker | None = None,
        stage_persister: AgentStagePersister | None = None,
        run_reconciler: AgentRunReconciler | None = None,
    ) -> None:
        self._context_resolver: AgentContextResolver = (
            context_resolver or _UnavailableAgentContextResolver()
        )
        self._deterministic_runner: DeterministicStageRunner = (
            deterministic_runner or _UnavailableDeterministicStageRunner()
        )
        self._model_invoker: ModelStageInvoker = model_invoker or _UnavailableModelStageInvoker()
        self._stage_persister: AgentStagePersister = (
            stage_persister or _UnavailableAgentStagePersister()
        )
        self._run_reconciler: AgentRunReconciler = (
            run_reconciler or _UnavailableAgentRunReconciler()
        )

    # ------------------------------------------------------------------
    # Activity 1: resolve_agent_context
    # ------------------------------------------------------------------

    @activity.defn(name=RESOLVE_AGENT_CONTEXT_ACTIVITY)
    async def resolve_agent_context(
        self,
        request: ResolveAgentContextInput,
    ) -> ActivityReceipt:
        """Resolve and validate an agent context.

        30s timeout.  CONTEXT_STALE is non-retryable.
        """
        activity.logger.info(
            "resolve_agent_context: context=%s operation=%s",
            request.context_id,
            request.operation,
        )

        try:
            output = await self._context_resolver.resolve(
                context_id=request.context_id,
                operation=request.operation,
                expected_digest=request.expected_digest,
            )
        except ApplicationError:
            raise
        except Exception as exc:
            activity.logger.exception("resolve_agent_context failed")
            raise ApplicationError(
                f"CONTEXT_STALE: {type(exc).__name__}: {exc}",
                non_retryable=True,
                type="CONTEXT_STALE",
            ) from exc

        return ActivityReceipt(
            attempt_id=str(uuid.uuid4()),
            lease_epoch=1,
            cancel_epoch=0,
            stage="resolve_agent_context",
            event_key=str(uuid.uuid4()),
            sequence=1,
            outcome="completed",
            next_state="running",
            output={
                "context_id": output.context_id,
                "source_version_snapshot": output.source_version_snapshot,
                "source_digests": list(output.source_digests),
                "allowed_operation": output.allowed_operation,
                "state": output.state,
                "expires_at": output.expires_at,
            },
        )

    # ------------------------------------------------------------------
    # Activity 2: run_deterministic_stage
    # ------------------------------------------------------------------

    @activity.defn(name=RUN_DETERMINISTIC_STAGE_ACTIVITY)
    async def run_deterministic_stage(
        self,
        request: RunDeterministicStageInput,
    ) -> ActivityReceipt:
        """Run a deterministic (non-model) stage.

        60s timeout.  At most 2 retries with jitter for transient failures.
        """
        activity.logger.info(
            "run_deterministic_stage: stage=%s digest=%s",
            request.stage,
            request.context_digest[:16],
        )

        try:
            input_ref_dicts = [
                {"type": r.type, "id": r.id, "version": r.version} for r in request.input_refs
            ]
            output = await self._deterministic_runner.run_stage(
                stage=request.stage,
                context_digest=request.context_digest,
                input_refs=input_ref_dicts,
            )
        except ApplicationError:
            raise
        except Exception as exc:
            activity.logger.exception("run_deterministic_stage failed")
            raise ApplicationError(
                f"deterministic stage failed: {type(exc).__name__}: {exc}",
                non_retryable=False,
                type=type(exc).__name__,
            ) from exc

        return ActivityReceipt(
            attempt_id=str(uuid.uuid4()),
            lease_epoch=1,
            cancel_epoch=0,
            stage="run_deterministic_stage",
            event_key=str(uuid.uuid4()),
            sequence=2,
            outcome="completed",
            next_state="running",
            output={
                "stage": output.stage,
                "result_digest": output.result_digest,
                "result_refs": [
                    {"type": r.type, "id": r.id, "version": r.version, "digest": r.digest}
                    for r in output.result_refs
                ],
                "verdict": output.verdict,
                "unknowns": list(output.unknowns),
            },
        )

    # ------------------------------------------------------------------
    # Activity 3: invoke_model_stage
    # ------------------------------------------------------------------

    @activity.defn(name=INVOKE_MODEL_STAGE_ACTIVITY)
    async def invoke_model_stage(
        self,
        request: InvokeModelStageInput,
    ) -> ActivityReceipt:
        """Invoke the model gateway for an agent stage.

        90s timeout.  At most 1 retry.  Never retries after consent/policy drift.
        """
        activity.logger.info(
            "invoke_model_stage: operation=%s consent=%s",
            request.operation,
            request.consent_id,
        )

        try:
            output = await self._model_invoker.invoke(
                operation=request.operation,
                context_digest=request.context_digest,
                consent_id=request.consent_id,
                preflight_id=request.preflight_id,
                field_set_hash=request.field_set_hash,
                provider_policy_version=request.provider_policy_version,
            )
        except ApplicationError:
            raise
        except Exception as exc:
            activity.logger.exception("invoke_model_stage failed")
            # Classify: 429/5xx are retryable, policy/consent errors are not.
            raise ApplicationError(
                f"model invocation failed: {type(exc).__name__}: {exc}",
                non_retryable=False,
                type=type(exc).__name__,
            ) from exc

        return ActivityReceipt(
            attempt_id=str(uuid.uuid4()),
            lease_epoch=1,
            cancel_epoch=0,
            stage="invoke_model_stage",
            event_key=str(uuid.uuid4()),
            sequence=3,
            outcome="completed",
            next_state="running",
            output={
                "provider_id": output.provider_id,
                "model_id": output.model_id,
                "response_digest": output.response_digest,
                "schema_version": output.schema_version,
                "prompt_version": output.prompt_version,
                "field_set_hash": output.field_set_hash,
                "usage": {
                    "input_tokens": output.usage.input_tokens,
                    "output_tokens": output.usage.output_tokens,
                    "latency_ms": output.usage.latency_ms,
                },
                "proposal_digest": output.proposal_digest,
                "uncertainties": list(output.uncertainties),
            },
        )

    # ------------------------------------------------------------------
    # Activity 4: persist_agent_stage
    # ------------------------------------------------------------------

    @activity.defn(name=PERSIST_AGENT_STAGE_ACTIVITY)
    async def persist_agent_stage(
        self,
        request: PersistAgentStageInput,
    ) -> ActivityReceipt:
        """Persist a stage event with CAS semantics.

        30s timeout.  Retry only with the same receipt (CAS conflict).
        A duplicate identical receipt is a replay; a different payload is 409.
        """
        activity.logger.info(
            "persist_agent_stage: stage=%s event_key=%s seq=%d",
            request.stage,
            request.event_key,
            request.sequence,
        )

        try:
            output = await self._stage_persister.persist(
                stage=request.stage,
                event_key=request.event_key,
                sequence=request.sequence,
                outcome=request.outcome,
                redacted_payload=dict(request.redacted_payload),
                result_digest=request.result_digest,
            )
        except ApplicationError:
            raise
        except Exception as exc:
            activity.logger.exception("persist_agent_stage failed")
            raise ApplicationError(
                f"persist failed: {type(exc).__name__}: {exc}",
                non_retryable=False,
                type=type(exc).__name__,
            ) from exc

        return ActivityReceipt(
            attempt_id=str(uuid.uuid4()),
            lease_epoch=1,
            cancel_epoch=0,
            stage="persist_agent_stage",
            event_key=output.event_key,
            sequence=output.sequence,
            outcome="completed",
            next_state="running",
            output={
                "stage_event_id": output.stage_event_id,
                "event_key": output.event_key,
                "sequence": output.sequence,
                "stored_state": output.stored_state,
                "audit_receipt_id": output.audit_receipt_id,
            },
        )

    # ------------------------------------------------------------------
    # Activity 5: reconcile_agent_run
    # ------------------------------------------------------------------

    @activity.defn(name=RECONCILE_AGENT_RUN_ACTIVITY)
    async def reconcile_agent_run(
        self,
        request: ReconcileAgentRunInput,
    ) -> ActivityReceipt:
        """Reconcile the agent run to its terminal state.

        30s timeout.  No blind retry — CAS terminal projection and audit.
        """
        activity.logger.info(
            "reconcile_agent_run: attempt=%s terminal=%s",
            request.expected_attempt_id,
            request.terminal_state,
        )

        try:
            output = await self._run_reconciler.reconcile(
                expected_attempt_id=request.expected_attempt_id,
                expected_lease_epoch=request.expected_lease_epoch,
                expected_cancel_epoch=request.expected_cancel_epoch,
                terminal_state=request.terminal_state,
            )
        except ApplicationError:
            raise
        except Exception as exc:
            activity.logger.exception("reconcile_agent_run failed")
            raise ApplicationError(
                f"reconcile failed: {type(exc).__name__}: {exc}",
                non_retryable=True,
                type=type(exc).__name__,
            ) from exc

        return ActivityReceipt(
            attempt_id=request.expected_attempt_id,
            lease_epoch=request.expected_lease_epoch,
            cancel_epoch=request.expected_cancel_epoch,
            stage="reconcile_agent_run",
            event_key=output.reconciled_event_key,
            sequence=5,
            outcome="completed",
            next_state=output.execution_state,
            output={
                "execution_state": output.execution_state,
                "capability_state": output.capability_state,
                "review_state": output.review_state,
                "current_attempt": output.current_attempt,
                "reconciled_event_key": output.reconciled_event_key,
            },
        )
