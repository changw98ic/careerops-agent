"""Agent-console Pydantic contracts (api-contract.md sections 2-3).

Every model inherits ``StrictContract`` (``extra="forbid"``) so unknown
fields are rejected at the boundary.  Field names and shapes are frozen
by the api-contract.md annex and must not drift.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Enums — closed sets from api-contract.md and design.md Decision 3
# ---------------------------------------------------------------------------


class ExecutionState(StrEnum):
    """Lifecycle of an agent run's execution.

    Frozen by design.md Decision 3 and api-contract.md section 3.1 (Run).
    """

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
    """Provider / capability readiness for an operation.

    Frozen by api-contract.md section 3.1 (Capability, Preflight).
    """

    ENABLED = "enabled"
    DISABLED_BY_POLICY = "disabled_by_policy"
    NOT_CONFIGURED = "not_configured"
    DEPENDENCY_NOT_READY = "dependency_not_ready"
    BLOCKED_BY_PREREQUISITE = "blocked_by_prerequisite"
    STALE = "stale"
    FAILED = "failed"


class ReviewState(StrEnum):
    """Human review status on a run result.

    Frozen by design.md Decision 3 and api-contract.md section 3.1 (Run).
    """

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EDITED = "edited"


class ActionState(StrEnum):
    """Lifecycle of an action queue item.

    Frozen by api-contract.md section 3.1 (ActionPage, ActionReceipt).
    """

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    SNOOZED = "snoozed"
    DISMISSED = "dismissed"
    COMPLETED = "completed"
    EXPIRED = "expired"
    BLOCKED = "blocked"


class ActionKind(StrEnum):
    """Action queue item kind.

    Frozen by api-contract.md section 3.1 (ActionPage).
    """

    RESUME_REVIEW = "resume_review"
    JOB_MATCHING = "job_matching"
    INTERVIEW_PREPARATION = "interview_preparation"
    SMART_FORM_INTAKE = "smart_form_intake"


class ModelOperation(StrEnum):
    """Operations accepted by preflight and capability endpoints.

    Frozen by api-contract.md section 3 (operation enum note).
    """

    JOB_MATCHING = "job_matching"
    RESUME_REVIEW = "resume_review"
    INTERVIEW_PREPARATION = "interview_preparation"
    SMART_FORM_INTAKE = "smart_form_intake"


class AgentRunOperation(StrEnum):
    """Strict subset of ModelOperation accepted by start/retry routes.

    Frozen by api-contract.md section 3 (operation enum note).
    ``smart_form_intake`` is preview-only and cannot start an Agent run.
    """

    JOB_MATCHING = "job_matching"
    RESUME_REVIEW = "resume_review"
    INTERVIEW_PREPARATION = "interview_preparation"


class SourceRefType(StrEnum):
    """Source reference type in action items and context."""

    JOB = "job"
    RESUME = "resume"
    PROFILE = "profile"
    EVIDENCE = "evidence"


class DismissReasonCode(StrEnum):
    """Reason codes for action dismissal."""

    NOT_RELEVANT = "NOT_RELEVANT"


class StopReasonCode(StrEnum):
    """Reason codes for stopping a run."""

    USER_STOPPED = "USER_STOPPED"


# ---------------------------------------------------------------------------
# Error envelope (api-contract.md section 2)
# ---------------------------------------------------------------------------


class AgentConsoleErrorBody(StrictContract):
    trace_id: str
    code: str
    message: str
    retryable: bool = False
    fields: dict[str, str] | None = None


class AgentConsoleErrorResponse(StrictContract):
    error: AgentConsoleErrorBody


# ---------------------------------------------------------------------------
# Common receipt base (api-contract.md section 2)
# ---------------------------------------------------------------------------


class MutationReceipt(StrictContract):
    """Common mutation receipt shape shared by all mutation responses."""

    trace_id: str
    receipt_id: str
    idempotency_key: str
    resource_type: str
    resource_id: str
    state: str
    accepted_at: datetime


# ---------------------------------------------------------------------------
# Action queue models (api-contract.md sections 3-3.1)
# ---------------------------------------------------------------------------


class ActionSourceRef(StrictContract):
    type: SourceRefType
    id: str
    version: int | None = None


class ActionPrerequisites(StrictContract):
    status: Literal["ready", "not_ready"]
    missing: list[str]


class NotificationPolicy(StrictContract):
    surface: Literal["in_app"]
    quiet_hours: bool


class ActionItem(StrictContract):
    action_key: str
    kind: ActionKind
    title: str
    reason_code: str
    priority: Literal["high", "medium", "low"]
    target_route: str
    deterministic_rank: int
    prerequisites: ActionPrerequisites
    context_id: str
    source_refs: list[ActionSourceRef]
    source_event_key: str
    source_freshness: datetime
    state: ActionState
    created_at: datetime
    expires_at: datetime | None = None


class ActionPage(StrictContract):
    queue_version: int
    generated_at: datetime
    user_goal_scope: str
    notification_policy: NotificationPolicy
    items: list[ActionItem]
    next_cursor: str | None = None
    trace_id: str


class ActionOutcome(StrictContract):
    route: str | None = None
    run_id: str | None = None


class ActionReceipt(MutationReceipt):
    action_key: str
    queue_version: int
    outcome: ActionOutcome
    audit_event_id: str
    idempotency_replayed: bool


# --- Mutation requests ---


class AcceptAction(StrictContract):
    expected_queue_version: int
    client_event_id: str


class SnoozeAction(StrictContract):
    expected_queue_version: int
    until: datetime
    client_event_id: str


class DismissAction(StrictContract):
    expected_queue_version: int
    reason_code: str
    client_event_id: str


class CompleteAction(StrictContract):
    expected_queue_version: int
    client_event_id: str


# ---------------------------------------------------------------------------
# Context models (api-contract.md section 3.1)
# ---------------------------------------------------------------------------


class SourceVersionSnapshot(StrictContract):
    job: int | None = None
    profile: int | None = None
    resume: int | None = None
    evidence: int | None = None


class SourceDigests(StrictContract):
    job: str | None = None
    profile: str | None = None
    resume: str | None = None


class ContextSourceRef(StrictContract):
    type: str
    id: str
    version: int


def _empty_context_source_refs() -> list[ContextSourceRef]:
    return []


class CreateContext(StrictContract):
    operation: ModelOperation
    job_id: str | None = None
    job_version: int | None = None
    profile_version_id: str | None = None
    resume_version_id: str | None = None
    evidence_claim_ids: list[str] = Field(default_factory=list)
    source_refs: list[ContextSourceRef] = Field(default_factory=_empty_context_source_refs)


class Context(StrictContract):
    context_id: str
    operation: ModelOperation
    schema_version: str
    source_version_snapshot: SourceVersionSnapshot
    source_digests: SourceDigests
    allowed_operation: ModelOperation
    created_at: datetime
    expires_at: datetime | None = None
    state: str
    invalidation_reason: str | None = None


# ---------------------------------------------------------------------------
# Capability model (api-contract.md section 3.1)
# ---------------------------------------------------------------------------


class Capability(StrictContract):
    operation: ModelOperation
    state: CapabilityState
    reason_code: str | None = None
    may_start: bool
    requires_preflight: bool
    retryable: bool = False
    trace_id: str


# ---------------------------------------------------------------------------
# Preflight models (api-contract.md section 3.1)
# ---------------------------------------------------------------------------


class PreflightRequest(StrictContract):
    context_id: str
    operation: ModelOperation


class BudgetInfo(StrictContract):
    run_calls_remaining: int
    candidate_calls_remaining: int
    input_tokens_remaining: int
    output_tokens_remaining: int


class ConsentScope(StrictContract):
    candidate_id: str
    source_digests: list[str]
    expires_at: datetime | None = None


class Preflight(StrictContract):
    preflight_id: str
    operation: ModelOperation
    context_id: str
    decision: Literal["ready", "blocked"]
    capability_state: CapabilityState
    reason_code: str | None = None
    field_set_hash: str | None = None
    redaction_version: str | None = None
    allowed_fields: list[str]
    budget: BudgetInfo
    consent_scope: ConsentScope
    consent_id: str | None = None
    consent_issued: bool
    provider_policy_version: str | None = None
    expires_at: datetime | None = None
    trace_id: str


# ---------------------------------------------------------------------------
# Run models (api-contract.md sections 3-3.1)
# ---------------------------------------------------------------------------


class StartRun(StrictContract):
    context_id: str
    operation: AgentRunOperation
    preflight_id: str


class RunProgress(StrictContract):
    completed: int
    total: int


class RunFailure(StrictContract):
    code: str | None = None
    retryable: bool = False


class Run(StrictContract):
    run_id: str
    candidate_id: str
    operation: AgentRunOperation
    execution_state: ExecutionState
    capability_state: CapabilityState
    review_state: ReviewState
    context_id: str
    trace_id: str
    current_attempt: int
    progress: RunProgress
    latest_stage: str | None = None
    failure: RunFailure | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunReceipt(MutationReceipt):
    pass


class RunPage(StrictContract):
    items: list[Run]
    next_cursor: str | None = None
    trace_id: str


# --- Retry / Stop ---


class RetryRun(StrictContract):
    expected_state: str
    context_id: str | None = None
    preflight_id: str | None = None


class StopRun(StrictContract):
    expected_state: str
    reason_code: str


# ---------------------------------------------------------------------------
# Stage models (api-contract.md section 3.1)
# ---------------------------------------------------------------------------


def _empty_stage_source_refs() -> list[dict[str, str]]:
    return []


class StageEvent(StrictContract):
    stage_event_id: str
    sequence: int
    attempt: int
    schema_version: str
    stage: str
    status: str
    terminal: bool
    retryable: bool = False
    provider_state: str | None = None
    duration_ms: int
    cause_code: str | None = None
    source_refs: list[dict[str, str]] = Field(default_factory=_empty_stage_source_refs)
    message: str | None = None
    occurred_at: datetime


class StagePage(StrictContract):
    items: list[StageEvent]
    next_cursor: str | None = None
    trace_id: str


# ---------------------------------------------------------------------------
# Review models (api-contract.md sections 3-3.1)
# ---------------------------------------------------------------------------


class FieldDecision(StrictContract):
    path: str
    decision: Literal["keep", "edit", "reject"]


class ReviewRun(StrictContract):
    expected_run_revision: int
    decision: Literal["accepted", "rejected", "edited"]
    field_decisions: list[FieldDecision]
    preview_id: str
    context_digest: str
    note: str | None = None


class ReviewFieldDecision(StrictContract):
    path: str
    decision: str
    edited_value: JsonValue = None
    evidence_refs: list[str] = Field(default_factory=list)


class ReviewItem(StrictContract):
    review_id: str
    run_id: str
    decision: str
    actor_id: str
    field_decisions: list[ReviewFieldDecision]
    note: str | None = None
    created_at: datetime


class ReviewPage(StrictContract):
    items: list[ReviewItem]
    trace_id: str


class ReviewReceipt(StrictContract):
    trace_id: str
    receipt_id: str
    idempotency_key: str
    resource_type: str
    resource_id: str
    state: str
    accepted_at: datetime
