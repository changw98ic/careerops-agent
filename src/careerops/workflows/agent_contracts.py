"""Workflow contracts for AgentRunWorkflow.

Activity names, input/output dataclasses, signal/query payloads, and
error codes for the agent-execution Temporal wiring.  Follows the same
frozen-dataclass pattern as ``s5_contracts.py`` and ``smoke_contracts.py``.

Reference: openspec/changes/agent-first-console-experience/docs/agent-console/temporal-contract.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Activity names
# ---------------------------------------------------------------------------

RESOLVE_AGENT_CONTEXT_ACTIVITY = "careerops.agent.resolve_agent_context"
RUN_DETERMINISTIC_STAGE_ACTIVITY = "careerops.agent.run_deterministic_stage"
INVOKE_MODEL_STAGE_ACTIVITY = "careerops.agent.invoke_model_stage"
PERSIST_AGENT_STAGE_ACTIVITY = "careerops.agent.persist_agent_stage"
RECONCILE_AGENT_RUN_ACTIVITY = "careerops.agent.reconcile_agent_run"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AgentOperation(StrEnum):
    JOB_MATCHING = "job_matching"
    RESUME_REVIEW = "resume_review"
    INTERVIEW_PREPARATION = "interview_preparation"


class ExecutionState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    STALE = "stale"
    BLOCKED = "blocked"


class CapabilityState(StrEnum):
    ENABLED = "enabled"
    DISABLED_BY_POLICY = "disabled_by_policy"
    NOT_CONFIGURED = "not_configured"
    DEPENDENCY_NOT_READY = "dependency_not_ready"
    BLOCKED_BY_PREREQUISITE = "blocked_by_prerequisite"
    STALE = "stale"
    FAILED = "failed"


class ReviewState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EDITED = "edited"


class StageOutcome(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class StopReason(StrEnum):
    USER_STOPPED = "USER_STOPPED"
    POLICY_REVOKED = "POLICY_REVOKED"
    OPERATOR_STOPPED = "OPERATOR_STOPPED"


class TerminalState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"
    BLOCKED = "blocked"


class DeterministicStage(StrEnum):
    NORMALIZE = "normalize"
    FILTER = "filter"
    RANK = "rank"
    PREPARE = "prepare"


class RefType(StrEnum):
    JOB = "job"
    EVIDENCE = "evidence"
    PROFILE = "profile"


# ---------------------------------------------------------------------------
# Shared envelope types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActivityEnvelope:
    """Shared envelope attached to every activity invocation.

    ``attempt_id``, ``lease_epoch``, ``cancel_epoch``, ``trace_id``, and
    ``input_digest`` are set by the workflow before each activity call.
    """

    run_id: str
    candidate_id: str
    attempt_id: str
    lease_epoch: int
    cancel_epoch: int
    trace_id: str
    input_digest: str


@dataclass(frozen=True, slots=True)
class ActivityReceipt:
    """Canonical receipt returned by every activity.

    ``output`` is activity-specific; its shape is described in the temporal
    contract annex.
    """

    attempt_id: str
    lease_epoch: int
    cancel_epoch: int
    stage: str
    event_key: str
    sequence: int
    outcome: str
    next_state: str
    output: dict[str, object] = field(default_factory=dict[str, object])
    trace_id: str = ""


# ---------------------------------------------------------------------------
# Activity payload inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolveAgentContextInput:
    context_id: str
    operation: str
    expected_digest: str


@dataclass(frozen=True, slots=True)
class InputRef:
    type: str
    id: str
    version: int


@dataclass(frozen=True, slots=True)
class RunDeterministicStageInput:
    stage: str
    context_digest: str
    input_refs: list[InputRef] = field(default_factory=list[InputRef])


@dataclass(frozen=True, slots=True)
class InvokeModelStageInput:
    operation: str
    context_digest: str
    consent_id: str
    preflight_id: str
    field_set_hash: str
    provider_policy_version: str


@dataclass(frozen=True, slots=True)
class PersistAgentStageInput:
    stage: str
    event_key: str
    sequence: int
    outcome: str
    redacted_payload: dict[str, object] = field(default_factory=dict[str, object])
    result_digest: str = ""


@dataclass(frozen=True, slots=True)
class ReconcileAgentRunInput:
    expected_attempt_id: str
    expected_lease_epoch: int
    expected_cancel_epoch: int
    terminal_state: str


# ---------------------------------------------------------------------------
# Activity output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolveAgentContextOutput:
    context_id: str
    source_version_snapshot: str
    source_digests: list[str] = field(default_factory=list[str])
    allowed_operation: str = ""
    state: str = ""
    expires_at: str = ""


@dataclass(frozen=True, slots=True)
class ResultRef:
    type: str
    id: str
    version: int
    digest: str


@dataclass(frozen=True, slots=True)
class RunDeterministicStageOutput:
    stage: str
    result_digest: str
    result_refs: list[ResultRef] = field(default_factory=list[ResultRef])
    verdict: str = ""
    unknowns: list[str] = field(default_factory=list[str])


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class InvokeModelStageOutput:
    provider_id: str
    model_id: str
    response_digest: str
    schema_version: str
    prompt_version: str
    field_set_hash: str
    usage: ModelUsage = field(default_factory=ModelUsage)
    proposal_digest: str = ""
    uncertainties: list[str] = field(default_factory=list[str])


@dataclass(frozen=True, slots=True)
class PersistAgentStageOutput:
    stage_event_id: str
    event_key: str
    sequence: int
    stored_state: str
    audit_receipt_id: str


@dataclass(frozen=True, slots=True)
class ReconcileAgentRunOutput:
    execution_state: str
    capability_state: str
    review_state: str
    current_attempt: int
    reconciled_event_key: str


# ---------------------------------------------------------------------------
# Workflow I/O
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentRunWorkflowInput:
    """Input for AgentRunWorkflow.

    Workflow ID: ``agent-run:{candidate_id}:{logical_run_id}``.
    """

    candidate_id: str
    logical_run_id: str
    context_id: str
    operation: str
    context_digest: str
    policy_version: str
    consent_id: str
    trace_id: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class AgentRunWorkflowResult:
    """Terminal result returned by AgentRunWorkflow."""

    run_id: str
    candidate_id: str
    logical_run_id: str
    execution_state: str
    capability_state: str
    review_state: str
    current_attempt: int
    last_event_key: str
    trace_id: str


# ---------------------------------------------------------------------------
# Signal / Query payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StopSignal:
    """Payload for the ``stop`` signal."""

    reason_code: str
    request_id: str


@dataclass(frozen=True, slots=True)
class RefreshContextSignal:
    """Payload for the ``refresh_context`` signal."""

    context_id: str
    context_digest: str
    source_event_key: str


@dataclass(frozen=True, slots=True)
class AgentRunStatus:
    """Redacted status returned by the ``status`` query.

    Never exposes worker ID, provider request, raw error, prompt, response,
    or lease token.
    """

    run_id: str
    execution_state: str
    capability_state: str
    review_state: str
    current_attempt: int
    latest_stage: str
    completed: int
    total: int
    retryable: bool
    failure_code: str | None
    trace_id: str
